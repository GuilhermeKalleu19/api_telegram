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

app = FastAPI(
    title="API de Alerta (Stateless + Firebase)",
    description="Permite login de múltiplos usuários sem erro de memória no Render."
)

# --- 2. Modelos de Dados ---

class LoginStartRequest(BaseModel):
    phone: str = Field(..., description="Número do telefone com DDD (ex: +5511999999999)")

class LoginCompleteRequest(BaseModel):
    phone: str = Field(..., description="O mesmo número usado no passo 1")
    code: str = Field(..., description="O código numérico recebido no Telegram")
    phone_code_hash: str = Field(..., description="O HASH que a API retornou no passo 1. OBRIGATÓRIO.")
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
    PASSO 1: Pede o código e RETORNA O HASH para o App guardar.
    """
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    
    try:
        # Envia solicitação de código para o Telegram
        sent_code = await client.send_code_request(request.phone)
        
        # O SEGRED0 ESTÁ AQUI: Retornamos o hash para o usuário
        return {
            "status": "sucesso", 
            "message": f"Código enviado para {request.phone}. Guarde o 'phone_code_hash'!",
            "phone_code_hash": sent_code.phone_code_hash
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao solicitar código: {str(e)}")
    finally:
        await client.disconnect()


@app.post("/autenticacao/finalizar")
async def login_step_2(request: LoginCompleteRequest):
    """
    PASSO 2: Recebe código + HASH + senha opcional.
    """
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        # Tenta fazer o login usando o hash que veio do App
        await client.sign_in(
            phone=request.phone,
            code=request.code,
            phone_code_hash=request.phone_code_hash
        )
        
    except SessionPasswordNeededError:
        # Se precisar de senha (2FA)
        if not request.password:
            await client.disconnect()
            raise HTTPException(
                status_code=401, 
                detail="Esta conta possui Senha de 2 Fatores (2FA). Preencha o campo 'password'."
            )
        
        try:
            # Tenta logar com a senha
            await client.sign_in(password=request.password)
        except Exception as e_pass:
            await client.disconnect()
            raise HTTPException(401, f"Senha 2FA incorreta: {str(e_pass)}")

    except Exception as e:
        await client.disconnect()
        raise HTTPException(400, f"Erro no login: {str(e)}")

    # --- SUCESSO! SALVANDO NO FIREBASE ---
    
    session_string = client.session.save()
    await client.disconnect()

    if not db:
        # Se o banco não estiver conectado, avisa mas não quebra (útil pra debug)
        print("AVISO: Banco de dados não conectado. Sessão não será salva.")
        return {"status": "erro_banco", "session_string": session_string}

    try:
        # Salva na coleção 'users'
        doc_ref = db.collection('users').document(request.phone)
        doc_ref.set({
            'phone': request.phone,
            'session_string': session_string,
            'updated_at': firestore.SERVER_TIMESTAMP
        })
    except Exception as e_db:
        raise HTTPException(500, f"Logou, mas erro ao salvar no Firebase: {e_db}")
    
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
        raise HTTPException(404, "Usuário não encontrado. Faça login primeiro.")
    
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
        await user_client.disconnect()