import os
import sys
import re
import io
import json
import base64
import unicodedata
import time
from urllib.parse import urlparse

from flask import Flask, render_template, request, redirect, url_for, flash, Response, jsonify, send_file, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from google import genai
from google.genai import types  
from pydantic import BaseModel
import pypdf
import docx2txt
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "chave_secreta_talent_pulse_a1")

# ==============================================================================
# CONFIGURAÇÃO DO FLASK-LOGIN
# ==============================================================================
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = "Por favor, faça o login para acessar esta página."
login_manager.login_message_category = "error"

class User(UserMixin):
    def __init__(self, id, email, nome, empresa_id):
        self.id = id
        self.email = email
        self.nome = nome
        self.empresa_id = empresa_id  

@login_manager.user_loader
def load_user(user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT id, email, nome, empresa_id FROM usuarios WHERE id = %s", (int(user_id),))
                user_data = cursor.fetchone()
                if user_data:
                    return User(
                        id=str(user_data['id']), 
                        email=user_data['email'], 
                        nome=user_data['nome'],
                        empresa_id=user_data['empresa_id']
                    )
    except Exception as e:
        print(f"Erro ao carregar usuário: {e}")
    return None

# ==============================================================================
# CONFIGURAÇÃO DO BANCO DE DADOS (POSTGRESQL - MULTI-TENANT)
# ==============================================================================
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    if DATABASE_URL:
        url_conexao = DATABASE_URL.strip()
        if url_conexao.startswith("postgres://"):
            url_conexao = url_conexao.replace("postgres://", "postgresql://", 1)
        try:
            return psycopg2.connect(url_conexao)
        except Exception as e:
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
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # 1. Tabela de Empresas Inquilinas (Tenants)
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS empresas (
                        id SERIAL PRIMARY KEY,
                        nome_comercial TEXT NOT NULL,
                        data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                ''')

                # 2. Tabela de Currículos
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS curriculos (
                        id SERIAL PRIMARY KEY,
                        empresa_id INTEGER REFERENCES empresas(id) ON DELETE CASCADE,
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
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS empresa_id INTEGER REFERENCES empresas(id) ON DELETE CASCADE;')
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS idiomas TEXT;')
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS hard_skills TEXT;')
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS soft_skills TEXT;')
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS whatsapp TEXT;')
                
                # ADICIONADO: Campo para armazenar as áreas mapeadas por IA como vetor de texto (múltiplas áreas)
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS areas_profissionais TEXT[];')
                
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS data_cadastro TIMESTAMP WITHOUT TIME ZONE;')
                cursor.execute('ALTER TABLE curriculos ALTER COLUMN data_cadastro TYPE TIMESTAMP WITHOUT TIME ZONE;')
                cursor.execute("""
                    ALTER TABLE curriculos 
                    ALTER COLUMN data_cadastro 
                    SET DEFAULT (timezone('America/Sao_Paulo', NOW()));
                """)
                
                # 3. Tabela de Usuários
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS usuarios (
                        id SERIAL PRIMARY KEY,
                        empresa_id INTEGER REFERENCES empresas(id) ON DELETE CASCADE,
                        nome TEXT NOT NULL,
                        email TEXT UNIQUE NOT NULL,
                        senha_hash TEXT NOT NULL
                    );
                ''')
                cursor.execute('ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS empresa_id INTEGER REFERENCES empresas(id) ON DELETE CASCADE;')

                # 4. Tabela de Vagas
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS vagas (
                        id SERIAL PRIMARY KEY,
                        empresa_id INTEGER REFERENCES empresas(id) ON DELETE CASCADE,
                        titulo TEXT NOT NULL,
                        descricao TEXT NOT NULL,
                        requisitos TEXT,
                        localizacao TEXT,
                        data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                ''')
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS empresa_id INTEGER REFERENCES empresas(id) ON DELETE CASCADE;')
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS atividades TEXT;')
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS beneficios TEXT;')
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS remuneracao TEXT;')
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS expediente TEXT;')
                
                conn.commit()
    except Exception as e:
        print(f"Erro ao inicializar o banco de dados: {e}")

init_db()

# ==============================================================================
# CONFIGURAÇÃO DO GOOGLE GEMINI AI
# ==============================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# AJUSTADO: Adicionado 'areas_profissionais' na resposta tipada da IA
class EstruturaCurriculo(BaseModel):
    nome: str
    idade: str  
    sexo: str
    localizacao: str
    formacao: str
    cursos: str
    hard_skills: str   
    soft_skills: str   
    idiomas: str
    whatsapp: str 
    areas_profissionais: list[str]

class CandidatoCompatibilidade(BaseModel):
    id_candidato: int
    nome: str
    porcentagem_compatibilidade: int
    justificativa: str

class ResultadoAnaliseVaga(BaseModel):
    vaga_id: int
    candidatos_compativeis: list[CandidatoCompatibilidade]

# ==============================================================================
# FUNÇÕES AUXILIARES DE TEXTO
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
        return ""

def extrair_texto_docx(dados_bytes):
    try:
        docx_file = io.BytesIO(dados_bytes)
        return docx2txt.process(docx_file)
    except Exception as e:
        return ""

def estruturar_curriculo_com_ia(texto_bruto):
    if not texto_bruto or not texto_bruto.strip():
        return {
            "nome": "Nome provisório", "idade": "Não Informado", "sexo": "Não Informado",
            "localizacao": "Manual necessário", "formacao": "Texto vazio.",
            "cursos": "Nenhum", "hard_skills": "Nenhuma", "soft_skills": "Nenhuma", "idiomas": "Não informado",
            "whatsapp": "", "areas_profissionais": ["Geral"]
        }
    
    texto_limitado = texto_bruto.strip()[:24000]
    if not client:
        return {
            "nome": "Sem Chave API", "idade": "Não Informado", "sexo": "Não Informado",
            "localizacao": "Configuração Pendente", "formacao": "A IA não pôde ser chamada.",
            "cursos": "Nenhum", "hard_skills": "Nenhuma", "soft_skills": "Nenhuma", "idiomas": "Não informado",
            "whatsapp": "", "areas_profissionais": ["Geral"]
        }
        
    system_prompt = (
        "Você é um especialista em recrutamento avançado, triagem de currículos e People Analytics.\n"
        "Sua tarefa é analisar o texto do candidato e extrair os dados dividindo estritamente as competências conforme as diretrizes abaixo:\n\n"
        "1. HARD SKILLS:\n"
        "Identifique e liste apenas habilidades técnicas palpáveis, conhecimentos operacionais, ferramentas, metodologias profissionais, frameworks e linguagens de programação. Separe-as por vírgula.\n\n"
        "2. SOFT SKILLS:\n"
        "Classifique e liste exclusivamente as características e competências comportamentais com base no texto do candidato.\n\n"
        "3. WHATSAPP / TELEFONE:\n"
        "Localize o contato de WhatsApp principal do candidato. Extraia apenas os dígitos numéricos incluindo o código de área (DDD).\n\n"
        "4. CLASSIFICAÇÃO DE ÁREAS PROFISSIONAIS (MÚLTIPLOS RÓTULOS):\n"
        "Analise holisticamente a formação acadêmica, histórico profissional e hard skills do candidato. Defina de 1 a 3 áreas profissionais que condizem com o perfil do candidato.\n"
        "Escolha as áreas estritamente a partir desta lista autorizada:\n"
        "- 'Administração'\n"
        "- 'Recursos Humanos'\n"
        "- 'TI / Tecnologia'\n"
        "- 'Vendas / Comercial'\n"
        "- 'Educação'\n"
        "- 'Logística / Operacional'\n"
        "- 'Financeiro'\n"
        "- 'Marketing / Comunicação'\n"
        "- 'Saúde'\n"
        "Se o candidato tiver um perfil híbrido (como formacao em Administração mas atuando fortemente em Recursos Humanos, ou trabalhando com Educação e Logística), atribua as múltiplas áreas condizentes na lista 'areas_profissionais'."
    )

    max_tentativas = 3
    tempo_espera = 2

    for tentativa in range(max_tentativas):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=f"Analise o seguinte currículo:\n\n{texto_limitado}",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=EstruturaCurriculo,
                    system_instruction=system_prompt,
                    temperature=0.1
                )
            )
            
            texto_resposta = response.text.strip() if response.text else ""
            if texto_resposta:
                dados = json.loads(texto_resposta)
                return {
                    "nome": limpar_caracteres_invalidos(str(dados.get("nome"))),
                    "idade": limpar_caracteres_invalidos(str(dados.get("idade"))),
                    "sexo": limpar_caracteres_invalidos(str(dados.get("sexo"))),
                    "localizacao": limpar_caracteres_invalidos(str(dados.get("localizacao"))),
                    "formacao": limpar_caracteres_invalidos(str(dados.get("formacao"))),
                    "cursos": limpar_caracteres_invalidos(str(dados.get("cursos"))),
                    "hard_skills": limpar_caracteres_invalidos(str(dados.get("hard_skills"))),
                    "soft_skills": limpar_caracteres_invalidos(str(dados.get("soft_skills"))),
                    "idiomas": limpar_caracteres_invalidos(str(dados.get("idiomas"))),
                    "whatsapp": limpar_caracteres_invalidos(str(dados.get("whatsapp"))),
                    "areas_profissionais": [limpar_caracteres_invalidos(str(a)) for a in dados.get("areas_profissionais", ["Geral"])]
                }
                
        except Exception as e:
            print(f"[AVISO] Tentativa {tentativa + 1} de {max_tentativas} falhou devido a instabilidade (Erro: {e})")
            if tentativa < max_tentativas - 1:
                time.sleep(tempo_espera)
                tempo_espera *= 2
        
    return {
        "nome": "Nome provisório", "idade": "Não Informado", "sexo": "Não Informado",
        "localizacao": "Manual necessário", "formacao": "Erro de processamento da IA.",
        "cursos": "Consulte o arquivo original", "hard_skills": "Análise Manual", "soft_skills": "Análise Manual", "idiomas": "Não informado",
        "whatsapp": "", "areas_profissionais": ["Geral"]
    }

# ==============================================================================
# ROTAS DE AUTENTICAÇÃO
# ==============================================================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        senha = request.form.get('senha')
        
        try:
            with get_db_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute("SELECT * FROM usuarios WHERE email = %s", (email,))
                    user_data = cursor.fetchone()
                    
            if user_data and check_password_hash(user_data['senha_hash'], senha):
                usuario = User(
                    id=str(user_data['id']), 
                    email=user_data['email'], 
                    nome=user_data['nome'], 
                    empresa_id=user_data['empresa_id']
                )
                login_user(usuario)
                flash(f"Bem-vindo de volta, {usuario.nome}!", "success")
                return redirect(url_for('index'))
            else:
                flash("E-mail ou senha incorretos.", "error")
        except Exception as e:
            print(f"Erro no login: {e}")
            flash("Erro interno ao processar login.", "error")
            
    return render_template('login.html')

@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if request.method == 'POST':
        nome = request.form.get('nome')
        email = request.form.get('email')
        senha = request.form.get('senha')
        nome_empresa = request.form.get('nome_empresa')  

        if not nome_empresa:
            flash("O nome da empresa é obrigatório para a criação da conta.", "error")
            return redirect(url_for('cadastro'))
        
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT id FROM usuarios WHERE email = %s", (email,))
                    if cursor.fetchone():
                        flash("Este e-mail já está cadastrado.", "error")
                        return redirect(url_for('cadastro'))
                    
                    cursor.execute(
                        "INSERT INTO empresas (nome_comercial) VALUES (%s) RETURNING id", 
                        (nome_empresa,)
                    )
                    empresa_id = cursor.fetchone()[0]

                    senha_hash = generate_password_hash(senha)
                    cursor.execute(
                        "INSERT INTO usuarios (nome, email, senha_hash, empresa_id) VALUES (%s, %s, %s, %s)",
                        (nome, email, senha_hash, empresa_id)
                    )
                    conn.commit()
            
            flash("Sua conta de empresa foi criada com sucesso! Faça seu login.", "success")
            return redirect(url_for('login'))
        except Exception as e:
            print(f"Erro no cadastro: {e}")
            flash("Erro ao salvar novo usuário e empresa.", "error")
        
    return render_template('cadastro.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Sessão encerrada com sucesso.", "success")
    return redirect(url_for('login'))

# ==============================================================================
# ROTAS DA APLICAÇÃO WEB
# ==============================================================================
@app.route('/', methods=['GET'])
@login_required
def index():
    busca_geral = request.args.get('busca', '').strip()
    f_genero = request.args.get('genero', '').strip()
    f_formacao = request.args.get('formacao', '').strip()
    f_localizacao = request.args.get('localizacao', '').strip()
    f_idioma = request.args.get('idioma', '').strip()
    f_nivel = request.args.get('nivel_idioma', '').strip()
    f_ordem = request.args.get('ordem', '').strip().lower()
    
    algum_filtro_ativo = any([busca_geral, f_genero, f_formacao, f_localizacao, f_idioma, f_nivel, f_ordem])
    
    if algum_filtro_ativo:
        session['ocultados'] = []
    elif 'ocultados' not in session:
        session['ocultados'] = []
        
    resultados_finais = []
    nome_empresa = "Sua Empresa"
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT nome_comercial FROM empresas WHERE id = %s", (current_user.empresa_id,))
                empresa_data = cursor.fetchone()
                if empresa_data:
                    nome_empresa = empresa_data['nome_comercial']

                cursor.execute("""
                    SELECT id, nome_arquivo, conteudo, nome_candidato AS nome, idade, sexo, 
                           localizacao, formacao, cursos, habilidades, hard_skills, soft_skills, idiomas, whatsapp,
                           areas_profissionais, data_cadastro
                    FROM curriculos 
                    WHERE empresa_id = %s
                """, (current_user.empresa_id,))
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
                        termos = busca_geral.split(',') if ',' in busca_geral else busca_geral.split()
                        for t in termos:
                            t_limpo = t.strip()
                            if t_limpo:
                                variacoes = obter_variacoes_busca(t_limpo)
                                if not any(v in texto_completo_candidato for v in variacoes):
                                    passou_filtro = False
                                    break

                    if f_genero and passou_filtro:
                        if remover_acentos(f_genero) != remover_acentos(item.get('sexo') or ""):
                            passou_filtro = False

                    if f_formacao and passou_filtro:
                        if remover_acentos(f_formacao) not in remover_acentos(item.get('formacao') or ""):
                            passou_filtro = False

                    if f_localizacao and passou_filtro:
                        if remover_acentos(f_localizacao) not in remover_acentos(item.get('localizacao') or ""):
                            passou_filtro = False

                    if f_idioma and passou_filtro:
                        if remover_acentos(f_idioma) not in texto_idiomas:
                            passou_filtro = False

                    if f_nivel and passou_filtro:
                        if remover_acentos(f_nivel) not in texto_idiomas:
                            passou_filtro = False

                    if passou_filtro:
                        resultados_finais.append(item)
                
                if f_ordem == 'nome':
                    resultados_finais.sort(key=lambda x: remover_acentos(x['nome'] or ""))
                elif f_ordem == 'nome_za':
                    resultados_finais.sort(key=lambda x: remover_acentos(x['nome'] or ""), reverse=True)
                elif f_ordem == 'antigo':
                    resultados_finais.sort(key=lambda x: x['id'])
                else:
                    resultados_finais.sort(key=lambda x: x['id'], reverse=True)
                        
    except Exception as e:
        print(f"Erro ao buscar dados: {e}")
        flash("Ocorreu um erro ao carregar os currículos.", "error")

    return render_template('index.html', candidatos=resultados_finais, nome_empresa=nome_empresa)

@app.route('/upload', methods=['POST'])
@login_required
def upload():
    if 'file' not in request.files:
        flash("Nenhum arquivo enviado.", "error")
        return redirect(url_for('index'))
        
    arquivo = request.files['file']
    if arquivo.filename == '':
        flash("Nenhum arquivo seleccionado.", "error")
        return redirect(url_for('index'))
        
    if arquivo:
        nome_original = arquivo.filename
        extensao = nome_original.rsplit('.', 1)[1].lower() if '.' in nome_original else ''
        
        if extensao not in ['pdf', 'docx']:
            flash("Formato inválido! Envie arquivos PDF ou DOCX.", "error")
            return redirect(url_for('index'))
            
        try:
            dados_bytes = arquivo.read()
            arquivo_b64 = base64.b64encode(dados_bytes).decode('utf-8')
            
            if extensao == 'pdf':
                texto_bruto = extrair_texto_pdf(dados_bytes)
            else:
                texto_bruto = extrair_texto_docx(dados_bytes)
                
            if not texto_bruto.strip():
                flash(f"Não foi possível ler o texto do arquivo '{nome_original}'.", "error")
                return redirect(url_for('index'))
                
            dados_ia = estruturar_curriculo_com_ia(texto_bruto)
            
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    # AJUSTADO: Inserção do campo areas_profissionais mapeado pela IA
                    cursor.execute("""
                        INSERT INTO curriculos (
                            empresa_id, nome_arquivo, conteudo, nome_candidato, idade, sexo, 
                            localizacao, formacao, cursos, habilidades, hard_skills, soft_skills, idiomas, arquivo_binario, whatsapp, areas_profissionais
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        current_user.empresa_id,
                        nome_original, 
                        texto_bruto, 
                        dados_ia['nome'], 
                        dados_ia['idade'], 
                        dados_ia['sexo'],
                        dados_ia['localizacao'], 
                        dados_ia['formacao'], 
                        dados_ia['cursos'], 
                        dados_ia['hard_skills'], 
                        dados_ia['hard_skills'], 
                        dados_ia['soft_skills'], 
                        dados_ia['idiomas'], 
                        arquivo_b64,
                        dados_ia['whatsapp'],
                        dados_ia['areas_profissionais']
                    ))
                    conn.commit()
                    
            flash(f"Currículo de '{dados_ia['nome']}' processado e salvo com sucesso!", "success")
        except Exception as e:
            print(f"Erro no upload: {e}")
            flash("Falha interna ao processar documento.", "error")
            
    return redirect(url_for('index'))

@app.route('/ocultar/<int:id_candidato>', methods=['POST'])
@login_required
def ocultar(id_candidato):
    if 'ocultados' not in session:
        session['ocultados'] = []
    lista = list(session['ocultados'])
    if id_candidato not in lista:
        lista.append(id_candidato)
        session['ocultados'] = lista
    return jsonify({"status": "sucesso", "mensagem": "Candidato ocultado da tela"})

@app.route('/excluir/<int:id_candidato>', methods=['POST'])
@login_required
def excluir(id_candidato):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM curriculos WHERE id = %s AND empresa_id = %s", (id_candidato, current_user.empresa_id))
                conn.commit()
        return jsonify({"status": "sucesso", "mensagem": "Currículo excluído com sucesso"})
    except Exception as e:
        print(f"Erro ao excluir currículo: {e}")
        return jsonify({"status": "erro", "mensagem": "Erro interno ao excluir o currículo"}), 500

# ==============================================================================
# VISUALIZAÇÃO E DOWNLOAD DE ARQUIVOS ORIGINAIS
# ==============================================================================
@app.route('/download/<int:id_candidato>', methods=['GET'])
@login_required
def download(id_candidato):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT nome_arquivo, arquivo_binario FROM curriculos WHERE id = %s AND empresa_id = %s", (id_candidato, current_user.empresa_id))
                resultado = cursor.fetchone()
                
                if resultado and resultado['arquivo_binario']:
                    dados_arquivos = base64.b64decode(resultado['arquivo_binario'])
                    return send_file(
                        io.BytesIO(dados_arquivos),
                        download_name=resultado['nome_arquivo'],
                        as_attachment=True
                    )
                else:
                    flash("Arquivo original não encontrado ou acesso não autorizado.", "error")
                    return redirect(url_for('index'))
    except Exception as e:
        print(f"Erro no download: {e}")
        flash("Erro ao resgatar arquivo do banco de dados.", "error")
        return redirect(url_for('index'))

@app.route('/visualizar_original/<int:id_candidato>', methods=['GET'])
@login_required
def visualizar_original(id_candidato):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT nome_arquivo, arquivo_binario FROM curriculos WHERE id = %s AND empresa_id = %s", (id_candidato, current_user.empresa_id))
                resultado = cursor.fetchone()
                
                if resultado and resultado['arquivo_binario']:
                    dados_arquivos = base64.b64decode(resultado['arquivo_binario'])
                    nome_arquivo = resultado['nome_arquivo']
                    extensao = nome_arquivo.rsplit('.', 1)[1].lower() if '.' in nome_arquivo else ''
                    
                    mimetype = 'application/pdf' if extensao == 'pdf' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                    
                    return send_file(
                        io.BytesIO(dados_arquivos),
                        mimetype=mimetype,
                        download_name=nome_arquivo,
                        as_attachment=False
                    )
                else:
                    flash("Arquivo original não encontrado ou acesso não autorizado.", "error")
                    return redirect(url_for('index'))
    except Exception as e:
        print(f"Erro ao visualizar arquivo original: {e}")
        flash("Erro ao abrir o arquivo original.", "error")
        return redirect(url_for('index'))

@app.route('/visualizar/<int:id_candidato>', methods=['GET'])
@login_required
def visualizar(id_candidato):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT id, nome_arquivo, conteudo, nome_candidato AS nome, idade, sexo, 
                           localizacao, formacao, cursos, hard_skills, soft_skills, idiomas, whatsapp, areas_profissionais
                    FROM curriculos WHERE id = %s AND empresa_id = %s
                """, (id_candidato, current_user.empresa_id))
                candidato = cursor.fetchone()
                
                if not candidato:
                    flash("Candidato não encontrado ou acesso não autorizado.", "error")
                    return redirect(url_for('index'))
                    
        return render_template('visualizar.html', candidato=candidato)
    except Exception as e:
        print(f"Erro ao visualizar currículo: {e}")
        flash("Erro ao carregar os detalhes do currículo.", "error")
        return redirect(url_for('index'))

# ==============================================================================
# GESTÃO DE VAGAS
# ==============================================================================
@app.route('/cadastrar_vaga', methods=['GET', 'POST'])
@login_required
def cadastrar_vaga():
    if request.method == 'POST':
        titulo = request.form.get('titulo')
        descricao = request.form.get('descricao')
        requisitos = request.form.get('requisitos')
        localizacao = request.form.get('localizacao')
        atividades = request.form.get('atividades')
        beneficios = request.form.get('beneficios')
        remuneracao = request.form.get('remuneracao')
        expediente = request.form.get('expediente')
        
        if not titulo or not descricao:
            flash("Título e Descrição são obrigatórios.", "error")
            return render_template('cadastrar_vaga.html')
            
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO vagas (empresa_id, titulo, descricao, requisitos, localizacao, atividades, beneficios, remuneracao, expediente)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (current_user.empresa_id, titulo, descricao, requisitos, localizacao, atividades, beneficios, remuneracao, expediente))
                    conn.commit()
            flash("Vaga cadastrada com sucesso!", "success")
            return redirect(url_for('listar_vagas'))
        except Exception as e:
            print(f"Erro ao cadastrar vaga: {e}")
            flash("Erro interno ao salvar vaga.", "error")
            
    return render_template('cadastrar_vaga.html')

@app.route('/vagas/<int:id_vaga>/editar', methods=['GET', 'POST'])
@login_required
def editar_vaga(id_vaga):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT id, titulo, descricao, requisitos, localizacao, 
                           atividades, beneficios, remuneracao, expediente 
                    FROM vagas WHERE id = %s AND empresa_id = %s
                """, (id_vaga, current_user.empresa_id))
                vaga = cursor.fetchone()

        if not vaga:
            flash("Vaga não encontrada ou acesso não autorizado.", "error")
            return redirect(url_for('listar_vagas'))

    except Exception as e:
        print(f"Erro ao buscar vaga para edição: {e}")
        flash("Erro ao carregar dados da vaga.", "error")
        return redirect(url_for('listar_vagas'))

    if request.method == 'POST':
        titulo = request.form.get('titulo')
        localizacao = request.form.get('localizacao')
        descricao = request.form.get('descricao')
        atividades = request.form.get('atividades')
        requisitos = request.form.get('requisitos')
        remuneracao = request.form.get('remuneracao')
        beneficios = request.form.get('beneficios')
        expediente = request.form.get('expediente')

        if not titulo or not descricao:
            flash("Título e Descrição são obrigatórios.", "error")
            return render_template('editar_vaga.html', vaga=vaga)

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE vagas 
                        SET titulo = %s, localizacao = %s, descricao = %s, atividades = %s, 
                            requisitos = %s, remuneracao = %s, beneficios = %s, expediente = %s
                        WHERE id = %s AND empresa_id = %s
                    """, (titulo, localizacao, descricao, atividades, requisitos, remuneracao, beneficios, expediente, id_vaga, current_user.empresa_id))
                    conn.commit()
            flash("Vaga updated com sucesso!", "success")
            return redirect(url_for('listar_vagas'))
        except Exception as e:
            print(f"Erro ao atualizar vaga: {e}")
            flash("Erro interno ao salvar as alterações da vaga.", "error")

    return render_template('editar_vaga.html', vaga=vaga)

@app.route('/vagas', methods=['GET'])
@login_required
def listar_vagas():
    vagas_disponiveis = []
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT id, titulo, descricao, requisitos, localizacao, 
                           atividades, beneficios, remuneracao, expediente, data_criacao 
                    FROM vagas 
                    WHERE empresa_id = %s 
                    ORDER BY id DESC
                """, (current_user.empresa_id,))
                vagas_disponiveis = cursor.fetchall()
    except Exception as e:
        print(f"Erro ao buscar vagas: {e}")
        flash("Erro ao carregar as vagas.", "error")
        
    return render_template('vagas.html', vagas=vagas_disponiveis)

# ==============================================================================
# CRUZAMENTO E ANÁLISE DE VAGAS VS CANDIDATOS
# ==============================================================================
@app.route('/vagas/<int:id_vaga>/analise', methods=['GET'])
@login_required
def analisar_vaga(id_vaga):
    if not client:
        flash("Integração com Inteligência Artificial não configurada.", "error")
        return redirect(url_for('listar_vagas'))
        
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT * FROM vagas WHERE id = %s AND empresa_id = %s", (id_vaga, current_user.empresa_id))
                vaga = cursor.fetchone()
                
                if not vaga:
                    flash("Vaga não encontrada ou acesso negado.", "error")
                    return redirect(url_for('listar_vagas'))
                
                cursor.execute("""
                    SELECT id, nome_candidato AS nome, formacao, hard_skills, soft_skills, idiomas, conteudo 
                    FROM curriculos 
                    WHERE empresa_id = %s
                """, (current_user.empresa_id,))
                candidatos = cursor.fetchall()
                
        if not candidatos:
            flash("Nenhum currículo cadastrado na sua empresa para cruzar com esta vaga.", "error")
            return redirect(url_for('listar_vagas'))
            
        dados_candidatos_prompt = []
        for c in candidatos:
            dados_candidatos_prompt.append({
                "id_candidato": c['id'],
                "nome": c['nome'] or "Sem Nome",
                "perfil_resumido": f"Skills Técnicas: {c['hard_skills']}. Comportamental: {c['soft_skills']}. Idiomas: {c['idiomas']}. Formação: {c['formacao']}"
            })

        system_instruction = (
            "Você é um Headhunter sênior focado em People Analytics.\n"
            "Sua tarefa é analisar uma vaga de emprego específica e gerar um ranking comparativo em formato JSON contendo a porcentagem de "
            "compatibilidade (de 0 a 100) e uma breve justificativa de aderência para cada candidato fornecido."
        )

        prompt_conteudo = (
            f"VAGA ALVO:\n"
            f"Título: {vaga['titulo']}\n"
            f"Descrição: {vaga['descricao']}\n"
            f"Requisitos: {vaga['requisitos']}\n\n"
            f"LISTA DE CANDIDATOS:\n"
            f"{json.dumps(dados_candidatos_prompt, ensure_ascii=False)}"
        )

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt_conteudo,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ResultadoAnaliseVaga,
                system_instruction=system_instruction,
                temperature=0.2
            )
        )
        
        analise_json = json.loads(response.text.strip()) if response.text else {}
        return render_template('analise.html', vaga=vaga, resultado=analise_json)
        
    except Exception as e:
        print(f"Erro na análise de vagas com IA: {e}")
        flash("Ocorreu um erro interno ao processar a inteligência artificial.", "error")
        return redirect(url_for('listar_vagas'))

# ==============================================================================
# CONTROLE MASTER ADMINISTRATIVO (GERENCIAMENTO DE TENANTS / CANCELAMENTOS)
# ==============================================================================
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "pulse_admin_2026")

@app.route('/master-admin/empresas', methods=['GET'])
def admin_listar_empresas():
    token = request.args.get('token')
    if token != ADMIN_TOKEN:
        return "Acesso não autorizado", 403
        
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT e.id, e.nome_comercial, e.data_cadastro,
                           (SELECT COUNT(*) FROM usuarios u WHERE u.empresa_id = e.id) as qtd_usuarios,
                           (SELECT COUNT(*) FROM curriculos c WHERE c.empresa_id = e.id) as qtd_curriculos,
                           (SELECT COUNT(*) FROM vagas v WHERE v.empresa_id = e.id) as qtd_vagas
                    FROM empresas e
                    ORDER BY e.id DESC
                """)
                empresas = cursor.fetchall()
                
        html_admin = f"""
        <html>
        <head>
            <title>Master Admin - TalentPulse</title>
            <style>
                body {{ font-family: sans-serif; padding: 40px; background: #f4f6f8; color: #1e293b; }}
                h2 {{ color: #0f172a; border-bottom: 2px solid #e2e8f0; padding-bottom: 10px; }}
                table {{ width: 100%; border-collapse: collapse; background: #fff; margin-top: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-radius: 6px; overflow: hidden; }}
                th, td {{ padding: 14px; border: 1px solid #e2e8f0; text-align: left; }}
                th {{ background: #0f172a; color: white; font-weight: 600; }}
                tr:nth-child(even) {{ background: #f8fafc; }}
                .btn-delete {{ background: #dc2626; color: white; border: none; padding: 8px 14px; cursor: pointer; border-radius: 4px; font-weight: bold; transition: background 0.2s; }}
                .btn-delete:hover {{ background: #b91c1c; }}
            </style>
        </head>
        <body>
            <h2>Painel de Controle de Clientes (Tenants)</h2>
            <p>Gerencie os contratos ativos da plataforma TalentPulse de forma unificada.</p>
            <table>
                <tr>
                    <th>ID</th><th>Nome da Empresa</th><th>Data Cadastro</th><th>Usuários</th><th>Currículos</th><th>Vagas</th><th>Ações</th>
                </tr>
        """
        for emp in empresas:
            html_admin += f"""
                <tr>
                    <td>{emp['id']}</td>
                    <td><strong>{emp['nome_comercial']}</strong></td>
                    <td>{emp['data_cadastro']}</td>
                    <td>{emp['qtd_usuarios']}</td>
                    <td>{emp['qtd_curriculos']}</td>
                    <td>{emp['qtd_vagas']}</td>
                    <td>
                        <form action="/master-admin/empresas/{emp['id']}/excluir?token={token}" method="POST" onsubmit="return confirm('ATENÇÃO CRÍTICA: Deletar esta empresa apagará TODOS os usuários, currículos e vagas dela permanentemente. Confirmar cancelamento?');">
                            <button class="btn-delete" type="submit">Cancelar Contrato</button>
                        </form>
                    </td>
                </tr>
            """
        html_admin += "</table></body></html>"
        return Response(html_admin, mimetype='text/html')
    except Exception as e:
        return f"Erro ao carregar o painel administrativo: {e}", 500

@app.route('/master-admin/empresas/<int:id_empresa>/excluir', methods=['POST'])
def admin_excluir_empresa(id_empresa):
    token = request.args.get('token')
    if token != ADMIN_TOKEN:
        return "Acesso não autorizado", 403
        
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM empresas WHERE id = %s", (id_empresa,))
                conn.commit()
                
        return f"""
        <div style="font-family: sans-serif; padding: 40px; text-align: center;">
            <h3 style="color: #16a34a;">Empresa ID {id_empresa} e todos os seus dados associados foram excluídos com sucesso do banco!</h3>
            <br>
            <a href='/master-admin/empresas?token={token}' style="display: inline-block; background: #0f172a; color: white; text-decoration: none; padding: 10px 20px; border-radius: 4px;">Voltar ao Painel</a>
        </div>
        """
    except Exception as e:
        return f"Erro ao deletar empresa: {e}", 500
