import os
import sys
import re
import io
import json
import base64
import unicodedata
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for, flash, Response, jsonify, send_file, session
from google import genai
from google.genai import types  # Importação necessária para o Object Schema nativo
from pydantic import BaseModel
import pypdf
import docx2txt
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "chave_secreta_talent_pulse_a1")

# ==============================================================================
# CONFIGURAÇÃO DO BANCO DE DADOS (POSTGRESQL) - ATUALIZADO PARA SOFT/HARD SKILLS
# ==============================================================================
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    if DATABASE_URL:
        url_conexao = DATABASE_URL.strip()
        
        if url_conexao.startswith("postgres://"):
            url_conexao = url_conexao.replace("postgres://", "postgresql://", 1)
        elif not url_conexao.startswith("postgresql://") and url_conexao.startswith("//"):
            url_conexao = "postgresql:" + url_conexao
            
        try:
            return psycopg2.connect(url_conexao)
        except Exception as e:
            print(f"Falha na conexão direta, tentando parse manual estruturado: {e}")
            
            url_limpa = url_conexao.split('?')[0]
            parsed = urlparse(url_limpa)
            
            return psycopg2.connect(
                database=parsed.path[1:],
                user=parsed.username,
                password=parsed.password,
                host=parsed.hostname,
                port=parsed.port or 5432,
                sslmode='require'
            )
    else:
        return psycopg2.connect("dbname=talent_pulse user=postgres password=postgres host=localhost")

