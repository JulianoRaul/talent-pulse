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
    def __init__(self, id, email, nome):
        self.id = id
        self.email = email
        self.nome = nome

@login_manager.user_loader
def load_user(user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT id, email, nome FROM usuarios WHERE id = %s", (int(user_id),))
                user_data = cursor.fetchone()
                if user_data:
                    return User(id=str(user_data['id']), email=user_data['email'], nome=user_data['nome'])
    except Exception as e:
        print(f"Erro ao carregar usuário: {e}")
    return None

# ==============================================================================
# CONFIGURAÇÃO DO BANCO DE DADOS (POSTGRESQL)
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
                # Tabela de Currículos
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
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS idiomas TEXT;')
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS hard_skills TEXT;')
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS soft_skills TEXT;')
                
                # Nova Tabela de Usuários do Sistema
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS usuarios (
                        id SERIAL PRIMARY KEY,
                        nome TEXT NOT NULL,
                        email TEXT UNIQUE NOT NULL,
                        senha_hash TEXT NOT NULL
                    );
                ''')
                conn.commit()
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
    id_idade: str
    sexo: str
    localizacao: str
    formacao: str
    cursos: str
    hard_skills: str   
    soft_skills: str   
    idiomas: str

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
            "cursos": "Nenhum", "hard_skills": "Nenhuma", "soft_skills": "Nenhuma", "idiomas": "Não informado"
        }
    
    texto_limitado = texto_bruto.strip()[:24000]
    if not client:
        return {
            "nome": "Sem Chave API", "idade": "Não Informado", "sexo": "Não Informado",
            "localizacao": "Configuração Pendente", "formacao": "A IA não pôde ser chamada.",
            "cursos": "Nenhum", "hard_skills": "Nenhuma", "soft_skills": "Nenhuma", "idiomas": "Não informado"
        }
        
    system_prompt = (
        "Você é um especialista em recrutamento avançado e triagem de currículos.\n"
        "Sua tarefa é analisar o texto do candidato e extrair os dados dividindo estritamente as competências conforme as diretrizes abaixo:\n\n"
        "1. HARD SKILLS:\n"
        "Identifique e liste apenas habilidades técnicas palpáveis, conhecimentos operacionais, ferramentas, metodologias profissionais, frameworks e linguagens de programação. Separe-as por vírgula.\n"
        "Exemplos: Python, JavaScript, Excel Avançado, SQL, Contabilidade, Gestão de Tráfego, Photoshop, CRM Pipedrive.\n\n"
        "2. SOFT SKILLS:\n"
        "Classifique e liste exclusivamente as características e competências comportamentais baseadas estritamente nos seguintes tipos mapeados:\n"
        "- Comunicação (expressar ideias claras/concisas, saber ouvir)\n"
        "- Liderança (influenciar, motivar pessoas, guiar equipes)\n"
        "- Trabalho em equipe (colaborar com terceiros, alcançar resultados juntos)\n"
        "- Resolução de problemas (analisar situações complexas, soluções criativas)\n"
        "- Inteligência emocional (gerenciar emoções, lidar com o estresse, relações saudáveis)\n"
        "- Adaptabilidade (ajustar-se a mudanças, aprender rápido)\n"
        "- Criatividade (pensar fora da caixa, ideias originais)\n"
        "Adicione na lista de soft_skills apenas os termos identificados que correspondam ou derivem desse grupo conceitual, separados por vírgula.\n\n"
        "Mapeie também o campo 'idiomas' (Iniciante, Intermediário ou Avançado/Fluente). Se algum campo não existir, marque 'Não informado'."
    )

    max_tentativas = 3
    tempo_espera = 2  # segundos inicial

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
                return {k: limpar_caracteres_invalidos(str(v)) for k, v in dados.items()}
                
        except Exception as e:
            print(f"[AVISO] Tentativa {tentativa + 1} de {max_tentativas} falhou devido a instabilidade na API (Erro: {e})")
            if tentativa < max_tentativas - 1:
                print(f"Aguardando {tempo_espera} segundos antes da próxima tentativa...")
                time.sleep(tempo_espera)
                tempo_espera *= 2
            else:
                print("Número máximo de retentativas atingido no Gemini GenAI. Aplicando proteção de fallback.")
        
    return {
        "nome": "Nome provisório", "idade": "Não Informado", "sexo": "Não Informado",
        "localizacao": "Manual necessário", "formacao": "O servidor da IA estava instável no momento do processamento.",
        "cursos": "Consulte o arquivo original", "hard_skills": "Análise Manual", "soft_skills": "Análise Manual", "idiomas": "Não informado"
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
                usuario = User(id=str(user_data['id']), email=user_data['email'], nome=user_data['nome'])
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
        
        try:
            with get_db_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    # Verifica se o e-mail já existe
                    cursor.execute("SELECT id FROM usuarios WHERE email = %s", (email,))
                    if cursor.fetchone():
                        flash("Este e-mail já está cadastrado.", "error")
                        return redirect(url_for('cadastro'))
                    
                    # Criptografa a senha antes de salvar
                    senha_hash = generate_password_hash(senha)
                    
                    # Salva no banco de dados real
                    cursor.execute(
                        "INSERT INTO usuarios (nome, email, senha_hash) VALUES (%s, %s, %s) RETURNING id",
                        (nome, email, senha_hash)
                    )
                    conn.commit()
            
            flash("Usuário criado com sucesso! Faça seu login.", "success")
            return redirect(url_for('login'))
        except Exception as e:
            print(f"Erro no cadastro: {e}")
            flash("Erro ao salvar novo usuário.", "error")
        
    return render_template('cadastro.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Sessão encerrada com sucesso.", "success")
    return redirect(url_for('login'))

# ==============================================================================
# ROTAS DA APLICAÇÃO WEB (PROTEGIDAS)
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
    
    algum_filtro_ativo = any([busca_geral, f_genero, f_formacao, f_localizacao, f_idioma, f_nivel])
    
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
                        
    except Exception as e:
        print(f"Erro ao buscar dados: {e}")
        flash("Ocorreu um erro ao carregar os currículos.", "error")

    return render_template('index.html', candidatos=resultados_finais)

@app.route('/upload', methods=['POST'])
@login_required
def upload():
    if 'file' not in request.files:
        flash("Nenhum arquivo enviado.", "error")
        return redirect(url_for('index'))
        
    arquivo = request.files['file']
    if arquivo.filename == '':
        flash("Nenhum arquivo selecionado.", "error")
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
                    cursor.execute("""
                        INSERT INTO curriculos (
                            nome_arquivo, conteudo, nome_candidato, idade, sexo, 
                            localizacao, formacao, cursos, habilidades, hard_skills, soft_skills, idiomas, arquivo_binario
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        nome_original, texto_bruto, dados_ia['nome'], dados_ia['idade'], dados_ia['sexo'],
                        dados_ia['localizacao'], dados_ia['formacao'], dados_ia['cursos'], 
                        dados_ia['hard_skills'], dados_ia['hard_skills'], dados_ia['soft_skills'], dados_ia['idiomas'], arquivo_b64
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

@app.route('/download/<int:id_candidato>', methods=['GET'])
@login_required
def download(id_candidato):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT nome_arquivo, arquivo_binario FROM curriculos WHERE id = %s", (id_candidato,))
                resultado = cursor.fetchone()
                
                if resultado and resultado['arquivo_binario']:
                    dados_arquivos = base64.b64decode(resultado['arquivo_binario'])
                    return send_file(
                        io.BytesIO(dados_arquivos),
                        download_name=resultado['nome_arquivo'],
                        as_attachment=True
                    )
                else:
                    flash("Arquivo original não encontrado.", "error")
                    return redirect(url_for('index'))
    except Exception as e:
        print(f"Erro no download: {e}")
        flash("Erro ao resgatar arquivo do banco de dados.", "error")
        return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)
