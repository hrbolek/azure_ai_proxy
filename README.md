Allows to create proxy keys to share access key to azure cognitive services

```bash
uvicorn main:app --env-file environment.secret.txt --port 8000
```

# 🌐 Azure OpenAI Reverse Proxy

Tento proxy server směruje OpenAI-kompatibilní požadavky (`/v1/chat/completions`, `/v1/responses`, `/v1/models`)  
na Azure OpenAI endpoint. Konfigurace probíhá výhradně přes **environment proměnné**.

---

## 🧩 Architektura ASGI aplikace

Celá aplikace je postavená jako modulární **ASGI** systém složený ze tří hlavních částí, které se spojují v souboru `main.py`:

```txt
main.py
├── proxy.py ← hlavní reverzní proxy vrstva (Azure OpenAI ↔ klienti)
├── management.py ← REST API pro správu API klíčů a usage
└── gui.py ← NiceGUI frontend + Entra ID (OIDC) přihlášení
```

### ⚙️ `proxy.py` — Azure OpenAI reverse proxy

- Poskytuje **OpenAI-kompatibilní endpointy** (`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`) i původní **Azure endpointy** (`/openai/deployments/...`).
- Přijímá klientské požadavky s vlastním API klíčem vydaným proxy (`Authorization: Bearer ...`).
- Ověřuje klíč vůči databázi (`ApiKeyModel`) a aplikuje:
  - **rate limiting**,
  - **expiration / disable flag**,
  - **usage logování** (počty tokenů, requestů, stream bytes, cena).
- K Azure OpenAI přistupuje přes **Key Vault API key provider**:
  - interní klíč (`AZURE_OPENAI_API_KEY`) je uložen v **Azure Key Vaultu**,  
  - načítá se pomocí **Managed Identity** (v Azure) nebo **Azure Arc MI** (on-prem),
  - proxy jej **cacheuje** po dobu `KEY_CACHE_TTL_SECONDS` a **automaticky obnoví** při rotaci nebo chybě `401/403`.
- Veškerá komunikace je asynchronní (`httpx.AsyncClient`).

---

### 🔐 `management.py` — REST API pro správu klíčů a usage

- Vystavuje interní API pod prefixem `/management/...`.
- Přihlašování probíhá přes:
  - **session cookie** (Entra ID token z GUI, uložený v `session_store`),
  - nebo **Bearer token** (např. při volání z Postmana).
- Ověření identity (`get_principal`) dekóduje OIDC token pomocí **Entra ID JWKS**.
- Umožňuje:
  - 🧾 **Vytvářet API klíče** (včetně expirace a rate-limitů),
  - ✅ **Aktivovat/deaktivovat klíče**,
  - 📊 **Získávat usage statistiky** (agregace `hour` / `day`).
- Každý uživatel má účet (`UserModel`), který se vytvoří automaticky při prvním přihlášení.

---

### 💼 `gui.py` — webové rozhraní (NiceGUI + Entra ID)

- Integruje **NiceGUI** do existujícího FastAPI (`init_gui(app)`).
- Přihlašování přes **Entra ID (Microsoft Entra / Azure AD)** pomocí knihovny **Authlib**.
- Uživatel se přihlásí přes `/auth/login`, po úspěchu se vytvoří session cookie (`sid`), která se ukládá do `session_store`.
- Rozhraní `/mgmt` (NiceGUI frontend):
  - zobrazuje API klíče (tabulka),
  - umožňuje vytvářet nové klíče, povolit/zakázat stávající,
  - zobrazuje **usage grafy** (ECharts, denní agregace),
  - volá interní API `/management/...` přes `httpx.ASGITransport` (bez vnější sítě).

---

### 🧠 Spojení všeho (`main.py`)

```python
from azure_proxy.proxy import app
from azure_proxy.gui import init_gui
from azure_proxy.management import router as management_router

init_gui(app)
app.include_router(management_router)
```



## 🔐 Základní principy přístupu

