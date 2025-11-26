import requests

# ---------------- CONFIGURAÇÃO ----------------
# COLOQUE AQUI A URL QUE O RENDER TE DEU
BASE_URL = "https://api-telegram-for-messages.onrender.com" 

# SEU NÚMERO (Para fazer login e enviar)
MEU_TELEFONE = "+5573998411448"

# QUEM VAI RECEBER O ALERTA (Pode ser o mesmo número pra testar)
DESTINO_ALERTA = "+5571985534124"
# ----------------------------------------------

def testar_fluxo_completo():
    print(f"--- 1. TESTANDO CONEXÃO COM {BASE_URL} ---")
    try:
        # Apenas bate na url base para ver se acorda o servidor
        requests.get(BASE_URL) 
    except:
        pass # Ignora erro na home, queremos testar a API

    print("\n--- 2. INICIANDO LOGIN (Passo 1) ---")
    payload_inicio = {"phone": MEU_TELEFONE}
    resp = requests.post(f"{BASE_URL}/autenticacao/iniciar", json=payload_inicio)
    
    if resp.status_code != 200:
        print(f"❌ Erro no passo 1: {resp.text}")
        return
    
    print(f"✅ Sucesso! O Render disse: {resp.json()['message']}")
    
    # Pausa para você digitar o código que chegou no Telegram
    codigo = input("\n>> Digite o código que chegou no seu Telegram: ")
    senha = input(">> Tem senha 2FA? (Digite a senha ou aperte ENTER se não tiver): ")

    print("\n--- 3. FINALIZANDO LOGIN (Passo 2) ---")
    payload_final = {
        "phone": MEU_TELEFONE,
        "code": codigo,
        "password": senha if senha else None
    }
    
    resp = requests.post(f"{BASE_URL}/autenticacao/finalizar", json=payload_final)
    
    if resp.status_code != 200:
        print(f"❌ Erro no login: {resp.text}")
        return

    print("✅ LOGIN REALIZADO E SALVO NO FIREBASE!")
    print(resp.json())

    print("\n--- 4. TESTANDO O ENVIO DE ALERTA ---")
    print("Agora vamos fingir que o usuário apertou o botão de pânico...")
    
    payload_alerta = {
        "phone": MEU_TELEFONE, # Quem envia (busca a sessão no banco)
        "contact_phone": DESTINO_ALERTA, # Quem recebe
        "message": "Teste final via Render + Firebase! 🚀",
        "latitude": 12,
        "longitude": 13
    }
    
    resp = requests.post(f"{BASE_URL}/enviar-alerta", json=payload_alerta)
    
    if resp.status_code == 200:
        print("\n🎉 SUCESSO TOTAL! O ALERTA FOI ENVIADO!")
        print(resp.json())
    else:
        print(f"\n❌ Erro ao enviar alerta: {resp.text}")

if __name__ == "__main__":
    testar_fluxo_completo()