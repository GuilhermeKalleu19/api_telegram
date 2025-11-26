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
if not firebase_admin._apps:
    try:
        if os.path.exists("firebase_credentials.json"):
            cred = credentials.Certificate("firebase_credentials.json")
            firebase_admin.initialize_app(cred)
            print("✅ Firebase conectado com sucesso!")
        else:
            print("⚠️ AVISO: Arquivo 'firebase_credentials.json' não encontrado.")
    except Exception as e:
        print(f"❌ Erro ao conectar Firebase: {e}")

db = firestore.client() if firebase_admin._apps else None

API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')

if not all([API_ID, API_HASH]):
    print("❌ ERRO: Faltam credenciais API_ID/API_HASH.")

app = FastAPI(
    title="API de Alerta (Fix Data Center)",
    description="Correção do erro 'Code Expired' mantendo a sessão temporária no banco."
)

# --- 2. Modelos de Dados ---

class LoginStartRequest(BaseModel):
    phone: str = Field(..., description="Número do telefone com DDD")

class LoginCompleteRequest(BaseModel):
    phone: str = Field(..., description="O mesmo número usado no passo 1")
    code: str = Field(..., description="Código recebido")
    password: Optional[str] = Field(None, description="Senha 2FA (opcional)")

class AlertRequest(BaseModel):
    phone: str
    contact_phone: str
    message: str
    latitude: float
    longitude: float

# --- 3. Endpoints de Autenticação ---

@app.post("/autenticacao/iniciar")
async def login_step_1(request: LoginStartRequest):
    """
    PASSO 1: Pede o código e SALVA A SESSÃO TEMPORÁRIA + HASH no Firebase.
    Isso garante que o Passo 2 use o mesmo Data Center.
    """
    if not db:
        raise HTTPException(500, "Erro interno: Banco de dados desconectado.")

    # Cria uma sessão virgem
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    
    try:
        sent_code = await client.send_code_request(request.phone)
        
        # O PULO DO GATO: Salvamos o estado atual da conexão (que já sabe qual DC usar)
        temp_session_string = client.session.save()
        
        # Salvamos tudo na tabela temporária
        db.collection('login_attempts').document(request.phone).set({
            'phone_code_hash': sent_code.phone_code_hash,
            'temp_session': temp_session_string, # <--- Importante para não dar erro de DC
            'created_at': firestore.SERVER_TIMESTAMP
        })
        
        return {
            "status": "sucesso", 
            "message": f"Código enviado para {request.phone}."
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao solicitar código: {str(e)}")
    finally:
        await client.disconnect()


@app.post("/autenticacao/finalizar")
async def login_step_2(request: LoginCompleteRequest):
    """
    PASSO 2: Recupera a sessão temporária e finaliza o login.
    """
    if not db:
        raise HTTPException(500, "Banco de dados desconectado.")

    # 1. Busca os dados temporários no Firebase
    doc_ref = db.collection('login_attempts').document(request.phone)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(400, "Sessão expirada. Faça o passo 1 novamente.")
    
    data = doc.to_dict()
    phone_code_hash = data.get('phone_code_hash')
    temp_session = data.get('temp_session') # Recupera a conexão do passo 1

    if not temp_session:
        raise HTTPException(400, "Erro de estado: Sessão temporária não encontrada.")

    # 2. Conecta usando a SESSÃO TEMPORÁRIA (Isso evita o erro 'Code Expired')
    client = TelegramClient(StringSession(temp_session), API_ID, API_HASH)
    await client.connect()

    try:
        # Tenta logar
        await client.sign_in(
            phone=request.phone,
            code=request.code,
            phone_code_hash=phone_code_hash
        )
        
    except SessionPasswordNeededError:
        if not request.password:
            await client.disconnect()
            raise HTTPException(401, "Senha 2FA necessária. Preencha o campo 'password'.")
        
        try:
            await client.sign_in(password=request.password)
        except Exception as e_pass:
            await client.disconnect()
            raise HTTPException(401, f"Senha 2FA incorreta: {str(e_pass)}")

    except Exception as e:
        await client.disconnect()
        # Se der erro, pode ser código errado mesmo
        raise HTTPException(400, f"Erro no login: {str(e)}")

    # 3. Sucesso! Salva a sessão definitiva
    final_session = client.session.save()
    await client.disconnect()

    try:
        # Salva em 'users'
        db.collection('users').document(request.phone).set({
            'phone': request.phone,
            'session_string': final_session,
            'updated_at': firestore.SERVER_TIMESTAMP
        })
        
        # Limpa a tentativa
        doc_ref.delete()
        
    except Exception as e_db:
        raise HTTPException(500, f"Login OK, mas erro ao salvar no banco: {e_db}")
    
    return {
        "status": "sucesso", 
        "message": "Login realizado com sucesso!"
    }

# --- 4. Endpoint de Envio (Mesma lógica) ---

@app.post("/enviar-alerta")
async def send_alert(alert: AlertRequest):
    if not db:
        raise HTTPException(500, "Banco de dados desconectado.")

    doc = db.collection('users').document(alert.phone).get()

    if not doc.exists:
        raise HTTPException(404, "Usuário não logado.")
    
    session_str = doc.to_dict().get('session_string')
    if not session_str:
        raise HTTPException(401, "Sessão inválida.")

    user_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    
    try:
        await user_client.connect()
        if not await user_client.is_user_authorized():
            raise HTTPException(401, "Sessão expirou.")

        msg = f"🚨 *PEDIDO DE SOCORRO* 🚨\n\n{alert.message}"
        await user_client.send_message(alert.contact_phone, msg)
        
        geo = InputMediaGeoPoint(InputGeoPoint(lat=alert.latitude, long=alert.longitude))
        await user_client.send_file(alert.contact_phone, file=geo)
        
        return {"status": "sucesso", "message": "Alerta enviado!"}
        
    except Exception as e:
        raise HTTPException(500, f"Erro envio: {str(e)}")
    finally:
        await user_client.disconnect()