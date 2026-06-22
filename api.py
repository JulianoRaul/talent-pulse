import os
import sys
import re
import io
import json
import base64
import unicodedata
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for, flash, Response, jsonify
from google import genai
from pydantic import BaseModel
import pypdf
import docx2txt
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "chave_secreta_talent_pulse_a1")

# ==============================================================================
# CONFIGURAÇÃO DO BANCO DE DADOS (POSTGRESQL)
# ==============================================================================
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    if DATABASE_URL:
        url_conexao = DATABASE_URL
        if url_conexao.startswith("postgres://"):
            url_conexao = url_conexao.replace("postgres://", "postgresql://", 1)
        
        try:
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
        except Exception as e:
            print(f"Erro no parse estruturado da URL: {e}")
            return psycopg2.connect(url_conexao)
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
                cursor.execute('''
                    ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS idiomas TEXT;
                ''')
                conn.commit()
        print("-> Banco de dados PostgreSQL pronto!")
    except Exception as e:
        print(f"Erro ao inicializar o banco de dados: {e}")

init_db()

# ==============================================================================
# CONFIGURAÇÃO DO GOOGLE GEMINI AI
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
    habilidades: str
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

def obter_variacoes_busca(termo_busca):
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
            "cursos": "Nenhum", "habilidades": "Nenhuma", "idiomas": "Não informado"
        }
    
    texto_limitado = texto_bruto.strip()[:18000]
    prompt_base = """
    Você é um assistente de RH especialista em triagem de currículos.
    Analise o texto bruto do currículo fornecido e extraia com precisão as informações solicitadas.
    Importante: No campo 'idiomas', identifique quais idiomas o candidato fala e classifique estritamente o nível informado como (Iniciante, Intermediário ou Avançado/Fluente). Exemplo de retorno: 'Inglês (Avançado), Espanhol (Iniciante)'. Se não houver menção explícita a idiomas, defina como 'Não informado'.
    Retorne strings simples e curtas para cada campo.
    """
    
    if not client:
        return {
            "nome": "Sem Chave API", "idade": "Não Informado", "sexo": "Não Informado",
            "localizacao": "Configuração Pendente", "formacao": "A IA não pôde ser chamada.",
            "cursos": "Nenhum", "habilidades": "Nenhuma", "idiomas": "Não informado"
        }
        
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"{prompt_base}\nRetorne em formato JSON válido usando estritamente as chaves: 'nome', 'idade', 'sexo', 'localizacao', 'formacao', 'cursos', 'habilidades', 'idiomas'.\n\nTexto do Currículo:\n{texto_limitado}"
        )
        
        texto_resposta = response.text.strip() if response.text else ""
        
        if "{" in texto_resposta:
            inicio = texto_resposta.find("{")
            fim = texto_resposta.rfind("}") + 1
            dados = json.loads(texto_resposta[inicio:fim])
            return {k: limpar_caracteres_invalidos(str(v)) for k, v in dados.items()}
            
    except Exception as e:
        print(f"Erro na geração de conteúdo do Gemini: {e}")
        
    return {
        "nome": "Nome provisório", "idade": "Não Informado", "sexo": "Não Informado",
        "localizacao": "Manual necessário", "formacao": "Estrutura complexa de leitura.",
        "cursos": "Consulte o arquivo original", "habilidades": "Análise Manual", "idiomas": "Não informado"
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
    f_nivel = request.args.get('nivel', '').strip()
    
    resultados_finais = []
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT id, nome_arquivo, conteudo, nome_candidato, idade, sexo, localizacao, formacao, cursos, habilidades, idiomas FROM curriculos ORDER BY id DESC")
                todos_candidatos = cursor.fetchall()
                
                for item in todos_candidatos:
                    texto_idiomas = remover_acentos(item.get('idiomas') or "")
                    texto_completo_candidato = remover_acentos(
                        f"{item['conteudo']} {item['nome_candidato']} {item['habilidades']} {item['cursos']} {item.get('idiomas', '')}"
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
        
    return render_template('index.html', candidatos=resultados_finais, filtros={
        'busca': busca_geral, 'sexo': f_sexo, 'formacao': f_formacao, 'localizacao': f_localizacao,
        'idioma': f_idioma, 'nivel': f_nivel
    })

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        flash("Nenhum arquivo enviado.", "danger")
        return redirect(url_for('index'))
        
    arquivo = request.files['file']
    if arquivo.filename == '':
        flash("Nenhum arquivo selecionado.", "danger")
        return redirect(url_for('index'))
        
    if arquivo and (arquivo.filename.lower().endswith('.pdf') or arquivo.filename.lower().endswith('.docx')):
        try:
            dados_bytes = arquivo.read()
            string_base64 = base64.b64encode(dados_bytes).decode('utf-8')
            
            if arquivo.filename.lower().endswith('.pdf'):
                texto_extraido = extrair_texto_pdf(dados_bytes)
            else:
                texto_extraido = extrair_texto_docx(dados_bytes)
                
            if not texto_extraido.strip():
                flash(f"Não foi possível extrair texto legível de: {arquivo.filename}", "warning")
                return redirect(url_for('index'))
                
            dados_ia = estruturar_curriculo_com_ia(texto_extraido)
            
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO curriculos (nome_arquivo, conteudo, nome_candidato, idade, sexo, localizacao, formacao, cursos, habilidades, arquivo_binario, idiomas)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        limpar_caracteres_invalidos(arquivo.filename),
                        texto_extraido,
                        dados_ia.get('nome', 'Nome provisório'),
                        dados_ia.get('idade', 'Não informado'),
                        dados_ia.get('sexo', 'Não informado'),
                        dados_ia.get('localizacao', 'Manual necessário'),
                        dados_ia.get('formacao', 'Não informado'),
                        dados_ia.get('cursos', 'Não informado'),
                        dados_ia.get('habilidades', 'Não informado'),
                        string_base64,
                        dados_ia.get('idiomas', 'Não informado')
                    ))
                    conn.commit()
                    
            flash(f"Currículo '{arquivo.filename}' processado com sucesso!", "success")
        except Exception as e:
            flash(f"Erro crítico no upload: {e}", "danger")
            print(f"Erro crítico no upload: {e}")
            
    return redirect(url_for('index'))

@app.route('/download/<int:id_curriculo>')
def download(id_curriculo):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT nome_arquivo, arquivo_binario FROM curriculos WHERE id = %s", (id_curriculo,))
                registro = cursor.fetchone()
                
                if registro and registro['arquivo_binario']:
                    dados_originais = base64.b64decode(registro['arquivo_binario'])
                    extensao = registro['nome_arquivo'].lower()
                    mimetype = "application/pdf" if extensao.endswith('.pdf') else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    
                    return Response(
                        dados_originais,
                        mimetype=mimetype,
                        headers={"Content-Disposition": f"attachment; filename={registro['nome_arquivo']}"}
                    )
    except Exception as e:
        print(f"Erro no download: {e}")
    flash("Arquivo original indisponível para download.", "danger")
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000, debug=True)
