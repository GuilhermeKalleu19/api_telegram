import os
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import InputMediaGeoPoint, InputGeoPoint
from telethon.errors import SessionPasswordNeededError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

# --- 1. Configuração Inicial ---
load_dotenv()

# Configuração do Firebase
# O Render vai procurar o arquivo 'firebase_credentials.json' (que você cria nos Secret Files)
if not firebase_admin._apps:
    try:
        # Tenta carregar as credenciais
        if os.path.exists("firebase_credentials.json"):
            cred = credentials.Certificate("firebase_credentials.json")
            firebase_admin.initialize_app(cred)
            print("✅ Firebase conectado com sucesso!")
        else:
            print("⚠️ AVISO: Arquivo 'firebase_credentials.json' não encontrado. O banco de dados não funcionará.")
    except Exception as e:
        print(f"❌ Erro ao conectar Firebase: {e}")

# Inicializa o cliente do Banco de Dados
db = firestore.client() if firebase_admin._apps else None

# Credenciais de Desenvolvedor (Suas credenciais do my.telegram.org)
API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')

# Verificação básica
if not all([API_ID, API_HASH]):
    print("❌ ERRO: Verifique seu .env ou variáveis do Render. Falta API_ID ou API_HASH.")

# Cache temporário na memória RAM (apenas para guardar o hash entre o passo 1 e 2 do login)
# Não persiste se o servidor reiniciar, mas serve para o fluxo rápido de login.
temp_login_cache = {}

app = FastAPI(
    title="API de Alerta (Multi-Usuário + Firebase)",
    description="Permite login de múltiplos usuários, salva no Firebase e envia alertas de emergência."
)

# --- 2. Modelos de Dados ---

class LoginStartRequest(BaseModel):
    phone: str = Field(..., description="Número do telefone com DDD (ex: +5511999999999)")

class LoginCompleteRequest(BaseModel):
    phone: str = Field(..., description="O mesmo número usado no passo 1")
    code: str = Field(..., description="O código numérico recebido no Telegram")
    password: Optional[str] = Field(None, description="Senha 2FA (se a conta tiver). Se não tiver, deixe vazio.")

class AlertRequest(BaseModel):
    phone: str = Field(..., description="Telefone de QUEM está enviando (usuário logado)")
    contact_phone: str = Field(..., description="Telefone de QUEM vai receber o alerta")
    message: str = Field(..., description="Mensagem de socorro")
    latitude: float
    longitude: float

# --- 3. Endpoints de Autenticação ---

@app.post("/autenticacao/iniciar")
async def login_step_1(request: LoginStartRequest):
    """
    PASSO 1: O usuário envia o número. A API conecta no Telegram e pede o código SMS.
    """
    # Cria cliente temporário sem sessão salva
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    
    try:
        # Envia solicitação de código para o Telegram
        sent_code = await client.send_code_request(request.phone)
        
        # Guarda o 'phone_code_hash' na memória. Ele é essencial para o passo 2.
        temp_login_cache[request.phone] = {
            "phone_code_hash": sent_code.phone_code_hash
        }
        
        return {
            "status": "sucesso", 
            "message": f"Código enviado para {request.phone}. Verifique seu Telegram/SMS."
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao solicitar código: {str(e)}")
    finally:
        await client.disconnect()


@app.post("/autenticacao/finalizar")
async def login_step_2(request: LoginCompleteRequest):
    """
    PASSO 2: Recebe código (+ senha opcional). Valida login e salva Sessão no Firebase.
    """
    # Verifica se o passo 1 foi feito
    if request.phone not in temp_login_cache:
        raise HTTPException(400, "Sessão não encontrada. Faça o passo 1 (/autenticacao/iniciar) novamente.")
    
    cached_data = temp_login_cache[request.phone]
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        # Tenta fazer o login com código
        await client.sign_in(
            phone=request.phone,
            code=request.code,
            phone_code_hash=cached_data["phone_code_hash"]
        )
        
    except SessionPasswordNeededError:
        # Se cair aqui, é porque precisa de senha (2FA)
        if not request.password:
            await client.disconnect()
            raise HTTPException(
                status_code=401, 
                detail="Esta conta possui Senha de 2 Fatores (2FA). Preencha o campo 'password'."
            )
        
        try:
            # Tenta logar com a senha fornecida
            await client.sign_in(password=request.password)
        except Exception as e_pass:
            await client.disconnect()
            raise HTTPException(401, f"Senha 2FA incorreta: {str(e_pass)}")

    except Exception as e:
        await client.disconnect()
        raise HTTPException(400, f"Erro no login (Código inválido?): {str(e)}")

    # --- SUCESSO! SALVANDO NO FIREBASE ---
    
    # Gera a string da sessão (Token de acesso permanente)
    session_string = client.session.save()
    await client.disconnect()

    if not db:
        raise HTTPException(500, "Erro interno: Banco de dados Firebase não conectado.")

    try:
        # Salva na coleção 'users', usando o telefone como ID do documento
        doc_ref = db.collection('users').document(request.phone)
        doc_ref.set({
            'phone': request.phone,
            'session_string': session_string,
            'updated_at': firestore.SERVER_TIMESTAMP
        })
    except Exception as e_db:
        raise HTTPException(500, f"Logou no Telegram, mas erro ao salvar no Firebase: {e_db}")
    
    # Limpa cache da memória
    del temp_login_cache[request.phone]
    
    return {
        "status": "sucesso", 
        "message": "Login realizado! Sessão salva no banco de dados."
    }

# --- 4. Endpoint de Envio (Lê do Firebase) ---

@app.post("/enviar-alerta")
async def send_alert(alert: AlertRequest):
    """
    Recebe o pedido de alerta, busca a sessão do usuário no Firebase e envia.
    """
    if not db:
        raise HTTPException(500, "Banco de dados desconectado.")

    # 1. Buscar Sessão no Firebase
    doc_ref = db.collection('users').document(alert.phone)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(404, "Usuário não encontrado. Por favor, faça login na API primeiro.")
    
    user_data = doc.to_dict()
    session_str = user_data.get('session_string')

    if not session_str:
        raise HTTPException(401, "Sessão inválida no banco de dados.")

    # 2. Conectar como o usuário
    user_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    
    try:
        await user_client.connect()
        
        # Verifica validade da sessão
        if not await user_client.is_user_authorized():
            raise HTTPException(401, "O login expirou. Faça autenticação novamente.")

        # 3. Enviar Mensagem
        final_message = f"🚨 *PEDIDO DE SOCORRO* 🚨\n\n{alert.message}"
        await user_client.send_message(alert.contact_phone, final_message)
        
        # 4. Enviar Localização
        geo = InputMediaGeoPoint(InputGeoPoint(lat=alert.latitude, long=alert.longitude))
        await user_client.send_file(alert.contact_phone, file=geo)
        
        return {
            "status": "sucesso",
            "message": f"Alerta enviado para {alert.contact_phone}"
        }
        
    except Exception as e:
        print(f"Erro no envio: {e}")
        raise HTTPException(500, f"Falha ao enviar pelo Telegram: {str(e)}")
    finally:
        # Sempre desconecta para liberar recursos no servidor
        await user_client.disconnect()