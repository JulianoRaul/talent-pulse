import os
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from google import genai
import pypdf
import docx2txt

app = Flask(__name__)

# ==========================================
# 🗄️ CONFIGURAÇÃO DO BANCO DE DADOS (POSTGRESQL)
# ==========================================
# Puxa a URL do banco da Render. Se não achar, usa um SQLite local para testes.
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL or 'sqlite:///talent_pulse_local.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ==========================================
# 🧬 MODELOS DO BANCO DE DADOS (TABELAS)
# ==========================================
class Candidato(db.Model):
    __tablename__ = 'candidatos'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), nullable=True)
    telefone = db.Column(db.String(50), nullable=True)
    resumo_ia = db.Column(db.Text, nullable=True)
    aderencia = db.Column(db.Integer, nullable=True) # Nota de 0 a 100

    def to_dict(self):
        return {
            "id": self.id,
            "nome": self.nome,
            "email": self.email,
            "telefone": self.telefone,
            "resumo_ia": self.resumo_ia,
            "aderencia": self.aderencia
        }

# ==========================================
# 🤖 CONFIGURAÇÃO DA API DO GEMINI
# ==========================================
# Puxa a chave de forma totalmente segura das configurações da Render
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ==========================================
# 🛠️ FUNÇÕES AUXILIARES (LEITURA DE ARQUIVOS)
# ==========================================
def extrair_texto_pdf(arquivo):
    leitor = pypdf.PdfReader(arquivo)
    texto = ""
    for pagina in leitor.pages:
        texto += pagina.extract_text() or ""
    return texto

def extrair_texto_docx(arquivo):
    return docx2txt.process(arquivo)

# ==========================================
# 🛣️ ROTAS DO SISTEMA
# ==========================================
@app.route('/')
def index():
    # Renderiza a tela principal do TalentPulse
    return render_template('index.html')

@app.route('/api/candidatos', methods=['GET'])
def listar_candidatos():
    candidatos = Candidato.query.order_by(Candidato.aderencia.desc()).all()
    return jsonify([c.to_dict() for c in candidatos])

@app.route('/api/triagem', methods=['POST'])
def realizar_triagem():
    if 'curriculo' not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    
    arquivo = request.files['curriculo']
    requisitos_vaga = request.form.get('requisitos', '')

    if arquivo.filename == '':
        return jsonify({"error": "Nome de arquivo vazio"}), 400

    # Extrai o texto baseado na extensão
    if arquivo.filename.endswith('.pdf'):
        texto_
