import os, json, hashlib, asyncio, time
import dataclasses
import uuid
import datetime
from typing import Optional, Any
from contextlib import asynccontextmanager

import uvicorn
import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Cookie, Header, Depends, Body
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse

from sqlalchemy import select

# from db import init_db
from .DBDefinitions import get_session, get_session_maker, AsyncSession
from .KeyVaultApiKeyProvider import KeyVaultApiKeyProvider

# ==== Konfigurace z env ====
UPSTREAM_ACCOUNT = os.getenv("AZURE_COGNITIVE_ACCOUNT_NAME", "")
UPSTREAM_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))
UPSTREAM_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
UPSTREAM_ENDPOINT = f"https://{UPSTREAM_ACCOUNT}.openai.azure.com"

PROXY_BIND = os.getenv("PROXY_BIND", "0.0.0.0")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8787"))

PROXY_TOKEN = os.getenv("PROXY_TOKEN", "")  # volitelné – pokud je nastaveno, vyžaduje X-Proxy-Token
LOG_PROMPTS = os.getenv("PROXY_LOG_PROMPTS", "false").lower() == "true"
FORCE_JSON_RESPONSE = os.getenv("FORCE_JSON_RESPONSE", "false").lower() == "true"
TIMEOUT_SECS = float(os.getenv("UPSTREAM_TIMEOUT", "60"))

# Key Vault konfigurace
KEYVAULT_URL = os.getenv("KEYVAULT_URL")  # např. https://myvault.vault.azure.net
KEYVAULT_SECRET_NAME = os.getenv("KEYVAULT_SECRET_NAME", "AZURE_OPENAI_API_KEY")
KEY_CACHE_TTL = int(os.getenv("KEY_CACHE_TTL_SECONDS", "300"))

# --- NOVÉ ENV pro OpenAI-compatible režim ---
OPENAI_COMPAT_ENABLED = os.getenv("OPENAI_COMPAT_ENABLED", "true").lower() == "true"
# JSON mapa: "openai_model" -> "azure_deployment"
# např: {"gpt-4o":"gpt4o-prod","gpt-4o-mini":"gpt4o-mini"}
OPENAI_COMPAT_MODEL_MAP = os.getenv("OPENAI_COMPAT_MODEL_MAP", "{}")

try:
    MODEL_MAP: dict[str, str] = json.loads(OPENAI_COMPAT_MODEL_MAP) if OPENAI_COMPAT_MODEL_MAP else {}
except Exception:
    MODEL_MAP = {
        "gpt-5-nano": "gpt-5-nano",
        "gpt-4.1": "orchestration-deployment",
        "gpt-4o-mini": "summarization-deployment",
    }

# region Usage Logs
DEFAULT_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEFAULT_DEPLOYMENT", "summarization-deployment")  # fallback, když model není v mapě

USAGE_LOG_PATH = os.getenv("USAGE_LOG_PATH", "")  # když nastavíš cestu, zapisuje se JSONL
USAGE_LOG_STDOUT = os.getenv("USAGE_LOG_STDOUT", "true").lower() == "true"

_usage_lock = asyncio.Lock()
def _now_iso():
    import datetime as _dt
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

