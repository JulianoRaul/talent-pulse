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
                
                # Tabela de Usuários do Sistema
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS usuarios (
                        id SERIAL PRIMARY KEY,
                        nome TEXT NOT NULL,
                        email TEXT UNIQUE NOT NULL,
                        senha_hash TEXT NOT NULL
                    );
                ''')

                # Tabela de Vagas
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS vagas (
                        id SERIAL PRIMARY KEY,
                        titulo TEXT NOT NULL,
                        descricao TEXT NOT NULL,
                        requisitos TEXT,
                        localizacao TEXT,
                        data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                ''')
                # NOVAS COLUNAS ADICIONADAS AUTOMATICAMENTE
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS atividades TEXT;')
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS beneficios TEXT;')
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS remuneracao TEXT;')
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS expediente TEXT;')
                
                conn.commit()
    except Exception as e:
        print(f"Erro ao inicializar o banco de dados: {e}")

# Inicializa o banco de dados
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
    hard_skills: str   
    soft_skills: str   
    idiomas: str
    
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
                    cursor.execute("SELECT id FROM usuarios WHERE email = %s", (email,))
                    if cursor.fetchone():
                        flash("Este e-mail já está cadastrado.", "error")
                        return redirect(url_for('cadastro'))
                    
                    senha_hash = generate_password_hash(senha)
                    
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
                        arquivo_b64
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
                cursor.execute("DELETE FROM curriculos WHERE id = %s", (id_candidato,))
                conn.commit()
        flash("Currículo excluído com sucesso!", "success")
        return redirect(url_for('index'))
    except Exception as e:
        print(f"Erro ao excluir currículo: {e}")
        flash("Erro interno ao excluir o currículo.", "error")
        return redirect(url_for('index'))

# ==============================================================================
# VISUALIZAÇÃO E DOWNLOAD DE ARQUIVOS ORIGINAIS
# ==============================================================================
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


@app.route('/visualizar_original/<int:id_candidato>', methods=['GET'])
@login_required
def visualizar_original(id_candidato):
    """Rota que abre o arquivo PDF/DOCX original diretamente no navegador."""
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT nome_arquivo, arquivo_binario FROM curriculos WHERE id = %s", (id_candidato,))
                resultado = cursor.fetchone()
                
                if resultado and resultado['arquivo_binario']:
                    dados_arquivos = base64.b64decode(resultado['arquivo_binario'])
                    nome_arquivo = resultado['nome_arquivo']
                    extensao = nome_arquivo.rsplit('.', 1)[1].lower() if '.' in nome_arquivo else ''
                    
                    # Define o tipo de conteúdo para o navegador saber como renderizar
                    mimetype = 'application/pdf' if extensao == 'pdf' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                    
                    return send_file(
                        io.BytesIO(dados_arquivos),
                        mimetype=mimetype,
                        download_name=nome_arquivo,
                        as_attachment=False  # Crucial para abrir no navegador
                    )
                else:
                    flash("Arquivo original não encontrado.", "error")
                    return redirect(url_for('index'))
    except Exception as e:
        print(f"Erro ao visualizar arquivo original: {e}")
        flash("Erro ao abrir o arquivo original.", "error")
        return redirect(url_for('index'))


