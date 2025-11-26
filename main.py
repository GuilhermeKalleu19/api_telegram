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

load_dotenv()


if not firebase_admin._apps:
    try:
        if os.path.exists("firebase_credentials.json"):
            cred = credentials.Certificate("firebase_credentials.json")
            firebase_admin.initialize_app(cred)
            print("✅ Firebase conectado com sucesso!")
        else:
            print("⚠️ AVISO: Arquivo 'firebase_credentials.json' não encontrado. O banco de dados não funcionará.")
    except Exception as e:
        print(f"❌ Erro ao conectar Firebase: {e}")

db = firestore.client() if firebase_admin._apps else None

API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')

if not all([API_ID, API_HASH]):
    print("❌ ERRO: Verifique seu .env ou variáveis do Render. Falta API_ID ou API_HASH.")

app = FastAPI(
    title="API de Alerta (Server-Side Storage)",
    description="Login simplificado: O servidor guarda o hash temporário no Firebase."
)


class LoginStartRequest(BaseModel):
    phone: str = Field(..., description="Número do telefone com DDD (ex: +5511999999999)")

class LoginCompleteRequest(BaseModel):
    phone: str = Field(..., description="O mesmo número usado no passo 1")
    code: str = Field(..., description="O código numérico recebido no Telegram")

    password: Optional[str] = Field(None, description="Senha 2FA (se a conta tiver).")

class AlertRequest(BaseModel):
    phone: str = Field(..., description="Telefone de QUEM está enviando")
    contact_phone: str = Field(..., description="Telefone de QUEM vai receber")
    message: str = Field(..., description="Mensagem de socorro")
    latitude: float
    longitude: float



@app.post("/autenticacao/iniciar")
async def login_step_1(request: LoginStartRequest):
    """
    PASSO 1: Envia código e SALVA O HASH NO FIREBASE ('login_attempts').
    """
    if not db:
        raise HTTPException(500, "Erro interno: Banco de dados desconectado.")

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    
    try:
        sent_code = await client.send_code_request(request.phone)
        

        db.collection('login_attempts').document(request.phone).set({
            'phone_code_hash': sent_code.phone_code_hash,
            'created_at': firestore.SERVER_TIMESTAMP
        })
        
        return {
            "status": "sucesso", 
            "message": f"Código enviado para {request.phone}. Prossiga para o passo 2."
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao solicitar código: {str(e)}")
    finally:
        await client.disconnect()


@app.post("/autenticacao/finalizar")
async def login_step_2(request: LoginCompleteRequest):
    """
    PASSO 2: Recebe apenas código e telefone. Busca o hash no banco.
    """
    if not db:
        raise HTTPException(500, "Banco de dados desconectado.")


    doc_ref = db.collection('login_attempts').document(request.phone)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(400, "Sessão expirada ou não encontrada. Faça o passo 1 novamente.")
    
    phone_code_hash = doc.to_dict().get('phone_code_hash')


    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
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
        raise HTTPException(400, f"Erro no login: {str(e)}")

    
    session_string = client.session.save()
    await client.disconnect()

    try:
        # Salva o login definitivo
        db.collection('users').document(request.phone).set({
            'phone': request.phone,
            'session_string': session_string,
            'updated_at': firestore.SERVER_TIMESTAMP
        })
        
        # Apaga o hash temporário (já usamos, não precisa mais)
        doc_ref.delete()
        
    except Exception as e_db:
        raise HTTPException(500, f"Login OK, mas erro ao salvar no banco: {e_db}")
    
    return {
        "status": "sucesso", 
        "message": "Login realizado com sucesso!"
    }


@app.post("/enviar-alerta")
async def send_alert(alert: AlertRequest):
    if not db:
        raise HTTPException(500, "Banco de dados desconectado.")

    doc = db.collection('users').document(alert.phone).get()

    if not doc.exists:
        raise HTTPException(404, "Usuário não logado/encontrado.")
    
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
        print(f"Erro: {e}")
        raise HTTPException(500, f"Erro envio: {str(e)}")
    finally:
        await user_client.disconnect()