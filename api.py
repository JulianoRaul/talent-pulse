import os
import sys
import unicodedata
from flask import Flask, render_template, request, redirect, url_for, flash, Response, jsonify
from flask_sqlalchemy import SQLAlchemy
from google import genai
from google.genai import types
from pydantic import BaseModel
import pypdf
import docx2txt
import psycopg2
from psycopg2.extras import RealDictCursor
import io
import base64

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "chave_secreta_talent_pulse_a1")

# ==========================================
# 🗄️ CONFIGURAÇÃO DO BANCO DE DADOS (POSTGRESQL)
# ==========================================
DATABASE_URL = os.environ.get('DATABASE_URL')

# Correção essencial para a nuvem da Render e SQLAlchemy/Psycopg2:
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Garante que o SSL seja exigido caso esteja conectando ao banco de dados em produção
def get_db_connection():
    # Se houver uma string de conexão da Render, adiciona o parâmetro de SSL que o PostgreSQL em nuvem exige
    if DATABASE_URL:
        # Se já não tiver a especificação de sslmode na string, adicionamos para o psycopg2
        if "sslmode=" not in DATABASE_URL:
            separator = "&" if "?" in DATABASE_URL else "?"
            url_conexao = f"{DATABASE_URL}{separator}sslmode=require"
        else:
            url_conexao = DATABASE_URL
        return psycopg2.connect(url_conexao)
    else:
        # Fallback local caso queira testar na máquina
        return psycopg2.connect("dbname=talent_pulse user=postgres password=postgres host=localhost")

# ==========================================
# 🧬 MODELOS E INICIALIZAÇÃO DO BANCO
# ==========================================
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
                conn.commit()
        print("-> Banco de dados PostgreSQL pronto!")
    except Exception as e:
        print(f"Erro ao inicializar o banco de dados: {e}")

# Chamar a inicialização para rodar assim que o servidor ligar
init_db()

# Configuração paralela para o Flask-SQLAlchemy (caso utilize em outras partes)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Candidato(db.Model):
    __tablename__ = 'curriculos'
    id = db.Column(db.Integer, primary_key=True)
    nome_arquivo = db.Column(db.String(250), nullable=False)
    conteudo = db.Column(db.Text, nullable=False)
    nome_candidato = db.Column(db.String(150), nullable=True)
    idade = db.Column(db.String(50), nullable=True)
    sexo = db.Column(db.String(50), nullable=True)
    localizacao = db.Column(db.String(250), nullable=True)
    formacao = db.Column(db.Text, nullable=True)
    cursos = db.Column(db.Text, nullable=True)
    habilidades = db.Column(db.Text, nullable=True)
    arquivo_binario = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "nome": self.nome_candidato or "Documento Digitalizado (Imagem/Scan)",
            "idade": self.idade or "Não informado",
            "sexo": self.sexo or "Não informado",
            "localizacao": self.localizacao or "OCE Necessário",
            "formacao": self.formacao or "Este currículo foi enviado como imagem ou scanner...",
            "cursos": self.cursos or "Apenas arquivos PDF ou Word são permitidos!",
            "habilidades": self.habilidades or "Imagem"
        }

# ==========================================
# 🤖 CONFIGURAÇÃO DA API DO GEMINI
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Pydantic schema que você estruturou para a IA mapear
class EstruturaCurriculo(BaseModel):
    nome: str
    idade: str
    sexo: str
    localizacao: str
    formacao: str
    cursos: str
    habilidades: str

# ==========================================
# 🛠️ FUNÇÕES AUXILIARES
# ==========================================
def remover_acentos(texto):
    if not texto:
        return ""
    texto_normalizado = unicodedata.normalize('NFKD', texto)
    return "".join([c for c in texto_normalizado if not unicodedata.combining(c)]).lower()

def extrair_texto_pdf(arquivo_storage):
    texto = ""
    try:
        leitor = pypdf.PdfReader(arquivo_storage)
        for pagina in leitor.pages:
            texto += pagina.extract_text() or ""
    except Exception as e:
        print(f"Erro ao ler arquivo em memória: {e}")
    return texto

def extrair_texto_docx(arquivo_storage):
    return docx2txt.process(arquivo_storage)

def obter_variacoes_busca(termo):
    termo_limpo = remover_acentos(termo)
    mapeamento_rh = {
        "analista": ["analis", "analit", "analista", "analista", "analit"],
        "gestao": ["gesto", "gerenc", "gestor", "gesto", "gerenc", "gerente", "gesto"],
        "desenvolvedor": ["desenvol", "dev", "program", "programador", "desenvol", "desenvolv", "dev"],
        "tecnico": ["tecnic", "coordenador", "coorden", "superv", "supervisor", "superv", "coorden"]
    }
    if termo_limpo in mapeamento_rh:
        return mapeamento_rh[termo_limpo]
    if len(termo_limpo) > 5:
        return [termo_limpo, termo_limpo[:4], termo_limpo[:5]]
    return [termo_limpo]

