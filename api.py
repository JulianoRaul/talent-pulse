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
    if DATABASE_URL:
        if "sslmode=" not in DATABASE_URL:
            separator = "&" if "?" in DATABASE_URL else "?"
            url_conexao = f"{DATABASE_URL}{separator}sslmode=require"
        else:
            url_conexao = DATABASE_URL
        return psycopg2.connect(url_conexao)
    else:
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
