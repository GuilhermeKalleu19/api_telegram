import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import InputMediaGeoPoint, InputGeoPoint
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Carrega variáveis de ambiente (útil para testes locais)
load_dotenv()

# --- Configurações ---
API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')
SESSION_STRING = os.getenv('TELEGRAM_SESSION') # A chave mágica para logar na nuvem

# Verificação de segurança
if not all([API_ID, API_HASH, SESSION_STRING]):
    print("AVISO: Faltam variáveis de ambiente (API_ID, HASH ou SESSION).")
    # Não damos exit(1) aqui para o servidor não crashar no boot, 
    # mas o envio falhará se não configurar.

# --- Inicialização do Cliente ---
# Aqui está o segredo: Usamos StringSession em vez de criar um arquivo .session
try:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
except Exception as e:
    print(f"Erro ao criar cliente: {e}")
    client = None

# --- Lifespan (Ciclo de Vida) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if client:
        print("Iniciando conexão com Telegram...")
        await client.connect()
        
        # Verifica se a Session String é válida
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Bot conectado como: {me.first_name} (ID: {me.id})")
        else:
            print("ERRO CRÍTICO: A String de Sessão é inválida ou expirou.")
    
    yield # O servidor roda aqui
    
    if client:
        print("Desconectando do Telegram...")
        await client.disconnect()

# --- App FastAPI ---
app = FastAPI(
    title="API de Alerta (Cloud Version)",
    description="API para envio de alertas via Telegram usando StringSession.",
    lifespan=lifespan
)

# --- Modelos ---
class AlertRequest(BaseModel):
    contact_phone: str = Field(..., description="Telefone do contato (ex: +5571...)")
    message: str = Field(..., description="Mensagem de emergência")
    latitude: float = Field(..., description="Latitude")
    longitude: float = Field(..., description="Longitude")

# --- Endpoints ---

@app.get("/")
async def health_check():
    """Verifica se a API está online."""
    authorized = await client.is_user_authorized() if client else False
    return {
        "status": "online",
        "telegram_connected": authorized,
        "message": "Servidor rodando. Use POST /enviar-alerta"
    }

@app.post("/enviar-alerta")
async def handle_send_alert(alert: AlertRequest):
    """
    Endpoint único para envio. Não requer login manual, 
    pois usa a credencial do ambiente.
    """
    if not client:
        raise HTTPException(500, "Cliente Telegram não inicializado.")

    if not await client.is_user_authorized():
        raise HTTPException(
            status_code=401, 
            detail="ERRO DE AUTENTICAÇÃO: A TELEGRAM_SESSION no servidor é inválida ou expirou. Gere uma nova."
        )
    
    try:
        # 1. Enviar Texto
        final_message = f"🚨 *MENSAGEM DE EMERGÊNCIA* 🚨\n\n{alert.message}"
        await client.send_message(alert.contact_phone, final_message)

        # 2. Enviar Localização
        geo_point = InputMediaGeoPoint(InputGeoPoint(lat=alert.latitude, long=alert.longitude))
        await client.send_file(alert.contact_phone, file=geo_point)
        
        return {
            "status": "sucesso",
            "message": f"Alerta enviado para {alert.contact_phone}"
        }

    except Exception as e:
        print(f"Erro no envio: {e}")
        raise HTTPException(status_code=500, detail=f"Falha ao enviar: {str(e)}")