Aplikace `proxy.py` funguje jako reverzní proxy mezi klienty (např. aplikacemi nebo agenty) a službou **Azure OpenAI**.  
Hlavním cílem je **bezpečně zprostředkovat přístup** k Azure modelům bez přímého sdílení upstream API klíče a zároveň zajistit audit, kontrolu přístupu a jednotné API rozhraní.

### Princip fungování
1. **Klient** se připojuje na proxy (`/v1/chat/completions`, `/v1/embeddings`, `/openai/deployments/...`)  
   – používá **svůj vlastní API klíč**, který vydává proxy (ne Azure).  
   – klíč je uložen v databázi v hashované podobě a může být kdykoliv deaktivován nebo expirován.

2. **Proxy ověří klíč** (`Authorization: Bearer ...`) proti databázi:
   - neaktivní nebo expirované klíče jsou zamítnuty (`401 Unauthorized`);
   - při každém volání se zaznamenává `last_used_at`.

3. **Proxy volá Azure OpenAI** pomocí **vlastního interního klíče**, který je uložen v **Azure Key Vaultu**:
   - proxy nikdy neobsahuje klíč v ENV ani v image – čte jej dynamicky přes `KeyVaultApiKeyProvider`;
   - klíč je načten z Key Vaultu s časovou cache (TTL) a automaticky se obnoví při rotaci nebo chybě 401/403.

4. **Azure Key Vault přístup** probíhá přes **Managed Identity** (v Azure) nebo přes **Azure Arc** (on-prem), bez statických tajemství.

5. **Každý požadavek je logován**:
   - anonymizované informace o klientovi, typu požadavku, modelu, délce odpovědi, využití tokenů (usage);
   - log se zapisuje do databáze (`UsageModel`) a volitelně do JSONL nebo na stdout.

6. **OpenAI-kompatibilní rozhraní**  
   Proxy poskytuje kompatibilní endpointy (`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`), takže ji lze použít jako drop-in náhradu za OpenAI API.  
   Mapování modelů se provádí podle proměnné `OPENAI_COMPAT_MODEL_MAP` (`OpenAI model → Azure deployment`).

7. **Rotace klíče bez výpadku**  
   Při změně hodnoty secretu v Key Vaultu proxy automaticky zjistí neplatnost klíče (401/403) a načte novou verzi.  
   Není nutný restart kontejneru ani přerušení služby.

### Shrnutí bezpečnostního modelu
- ✅ Klienti nikdy nevidí Azure API klíč.  
- ✅ Proxy klíče klientů lze spravovat, omezovat a auditovat.  
- ✅ Azure Key Vault zajišťuje bezpečné uložení a rotaci tajemství.  
- ✅ Všechny přenosy probíhají přes HTTPS a proxy nepřeposílá citlivé hlavičky.  
- ✅ Usage logy poskytují transparentní sledování spotřeby a umožňují vyúčtování per klient.

> **Cíl:** Zajistit bezpečný, auditovatelný a škálovatelný přístup k Azure OpenAI modelům,  
> aniž by bylo nutné distribuovat nebo spravovat upstream API klíče mimo Azure.

## 🧩 Environment Variables for `proxy.py`

Proxy server lze konfigurovat čistě pomocí environment proměnných.  
Níže jsou uvedeny všechny podporované proměnné, jejich význam a výchozí hodnoty.

| Název proměnné | Výchozí hodnota | Popis |
|----------------|------------------|-------|
| **AZURE_COGNITIVE_ACCOUNT_NAME** | *(žádná)* | Název Azure Cognitive Services účtu (např. `myopenaiacct`). Slouží pro sestavení `UPSTREAM_ENDPOINT`. |
| **AZURE_OPENAI_API_KEY** | *(žádná)* | Primární API klíč pro Azure OpenAI. Pokud není zadán, použije se `OPENAI_API_KEY`. |
| **OPENAI_API_KEY** | *(žádná)* | Alternativní fallback klíč (např. pro kompatibilitu s OpenAI). |
| **AZURE_OPENAI_API_VERSION** | `2024-12-01-preview` | Verze API volaná vůči Azure OpenAI endpointu. |
| **AZURE_OPENAI_DEFAULT_DEPLOYMENT** | `summarization-deployment` | Název výchozího deploymentu, pokud není uveden model nebo není nalezen v mapě. |

