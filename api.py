import sys
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import json
import unicodedata
import base64
from flask import Flask, render_template, request, redirect, url_for, flash, Response
import pypdf
import docx2txt
from google import genai
from google.genai import types
from pydantic import BaseModel

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "chave_secreta_talent_pulse_ai")

# 🔴 CONFIGURAÇÃO DA CHAVE DA API DO GEMINI:
CONFIG_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=CONFIG_API_KEY)

# Pega a URL de conexão do banco que configuraremos no painel da Render
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    # Conexão segura e direta com o PostgreSQL na nuvem
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    print("-> Verificando/Criando tabelas no PostgreSQL na nuvem...")
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
                    arquivo_binario TEXT  -- Nova coluna que salva o PDF/DOCX original em Base64
                );
            ''')
        conn.commit()
    print("-> Banco de dados PostgreSQL pronto!")

def extrair_texto_memoria(arquivo_storage, extensao):
    texto = ""
    try:
        if extensao == '.pdf':
            leitor = pypdf.PdfReader(arquivo_storage)
            for pagina in leitor.pages:
                texto += pagina.extract_text() or ""
        elif extensao in ['.docx', '.doc']:
            texto = docx2txt.process(arquivo_storage)
    except Exception as e:
        print(f"Erro ao ler arquivo em memória: {e}")
    return texto

class EstruturaCurriculo(BaseModel):
    nome: str
    idade: str
    sexo: str
    localizacao: str
    formacao: str
    cursos: str
    habilidades: str

def estruturar_curriculo_com_ia(texto_bruto):
    if not texto_bruto or not texto_bruto.strip():
        return {
            "nome": "Documento Digitalizado (Imagem/Scan)", 
            "idade": "Não informado", "sexo": "Não informado",
            "localizacao": "OCR Necessário", 
            "formacao": "Este currículo foi enviado como imagem ou scanner. Para extração automática, utilize PDFs digitais nativos.", 
            "cursos": "Não informado", "habilidades": "Imagem"
        }

    texto_limitado = texto_bruto.strip()[:8000]
    prompt_base = f"""
    Você é um assistente de RH especialista em triagem de currículos. 
    Analise o texto bruto do currículo fornecido e extraia com precisão as informações solicitadas.
    Importante: Retorne strings simples e curtas para cada campo. Se não encontrar uma informação de forma explícita, preencha o campo como "Não informado".
    
    Texto do Currículo:
    {texto_limitado}
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt_base,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EstruturaCurriculo,
                temperature=0.1
            ),
        )
        if response.text:
            return json.loads(response.text)
    except Exception as e:
        print(f"Tentativa 1 (Estruturada) falhou: {e}")

    try:
        prompt_fallback = prompt_base + "\nRetorne a resposta estritamente no formato JSON válido, usando exatamente as seguintes chaves entre aspas: \"nome\", \"idade\", \"sexo\", \"localizacao\", \"formacao\", \"cursos\", \"habilidades\"."
        response_fallback = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt_fallback,
            config=types.GenerateContentConfig(temperature=0.2),
        )
        texto_resposta = response_fallback.text.strip() if response_fallback.text else ""
        if "```" in texto_resposta:
            partes = texto_resposta.split("```")
            for parte in partes:
                parte_limpa = parte.strip()
                if parte_limpa.startswith("json"):
                    parte_limpa = parte_limpa[4:].strip()
                if parte_limpa.startswith("{") and parte_limpa.endswith("}"):
                    texto_resposta = parte_limpa
                    break
        return json.loads(texto_resposta)
    except Exception:
        pass
        
    linhas = [l.strip() for l in texto_limitado.split('\n') if l.strip()]
    nome_provisorio = linhas[0][:40] if linhas else "Candidato Solicitado"
    return {
        "nome": nome_provisorio, "idade": "Não informado", "sexo": "Não informado",
        "localizacao": "Análise Manual", "formacao": "Estrutura complexa de leitura.", 
        "cursos": "Consulte o arquivo original clicando no botão (+)", "habilidades": "Análise Manual"
    }

def remover_acentos(texto):
    if not texto: return ""
    texto_normalizado = unicodedata.normalize('NFKD', texto)
    return "".join([c for c in texto_normalizado if not unicodedata.combining(c)]).lower()

def obter_variacoes_busca(termo):
    termo_limpo = remover_acentos(termo)
    mapeamento_rh = {
        "analise": ["analis", "analit"], "analista": ["analis", "analit"],
        "gestao": ["gesto", "gerenc"], "gestor": ["gesto", "gerenc"], "gerente": ["gerenc", "gesto"],
        "desenvolvedor": ["desenvol", "dev", "program"], "programador": ["program", "desenvol", "dev"],
        "tecnico": ["tecnic"], "coordenador": ["coorden", "superv"], "supervisor": ["superv", "coorden"]
    }
    if termo_limpo in mapeamento_rh: return mapeamento_rh[termo_limpo]
    if len(termo_limpo) > 5: return [termo_limpo, termo_limpo[:5]]
    return [termo_limpo]

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
                        if not match_encontrado: passou_filtro = False
                    
                    if f_sexo and item['sexo'] != f_sexo: passou_filtro = False
                    if f_formacao and remover_acentos(f_formacao) not in remover_acentos(item['formacao']): passou_filtro = False
                    if f_localizacao and remover_acentos(f_localizacao) not in remover_acentos(item['localizacao']): passou_filtro = False

                    if passou_filtro:
                        item['resumo'] = item['conteudo'][:150] + "..." if len(item['conteudo']) > 150 else item['conteudo']
                        resultados_finais.append(dict(item))
    except Exception as e:
        print(f"Erro ao buscar dados: {e}")
        init_db()

    filtros = {'busca': busca_geral, 'sexo': f_sexo, 'formacao': f_formacao, 'localizacao': f_localizacao}
    return render_template('index.html', candidatos=resultados_finais, filtros=filtros)

@app.route('/upload', methods=['POST'])
def upload():
    if 'arquivo' not in request.files:
        flash('Nenhum arquivo enviado!')
        return redirect(url_for('index'))
    
    arquivo = request.files['arquivo']
    if arquivo.filename == '':
        flash('Nenhum arquivo selecionado!')
        return redirect(url_for('index'))
    
    extensao = os.path.splitext(arquivo.filename)[1].lower()
    if extensao not in ['.pdf', '.docx', '.doc']:
        flash('Apenas arquivos PDF ou Word são permitidos!')
        return redirect(url_for('index'))
    
    # Lê o arquivo direto da memória do upload para processar e converter em Base64
    bytes_arquivo = arquivo.read()
    string_base64 = base64.b64encode(bytes_arquivo).decode('utf-8')
    
    # Reseta o ponteiro para extrair o texto limpo
    import io
    arquivo_memoria = io.BytesIO(bytes_arquivo)
    texto_extraido = extrair_texto_memoria(arquivo_memoria, extensao)
    
    dados_ia = estruturar_curriculo_com_ia(texto_extraido)
    
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO curriculos 
                   (nome_arquivo, conteudo, nome_candidato, idade, sexo, localizacao, formacao, cursos, habilidades, arquivo_binario) 
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", 
                (arquivo.filename, texto_extraido, dados_ia['nome'], dados_ia['idade'], 
                 dados_ia['sexo'], dados_ia['localizacao'], dados_ia['formacao'], dados_ia['cursos'], dados_ia['habilidades'], string_base64)
            )
        conn.commit()
        
    flash(f'Candidato "{dados_ia["nome"]}" cadastrado com sucesso!')
    return redirect(url_for('index'))

@app.route('/candidato/<int:id_candidato>')
def ver_curriculo(id_candidato):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT nome_arquivo, arquivo_binario FROM curriculos WHERE id = %s", (id_candidato,))
            resultado = cursor.fetchone()
        
    if resultado and resultado[1]:
        nome_arquivo = resultado[0]
        bytes_originais = base64.b64decode(resultado[1])
        extensao = os.path.splitext(nome_arquivo)[1].lower()
        
        mimetype = "application/pdf" if extensao == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return Response(bytes_originais, mimetype=mimetype, headers={"Content-Disposition": f"inline; filename={nome_arquivo}"})
        
    return "Arquivo original não encontrado.", 404

@app.route('/deletar/<int:id_candidato>', methods=['POST'])
def deletar_candidato(id_candidato):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM curriculos WHERE id = %s", (id_candidato,))
        conn.commit()
    flash("Candidato removido com sucesso!")
    return redirect(url_for('index'))

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)