async def log_usage_record(
    request: Request,
    record: dict
):
    """Zapíše jednu řádku s usage do JSONL + volitelně na stdout."""
    line = json.dumps(record, ensure_ascii=False)
    if USAGE_LOG_STDOUT:
        print(f"[USAGE] {line}")
    if USAGE_LOG_PATH:
        async with _usage_lock:
            with open(USAGE_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    from .DBDefinitions import UsageModel
    # session = getattr(request.state, "session")
    session_maker: AsyncSession = getattr(request.state, "session_maker")
    async with session_maker() as session:
        usage_row = UsageModel(
            api_key_id=record.get("token_id"),
            id=uuid.uuid4().hex,
            ts=datetime.datetime.now(tz=datetime.timezone.utc),
            stream=record.get("stream"),
            # token_id=record.get("token_id"),
            route=record.get("route"),
            deployment=record.get("deployment"),
            status=record.get("status"),
            stream_bytes=record.get("usage", {}).get("usage_counter"),
            
            prompt_tokens=record.get("usage", {}).get("prompt_tokens"),
            completion_tokens=record.get("usage", {}).get("completion_tokens"),
            total_tokens=record.get("usage", {}).get("total_tokens"),
            # usage=record.get("usage"),
            # idempotency_key=record.get("idempotency_key"),
            # client_ip=record.get("client_ip"),
            # req_x_request_id=record.get("req_x_request_id"),
            # upstream_request_id=record.get("upstream_request_id"),
            # ratelimit_remaining_tokens=record.get("ratelimit_remaining_tokens"),
            # ratelimit_limit_tokens=record.get("ratelimit_limit_tokens"),
        )
        # print(f"usage, {usage_row}")
        session.add(usage_row)
        await session.commit()
        # print(f"commit ok")
    return record

def make_usage_record(
    *,
    route: str,
    request: Request | None,
    deployment: str | None,
    # model: str | None,
    stream: bool,
    status: int | None,
    usage: dict | None,
    idempotency_key: str | None,
    upstream_headers: dict | None = None,
    extra: dict | None = None,
) -> dict:
    rec = {
        "ts": _now_iso(),
        "token_id": getattr(request.state, "token_row", None).id if request and hasattr(request.state, "token_row") else None,
        "route": route,
        "deployment": deployment,
        # "model": model,
        "stream": bool(stream),
        "status": status,
        "usage": usage or {},
        "idempotency_key": idempotency_key,
        "client_ip": getattr(request.client, "host", None) if request else None,
        "req_x_request_id": request.headers.get("X-Request-Id") if request else None,
    }
    if upstream_headers:
        # Azure/OpenAI často vrací X-Request-Id, X-Ratelimit*, apod.
        rec["upstream_request_id"] = upstream_headers.get("x-request-id") or upstream_headers.get("X-Request-Id")
        rec["ratelimit_remaining_tokens"] = upstream_headers.get("x-ratelimit-remaining-tokens")
        rec["ratelimit_limit_tokens"] = upstream_headers.get("x-ratelimit-limit-tokens")
    if extra:
        rec.update(extra)
    return rec


# endregion



# --- POMOCNÉ FUNKCE PRO OPENAI KOMPAT ---
def resolve_deployment_from_model(model_name: str) -> str:
    """
    Přeloží OpenAI model (např. 'gpt-4o') na Azure deployment (např. 'gpt4o-prod').
    Fallback: DEFAULT_DEPLOYMENT.
    """
    dep = MODEL_MAP.get(model_name)
    if not dep:
        dep = DEFAULT_DEPLOYMENT
    if not dep:
        # nechceme spadnout; ať je chyba čitelná
        raise HTTPException(
            status_code=400, 
            detail=(
                f"No deployment mapped for model '{model_name}'. "
                "\n"
                f"model map: {json.dumps(MODEL_MAP, indent=1)}"
                "\n"
                f"Provide OPENAI_COMPAT_MODEL_MAP or AZURE_OPENAI_DEFAULT_DEPLOYMENT."
            )
        )
    return dep

def azure_chat_url(deployment: str) -> str:
    return f"{UPSTREAM_ENDPOINT}/openai/deployments/{deployment}/chat/completions?api-version={UPSTREAM_API_VERSION}"

def azure_responses_url(deployment: str) -> str:
    return f"{UPSTREAM_ENDPOINT}/openai/deployments/{deployment}/responses?api-version={UPSTREAM_API_VERSION}"

def azure_embeddings_url(deployment: str) -> str:
    return f"{UPSTREAM_ENDPOINT}/openai/deployments/{deployment}/embeddings?api-version={UPSTREAM_API_VERSION}"


# ==== HTTP klient ====
client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECS, connect=10.0))

# provider instance
kv_provider: KeyVaultApiKeyProvider | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # await init_db()
    global kv_provider
    # init KV provider
    if KEYVAULT_URL:
        kv_provider = KeyVaultApiKeyProvider(
            vault_url=KEYVAULT_URL,
            secret_name=KEYVAULT_SECRET_NAME,
            ttl_seconds=KEY_CACHE_TTL
        )
        await kv_provider.start()    
    yield
    if kv_provider:
        await kv_provider.close()    
    
    await client.aclose()

app = FastAPI(
    title="Azure OpenAI Reverse Proxy",
    lifespan=lifespan
)

async def require_auth(request: Request):
    if PROXY_TOKEN:
        token = request.headers.get("X-Proxy-Token")
        if token != PROXY_TOKEN:
            raise HTTPException(status_code=401, detail="Unauthorized")

