import os
import sys
import re
import io
import json
import base64
import unicodedata
import time
from datetime import datetime, timedelta
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
            # Adiciona options ou força sslmode se necessário
            return psycopg2.connect(url_conexao, sslmode='require')
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
                
                cursor.execute("ALTER TABLE empresas ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'ativo';")
                cursor.execute("ALTER TABLE empresas ADD COLUMN IF NOT EXISTS data_expiracao TIMESTAMP WITHOUT TIME ZONE;")
                cursor.execute("ALTER TABLE empresas ADD COLUMN IF NOT EXISTS plano TEXT DEFAULT 'starter';")
                cursor.execute("ALTER TABLE empresas ADD COLUMN IF NOT EXISTS limite_mensal INTEGER DEFAULT 300;")

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
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS areas_profissionais TEXT[];')
                cursor.execute('ALTER TABLE curriculos ADD COLUMN IF NOT EXISTS data_cadastro TIMESTAMP WITHOUT TIME ZONE;')
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
                
                # Garante a existência de todas as colunas necessárias na tabela vagas
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS atividades TEXT;')
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS beneficios TEXT;')
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS remuneracao TEXT;')
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS expediente TEXT;')
                cursor.execute('ALTER TABLE vagas ADD COLUMN IF NOT EXISTS token_compartilhamento TEXT UNIQUE;')
                
                # Bloco seguro para renomear 'actividades' para 'atividades' apenas se ambas existirem ou se necessário
                cursor.execute('''
                    DO $$ 
                    BEGIN 
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns 
                            WHERE table_name='vagas' and column_name='actividades'
                        ) AND NOT EXISTS (
                            SELECT 1 FROM information_schema.columns 
                            WHERE table_name='vagas' and column_name='atividades'
                        ) THEN
                            ALTER TABLE vagas RENAME COLUMN actividades TO atividades;
                        END IF;
                    END $$;
                ''')

                # 5. Tabela de Cache de Análise Inteligente do Currículo
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS analises_ia (
                        id SERIAL PRIMARY KEY,
                        curriculo_id INTEGER UNIQUE REFERENCES curriculos(id) ON DELETE CASCADE,
                        dados_json JSONB NOT NULL,
                        data_analise TIMESTAMP DEFAULT (timezone('America/Sao_Paulo', NOW()))
                    );
                ''')

                # 6. Tabela de Histórico de Match do Candidato na Vaga
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS historico_analises_vagas (
                        id SERIAL PRIMARY KEY,
                        vaga_id INTEGER REFERENCES vagas(id) ON DELETE CASCADE,
                        curriculo_id INTEGER REFERENCES curriculos(id) ON DELETE CASCADE,
                        porcentagem_compatibilidade INTEGER NOT NULL,
                        justificativa TEXT NOT NULL,
                        data_analise TIMESTAMP DEFAULT (timezone('America/Sao_Paulo', NOW())),
                        UNIQUE(vaga_id, curriculo_id)
                    );
                ''')

                # 7. Tabela de Histórico do Chat Interativo IA (Isolado por Tenant)
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS mensagens_chat (
                        id SERIAL PRIMARY KEY,
                        empresa_id INTEGER REFERENCES empresas(id) ON DELETE CASCADE,
                        usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
                        remetente TEXT NOT NULL, -- 'usuario' ou 'ia'
                        mensagem TEXT NOT NULL,
                        data_envio TIMESTAMP DEFAULT (timezone('America/Sao_Paulo', NOW()))
                    );
                ''')
                
                conn.commit()
    except Exception as e:
        print(f"Erro ao inicializar o banco de dados: {e}")

init_db()
# ==============================================================================
# CONFIGURAÇÃO DO GOOGLE GEMINI AI & SCHEMAS DE RETORNO
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

class ParecerRetroJogo(BaseModel):
    titulo_classe: str 
    level: int 
    vida_hp: int 
    mana_mp: int 
    pontos_fortes: list[str] 
    pontos_fracos: list[str] 
    habilities_especiais: list[str] = [] 
    habilidades_especiais: list[str] 
    tipos_de_vagas_recomendadas: list[str] 
    resumo_narrativa: str 

# ==============================================================================
# FUNÇÕES AUXILIARES DE TEXTO E FILTRAGEM DE CUSTO
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

def otimizar_texto_ia(texto):
    if not texto:
        return ""
    texto_limpo = re.sub(r'[ \t]+', ' ', texto)
    texto_limpo = re.sub(r'\n+', '\n', texto_limpo)
    return texto_limpo.strip()[:8000]

def pre_filtro_compatibilidade(requisitos_vaga, descricao_vaga, titulo_vaga, texto_curriculo):
    texto_comparativo_vaga = f"{requisitos_vaga or ''} {descricao_vaga or ''} {titulo_vaga or ''}"
    vaga_normalizada = remover_acentos(texto_comparativo_vaga)
    curriculo_normalizado = remover_acentos(texto_curriculo)
    
    palavras_vaga = set(vaga_normalizada.split())
    palavras_curriculo = set(curriculo_normalizado.split())
    
    stop_words = {
        'e', 'o', 'a', 'os', 'as', 'de', 'do', 'da', 'em', 'para', 'com', 'que', 'em', 'um', 'uma',
        'para', 'por', 'sobre', 'sob', 'atras', 'entre', 'com', 'sem', 'no', 'na', 'nos', 'nas'
    }
    palavras_vaga -= stop_words
    palavras_curriculo -= stop_words
    
    palavras_vaga = {p for p in palavras_vaga if len(p) > 2}
    palavras_curriculo = {p for p in palavras_curriculo if len(p) > 2}
    
    coincidencias = palavras_vaga.intersection(palavras_curriculo)
    if len(coincidencias) < 2:
        return False
    return True

def validar_se_e_curriculo(texto_bruto):
    if not client or not texto_bruto or not texto_bruto.strip():
        return True
    
    try:
        prompt_validacao = (
            "Analise o texto abaixo extraído de um arquivo enviado por um candidato. "
            "Determine se ele contém informações características de um currículo profissional "
            "(como histórico profissional, formação acadêmica, habilidades ou dados de contato voltados a emprego). "
            "Se for um boleto, fatura, recibo, manual técnico, comprovante de pagamento ou qualquer documento que NÃO seja um currículo, retorne false.\n\n"
            f"TEXTO EXTRAÍDO:\n{texto_bruto[:3000]}"
        )

        class ValidacaoCurriculo(BaseModel):
            is_curriculo: bool
            motivo: str

        resp_val = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt_validacao,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ValidacaoCurriculo,
                system_instruction="Você é um validador rigoroso de documentos de RH.",
                temperature=0.0
            )
        )
        
        texto_resp = resp_val.text.strip() if resp_val.text else "{}"
        dados_val = json.loads(texto_resp)
        return dados_val.get("is_curriculo", True)
    except Exception as val_err:
        print(f"[AVISO] Erro na validação de currículo: {val_err}")
        return True

def estruturar_curriculo_com_ia(texto_bruto):
    if not texto_bruto or not texto_bruto.strip():
        return {
            "nome": "Nome provisório", "idade": "Não Informado", "sexo": "Não Informado",
            "localizacao": "Manual necessário", "formacao": "Texto vazio.",
            "cursos": "Nenhum", "hard_skills": "Nenhuma", "soft_skills": "Nenhuma", "idiomas": "Não informado",
            "whatsapp": "", "areas_profissionais": ["Geral"]
        }
    
    # Validação inicial para garantir que o arquivo enviado é realmente um currículo
    if not validar_se_e_curriculo(texto_bruto):
        return {
            "nome": "Documento Inválido", "idade": "Não Informado", "sexo": "Não Informado",
            "localizacao": "Não Aplicável", "formacao": "O arquivo enviado não é um currículo válido (ex: boleto ou fatura).",
            "cursos": "Nenhum", "hard_skills": "Nenhuma", "soft_skills": "Nenhuma", "idiomas": "Não informado",
            "whatsapp": "", "areas_profissionais": ["Geral"],
            "documento_invalido": True
        }
    
    texto_limitado = otimizar_texto_ia(texto_bruto)
    if not client:
        return {
            "nome": "Sem Chave API", "idade": "Não Informado", "sexo": "Não Informado",
            "localizacao": "Configuração Pendente", "formacao": "A IA não pôde ser chamada.",
            "cursos": "Nenhum", "hard_skills": "Nenhuma", "soft_skills": "Nenhuma", "idiomas": "Não informado",
            "whatsapp": "", "areas_profissionais": ["Geral"]
        }
        
    system_prompt = (
        "Você é um specialist em recrutamento avançado, triagem de currículos e People Analytics.\n"
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
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT status, data_expiracao FROM empresas WHERE id = %s", (current_user.empresa_id,))
                empresa_status = cursor.fetchone()
                
                if empresa_status:
                    expirado = False
                    if empresa_status['data_expiracao']:
                        if datetime.now() > empresa_status['data_expiracao']:
                            expirado = True
                    
                    if empresa_status['status'] == 'bloqueado' or expirado:
                        logout_user()
                        return """
                        <div style="font-family: 'Inter', sans-serif; padding: 60px; text-align: center; background: #f8fafc; color: #334155; height: 100vh; display: flex; flex-direction: column; justify-content: center; align-items: center;">
                            <div style="background: white; padding: 40px; border-radius: 16px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); border: 1px solid #e2e8f0; max-width: 500px;">
                                <h2 style="color: #dc2626; margin-bottom: 12px; font-size: 24px;">Acesso Suspenso</h2>
                                <p style="color: #64748b; font-size: 15px; line-height: 1.6; margin-bottom: 24px;">O período de licenciamento da sua empresa no <strong>TalentPulse</strong> expirou ou o acesso foi temporariamente suspenso pelo administrador do sistema.</p>
                                <p style="font-size: 13px; color: #94a3b8;">Por favor, entre em contato com o suporte para renovar seu plano.</p>
                            </div>
                        </div>
                        """, 403
    except Exception as e:
        print(f"Erro na verificação de licença: {e}")

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
    plano_empresa = "starter"
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT nome_comercial, plano FROM empresas WHERE id = %s", (current_user.empresa_id,))
                empresa_data = cursor.fetchone()
                if empresa_data:
                    nome_empresa = empresa_data['nome_comercial']
                    plano_empresa = empresa_data['plano'] or "starter"

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

    return render_template('index.html', candidatos=resultados_finais, nome_empresa=nome_empresa, plano_empresa=plano_empresa)

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
            with get_db_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute("SELECT limite_mensal, plano FROM empresas WHERE id = %s", (current_user.empresa_id,))
                    dados_empresa = cursor.fetchone()
                    limite_mensal = dados_empresa['limite_mensal'] if dados_empresa else 300
                    plano_atual = dados_empresa['plano'].upper() if dados_empresa else 'STARTER'

                    primeiro_dia_mes = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    cursor.execute("""
                        SELECT COUNT(*) as total FROM curriculos 
                        WHERE empresa_id = %s AND data_cadastro >= %s
                    """, (current_user.empresa_id, primeiro_dia_mes))
                    contagem = cursor.fetchone()
                    total_enviado = contagem['total'] if contagem else 0

            if total_enviado >= limite_mensal:
                flash(f"⚠️ Limite atingido! Sua empresa já analisou {total_enviado}/{limite_mensal} currículos este mês no plano {plano_atual}. Realize um upgrade para continuar.", "error")
                return redirect(url_for('index'))

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
                    
            flash(f"Currículo de '{dados_ia['nome']}' processado e saved com sucesso! ({total_enviado + 1}/{limite_mensal} usados)", "success")
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
# PAINEL DE INDICADORES (DASHBOARD DE RH)
# ==============================================================================
@app.route('/dashboard', methods=['GET'])
@login_required
def dashboard_rh():
    return render_template('dashboard.html')

@app.route('/api/dashboard-stats', methods=['GET'])
@login_required
def dashboard_stats():
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # 1. Total de currículos recebidos no mês atual
                primeiro_dia_mes = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                cursor.execute("""
                    SELECT COUNT(*) as total_mes 
                    FROM curriculos 
                    WHERE empresa_id = %s AND data_cadastro >= %s
                """, (current_user.empresa_id, primeiro_dia_mes))
                res_mes = cursor.fetchone()
                total_mes = res_mes['total_mes'] if res_mes else 0

                # Total geral no banco da empresa
                cursor.execute("SELECT COUNT(*) as total_geral FROM curriculos WHERE empresa_id = %s", (current_user.empresa_id,))
                res_geral = cursor.fetchone()
                total_geral = res_geral['total_geral'] if res_geral else 0

                # 2. Distribuição por área profissional (extraindo do array areas_profissionais)
                cursor.execute("""
                    SELECT unnest(areas_profissionais) as area, COUNT(*) as qtd
                    FROM curriculos
                    WHERE empresa_id = %s AND areas_profissionais IS NOT NULL
                    GROUP BY area
                    ORDER BY qtd DESC
                """, (current_user.empresa_id,))
                dist_areas = cursor.fetchall()

                # 3. Média de idade
                cursor.execute("""
                    SELECT idade 
                    FROM curriculos 
                    WHERE empresa_id = %s AND idade IS NOT NULL AND idade != ''
                """, (current_user.empresa_id,))
                idades_raw = cursor.fetchall()
                
                soma_idades = 0
                contador_idades = 0
                for row in idades_raw:
                    # Tenta extrair apenas os números da string de idade (ex: "33 anos", "33")
                    match_nums = re.findall(r'\d+', row['idade'])
                    if match_nums:
                        idade_val = int(match_nums[0])
                        if 16 <= idade_val <= 100: # Filtro de sanidade para evitar anos de nascimento (ex: 1990)
                            soma_idades += idade_val
                            contador_idades += 1
                
                media_idade = round(soma_idades / contador_idades, 1) if contador_idades > 0 else 0

                # 4. Principais Hard Skills mais encontradas
                cursor.execute("""
                    SELECT hard_skills 
                    FROM curriculos 
                    WHERE empresa_id = %s AND hard_skills IS NOT NULL AND hard_skills != ''
                """, (current_user.empresa_id,))
                skills_rows = cursor.fetchall()

                skills_contador = {}
                for row in skills_rows:
                    texto_skills = row['hard_skills']
                    # Separa por vírgula ou ponto e vírgula
                    lista_s = re.split(r'[,;]', texto_skills)
                    for s in lista_s:
                        s_limpa = s.strip().capitalize()
                        if s_limpa and len(s_limpa) > 1:
                            skills_contador[s_limpa] = skills_contador.get(s_limpa, 0) + 1

                # Ordena e pega as top 6 hard skills
                top_skills = sorted(skills_contador.items(), key=lambda x: x[1], reverse=True)[:6]

                return jsonify({
                    "total_mes": total_mes,
                    "total_geral": total_geral,
                    "media_idade": media_idade,
                    "distribuicao_areas": [{"area": d['area'], "quantidade": d['qtd']} for d in dist_areas],
                    "top_hard_skills": [{"skill": item[0], "quantidade": item[1]} for item in top_skills]
                })
    except Exception as e:
        print(f"Erro ao buscar estatísticas do dashboard: {e}")
        return jsonify({"error": "Erro interno ao processar indicadores."}), 500

# ==============================================================================
# ANÁLISE CORPORATIVA COM SISTEMA DE CACHE NO BANCO DE DADOS
# ==============================================================================
@app.route('/candidato/<int:id_candidato>/analise-retro', methods=['GET'])
@login_required
def analise_retro_candidato(id_candidato):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT dados_json FROM analises_ia 
                    WHERE curriculo_id = %s
                """, (id_candidato,))
                cached_data = cursor.fetchone()

                if cached_data:
                    cursor.execute("SELECT id, titulo, descricao FROM vagas WHERE empresa_id = %s", (current_user.empresa_id,))
                    vagas_disponiveis = cursor.fetchall()

                    analise_retro = cached_data['dados_json']
                    vaga_recomendada_id = None
                    vaga_recomendada_titulo = None

                    if vagas_disponiveis and "tipos_de_vagas_recomendadas" in analise_retro:
                        recomendacoes = [remover_acentos(v) for v in analise_retro["tipos_de_vagas_recomendadas"]]
                        for vaga in vagas_disponiveis:
                            titulo_vaga_limpo = remover_acentos(vaga['titulo'])
                            desc_vaga_limpo = remover_acentos(vaga['descricao'])
                            for rec in recomendacoes:
                                if rec in titulo_vaga_limpo or rec in desc_vaga_limpo:
                                    vaga_recomendada_id = vaga['id']
                                    vaga_recomendada_titulo = vaga['titulo']
                                    break
                            if vaga_recomendada_id:
                                break

                    analise_retro['vaga_compativel_banco'] = {
                        "id": vaga_recomendada_id,
                        "titulo": vaga_recomendada_titulo
                    } if vaga_recomendada_id else None

                    return jsonify(analise_retro)

        if not client:
            return jsonify({"error": "Gemini API Key não está configurada."}), 500

        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT id, nome_candidato AS nome, conteudo, hard_skills, soft_skills, formacao, cursos 
                    FROM curriculos WHERE id = %s AND empresa_id = %s
                """, (id_candidato, current_user.empresa_id))
                candidato = cursor.fetchone()

                if not candidato:
                    return jsonify({"error": "Candidato não encontrado."}), 404

                cursor.execute("SELECT id, titulo, descricao FROM vagas WHERE empresa_id = %s", (current_user.empresa_id,))
                vagas_disponiveis = cursor.fetchall()

        system_instruction = (
            "Você é um especialista sênior em People Analytics e Recrutamento de Alta Performance.\n"
            "Sua tarefa é analisar o perfil profissional do candidato e preencher o schema JSON com dados realistas e corporativos.\n\n"
            "REGRAS DE ADAPTAÇÃO DE CAMPOS:\n"
            "- 'titulo_classe': Deve ser o título/cargo ideal do candidato no mercado real (ex: 'Analista de Sistemas', 'Gerente de Vendas'). Nunca use termos de fantasia ou RPG.\n"
            "- 'level': Nível de senioridade (número de 1 a 99).\n"
            "- 'vida_hp': Porcentagem estimada de 'Fit Técnico / Hard Skills' (0 a 100).\n"
            "- 'mana_mp': Porcentagem estimada de 'Fit Cultural / Soft Skills' (0 a 100).\n"
            "- 'habilidades_especiais': Mapeie competências técnicas reais de alto impacto profissional.\n"
            "- 'resumo_narrativa': Escreva um parecer profissional conciso e estratégico sobre o candidato.\n"
            "NUNCA use termos lúdicos de RPG, jogos ou fantasia."
        )

        conteudo_otimizado = otimizar_texto_ia(candidato['conteudo'])
        prompt_conteudo = f"Candidato: {candidato['nome']}\nPerfil Técnico: {candidato['hard_skills']}\nComportamental: {candidato['soft_skills']}\nHistórico: {conteudo_otimizado}"

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt_conteudo,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ParecerRetroJogo,
                system_instruction=system_instruction,
                temperature=0.3
            )
        )

        analise_retro = json.loads(response.text.strip()) if response.text else {}

        if not analise_retro.get("habilidades_especiais") and analise_retro.get("habilities_especiais"):
            analise_retro["habilidades_especiais"] = analise_retro["habilities_especiais"]

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO analises_ia (curriculo_id, dados_json) 
                    VALUES (%s, %s)
                    ON CONFLICT (curriculo_id) DO UPDATE SET dados_json = EXCLUDED.dados_json
                """, (id_candidato, json.dumps(analise_retro)))
                conn.commit()

        vaga_recomendada_id = None
        vaga_recomendada_titulo = None

        if vagas_disponiveis and "tipos_de_vagas_recomendadas" in analise_retro:
            recomendacoes = [remover_acentos(v) for v in analise_retro["tipos_de_vagas_recomendadas"]]
            for vaga in vagas_disponiveis:
                titulo_vaga_limpo = remover_acentos(vaga['titulo'])
                desc_vaga_limpo = remover_acentos(vaga['descricao'])
                for rec in recomendacoes:
                    if rec in titulo_vaga_limpo or rec in desc_vaga_limpo:
                        vaga_recomendada_id = vaga['id']
                        vaga_recomendada_titulo = vaga['titulo']
                        break
                if vaga_recomendada_id:
                    break

        analise_retro['vaga_compativel_banco'] = {
            "id": vaga_recomendada_id,
            "titulo": vaga_recomendada_titulo
        } if vaga_recomendada_id else None

        return jsonify(analise_retro)

    except Exception as e:
        print(f"Erro na geração de análise de perfil: {e}")
        return jsonify({"error": "Não foi possível gerar a análise no momento."}), 500

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
            flash("Vaga actualizada com sucesso!", "success")
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
# MATCH INTELIGENTE OTIMIZADO COM LÓGICA DE FALLBACK ATUALIZADA
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
                    SELECT curriculo_id FROM historico_analises_vagas 
                    WHERE vaga_id = %s
                """, (id_vaga,))
                analisados_ids = [r['curriculo_id'] for r in cursor.fetchall()]

                if analisados_ids:
                    cursor.execute("""
                        SELECT id, nome_candidato AS nome, formacao, hard_skills, soft_skills, idiomas, conteudo 
                        FROM curriculos 
                        WHERE empresa_id = %s AND id NOT IN %s
                    """, (current_user.empresa_id, tuple(analisados_ids)))
                else:
                    cursor.execute("""
                        SELECT id, nome_candidato AS nome, formacao, hard_skills, soft_skills, idiomas, conteudo 
                        FROM curriculos 
                        WHERE empresa_id = %s
                    """, (current_user.empresa_id,))
                
                todos_candidatos_vaga = cursor.fetchall()

        candidatos_para_analise = []
        candidatos_rejeitados_localmente = []

        for c in todos_candidatos_vaga:
            texto_curriculo_completo = f"{c['conteudo']} {c['hard_skills'] or ''} {c['soft_skills'] or ''} {c['formacao'] or ''}"
            if pre_filtro_compatibilidade(vaga['requisitos'], vaga['descricao'], vaga['titulo'], texto_curriculo_completo):
                candidatos_para_analise.append(c)
            else:
                candidatos_rejeitados_localmente.append(c)

        if candidatos_rejeitados_localmente:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    for rej in candidatos_rejeitados_localmente:
                        cursor.execute("""
                            INSERT INTO historico_analises_vagas (vaga_id, curriculo_id, porcentagem_compatibilidade, justificativa)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (vaga_id, curriculo_id) DO NOTHING
                        """, (id_vaga, rej['id'], 10, 'Baixa aderência inicial identificada com base em competências e requisitos técnicos mínimos.'))
                    conn.commit()

        novos_matches_exibir = []
        mensagem_aviso = None

        if candidatos_para_analise:
            dados_candidatos_prompt = []
            for c in candidatos_para_analise:
                dados_candidatos_prompt.append({
                    "id_candidato": c['id'],
                    "nome": c['nome'] or "Sem Nome",
                    "perfil_resumido": f"Skills Técnicas: {otimizar_texto_ia(c['hard_skills'])}. Comportamental: {otimizar_texto_ia(c['soft_skills'])}. Idiomas: {c['idiomas']}. Formação: {otimizar_texto_ia(c['formacao'])}"
                })

            system_instruction = (
                "Você é um Headhunter sênior focado em People Analytics.\n"
                "Sua tarefa é analisar uma vaga de emprego específica e gerar um ranking comparativo estruturado contendo a porcentagem de "
                "compatibilidade (de 0 a 100) e uma breve justificativa de aderência para cada candidato fornecido.\n"
                "Você DEVE respeitar o schema estruturado e retornar UNICAMENTE o JSON no formato exigido, sem trechos adicionais de texto."
            )

            prompt_conteudo = (
                f"VAGA ALVO:\n"
                f"Título: {vaga['titulo']}\n"
                f"Descrição: {vaga['descricao']}\n"
                f"Requisitos: {vaga['requisitos']}\n\n"
                f"LISTA DE CANDIDATOS:\n"
                f"{json.dumps(dados_candidatos_prompt, ensure_ascii=False)}"
            )

            try:
                time.sleep(3)

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
                
                texto_resposta = response.text.strip() if response.text else ""
                
                if texto_resposta.startswith("```json"):
                    texto_resposta = re.sub(r"^```json\s*", "", texto_resposta)
                    texto_resposta = re.sub(r"\s*```$", "", texto_resposta)
                elif texto_resposta.startswith("```"):
                    texto_resposta = re.sub(r"^```\s*", "", texto_resposta)
                    texto_resposta = re.sub(r"\s*```$", "", texto_resposta)

                analise_json = json.loads(texto_resposta) if texto_resposta else {}
                candidatos_analisados_ia = analise_json.get("candidatos_compativeis", [])

                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        for cand in candidatos_analisados_ia:
                            c_id = int(cand.get('id_candidato'))
                            c_compatibilidade = int(cand.get('porcentagem_compatibilidade', 0))
                            c_justificativa = str(cand.get('justificativa', 'Aderência média ao perfil cadastrado.'))
                            
                            cursor.execute("""
                                INSERT INTO historico_analises_vagas (vaga_id, curriculo_id, porcentagem_compatibilidade, justificativa)
                                VALUES (%s, %s, %s, %s)
                                ON CONFLICT (vaga_id, curriculo_id) DO NOTHING
                            """, (id_vaga, c_id, c_compatibilidade, c_justificativa))
                        conn.commit()

                # 1. Tenta primeiramente filtrar os candidatos ideais (>= 70%)
                novos_matches_exibir = [
                    cand for cand in candidatos_analisados_ia 
                    if int(cand.get('porcentagem_compatibilidade', 0)) >= 70
                ]

                # 2. Fallback: Se não houver nenhum >= 70%, pega os candidatos regulares (>= 50%)
                if not novos_matches_exibir:
                    novos_matches_exibir = [
                        cand for cand in candidatos_analisados_ia 
                        if int(cand.get('porcentagem_compatibilidade', 0)) >= 50
                    ]

                # 3. Se ainda assim estiver vazio, define a mensagem de aviso customizada
                if not novos_matches_exibir:
                    mensagem_aviso = "Nenhum candidato avaliado cumpriu o mínimo exigido de competências ou requisitos para esta vaga."

            except json.JSONDecodeError as jde:
                print(f"[ERRO CRÍTICO] Falha ao decodificar JSON retornado pelo Gemini: {jde}")
                flash("Erro ao processar estrutura de análise inteligente. Tente novamente.", "error")
            except Exception as inner_e:
                print(f"[ERRO CRÍTICO] Falha ao se comunicar ou salvar dados do Gemini: {inner_e}")
                flash("Instabilidade na comunicação com a IA. Os candidatos não puderam ser triados.", "error")
        else:
            # Caso nenhum candidato cadastrado tenha passado do pré-filtro local estático
            mensagem_aviso = "Nenhum candidato avaliado cumpriu o mínimo exigido de competências ou requisitos para esta vaga."

        return render_template(
            'analise.html', 
            vaga=vaga, 
            resultado={"vaga_id": id_vaga, "candidatos_compativeis": novos_matches_exibir},
            mensagem_aviso=mensagem_aviso
        )
        
    except Exception as e:
        print(f"Erro geral na análise de vagas: {e}")
        flash("Ocorreu um erro interno ao processar a inteligência artificial.", "error")
        return redirect(url_for('listar_vagas'))

# ==============================================================================
# HISTÓRICO DE CANDIDATOS ANALISADOS POR VAGA (MATCH >= 70%)
# ==============================================================================
@app.route('/vagas/<int:id_vaga>/historico', methods=['GET'])
@login_required
def historico_vaga(id_vaga):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT * FROM vagas WHERE id = %s AND empresa_id = %s", (id_vaga, current_user.empresa_id))
                vaga = cursor.fetchone()

                if not vaga:
                    return jsonify({"error": "Vaga não encontrada ou acesso negado"}), 404

                cursor.execute("""
                    SELECT h.porcentagem_compatibilidade, h.justificativa, h.data_analise, 
                           c.id AS id_candidato, c.nome_candidato AS nome, c.localizacao
                    FROM historico_analises_vagas h
                    JOIN curriculos c ON h.curriculo_id = c.id
                    WHERE h.vaga_id = %s AND h.porcentagem_compatibilidade >= 70
                    ORDER BY h.porcentagem_compatibilidade DESC, h.data_analise DESC
                """, (id_vaga,))
                historico = cursor.fetchall()

                for item in historico:
                    if item['data_analise']:
                        item['data_analise'] = item['data_analise'].strftime('%d/%m/%Y %H:%M')

                return jsonify({"vaga": vaga, "historico": historico})
    except Exception as e:
        print(f"Erro ao buscar histórico de vagas: {e}")
        return jsonify({"error": "Erro interno ao buscar histórico de candidatos."}), 500

# ==============================================================================
# CHAT INTERATIVO COM IA (CORRIGIDO E ROBUSTO)
# ==============================================================================
import re # Certifique-se de ter essa importação no topo do seu arquivo

@app.route('/chat-vanessa', methods=['POST'])
@login_required
def chat_vanessa():
    data = request.json
    mensagem_usuario = data.get('mensagem', '').strip()
    
    # 1. Tenta buscar os dados no banco
    candidatos_texto = ""
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT nome_candidato, hard_skills, experiencia_anos, resumo 
                    FROM curriculos 
                    WHERE empresa_id = %s 
                    AND (hard_skills ILIKE '%vendas%' OR hard_skills ILIKE '%informatica%')
                    ORDER BY experiencia_anos DESC
                    LIMIT 3
                """, (current_user.empresa_id,))
                candidatos = cursor.fetchall()
                
                if candidatos:
                    # Formata os dados para o Gemini
                    candidatos_texto = "Aqui estão os candidatos encontrados:\n"
                    for c in candidatos:
                        candidatos_texto += f"- Nome: {c['nome_candidato']}, Experiência: {c['experiencia_anos']} anos, Skills: {c['hard_skills']}. Resumo: {c['resumo']}\n"
    except Exception as e:
        print(f"Erro no banco: {e}")

    # 2. Envia os dados para o Gemini com uma System Instruction clara
    try:
        system_prompt = f"""
        Você é a Vanessa, assistente da TalentPulse. 
        Seu objetivo é analisar os dados abaixo e responder ao usuário de forma natural e profissional.
        
        Dados de candidatos encontrados no sistema:
        {candidatos_texto if candidatos_texto else "Nenhum candidato encontrado com os critérios."}
        
        Se houver candidatos, apresente-os de forma persuasiva. Se não, explique que não achou e sugira ajustar os filtros.
        """
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=mensagem_usuario,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt
            )
        )
        return jsonify({"resposta": response.text})
    except Exception as e:
        return jsonify({"resposta": "Desculpe, estou com instabilidade técnica no momento."})
    # --- FLUXO PADRÃO (SE NÃO FOR BUSCA ESPECÍFICA) ---
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=mensagem_usuario,
            config=types.GenerateContentConfig(
                system_instruction="Você é Vanessa. Se a pergunta for sobre triagem, tente responder com base no contexto ou oriente sobre como filtrar."
            )
        )
        return jsonify({"resposta": response.text})
    except Exception as e:
        return jsonify({"resposta": "Desculpe, estou com instabilidade técnica."})
@app.route('/chat', methods=['GET'])
@login_required
def renderizar_chat():
    return render_template('chat.html')

@app.route('/chat/historico', methods=['GET'])
@login_required
def historico_chat():
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT remetente, mensagem, TO_CHAR(data_envio, 'DD/MM HH24:MI') as data_envio 
                    FROM mensagens_chat 
                    WHERE empresa_id = %s 
                    ORDER BY data_envio ASC
                """, (current_user.empresa_id,))
                return jsonify(cursor.fetchall())
    except Exception as e:
        print(f"Erro ao buscar histórico do chat: {e}")
        return jsonify({"error": "Erro ao carregar mensagens."}), 500

@app.route('/chat/enviar', methods=['POST'])
@login_required
def enviar_mensagem_chat():
    if not client:
        return jsonify({"error": "Integração com IA não configurada."}), 500
        
    dados = request.get_json() or {}
    mensagem_usuario = dados.get('mensagem', '').strip()
    
    if not mensagem_usuario:
        return jsonify({"error": "A mensagem não pode estar vazia."}), 400
        
    try:
        # 1. Salva a mensagem do usuário
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO mensagens_chat (empresa_id, usuario_id, remetente, mensagem)
                    VALUES (%s, %s, 'usuario', %s)
                """, (current_user.empresa_id, current_user.id, mensagem_usuario))
                
                # 2. Resgata histórico para contexto
                cursor.execute("""
                    SELECT remetente, mensagem FROM (
                        SELECT remetente, mensagem, data_envio 
                        FROM mensagens_chat 
                        WHERE empresa_id = %s 
                        ORDER BY data_envio DESC 
                        LIMIT 15
                    ) AS subquery_chat ORDER BY data_envio ASC
                """, (current_user.empresa_id,))
                mensagens_anteriores = cursor.fetchall()
                conn.commit()

        # 3. Formata histórico para Gemini
        historico_gemini = []
        for msg in mensagens_anteriores:
            role = "user" if msg['remetente'] == 'usuario' else "model"
            historico_gemini.append(types.Content(role=role, parts=[types.Part.from_text(text=msg['mensagem'])]))

        system_instruction = (
            f"Você é a Vanessa, assistente virtual do TalentPulse. "
            f"Usuário: {current_user.nome} (Empresa ID: {current_user.empresa_id}). "
            "Ajude com análise de currículos, vagas e recrutamento. Responda apenas com texto puro, sem blocos de código."
        )

        # 4. Processamento IA
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=historico_gemini,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.7
            )
        )
        
        # Limpeza robusta da resposta da IA
        resposta_bruta = response.text if response.text else "Não consegui processar uma resposta."
        # Remove blocos Markdown (```json, ```text, ```, etc)
        resposta_limpa = re.sub(r'```[a-zA-Z]*', '', resposta_bruta).replace('```', '').strip()

        # 5. Salva resposta da IA
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO mensagens_chat (empresa_id, usuario_id, remetente, mensagem)
                    VALUES (%s, NULL, 'ia', %s)
                """, (current_user.empresa_id, resposta_limpa))
                conn.commit()

        return jsonify({"resposta": resposta_limpa})

    except Exception as e:
        print(f"[ERRO NO CHAT]: {e}")
        return jsonify({"error": "Erro interno ao processar a resposta da IA."}), 500

        import uuid

# ==============================================================================
# ROTA PÚBLICA DE INSCRIÇÃO NA VAGA (COMPARTILHAMENTO)
# ==============================================================================
@app.route('/vaga/candidatar/<token>', methods=['GET', 'POST'])
def pagina_candidatura_publica(token):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT * FROM vagas WHERE token_compartilhamento = %s", (token,))
                vaga = cursor.fetchone()
                
                if not vaga:
                    # Se a vaga antiga não tiver token, gera um retroativamente
                    cursor.execute("SELECT * FROM vagas WHERE id::text = %s", (token,))
                    vaga = cursor.fetchone()
                    if not vaga:
                        return render_template('erro_vaga.html', mensagem="Vaga não encontrada ou link expirado."), 404

        if request.method == 'POST':
            # Identificar se foi preenchimento manual ou upload de arquivo
            tipo_envio = request.form.get('tipo_envio', 'upload')
            
            nome = request.form.get('nome', '').strip()
            email = request.form.get('email', '').strip()
            whatsapp = request.form.get('whatsapp', '').strip()
            localizacao = request.form.get('localizacao', '').strip()
            
            texto_bruto = ""
            nome_arquivo = "candidatura_manual.txt"
            arquivo_b64 = ""
            
            if tipo_envio == 'upload':
                arquivo = request.files.get('file')
                if arquivo and arquivo.filename != '':
                    nome_original = arquivo.filename
                    extensao = nome_original.rsplit('.', 1)[1].lower() if '.' in nome_original else ''
                    if extensao in ['pdf', 'docx']:
                        dados_bytes = arquivo.read()
                        arquivo_b64 = base64.b64encode(dados_bytes).decode('utf-8')
                        nome_arquivo = nome_original
                        if extensao == 'pdf':
                            texto_bruto = extrair_texto_pdf(dados_bytes)
                        else:
                            texto_bruto = extrair_texto_docx(dados_bytes)
            else:
                # Monta texto estruturado com os dados do formulário manual
                formacao = request.form.get('formacao', '')
                experiencia = request.form.get('experiencia', '')
                habilidades = request.form.get('habilidades', '')
                texto_bruto = f"Nome: {nome}\nE-mail: {email}\nWhatsApp: {whatsapp}\nLocalização: {localizacao}\nFormação: {formacao}\nExperiência: {experiencia}\nHabilidades: {habilidades}"

            if not texto_bruto.strip():
                flash("Por favor, preencha o formulário ou envie um currículo válido.", "error")
                return redirect(request.url)

            # Processa com a IA do TalentPulse
            dados_ia = estruturar_curriculo_com_ia(texto_bruto)
            if nome: 
                dados_ia['nome'] = nome  # Prioriza o nome preenchido manualmente se houver
            if whatsapp:
                dados_ia['whatsapp'] = whatsapp
            if localizacao:
                dados_ia['localizacao'] = localizacao

            # Salva no banco de dados vinculado à empresa dona da vaga
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO curriculos (
                            empresa_id, nome_arquivo, conteudo, nome_candidato, idade, sexo, 
                            localizacao, formacao, cursos, habilidades, hard_skills, soft_skills, idiomas, arquivo_binario, whatsapp, areas_profissionais
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (
                        vaga['empresa_id'],
                        nome_arquivo, 
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
                    novo_curriculo_id = cursor.fetchone()[0]

                    # Cálculo dinâmico de match por IA para a vaga pública
                    compatibilidade = 50  # Valor padrão de segurança caso a IA falhe
                    justificativa = "Candidato submetido via link público. Análise de compatibilidade realizada pelo sistema."

                    if client:
                        try:
                            prompt_match = (
                                f"VAGA ALVO:\n"
                                f"Título: {vaga.get('titulo', '')}\n"
                                f"Descrição: {vaga.get('descricao', '')}\n"
                                f"Requisitos: {vaga.get('requisitos', '')}\n\n"
                                f"CANDIDATO:\n"
                                f"Nome: {dados_ia['nome']}\n"
                                f"Hard Skills: {dados_ia['hard_skills']}\n"
                                f"Soft Skills: {dados_ia['soft_skills']}\n"
                                f"Formação: {dados_ia['formacao']}\n"
                                f"Conteúdo Geral: {otimizar_texto_ia(texto_bruto)}"
                            )

                            class MatchUnico(BaseModel):
                                porcentagem_compatibilidade: int
                                justificativa: str

                            response = client.models.generate_content(
                                model='gemini-2.5-flash',
                                contents=prompt_match,
                                config=types.GenerateContentConfig(
                                    response_mime_type="application/json",
                                    response_schema=MatchUnico,
                                    system_instruction="Você é um Headhunter sênior. Avalie o fit deste candidato para a vaga e retorne a porcentagem (0 a 100) e uma justificativa clara.",
                                    temperature=0.2
                                )
                            )
                            
                            texto_resp = response.text.strip() if response.text else "{}"
                            dados_match = json.loads(texto_resp)
                            compatibilidade = int(dados_match.get("porcentagem_compatibilidade", 50))
                            justificativa = str(dados_match.get("justificativa", justificativa))
                        except Exception as ai_err:
                            print(f"[AVISO] Erro ao calcular match dinâmico na vaga pública: {ai_err}")

                    cursor.execute("""
                        INSERT INTO historico_analises_vagas (vaga_id, curriculo_id, porcentagem_compatibilidade, justificativa)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (vaga_id, curriculo_id) DO UPDATE 
                        SET porcentagem_compatibilidade = EXCLUDED.porcentagem_compatibilidade,
                            justificativa = EXCLUDED.justificativa
                    """, (vaga['id'], novo_curriculo_id, compatibilidade, justificativa))
                    
                    conn.commit()

            return render_template('sucesso_candidatura.html', vaga=vaga)

        return render_template('candidatar_vaga.html', vaga=vaga)
    except Exception as e:
        print(f"Erro na candidatura pública: {e}")
        return render_template('erro_vaga.html', mensagem="Ocorreu um erro ao processar sua candidatura."), 500
# ==============================================================================
# CONTROLE MASTER ADMINISTRATIVO 
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
                    SELECT e.id, e.nome_comercial, e.data_cadastro, e.status, e.data_expiracao, e.plano, e.limite_mensal,
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
            <link href="https://fonts.googleapis.com/css2?family=Inter:wght=400;500;600;700&display=swap" rel="stylesheet">
            <style>
                body {{ font-family: 'Inter', sans-serif; padding: 40px; background: #f8fafc; color: #334155; margin: 0; }}
                .container {{ max-width: 1400px; margin: 0 auto; }}
                h2 {{ color: #0f172a; font-weight: 700; margin-bottom: 6px; }}
                p.subtitle {{ color: #64748b; font-size: 14px; margin-bottom: 24px; }}
                table {{ width: 100%; border-collapse: collapse; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.02); border-radius: 12px; overflow: hidden; border: 1px solid #e2e8f0; }}
                th, td {{ padding: 14px 14px; text-align: left; font-size: 13px; border-bottom: 1px solid #e2e8f0; }}
                th {{ background: #0f172a; color: white; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; font-size: 11px; }}
                tr:last-child td {{ border-bottom: none; }}
                tr:nth-child(even) {{ background: #f8fafc; }}
                .badge {{ padding: 4px 10px; border-radius: 99px; font-size: 11px; font-weight: 600; display: inline-flex; align-items: center; text-transform: uppercase; }}
                .badge-ativo {{ background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }}
                .badge-bloqueado {{ background: #fef2f2; color: #b91c1c; border: 1px solid #fca5a5; }}
                .badge-expirado {{ background: #fff7ed; color: #c2410c; border: 1px solid #fed7aa; }}
                .form-inline {{ display: flex; gap: 6px; align-items: center; margin: 0; }}
                .input-days {{ width: 70px; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 12px; font-weight: 500; text-align: center; }}
                .select-plan {{ padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 12px; font-weight: 500; }}
                .btn-save {{ background: #4f46e5; color: white; border: none; padding: 7px 10px; cursor: pointer; border-radius: 6px; font-size: 12px; font-weight: 600; transition: background 0.15s; }}
                .btn-save:hover {{ background: #4338ca; }}
                .btn-toggle {{ background: #ffffff; color: #475569; border: 1px solid #cbd5e1; padding: 7px 10px; cursor: pointer; border-radius: 6px; font-size: 12px; font-weight: 600; transition: all 0.15s; }}
                .btn-toggle:hover {{ background: #f1f5f9; color: #1e293b; border-color: #94a3b8; }}
                .btn-delete {{ background: #fef2f2; color: #b91c1c; border: 1px solid #fca5a5; padding: 7px 10px; cursor: pointer; border-radius: 6px; font-size: 12px; font-weight: 600; transition: all 0.15s; }}
                .btn-delete:hover {{ background: #fee2e2; border-color: #f87171; }}
                .actions-cell {{ display: flex; gap: 6px; align-items: center; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h2>Painel de Controle de Clientes (Tenants)</h2>
                <p class="subtitle">Gerencie os contratos ativos, prazos de aluguel, tipos de planos e limites da plataforma TalentPulse.</p>
                <table>
                    <tr>
                        <th>ID</th><th>Nome da Empresa</th><th>Cadastro</th><th>Status</th><th>Plano Atual</th><th>Limite</th><th>Expira Em</th><th>Aluguel (Dias)</th><th>Alterar Plano/Limite</th><th>Métricas</th><th>Ações</th>
                    </tr>
        """
        for emp in empresas:
            data_exp_formatada = "Sem limite"
            status_badge = f'<span class="badge badge-ativo">Ativo</span>'
            
            if emp['status'] == 'bloqueado':
                status_badge = f'<span class="badge badge-bloqueado">Bloqueado</span>'
                
            if emp['data_expiracao']:
                data_exp_formatada = emp['data_expiracao'].strftime('%d/%m/%Y %H:%M')
                if datetime.now() > emp['data_expiracao']:
                    status_badge = f'<span class="badge badge-expirado">Expirado</span>'
            
            plano_str = str(emp['plano']).upper() if emp['plano'] else 'STARTER'
            limite_num = emp['limite_mensal'] or 300
            
            html_admin += f"""
                <tr>
                    <td>{emp['id']}</td>
                    <td><strong>{emp['nome_comercial']}</strong></td>
                    <td>{emp['data_cadastro'].strftime('%d/%m/%Y')}</td>
                    <td>{status_badge}</td>
                    <td><span style="font-weight:600; color:#4f46e5;">{plano_str}</span></td>
                    <td><strong>{limite_num}</strong> /mês</td>
                    <td style="font-weight: 500; font-size: 12px; color: #475569;">{data_exp_formatada}</td>
                    <td>
                        <form class="form-inline" action="/master-admin/empresas/{emp['id']}/atualizar-prazo?token={token}" method="POST">
                            <input class="input-days" type="number" name="dias" placeholder="+ Dias" required min="1">
                            <button class="btn-save" type="submit">Adicionar</button>
                        </form>
                    </td>
                    <td>
                        <form class="form-inline" action="/master-admin/empresas/{emp['id']}/atualizar-plano?token={token}" method="POST">
                            <select class="select-plan" name="plano">
                                <option value="starter" {"selected" if plano_str == "STARTER" else ""}>Starter</option>
                                <option value="pro" {"selected" if plano_str == "PRO" else ""}>Pro</option>
                                <option value="premium" {"selected" if plano_str == "PREMIUM" else ""}>Premium</option>
                            </select>
                            <input class="input-days" type="number" name="limite" value="{limite_num}" required min="0" title="Limite de currículos por mês">
                            <button class="btn-save" type="submit" style="background:#059669;">Salvar</button>
                        </form>
                    </td>
                    <td style="color: #64748b; font-size:12px;">
                        U: {emp['qtd_usuarios']} | C: {emp['qtd_curriculos']} | V: {emp['qtd_vagas']}
                    </td>
                    <td>
                        <div class="actions-cell">
                            <form action="/master-admin/empresas/{emp['id']}/toggle-status?token={token}" method="POST" style="margin:0;">
                                <button class="btn-toggle" type="submit">{"Bloquear" if emp['status'] == 'ativo' else "Ativar"}</button>
                            </form>
                            <form action="/master-admin/empresas/{emp['id']}/excluir?token={token}" method="POST" style="margin:0;" onsubmit="return confirm('ATENÇÃO CRÍTICA: Deletar esta empresa apagará TODOS os dados permanentemente. Confirmar?');">
                                <button class="btn-delete" type="submit">Deletar</button>
                            </form>
                        </div>
                    </td>
                </tr>
            """
        html_admin += "</table></div></body></html>"
        return Response(html_admin, mimetype='text/html')
    except Exception as e:
        return f"Erro ao carregar o painel administrativo: {e}", 500

@app.route('/master-admin/empresas/<int:id_empresa>/atualizar-prazo', methods=['POST'])
def admin_atualizar_prazo(id_empresa):
    token = request.args.get('token')
    if token != ADMIN_TOKEN:
        return "Acesso não autorizado", 403
        
    dias = int(request.form.get('dias', 0))
    if dias <= 0:
        return "Quantidade de dias inválida", 400
        
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT data_expiracao FROM empresas WHERE id = %s", (id_empresa,))
                emp = cursor.fetchone()
                
                base_calculo = datetime.now()
                if emp and emp['data_expiracao'] and emp['data_expiracao'] > datetime.now():
                    base_calculo = emp['data_expiracao']
                    
                nova_data_limite = base_calculo + timedelta(days=dias)
                
                cursor.execute("""
                    UPDATE empresas 
                    SET data_expiracao = %s, status = 'ativo' 
                    WHERE id = %s
                """, (nova_data_limite, id_empresa))
                conn.commit()
                
        return redirect(f"/master-admin/empresas?token={token}")
    except Exception as e:
        return f"Erro ao estender prazo da licença: {e}", 500

@app.route('/master-admin/empresas/<int:id_empresa>/atualizar-plano', methods=['POST'])
def admin_atualizar_plano(id_empresa):
    token = request.args.get('token')
    if token != ADMIN_TOKEN:
        return "Acesso não autorizado", 403
        
    plano = request.form.get('plano', 'starter').strip().lower()
    limite = int(request.form.get('limite', 300))
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE empresas 
                    SET plano = %s, limite_mensal = %s 
                    WHERE id = %s
                """, (plano, limite, id_empresa))
                conn.commit()
                
        return redirect(f"/master-admin/empresas?token={token}")
    except Exception as e:
        return f"Erro ao atualizar o plano da empresa: {e}", 500

@app.route('/master-admin/empresas/<int:id_empresa>/toggle-status', methods=['POST'])
def admin_toggle_status(id_empresa):
    token = request.args.get('token')
    if token != ADMIN_TOKEN:
        return "Acesso não autorizado", 403
        
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT status FROM empresas WHERE id = %s", (id_empresa,))
                emp = cursor.fetchone()
                
                if emp:
                    novo_status = 'bloqueado' if emp['status'] == 'ativo' else 'ativo'
                    cursor.execute("UPDATE empresas SET status = %s WHERE id = %s", (novo_status, id_empresa))
                    conn.commit()
                    
        return redirect(f"/master-admin/empresas?token={token}")
    except Exception as e:
        return f"Erro ao alterar status da licença: {e}", 500

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

if __name__ == '__main__':
    app.run(debug=True)