---

### 🔒 Key Vault integrace
| Proměnná | Výchozí | Popis |
|-----------|----------|--------|
| **KEYVAULT_URL** | *(žádná)* | URL Azure Key Vaultu, např. `https://myvault.vault.azure.net`. Pokud je nastavena, proxy získává API klíč z Key Vaultu místo z ENV. |
| **KEYVAULT_SECRET_NAME** | `AZURE_OPENAI_API_KEY` | Název secretu v Key Vaultu, který obsahuje API klíč. |
| **KEY_CACHE_TTL_SECONDS** | `300` | Jak dlouho (v sekundách) držet klíč v paměti, než se znovu načte z Key Vaultu. |
> 🟢 Klíč se automaticky obnoví při vypršení TTL nebo pokud upstream vrátí `401/403`.  
> Pokud `KEYVAULT_URL` není nastaven, použije se klíč z ENV (`AZURE_OPENAI_API_KEY` nebo `OPENAI_API_KEY`).

---

### 🌐 Proxy konfigurace
| Proměnná | Výchozí | Popis |
|-----------|----------|--------|
| **PROXY_BIND** | `0.0.0.0` | IP adresa, na které bude proxy poslouchat. |
| **PROXY_PORT** | `8787` | TCP port, na kterém poběží FastAPI server (Uvicorn). |
| **PROXY_TOKEN** | *(žádná)* | Pokud je nastaveno, všechny požadavky musí obsahovat hlavičku `X-Proxy-Token: <hodnota>`. |
| **PROXY_LOG_PROMPTS** | `false` | Pokud `true`, loguje i obsah promptů (může obsahovat citlivá data). |
| **FORCE_JSON_RESPONSE** | `false` | Pokud `true`, proxy vynucuje `response_format={"type":"json_object"}` pro všechny chat požadavky. |
| **UPSTREAM_TIMEOUT** | `60` | Timeout (v sekundách) pro komunikaci s upstream API. |

---

### 🤖 OpenAI-kompatibilní režim
| Proměnná | Výchozí | Popis |
|-----------|----------|--------|
| **OPENAI_COMPAT_ENABLED** | `true` | Povolit `/v1/chat/completions` a `/v1/responses` endpointy kompatibilní s OpenAI API. |
| **OPENAI_COMPAT_MODEL_MAP** | `{}` | JSON mapa `openai_model → azure_deployment`. Např. `{"gpt-4o":"gpt4o-prod","gpt-4o-mini":"gpt4o-mini"}`. |
> Pokud není nalezen odpovídající model, použije se `AZURE_OPENAI_DEFAULT_DEPLOYMENT`.

---

### 🧾 Usage logging
| Proměnná | Výchozí | Popis |
|-----------|----------|--------|
| **USAGE_LOG_PATH** | *(žádná)* | Cesta k souboru (např. `/var/log/usage.jsonl`). Pokud je nastavena, loguje se každá odpověď v JSON Lines formátu. |
| **USAGE_LOG_STDOUT** | `true` | Pokud `true`, usage logy se tisknou i na stdout. |

---

### 🧠 Shrnutí závislostí a chování
- Proxy směruje požadavky typu `/v1/chat/completions`, `/v1/responses` nebo `/openai/deployments/...` na Azure OpenAI endpoint.
- Klíč se načítá z Key Vaultu (pokud je k dispozici) a ukládá se do cache.
- Při rotaci klíče v Key Vaultu se proxy automaticky přepne na novou verzi.
- Volitelně vyžaduje `X-Proxy-Token` pro autorizaci přístupu.
- Usage data (počty tokenů, statusy, latence) se ukládají do DB (tabulka `UsageModel`) a volitelně do JSONL.