@app.route('/visualizar/<int:id_candidato>', methods=['GET'])
@login_required
def visualizar(id_candidato):
    """Mantém a visualização do perfil estruturado pela IA."""
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT id, nome_arquivo, conteudo, nome_candidato AS nome, idade, sexo, 
                           localizacao, formacao, cursos, hard_skills, soft_skills, idiomas 
                    FROM curriculos WHERE id = %s
                """, (id_candidato,))
                candidato = cursor.fetchone()
                
                if not candidato:
                    flash("Candidato não encontrado.", "error")
                    return redirect(url_for('index'))
                    
        return render_template('visualizar.html', candidato=candidato)
    except Exception as e:
        print(f"Erro ao visualizar currículo: {e}")
        flash("Erro ao carregar os detalhes do currículo.", "error")
        return redirect(url_for('index'))

# ==============================================================================
# GESTÃO DE VAGAS (CAMPOS ADICIONAIS ATUALIZADOS)
# ==============================================================================
@app.route('/cadastrar_vaga', methods=['GET', 'POST'])
@login_required
def cadastrar_vaga():
    if request.method == 'POST':
        titulo = request.form.get('titulo')
        descricao = request.form.get('descricao')
        requisitos = request.form.get('requisitos')
        localizacao = request.form.get('localizacao')
        
        # CAPTURA DOS NOVOS CAMPOS DO FORMULÁRIO HTML
        atividades = request.form.get('atividades')
        beneficios = request.form.get('beneficios')
        remuneracao = request.form.get('remuneracao')
        expediente = request.form.get('expediente')
        
        if not titulo or not descricao:
            flash("Título e Descrição são obrigatórios.", "error")
            return redirect(url_for('cadastrar_vaga'))
            
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO vagas (titulo, descricao, requisitos, localizacao, atividades, beneficios, remuneracao, expediente)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (titulo, descricao, requisitos, localizacao, atividades, beneficios, remuneracao, expediente))
                    conn.commit()
            flash("Vaga cadastrada com sucesso!", "success")
            return redirect(url_for('listar_vagas'))
        except Exception as e:
            print(f"Erro ao cadastrar vaga: {e}")
            flash("Erro interno ao salvar vaga.", "error")
            
    return render_template('cadastrar_vaga.html')

@app.route('/vagas', methods=['GET'])
@login_required
def listar_vagas():
    vagas_disponiveis = []
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # SELECT ATUALIZADO PARA RESGATAR AS NOVAS INFORMAÇÕES
                cursor.execute("""
                    SELECT id, titulo, descricao, requisitos, localizacao, 
                           atividades, beneficios, remuneracao, expediente, data_criacao 
                    FROM vagas ORDER BY id DESC
                """)
                vagas_disponiveis = cursor.fetchall()
    except Exception as e:
        print(f"Erro ao buscar vagas: {e}")
        flash("Erro ao carregar as vagas.", "error")
        
    return render_template('vagas.html', vagas=vagas_disponiveis)

# ==============================================================================
# CRUZAMENTO E ANÁLISE DE VAGAS VS CANDIDATOS (INTEGRAÇÃO DE NOVOS CAMPOS)
# ==============================================================================
@app.route('/vagas/<int:id_vaga>/analise', methods=['GET'])
@login_required
def analisar_vaga(id_vaga):
    if not client:
        flash("A chave da API do Gemini não está configurada.", "error")
        return redirect(url_for('listar_vagas'))

    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # 1. Busca os detalhes da vaga selecionada incluindo os novos campos
                cursor.execute("""
                    SELECT id, titulo, descricao, requisitos, localizacao, 
                           atividades, beneficios, remuneracao, expediente 
                    FROM vagas WHERE id = %s
                """, (id_vaga,))
                vaga = cursor.fetchone()
                
                if not vaga:
                    flash("Vaga não encontrada.", "error")
                    return redirect(url_for('listar_vagas'))
                
                # 2. Busca todos os candidatos/currículos estruturados do sistema
                cursor.execute("""
                    SELECT id, nome_candidato AS nome, localizacao, formacao, 
                           cursos, hard_skills, soft_skills, idiomas 
                    FROM curriculos
                """)
                candidatos = cursor.fetchall()

        if not candidatos:
            flash("Nenhum candidato cadastrado no sistema para realizar a análise.", "warning")
            return render_template('analise_vaga.html', vaga=vaga, resultados=[])

        # 3. Prepara o contexto de dados adicionando os novos parâmetros na string de envio para a IA
        dados_vaga_txt = (
            f"TÍTULO DA VAGA: {vaga['titulo']}\n"
            f"DESCRIÇÃO: {vaga['descricao']}\n"
            f"PRINCIPAIS ATIVIDADES: {vaga.get('atividades') or 'Não informadas'}\n"
            f"REQUISITOS: {vaga['requisitos']}\n"
            f"REMUNERAÇÃO: {vaga.get('remuneracao') or 'Não informada'}\n"
            f"BENEFÍCIOS: {vaga.get('beneficios') or 'Não informados'}\n"
            f"EXPEDIENTE: {vaga.get('expediente') or 'Não informado'}\n"
            f"LOCALIZAÇÃO DA VAGA: {vaga['localizacao']}\n"
        )

        lista_candidatos_txt = ""
        for c in candidatos:
            lista_candidatos_txt += (
                f"--- CANDIDATO ID: {c['id']} ---\n"
                f"Nome: {c['nome']}\n"
                f"Localização: {c['localizacao']}\n"
                f"Formação: {c['formacao']}\n"
                f"Cursos: {c['cursos']}\n"
                f"Hard Skills: {c['hard_skills']}\n"
                f"Soft Skills: {c['soft_skills']}\n"
                f"Idiomas: {c['idiomas']}\n\n"
            )

        # 4. Prompt do sistema para orientar o Gemini (Instruções atualizadas)
        system_prompt = (
            "Você é um Headhunter de TI e Especialista em Recrutamento e Seleção avançado.\n"
            "Sua missão é realizar o cruzamento de dados (matching) entre uma vaga de emprego específica e a lista de candidatos fornecida.\n\n"
            "Diretrizes:\n"
            "1. Avalie cuidadosamente a compatibilidade de cada candidato considerando as hard skills, soft skills, localização, as principais atividades exigidas e se há match com o expediente/remuneração.\n"
            "2. Atribua uma porcentagem de compatibilidade (0 a 100) baseada puramente em critérios técnicos e de negócios.\n"
            "3. Crie uma justificativa direta, profissional e clara (máximo 3 linhas) explicando o porquê dessa pontuação.\n"
            "4. Retorne a lista ordenada de forma decrescente, colocando os candidatos mais compatíveis no topo."
        )

        conteudo_requisicao = (
            f"Aqui estão os detalhes da vaga:\n\n{dados_vaga_txt}\n"
            f"Aqui está a lista de candidatos cadastrados:\n\n{lista_candidatos_txt}"
        )

        # 5. Chamada ao modelo Gemini utilizando a estrutura Pydantic configurada
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=conteudo_requisicao,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ResultadoAnaliseVaga,
                system_instruction=system_prompt,
                temperature=0.2
            )
        )

        # 6. Trata o retorno JSON
        texto_resposta = response.text.strip() if response.text else ""
        if texto_resposta:
            dados_analise = json.loads(texto_resposta)
            resultados = dados_analise.get("candidatos_compativeis", [])
            return render_template('analise_vaga.html', vaga=vaga, resultados=resultados)
        else:
            flash("Erro ao processar análise inteligente.", "error")
            return redirect(url_for('listar_vagas'))

    except Exception as e:
        print(f"Erro ao analisar vaga: {e}")
        flash("Erro interno ao gerar análise de match.", "error")
        return redirect(url_for('listar_vagas'))

if __name__ == '__main__':
    app.run(debug=True)
