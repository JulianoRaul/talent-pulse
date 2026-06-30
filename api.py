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
                usuario = User(id=str(user_data['id']), email=user_data