---

### 🧪 Příklad spuštění
```bash
export AZURE_COGNITIVE_ACCOUNT_NAME="myaccount"
export KEYVAULT_URL="https://myvault.vault.azure.net"
export PROXY_PORT=8080
export OPENAI_COMPAT_MODEL_MAP='{"gpt-4o":"gpt4o-prod","gpt-4o-mini":"gpt4o-mini"}'
uvicorn proxy:app --host 0.0.0.0 --port $PROXY_PORT


## 🐳 Nasazení `proxy.py` s Azure Key Vault (on-prem, přes Azure Arc)

Tento projekt lze provozovat v Docker kontejneru běžícím **on-prem**.  
Pro bezpečný přístup k **Azure Key Vaultu** se používá **Azure Arc** a **Managed Identity**, takže v kontejneru nejsou žádná tajemství ani certifikáty.
```
---

### 🧱 1. Dockerfile – kompozice pro Python aplikaci

```dockerfile
# ===========================================
# Dockerfile – Azure OpenAI Reverse Proxy
# ===========================================

FROM python:3.11-slim

# --- systémové knihovny ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- kopírování projektu ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# --- proměnné prostředí (pouze výchozí, ne tajemství!) ---
ENV PYTHONUNBUFFERED=1 \
    PROXY_PORT=8787 \
    KEY_CACHE_TTL_SECONDS=300

EXPOSE 8787

CMD ["python", "-m", "proxy"]
```

### 2. docker-compose

```yaml
version: "3.9"
services:
  proxy:
    image: your/proxy:latest
    container_name: azure-proxy
    network_mode: "host"         # kvůli přístupu na Arc endpoint (127.0.0.1:40342)
    group_add:
      - 993                      # GID skupiny "himds" na hostu (viz níže)
    volumes:
      - /var/opt/azcmagent:/var/opt/azcmagent:ro  # challenge soubor od Azure Arc
    environment:
      KEYVAULT_URL: "https://myvault.vault.azure.net"
      KEYVAULT_SECRET_NAME: "AZURE_OPENAI_API_KEY"
      KEY_CACHE_TTL_SECONDS: "300"
      PROXY_PORT: "8787"
      # další volitelné proměnné – viz tabulka v README (PROXY_TOKEN, LOG_PROMPTS …)
    restart: unless-stopped

```

### 3. linux host
```sh
# 1️⃣ Připoj server do Azure Arc
az login
az account set --subscription "<SUBSCRIPTION_ID>"
az connectedmachine connect \
    --name myserver \
    --resource-group my-rg \
    --location westeurope

# 2️⃣ Ověř, že běží Arc agent
sudo systemctl status himdsd
sudo azcmagent show

# 3️⃣ Zapni system-assigned Managed Identity
az connectedmachine identity assign \
    --name myserver \
    --resource-group my-rg

# 4️⃣ Přidej roli pro přístup ke Key Vaultu
az role assignment create \
    --assignee-object-id $(az connectedmachine show \
        --name myserver --resource-group my-rg --query identity.principalId -o tsv) \
    --role "Key Vault Secrets User" \
    --scope /subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RG>/providers/Microsoft.KeyVault/vaults/<VAULT_NAME>

# 5️⃣ Zkontroluj skupinu 'himds'
getent group himds
# Výstup např.: himds:x:993:  → číslo použij v docker-compose.yml (group_add)

# 6️⃣ Spusť kontejner
docker compose up -d
```

### 5. overeni
```bash
docker logs azure-proxy | grep KV


> [KV] Using Managed Identity via Azure Arc
> [KV] Loaded secret 'AZURE_OPENAI_API_KEY' (expires in 300s)
```

```bash
docker run -d  -p 8880:8000 --name local_azureaiproxy --env-file environment.secret.txt local_azureaiproxy:latest
```