def redact(s: str, keep: int = 4) -> str:
    if not s: return ""
    return s[:keep] + "…" if len(s) > keep else "****"

def gen_idempotency_key(body: dict) -> str:
    # deterministicky z modelu + messages + function/tool calls …
    canonical = json.dumps(
        {k: body.get(k) for k in ("model", "messages", "tools", "tool_choice", "response_format", "temperature")},
        sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

async def backoff_delays(max_retries: int = 3):
    # 0.5s, 1s, 2s (+ jitter)
    base = 0.5
    for i in range(max_retries):
        delay = base * (2 ** i)
        yield delay + (0.05 * (i + 1))

def build_upstream_url(deployment: str) -> str:
    return f"{UPSTREAM_ENDPOINT}/openai/deployments/{deployment}/chat/completions?api-version={UPSTREAM_API_VERSION}"

def maybe_force_json_response(body: dict) -> dict:
    if FORCE_JSON_RESPONSE:
        rf = body.get("response_format")
        if not isinstance(rf, dict) or rf.get("type") not in ("json_object", "json_schema"):
            body["response_format"] = {"type": "json_object"}
    return body

def log_req(deployment: str, body: dict):
    meta = {k: body.get(k) for k in ("model","temperature","stream")}
    if LOG_PROMPTS:
        # POZOR: může obsahovat PII
        print(f"[REQ] dep={deployment} meta={meta} messages={json.dumps(body.get('messages', [])[:2], ensure_ascii=False)[:500]}…")
    else:
        # bezpečné minimum
        print(f"[REQ] dep={deployment} meta={meta} messages_count={len(body.get('messages', []))}")

def log_res(status: int, usage: Optional[dict]):
    print(f"[RES] status={status} usage={usage or {}}")

def extract_usage(json_obj: dict) -> Optional[dict]:
    if not isinstance(json_obj, dict):
        return None
    return (
        json_obj.get("usage")
        or (json_obj.get("response") or {}).get("usage")
        or (json_obj.get("output") or {}).get("usage")
    )

# NEW: získání aktuálního API klíče (KV -> fallback na ENV)
async def get_upstream_api_key(force_refresh: bool = False) -> str:
    if kv_provider:
        try:
            return await kv_provider.get(force_refresh=force_refresh)
        except Exception as e:
            # graceful degrade – když KV selže, sáhni po ENV
            print(f"[KV WARN] {type(e).__name__}: {e}. Falling back to ENV key.")
    if not UPSTREAM_API_KEY:
        raise HTTPException(status_code=500, detail="Missing upstream API key")
    return UPSTREAM_API_KEY

async def upstream_headers(request: Request, idemp: str | None) -> dict:
    # klient nemusí posílat Azure API key; proxy vloží vlastní
    api_key = await get_upstream_api_key()
    h = {
        "api-key": api_key,
        "Content-Type": "application/json",
    }
    # Idempotency-Key (Azure jej akceptuje; OpenAI standard taky)
    if idemp:
        h["Idempotency-Key"] = idemp
    # Forward-X pro audit
    if request.headers.get("X-Request-Id"):
        h["X-Request-Id"] = request.headers["X-Request-Id"]
    return h

async def forward_nonstream(
    request: Request,
    deployment: str,
    url: str, 
    headers: dict, 
    body: dict, 
    idempotency_key: str,
    routelabel: str="unknown"
) -> Response:
    # non-stream s retry
    last_exc = None
    had_auth_retry = False # zda už proběhl retry po auth chybě

    for delay in [0.0, *[d async for d in backoff_delays(3)]]:
        if delay:
            await asyncio.sleep(delay)
        try:
            r = await client.post(url, headers=headers, json=body)
            
            # pokud 401/403 -> jednorázový “hard refresh” klíče + retry
            if r.status_code in (401, 403) and not had_auth_retry:
                had_auth_retry = True
                try:
                    if kv_provider:  # invalidate & refresh
                        await kv_provider.invalidate()
                    # přestav hlavičky s novým klíčem
                    headers = await upstream_headers(request, idempotency_key)
                except Exception as _e:
                    last_exc = f"auth refresh failed: {_e}"
                    continue
                # a rovnou pokračuj dalším kolem smyčky
                continue

            if not should_retry(r.status_code):
                try:
                    data = r.json()
                except Exception:
                    data = None
                usage = extract_usage(data or {})
                usage_record = make_usage_record(
                    route=routelabel,
                    request=request,
                    deployment=deployment,
                    stream=False,
                    status=r.status_code,
                    usage=usage,
                    # route=routelabel,
                    idempotency_key=idempotency_key,
                    upstream_headers=r.headers
                )
                await log_usage_record(
                    request,
                    usage_record
                )
                log_res(f"OPENAI {routelabel} {r.status_code}", usage=usage)
                if data is not None:
                    return JSONResponse(status_code=r.status_code, content=data)
                return PlainTextResponse(status_code=r.status_code, content=r.text)
            else:
                last_exc = f"Upstream status {r.status_code}, retrying…"
        except httpx.HTTPError as e:
            last_exc = f"HTTP error: {e}"
    raise HTTPException(status_code=502, detail=f"Upstream failed after retries: {last_exc}")

# async def stream_generator(url: str, headers: dict, body: dict):
#     async with client.stream("POST", url, headers=headers, json=body) as r:
#         async for chunk in r.aiter_bytes():
#             yield chunk

# async def forward_stream(url: str, headers: dict, body: dict) -> Response:
#     # zachovej event-stream
#     return StreamingResponse(stream_generator(url, headers, body), media_type="text/event-stream")


async def forward_stream_with_usage(
    url: str, 
    headers: dict, 
    body: dict, 
    *,
    route: str, 
    request: Request, 
    deployment: str | None,
    # model: str | None, 
    idempotency_key: str | None,
    parse_responses_usage: bool
):
    stream_options = body.get("stream_options")
    if stream_options is None:
        stream_options = {
            "include_usage": True
        }
        body["stream_options"] = stream_options
    else:
        stream_options["include_usage"] = True

    
    async def _gen():
        usage_holder = None
        usage_counter = 0
        status_code = None
        upstream_headers = {}
        text_buf = ""
        had_auth_retry = False

        async def do_stream(hdrs: dict):
            return client.stream("POST", url, headers=hdrs, json=body)
    
        hdrs = headers
        while True:
            async with await do_stream(hdrs) as r:
                status_code = r.status_code
                upstream_headers = dict(r.headers)

                if status_code in (401, 403) and not had_auth_retry:
                    had_auth_retry = True
                    try:
                        if kv_provider:
                            await kv_provider.invalidate()
                        hdrs = await upstream_headers(request, idempotency_key)
                        continue  # otevři nový stream s čerstvým klíčem
                    except Exception as _e:
                        print(f"[AUTH RETRY FAIL] {type(_e).__name__}: {_e}")

                if status_code != 200:
                    # Přečti tělo a vrať jako chybovou odpověď před tím, než bys cokoliv yieldnul.
                    text = await r.aread()
                    yield text
                    break


                async for chunk in r.aiter_bytes():
                    # pošli dál
                    usage_counter += len(chunk)
                    yield chunk

                    if not parse_responses_usage:
                        continue

                    # zkus poskládat SSE bloky a číst "data: {...}"
                    try:
                        text_buf += chunk.decode("utf-8", errors="ignore")
                    except Exception:
                        continue

                    while "\n\n" in text_buf:
                        block, text_buf = text_buf.split("\n\n", 1)
                        for line in block.splitlines():
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if not payload or payload == "[DONE]":
                                continue
                            try:
                                evt = json.loads(payload)
                            except Exception:
                                continue

                            # OpenAI/Azure Responses: zakončovací event nese usage
                            if isinstance(evt, dict) and evt.get("type") == "response.completed":
                                explicit_usage = (evt.get("response") or {}).get("usage") or evt.get("usage")
                                usage_holder = explicit_usage or {"usage_counter": usage_counter}
                                print(f"[DEBUG] extracted usage from response.completed: {usage_holder}")
                            # OpenAI Chat stream se zapnutým include_usage: přijde extra blok s "usage"
                            elif isinstance(evt, dict) and "usage" in evt and isinstance(evt["usage"], dict):
                                usage_holder = evt["usage"]                            
                                print(f"[DEBUG] extracted usage from stream chunk: {usage_holder}")
                break  # úspěšně zpracován status 200, pokračuj ve streamu

        # zapiš usage/metu po skončení streamu
        try:
            await log_usage_record(
                request,
                make_usage_record(
                    route=route, 
                    request=request, 
                    deployment=deployment, 
                    # model=model,
                    stream=True, 
                    status=status_code, 
                    usage=usage_holder or {"usage_counter": usage_counter},
                    idempotency_key=idempotency_key, 
                    upstream_headers=upstream_headers
                )
            )
        except Exception as _e:
            print(f"[USAGE WARN] {type(_e).__name__}: {_e}")

    return StreamingResponse(
        _gen(), 
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

def should_retry(status: int) -> bool:
    return status in (429, 500, 502, 503, 504)

def gen_idempotency_key_embeddings(body: dict) -> str:
    canonical = json.dumps(
        {k: body.get(k) for k in ("model", "input", "encoding_format", "dimensions")},
        sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

async def openai_v1_chat_completions_general(
    request: Request, 
    useforce: bool, 
    deployment: str = None,
    endpoint: str = "chat",
    routelabel: str=""
):
    """
    Přijme OpenAI styl (model=..., messages=[...]) a přesměruje na Azure chat/completions.
    """
    if not OPENAI_COMPAT_ENABLED:
        raise HTTPException(status_code=404, detail="OpenAI-compatible mode disabled")

    await require_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    model = body.get("model")
    if model:
        deployment = resolve_deployment_from_model(model)    
    elif deployment is None:
        if isinstance(model, str) and model.strip():
            deployment = resolve_deployment_from_model(model)
        else:
            raise HTTPException(status_code=400, detail="Missing 'model' (or explicit deployment)")
    
    if useforce and endpoint == "chat":
        body = maybe_force_json_response(body)  # volitelný forcing JSON output
    idempotency_key = request.headers.get("Idempotency-Key") or gen_idempotency_key(body)

    # Log a hlavičky (vezmeme Azure api-key, ne Authorization)
    log_req(f"OPENAI {routelabel} -> {deployment}", body)
    # headers = {
    #     "api-key": UPSTREAM_API_KEY,
    #     "Content-Type": "application/json",
    #     "Idempotency-Key": idemp,
    # }
    headers = await upstream_headers(request, idempotency_key)
    if endpoint == "responses":
        url = azure_responses_url(deployment)
    else:
        # url = azure_chat_url(deployment)
        url = build_upstream_url(deployment)

    # stream?
    if body.get("stream"):
        return await forward_stream_with_usage(
            url, 
            headers, 
            body,
            request=request,
            route=routelabel,
            deployment=deployment,
            idempotency_key=idempotency_key,
            parse_responses_usage=True
        )
    return await forward_nonstream(
        request=request,
        deployment=deployment,
        url=url,
        headers=headers,
        body=body,
        idempotency_key=idempotency_key,
        routelabel=routelabel
    )


async def openai_v1_embeddings_general(
    request: Request,
    deployment: str | None = None,
    routelabel: str = "openai_v1_embeddings"
):
    if not OPENAI_COMPAT_ENABLED:
        raise HTTPException(status_code=404, detail="OpenAI-compatible mode disabled")

    await require_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # mapování model -> deployment (stejný mechanizmus jako u chatů)
    model = body.get("model")
    if model:
        deployment = resolve_deployment_from_model(model)
    elif deployment is None:
        raise HTTPException(status_code=400, detail="Missing 'model' (or explicit deployment)")

    # idempotency (deterministické podle vstupu)
    idempotency_key = request.headers.get("Idempotency-Key") or gen_idempotency_key_embeddings(body)

    # log + hlavičky
    print(f"[REQ] EMBEDDINGS -> {deployment} dims={body.get('dimensions')} encoding={body.get('encoding_format')}")
    headers = await upstream_headers(request, idempotency_key)

    url = azure_embeddings_url(deployment)
    return await forward_nonstream(
        request=request,
        deployment=deployment,
        url=url,
        headers=headers,
        body=body,
        idempotency_key=idempotency_key,
        routelabel=routelabel
    )

# ============= ROUTES =============

async def get_token(
    authorization: str = Header(None, alias="Authorization")
):
    # print(f"Authorization header: {authorization}")
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    # print(f"Extracted token: {token}")
    return token

async def get_token_row(
    # session: Any = Depends(get_session), 
    session_maker: Any = Depends(get_session_maker),
    token: str = Depends(get_token)
):
    from .DBDefinitions import ApiKeyModel, hash_token
    hashed_token = hash_token(token) if token else None
    # print(f"Looking for token hash: {redact(hashed_token)}")
    # statement = select(ApiKey)
    # results = await session.execute(statement)
    # for result in results:
    #     print(f"DB ApiKey row: {result}")

    statement = select(ApiKeyModel).filter_by(key_hash=hashed_token, is_active=True)
    async with session_maker() as session:
        results = await session.execute(statement)
        result = next(results, None)
        # print(f"token: {token}, result: {result}")
        if result:

            # from sqlalchemy.inspection import inspect as sa_inspect        
            # def asdict_columns(obj) -> dict[str, Any]:
            #     insp = sa_inspect(obj)
            #     # `mapper.column_attrs` = jen sloupce (včetně PK/FK), bez relationshipů
            #     return {attr.key: getattr(obj, attr.key) for attr in insp.mapper.column_attrs}
            first = result[0]
            # print(f"first: {first}")
            # fist_json = dataclasses.asdict(first)
            # print(f"first as dict: {fist_json}")
            # print(f"{first.expires_at=}, {first.user_id=}")
            current_datetime = datetime.datetime.now(tz=None)
            if first.expires_at and first.expires_at < current_datetime:
                first.is_active = False
                await session.commit()
                return None
            first.last_used_at = current_datetime
            await session.commit()
            return first
    return None



@app.post("/openai/deployments/{deployment}/chat/completions")
async def chat_completions(
    deployment: str, 
    request: Request, 
    # authorization: str = Cookie(None),
    # session: Any = Depends(get_session),
    session_maker: Any = Depends(get_session_maker),
    token_row: Any = Depends(get_token_row)
):
    if not token_row:
        raise HTTPException(status_code=401, detail="Unauthorized API key")
    request.state.token_row = token_row  # pro případné další použití v middlewaru apod.
    # request.state.session = session  # pro logování usage
    request.state.session_maker = session_maker  # pro logování usage
    return await openai_v1_chat_completions_general(
        request=request,
        useforce=True,
        deployment=deployment,
        endpoint="chat",
        routelabel="chat_completions",
    ) 

@app.post("/openai/deployments/{deployment}/embeddings")
async def embeddings_azure(
    deployment: str,
    request: Request,
    # session: Any = Depends(get_session),
    session_maker: Any = Depends(get_session_maker),
    token_row: Any = Depends(get_token_row)
):
    if not token_row:
        raise HTTPException(status_code=401, detail="Unauthorized API key")
    request.state.token_row = token_row
    # request.state.session = session
    request.state.session_maker = session_maker  # pro logování usage
    return await openai_v1_embeddings_general(
        request=request,
        deployment=deployment,
        routelabel="embeddings"
    )

# ---------- OpenAI-compatible: /v1/models ----------
@app.get("/v1/models")
@app.get("/models")  # volitelně alias
async def list_models_openai(
    token_row: Any = Depends(get_token_row)
):
    """
    Vrátí seznam 'modelů' podle klíčů v OPENAI_COMPAT_MODEL_MAP.
    OpenAI vrací object=list a položky s object=model.
    """
    if not token_row:
        raise HTTPException(status_code=401, detail="Unauthorized API key")
    
    items = []
    for mid in (MODEL_MAP.keys() or []):
        items.append({"id": mid, "object": "model", "created": 0, "owned_by": "azure-proxy"})
    # fallback: když není mapa, ale je DEFAULT_DEPLOYMENT, ukaž aspoň 1 id
    if not items and DEFAULT_DEPLOYMENT:
        items = [{"id": "gpt-azure", "object": "model", "created": 0, "owned_by": "azure-proxy"}]
    result = {"object": "list", "data": items}
    print(f"list_models_openai: {json.dumps(result, indent=2)}")
    return result

# ---------- OpenAI-compatible: /v1/chat/completions ----------
@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def openai_v1_chat_completions(
    request: Request,
    # session: Any = Depends(get_session),
    session_maker: Any = Depends(get_session_maker),
    token_row: Any = Depends(get_token_row)
):
    """
    Přijme OpenAI styl (model=..., messages=[...]) a přesměruje na Azure chat/completions.
    """
    if not token_row:
        raise HTTPException(status_code=401, detail="Unauthorized API key")
    
    request.state.token_row = token_row  # pro případné další použití v middlewaru apod.
    # request.state.session = session  # pro logování usage
    request.state.session_maker = session_maker  # pro logování usage
    return await openai_v1_chat_completions_general(
        request=request,
        useforce=True,
        endpoint="chat",
        routelabel="openai_v1_chat_completions"
    )

# ---------- OpenAI-compatible: /v1/responses ----------
@app.post("/v1/responses")
@app.post("/responses")
async def openai_v1_responses(
    request: Request,
    session_maker: Any = Depends(get_session_maker),
    token_row: Any = Depends(get_token_row)
):
    """
    OpenAI Responses API (model=..., input=[...]).
    Přesměruje na Azure /responses (2024-12-01-preview a novější).
    """
    request.state.token_row = token_row  # pro případné další použití v middlewaru apod.
    request.state.session_maker = session_maker  # pro logování usage
    return await openai_v1_chat_completions_general(
        request=request,
        useforce=False,
        endpoint="responses",
        routelabel="openai_v1_responses"
    )
    
@app.post("/v1/embeddings")
@app.post("/embeddings")  # volitelný alias
async def embeddings_openai(
    request: Request,
    # session: Any = Depends(get_session),
    session_maker: Any = Depends(get_session_maker),
    token_row: Any = Depends(get_token_row)
):
    if not token_row:
        raise HTTPException(status_code=401, detail="Unauthorized API key")
    request.state.token_row = token_row
    # request.state.session = session
    request.state.session_maker = session_maker  # pro logování usage
    return await openai_v1_embeddings_general(
        request=request,
        deployment=None,
        routelabel="openai_v1_embeddings"
    )

@app.get("/healthcheck")
async def healthcheck():
    return {"ok": True}

@app.post("/llmtest/{deployment}")
async def llmtest(
    deployment: str,
    query: str,
    apikey: str | None = Header(..., alias="apikey"),
):
    
    from openai import AzureOpenAI, AsyncAzureOpenAI
    from openai.types.chat import ChatCompletion
    from openai.resources.chat.completions import AsyncCompletions
    UPSTREAM_ENDPOINT = "http://localhost:8000/"
    client = AsyncAzureOpenAI(
        azure_endpoint=UPSTREAM_ENDPOINT,
        azure_deployment=deployment,  # tvůj deployment name
        api_key=apikey or UPSTREAM_API_KEY,
        api_version=UPSTREAM_API_VERSION
    )

    azureCompletions: AsyncCompletions = client.chat.completions
    resp = await azureCompletions.create(
            model=deployment,          # = deployment name
            messages=[
                {"role": "system", "content": "You are assistent."},
                {"role": "user", "content": query}
            ],
            temperature=0.8,
            max_tokens=1000,
        )
    asjson = resp.model_dump()
    return asjson

@app.middleware("http")
async def access_log(request: Request, call_next):
    # --- request info ---
    url = str(request.url)                   # plná URL
    path = request.url.path                  # jen /cesta
    query = request.url.query                # bez '?'
    method = request.method
    scheme = request.scope.get("scheme")
    http_ver = request.scope.get("http_version")
    client_host, client_port = (request.client.host, request.client.port) if request.client else (None, None)
    ua = request.headers.get("user-agent", "")
    xff = request.headers.get("x-forwarded-for")
    req_id = request.headers.get("x-request-id")

    print(f"[REQ] {method} {url} hv={http_ver} client={client_host}:{client_port} ua={ua[:80]} xff={xff} req_id={req_id}")

    # --- timing ---
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        dur = (time.perf_counter() - start)
    # --- response info ---
    status = getattr(response, "status_code", None)
    clen = response.headers.get("content-length")
    ctype = response.headers.get("content-type")
    upstream_id = response.headers.get("x-request-id")  # pokud ho upstream přepošleš

    # přidej header s časem
    response.headers["X-Process-Time"] = f"{dur:.6f}"

    print(f"[RES] {method} {path}{'?' + query if query else ''} -> {status} len={clen} type={ctype} t={dur:.3f}s upstream_id={upstream_id}")
    return response

# from gui import init_gui
# init_gui(app)

# from management import router
# app.include_router(router)
if __name__ == "__main__":
    # Pokud chceš TLS přímo v uvicorn:
    # uvicorn.run(app, host=PROXY_BIND, port=PROXY_PORT, ssl_keyfile="key.pem", ssl_certfile="cert.pem")
    uvicorn.run(app, host=PROXY_BIND, port=PROXY_PORT)