def init_db():
    print("-> Verificando/Criando tabelas no PostgreSQL na nuvem...")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS curriculos (
                        id SERIAL PRIMARY KEY,
                        nome_arquivo TEXT NOT NULL,
                        conteudo TEXT NOT NULL,
                        nome_candidato TEXT,
                        idade TEXT,
                        sexo TEXT,
                        localizacao TEXT,
                        formacao TEXT,
                        cursos TEXT,
                        habilidades TEXT,
                        arquivo_binario TEXT
                    );
                ''')
                # Adiciona as colunas novas caso não existam
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS idiomas TEXT;')
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS hard_skills TEXT;')
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS soft_skills TEXT;')
                conn.commit()
        print("-> Banco de dados PostgreSQL pronto!")
    except Exception as e:
        print(f"Erro ao inicializar o banco de dados: {e}")

init_db()

# ==============================================================================
# CONFIGURAÇÃO DO GOOGLE GEMINI AI (ATUALIZADO COM SEPARAÇÃO DE SKILLS)
# ==============================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

class EstruturaCurriculo(BaseModel):
    nome: str
    idade: str
    sexo: str
    localizacao: str
    formacao: str
    cursos: str
    hard_skills: str   # Técnico/Ferramentas (ex: Python, Vendas B2B, Pipedrive)
    soft_skills: str   # Comportamental (ex: Liderança, Comunicação, Empatia)
    idiomas: str

# ==============================================================================
# FUNÇÕES AUXILIARES DE TEXTO E BUSCA
# ==============================================================================
def remover_acentos(texto):
    if not texto:
        return ""
    texto_normalizado = unicodedata.normalize('NFD', texto)
    return "".join(c for c in texto_normalizado if unicodedata.category(c) != 'Mn').lower()

def limpar_caracteres_invalidos(texto):
    if not texto:
        return ""
    return texto.replace('\x00', '')

def obtener_variacoes_busca(termo_busca):
    termo_limpo = remover_acentos(termo_busca).strip()
    palavras = termo_limpo.split()
    variacoes = set()
    
    for palabra in palavras:
        if len(palabra) > 2:
            if palabra.endswith(('s', 'es')):
                variacoes.add(palabra[:-1] if palabra.endswith('s') else palabra[:-2])
            if palabra.endswith(('r', 'cao', 'mento')):
                variacoes.add(palabra[:int(len(palabra)*0.7)])
        variacoes.add(palabra)
    return list(variacoes) if variacoes else [termo_limpo]

def extrair_texto_pdf(dados_bytes):
    try:
        pdf_file = io.BytesIO(dados_bytes)
        reader = pypdf.PdfReader(pdf_file)
        texto = ""
        for page in reader.pages:
            texto_pagina = page.extract_text()
            if texto_pagina:
                texto += texto_pagina + "\n"
        return texto
    except Exception as e:
        print(f"Erro ao extrair PDF: {e}")
        return ""

def extrair_texto_docx(dados_bytes):
    try:
        docx_file = io.BytesIO(dados_bytes)
        return docx2txt.process(docx_file)
    except Exception as e:
        print(f"Erro ao extrair DOCX: {e}")
        return ""

def estruturar_curriculo_com_ia(texto_bruto):
    if not texto_bruto or not texto_bruto.strip():
        return {
            "nome": "Nome provisório", "idade": "Não Informado", "sexo": "Não Informado",
            "localizacao": "Manual necessário", "formacao": "Texto vazio.",
            "cursos": "Nenhum", "hard_skills": "Nenhuma", "soft_skills": "Nenhuma", "idiomas": "Não informado"
        }
    
    texto_limitado = texto_bruto.strip()[:24000]
    
    if not client:
        return {
            "nome": "Sem Chave API", "idade": "Não Informado", "sexo": "Não Informado",
            "localizacao": "Configuração Pendente", "formacao": "A IA não pôde ser chamada.",
            "cursos": "Nenhum", "hard_skills": "Nenhuma", "soft_skills": "Nenhuma", "idiomas": "Não informado"
        }
        
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Extraia com precisão os dados do seguinte currículo profissional:\n\n{texto_limitado}",
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EstruturaCurriculo,
                system_instruction=(
                    "Você é um sistema automatizado de triagem de currículos para o RH. "
                    "Analise o texto do candidato e preencha todos os campos do Schema JSON.\n\n"
                    "REGRAS DE SEPARAÇÃO DE HABILIDADES:\n"
                    "- 'hard_skills': Liste estritamente competências técnicas, metodologias, ferramentas e frameworks separados por vírgula. Ex: Python, JavaScript, CRM Pipedrive, Vendas B2B, IoT, Excel Avançado.\n"
                    "- 'soft_skills': Liste estritamente características comportamentais, inteligência emocional e habilidades interpessoais separadas por vírgula. Ex: Liderança, Comunicação Eficaz, Trabalho em Equipe, Resolução de Problemas, Proatividade, Empatia.\n\n"
                    "No campo 'idiomas', classifique estritamente o nível informado como (Iniciante, Intermediário ou Avançado/Fluente). "
                    "Caso não haja menção a algum campo, preencha como 'Não informado'."
                )
            )
        )
        
        texto_resposta = response.text.strip() if response.text else ""
        
        if texto_resposta:
            dados = json.loads(texto_resposta)
            return {k: limpar_caracteres_invalidos(str(v)) for k, v in dados.items()}
            
    except Exception as e:
        print(f"Erro na geração estruturada com Gemini GenAI: {e}")
        
    return {
        "nome": "Nome provisório", "idade": "Não Informado", "sexo": "Não Informado",
        "localizacao": "Manual necessário", "formacao": "Estrutura complexa de leitura.",
        "cursos": "Consulte o arquivo original", "hard_skills": "Análise Manual", "soft_skills": "Análise Manual", "idiomas": "Não informado"
    }

# ==============================================================================
# ROTAS DA APLICAÇÃO WEB
# ==============================================================================
@app.route('/', methods=['GET'])
def index():
    busca_geral = request.args.get('busca', '').strip()
    f_sexo = request.args.get('sexo', '').strip()
    f_formacao = request.args.get('formacao', '').strip()
    f_localizacao = request.args.get('localizacao', '').strip()
    f_idioma = request.args.get('idioma', '').strip()
    f_nivel = request.args.get('nivel_idioma', '').strip()
    
    algum_filtro_ativo = any([busca_geral, f_sexo, f_formacao, f_localizacao, f_idioma, f_nivel])
    
    if algum_filtro_ativo:
        session['ocultados'] = []
    elif 'ocultados' not in session:
        session['ocultados'] = []
        
    resultados_finais = []
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT id, nome_arquivo, conteudo, nome_candidato AS nome, idade, sexo, 
                           localizacao, formacao, cursos, habilidades, hard_skills, soft_skills, idiomas 
                    FROM curriculos ORDER BY id DESC
                """)
                todos_candidatos = cursor.fetchall()
                
                for item in todos_candidatos:
                    if not algum_filtro_ativo and item['id'] in session['ocultados']:
                        continue
                        
                    texto_idiomas = remover_acentos(item.get('idiomas') or "")
                    texto_completo_candidato = remover_acentos(
                        f"{item['conteudo']} {item['nome']} {item.get('hard_skills', '')} {item.get('soft_skills', '')} {item['cursos']} {item.get('idiomas', '')}"
                    )
                    passou_filtro = True
                    
                    if busca_geral:
                        radicais_procurados = obter_variacoes_busca(busca_geral)
                        match_encontrado = False
                        for radical in radicais_procurados:
                            if radical in texto_completo_candidato:
                                match_encontrado = True
                                break
                        if not match_encontrado:
                            passou_filtro = False
                            
                    if f_sexo and item['sexo'] != f_sexo:
                        passou_filtro = False
                    if f_formacao and f_formacao.lower() not in remover_acentos(item['formacao']):
                        passou_filtro = False
                    if f_localizacao and f_localizacao.lower() not in remover_acentos(item['localizacao']):
                        passou_filtro = False
                    if f_idioma and f_idioma.lower() not in texto_idiomas:
                        passou_filtro = False
                    if f_nivel and f_nivel.lower() not in texto_idiomas:
                        passou_filtro = False
                        
                    if passou_filtro:
                        item['resumo'] = item['conteudo'][:150] + "..." if len(item['conteudo']) > 150 else item['conteudo']
                        resultados_finais.append(item)
    except Exception as e:
        print(f"Erro ao buscar dados: {e}")
        
    return render_template('index.html', candidatos=resultados_finais)

@app.route('/ocultar/<int:id_curriculo>', methods=['POST'])
def ocultar_candidato(id_curriculo):
    if 'ocultados' not in session:
        session['ocultados'] = []
    
    lista_atual = list(session['ocultados'])
    if id_curriculo not in lista_atual:
        lista_atual.append(id_curriculo)
        
    session['ocultados'] = lista_atual
    return jsonify({"status": "sucesso", "id_ocultado": id_curriculo})

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        flash("Nenhum arquivo enviado.", "danger")
        return redirect(url_for('index'))
        
    arquivo = request.files['file']
    if arquivo.filename == '':
        flash("Nenhum arquivo selecionado.", "danger")
        return redirect(url_for('index'))
        
    if arquivo:
        try:
            nome_arquivo = arquivo.filename
            dados_bytes = arquivo.read()
            
            # ------------------------------------------------------------------
            # VALIDAÇÃO 1: VERIFICAÇÃO POR NOME DE ARQUIVO JÁ EXISTENTE
            # ------------------------------------------------------------------
            with get_db_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute("SELECT id FROM curriculos WHERE nome_arquivo = %s LIMIT 1;", (nome_arquivo,))
                    arquivo_duplicado = cursor.fetchone()
                    if arquivo_duplicado:
                        flash(f'O arquivo "{nome_arquivo}" já foi processado anteriormente!', "warning")
                        return redirect(url_for('index'))
            
            # Converte arquivo em string base64 para armazenar se necessário
            arquivo_binario_b64 = base64.b64encode(dados_bytes).decode('utf-8')
            
            texto_extraido = ""
            if nome_arquivo.lower().endswith('.pdf'):
                texto_extraido = extrair_texto_pdf(dados_bytes)
            elif nome_arquivo.lower().endswith('.docx'):
                texto_extraido = extrair_texto_docx(dados_bytes)
            
            if not texto_extraido.strip():
                flash(f"Não foi possível extrair texto legível de {nome_arquivo}.", "warning")
                return redirect(url_for('index'))
                
            # Chamada da Inteligência Artificial estruturada
            dados_ia = estruturar_curriculo_com_ia(texto_extraido)
            nome_candidato = dados_ia.get('nome')
            
            # ------------------------------------------------------------------
            # VALIDAÇÃO 2: VERIFICAÇÃO POR NOME DO CANDIDATO JÁ EXISTENTE
            # ------------------------------------------------------------------
            if nome_candidato and nome_candidato != "Nome provisório" and nome_candidato != "Sem Chave API":
                with get_db_connection() as conn:
                    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                        cursor.execute("SELECT id FROM curriculos WHERE nome_candidato = %s LIMIT 1;", (nome_candidato,))
                        candidato_duplicado = cursor.fetchone()
                        if candidato_duplicado:
                            flash(f'O(A) candidato(a) "{nome_candidato}" já está cadastrado(a) no sistema!', "warning")
                            return redirect(url_for('index'))
            
            # Salvando no banco de dados PostgreSQL se passar nas validações
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO curriculos (
                            nome_arquivo, conteudo, nome_candidato, idade, sexo, 
                            localizacao, formacao, cursos, hard_skills, soft_skills, idiomas, arquivo_binario
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        nome_arquivo, texto_extraido, nome_candidato, dados_ia.get('idade'), 
                        dados_ia.get('sexo'), dados_ia.get('localizacao'), dados_ia.get('formacao'), 
                        dados_ia.get('cursos'), dados_ia.get('hard_skills'), dados_ia.get('soft_skills'), 
                        dados_ia.get('idiomas'), arquivo_binario_b64
                    ))
                    conn.commit()
            
            flash(f"Currículo de {nome_candidato} processado com sucesso!", "success")
        except Exception as e:
            print(f"Erro no upload/processamento: {e}")
            flash("Erro interno ao processar o arquivo.", "danger")
            
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)
