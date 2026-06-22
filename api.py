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
# CONFIGURAÇÃO DO BANCO DE DADOS (POSTGRESQL)
# ==============================================================================
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    if DATABASE_URL:
        url_conexao = DATABASE_URL.strip()
        
        # Garante de forma estrita que a URL comece com postgresql://
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
                cursor.execute('''
                    ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS idiomas TEXT;
                ''')
                conn.commit()
        print("-> Banco de dados PostgreSQL pronto!")
    except Exception as e:
        print(f"Erro ao inicializar o banco de dados: {e}")

init_db()

# ==============================================================================
# CONFIGURAÇÃO DO GOOGLE GEMINI AI (ATUALIZADO COM ESTRUTURAÇÃO NATIVA)
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
    
    texto_limitado = texto_bruto.strip()[:24000] # Aumentado o limite de tokens avaliados
    
    if not client:
        return {
            "nome": "Sem Chave API", "idade": "Não Informado", "sexo": "Não Informado",
            "localizacao": "Configuração Pendente", "formacao": "A IA não pôde ser chamada.",
            "cursos": "Nenhum", "habilidades": "Nenhuma", "idiomas": "Não informado"
        }
        
    try:
        # ATUALIZAÇÃO CRÍTICA: Usando o config=types.GenerateContentConfig para mapeamento tipado estrito
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Extraia com precisão os dados do seguinte currículo profissional:\n\n{texto_limitado}",
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EstruturaCurriculo, # Valida os tipos direto no motor da IA
                system_instruction=(
                    "Você é um sistema automatizado de triagem de currículos para o RH. "
                    "Analise o texto do candidato e preencha todos os campos do Schema JSON. "
                    "Se um campo como 'nome' ou 'localizacao' for oculto ou complexo demais, tente deduzir "
                    "ou use informações do cabeçalho. No campo 'idiomas', classifique estritamente o nível informado "
                    "como (Iniciante, Intermediário ou Avançado/Fluente). Exemplo: 'Inglês (Avançado)'. "
                    "Caso não haja menção a algum campo, preencha como 'Não informado'."
                )
            )
        )
        
        texto_resposta = response.text.strip() if response.text else ""
        
        # Conversão direta e limpa garantida pelo schema tipado
        if texto_resposta:
            dados = json.loads(texto_resposta)
            return {k: limpar_caracteres_invalidos(str(v)) for k, v in dados.items()}
            
    except Exception as e:
        print(f"Erro na geração estruturada com Gemini GenAI: {e}")
        
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
    
    # Verifica se há qualquer tipo de parâmetro de pesquisa/filtro ativo
    algum_filtro_ativo = any([busca_geral, f_sexo, f_formacao, f_localizacao, f_idioma, f_nivel])
    
    # Se o usuário realizou uma nova busca, nós "resetamos" a lista de ocultados para reavaliá-los
    if algum_filtro_ativo:
        session['ocultados'] = []
    elif 'ocultados' not in session:
        session['ocultados'] = []
        
    resultados_finais = []
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT id, nome_arquivo, conteudo, nome_candidato AS nome, idade, sexo, localizacao, formacao, cursos, habilidades, idiomas FROM curriculos ORDER BY id DESC")
                todos_candidatos = cursor.fetchall()
                
                for item in todos_candidatos:
                    # Se NÃO há filtro ativo e o candidato foi retirado anteriormente, pula a exibição dele
                    if not algum_filtro_ativo and item['id'] in session['ocultados']:
                        continue
                        
                    texto_idiomas = remover_acentos(item.get('idiomas') or "")
                    texto_completo_candidato = remover_acentos(
                        f"{item['conteudo']} {item['nome']} {item['habilidades']} {item['cursos']} {item.get('idiomas', '')}"
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

# NOVA ROTA: Adiciona o ID do candidato à lista de itens temporariamente ocultos
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
        flash("Nenhum arquivo secretário.", "danger")
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

@app.route('/visualizar/<int:id_curriculo>')
def visualizar(id_curriculo):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT nome_arquivo, arquivo_binario FROM curriculos WHERE id = %s", (id_curriculo,))
                registro = cursor.fetchone()
                
                if registro and registro['arquivo_binario']:
                    dados_originais = base64.b64decode(registro['arquivo_binario'])
                    extensao = registro['nome_arquivo'].lower()
                    
                    mimetype = "application/pdf" if extensao.endswith('.pdf') else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    
                    return send_file(
                        io.BytesIO(dados_originais),
                        mimetype=mimetype,
                        as_attachment=False,
                        download_name=registro['nome_arquivo']
                    )
    except Exception as e:
        print(f"Erro ao visualizar arquivo: {e}")
        
    return "O arquivo solicitado está indisponível ou não foi encontrado.", 404

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