def estruturar_curriculo_com_ia(texto_bruto):
    if not texto_bruto or not texto_bruto.strip():
        return None
    
    texto_limitado = texto_bruto.strip()[:18000]
    prompt_base = """
    Você é um assistente de RH especialista em triagem de currículos.
    Analise o texto bruto do currículo fornecido e extraia com precisão as informações solicitadas.
    Importante: Retorne strings simples e curtas para cada campo. Se não encontrar uma informação de forma explícita, preencha o campo como 'Não informado'.
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt_base + f"\nTexto do Currículo:\n{texto_limitado}",
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EstruturaCurriculo,
                temperature=0.1
            )
        )
        if response.text:
            import json
            return json.loads(response.text)
    except Exception as e:
        print(f"Tentativa 1 (Estruturada) Falhou: {e}")
        
    # Fallback caso a saída estruturada falhe
    try:
        prompt_fallback = "Retorne a resposta estritamente no formato JSON válido usando as chaves: 'nome', 'idade', 'sexo', 'localizacao', 'formacao', 'cursos', 'habilidades'."
        response_fallback = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt_fallback + f"\nTexto:\n{texto_limitado}",
            config=types.GenerateContentConfig(temperature=0.2)
        )
        texto_resposta = response_fallback.text.strip() if response_fallback.text else ""
        if "{" in texto_resposta:
            import json
            partes = texto_resposta.split("{")
            for parte in partes:
                parte_limpa = parte.strip()
                if parte_limpa.startswith('"nome"') or parte_limpa.startswith("'nome'"):
                    return json.loads("{" + parte_limpa.split("}")[0] + "}")
    except Exception as e:
        print(f"Fallback falhou: {e}")
        
    return {
        "nome": "Nome provisório", "idade": "Não Informado", "sexo": "Não Informado",
        "localizacao": "Manual necessário", "formacao": "Estrutura complexa de leitura.",
        "cursos": "Consulte o arquivo original clicando no botão (+)", "habilidades": "Análise Manual"
    }

# ==========================================
# 🛣️ ROTAS DO SISTEMA
# ==========================================
@app.route('/', methods=['GET'])
def index():
    busca_geral = request.args.get('busca', '').strip()
    f_sexo = request.args.get('sexo', '').strip()
    f_formacao = request.args.get('formacao', '').strip()
    f_localizacao = request.args.get('localizacao', '').strip()
    
    resultados_finais = []
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT id, nome_arquivo, conteudo, nome_candidato, idade, sexo, localizacao, formacao, cursos, habilidades FROM curriculos")
                todos_candidatos = cursor.fetchall()
                
                for item in todos_candidatos:
                    texto_completo_candidato = remover_acentos(
                        f"{item['conteudo']} {item['nome_candidato']} {item['habilidades']} {item['cursos']}"
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
                        
                    if passou_filtro:
                        # Limita o resumo na listagem para visualização
                        item['resumo'] = item['conteudo'][:150] + "..." if len(item['conteudo']) > 150 else item['conteudo']
                        resultados_finais.append(item)
    except Exception as e:
        print(f"Erro ao buscar dados: {e}")
        
    return render_template('index.html', candidatos=resultados_finais, filtros_ativos={
        'busca': busca_geral, 'sexo': f_sexo, 'formacao': f_formacao, 'localizacao': f_localizacao
    })

@app.route('/api/candidatos', methods=['GET'])
def listar_candidatos():
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT * FROM curriculos")
                candidatos = cursor.fetchall()
                return jsonify(candidatos)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/upload', methods=['POST'])
def upload():
    if 'arquivo' not in request.files:
        flash('Nenhum arquivo selecionado!')
        return redirect(url_for('index'))
        
    arquivo = request.files['arquivo']
    if arquivo.filename == '':
        flash('Nenhum arquivo selecionado!')
        return redirect(url_for('index'))
        
    extensao = os.path.splitext(arquivo.filename)[1].lower()
    if extensao not in ['.pdf', '.docx', '.doc']:
        flash('Apenas arquivos PDF ou Word são permitidos!')
        return redirect(url_for('index'))
        
    try:
        arquivo_read = arquivo.read()
        string_base64 = base64.b64encode(arquivo_read).decode('utf-8')
        
        arquivo_memoria = io.BytesIO(arquivo_read)
        if extensao == '.pdf':
            texto_extraido = extrair_texto_pdf(arquivo_memoria)
        else:
            texto_extraido = extrair_texto_docx(arquivo_memoria)
            
        dados_ia = estruturar_curriculo_com_ia(texto_extraido)
        
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO curriculos (nome_arquivo, conteudo, nome_candidato, idade, sexo, localizacao, formacao, cursos, habilidades, arquivo_binario)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    arquivo.filename, texto_extraido, dados_ia.get('nome'), dados_ia.get('idade'),
                    dados_ia.get('sexo'), dados_ia.get('localizacao'), dados_ia.get('formacao'),
                    dados_ia.get('cursos'), dados_ia.get('habilidades'), string_base64
                ))
                conn.commit()
                
        flash(f"Candidato '{dados_ia.get('nome')}' cadastrado com sucesso!")
    except Exception as e:
        flash(f"Erro ao processar arquivo: {e}")
        
    return redirect(url_for('index'))

@app.route('/curriculo/<int:id_candidato>')
def ver_curriculo(id_candidato):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT nome_arquivo, arquivo_binario FROM curriculos WHERE id = %s", (id_candidato,))
                resultado = cursor.fetchone()
                
                if resultado and resultado[1]:
                    nome_arquivo = resultado[0]
                    bytes_originais = base64.b64decode(resultado[1])
                    extensao = os.path.splitext(nome_arquivo)[1].lower()
                    
                    mimetype = "application/pdf" if extensao == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    return Response(
                        bytes_originais,
                        mimetype=mimetype,
                        headers={"Content-Disposition": f"inline; filename={nome_arquivo}"}
                    )
    except Exception as e:
        print(f"Erro ao recuperar arquivo: {e}")
        
    return "Arquivo original não encontrado", 404

@app.route('/deletar/<int:id_candidato>', methods=['POST'])
def deletar_candidato(id_candidato):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM curriculos WHERE id = %s", (id_candidato,))
                conn.commit()
        flash("Candidato removido com sucesso!")
    except Exception as e:
        flash(f"Erro ao deletar: {e}")
    return redirect(url_for('index'))

# ==========================================
# 🚀 INICIALIZAÇÃO DO SERVIDOR
# ==========================================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
