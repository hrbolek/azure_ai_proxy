# management.py
import typing
import os, json, asyncio, time
from datetime import datetime, timedelta, timezone
from typing import Optional, Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Header, Cookie, Request, status
from pydantic import BaseModel, Field
from jose import jwt

from sqlalchemy import or_, and_

from .DBDefinitions import (
    get_session, get_session_maker,
    AsyncSession, 
    init_db, backupDB,
    UserModel, ApiKeyModel,
    generate_api_key, hash_token,
    # record_usage, 
    usage_timeseries_for_key,
    API_KEY_PREFIX_LEN,
)

from .gui import session_store

async def lifespan(app):
    yield
    session_maker = await get_session_maker()
    await backupDB(session_maker)
    
    pass

router = APIRouter(
    prefix="/management", 
    tags=["management"],
    lifespan=lifespan
)

# ---------- Entra ID (Azure AD) OIDC config ----------
TENANT_ID = os.getenv("AZURE_TENANT_ID", "")
CLIENT_ID = os.getenv("AZURE_APP_CLIENT_ID", "")  # audience
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
OPENID_CONFIG_URL = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0/.well-known/openid-configuration"

_jwks_cache: dict = {}
_jwks_cache_expiry: float = 0.0
_openid_cache: Optional[dict] = None
_openid_cache_expiry: float = 0.0

async def _fetch_openid_and_jwks() -> tuple[dict, dict]:
    global _openid_cache, _openid_cache_expiry, _jwks_cache, _jwks_cache_expiry
    now = time.time()
    if not _openid_cache or now > _openid_cache_expiry:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(OPENID_CONFIG_URL)
            r.raise_for_status()
            _openid_cache = r.json()
            _openid_cache_expiry = now + 60 * 60  # 1h
    jwks_uri = _openid_cache["jwks_uri"]
    if not _jwks_cache or now > _jwks_cache_expiry:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(jwks_uri)
            r.raise_for_status()
            _jwks_cache = r.json()
            _jwks_cache_expiry = now + 60 * 30  # 30m
    return _openid_cache, _jwks_cache

class Principal(BaseModel):
    sub: str
    oid: Optional[str] = None
    upn: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    roles: list[str] = Field(default_factory=list)

# async def get_principal(authorization: Annotated[Optional[str], Header(alias="Authorization")] = None) -> Principal:
#     if not authorization or not authorization.lower().startswith("bearer "):
#         raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
#     token = authorization.split(" ", 1)[1].strip()
#     openid, jwks = await _fetch_openid_and_jwks()
#     try:
#         claims = jwt.decode(
#             token,
#             jwks,  # jose umí předat přímo dict s "keys"
#             algorithms=["RS256", "RS512"],
#             audience=CLIENT_ID,
#             issuer=ISSUER,
#             options={"verify_at_hash": False},
#         )
#     except Exception as e:
#         raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
#     # map claims
#     principal = Principal(
#         sub=claims.get("sub"),
#         oid=claims.get("oid") or claims.get("sub"),
#         upn=claims.get("upn") or claims.get("preferred_username"),
#         email=claims.get("email") or claims.get("preferred_username"),
#         name=claims.get("name"),
#         roles=list(claims.get("roles", [])),
#     )
#     return principal

async def get_principal(
    request: Request,
    sid: Annotated[Optional[str], Cookie()] = None,
    authorization: Annotated[Optional[str], Header(alias="Authorization")] = None,
) -> Principal:
    # 1) Primárně se pokus o přihlášení přes session cookie (NiceGUI flow)
    if sid:
        session = session_store.get(sid)
        if session:
            # Volitelně „touch“ – prodloužení TTL při aktivitě (pokud to tvůj store umí)
            # session_store.touch(sid, ttl_sec=1800)
            return Principal(
                sub=session.get("user_oid") or session.get("upn") or session.get("name"),
                oid=session.get("user_oid"),
                upn=session.get("upn"),
                email=session.get("upn") or None,
                name=session.get("name") or "user",
                # roles=sess.get("roles", []),
            )
        # máme sid, ale session nebyla nalezena/expirovala
        # pokračuj na Bearer fallback (nebo vrať 401)

    # 2) Fallback: čisté API s Bearer tokenem (např. Postman)
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing session or bearer token")

    token = authorization.split(" ", 1)[1].strip()
    openid, jwks = await _fetch_openid_and_jwks()  # stejné jako dřív

    try:
        claims = jwt.decode(
            token,
            jwks,  # jose umí přímo dict s "keys"
            algorithms=["RS256", "RS512"],
            audience=CLIENT_ID,
            issuer=ISSUER,
            options={"verify_at_hash": False},
        )
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    principal = Principal(
        sub=claims.get("sub"),
        oid=claims.get("oid") or claims.get("sub"),
        upn=claims.get("upn") or claims.get("preferred_username"),
        email=claims.get("email") or claims.get("preferred_username"),
        name=claims.get("name"),
        roles=list(claims.get("roles", [])),
    )
    return principal

# role helper
def require_admin(p: Principal):
    if "Proxy.Admin" not in (p.roles or []):
        raise HTTPException(status_code=403, detail="Admin role required")

# ----- Pydantic schemas -----
class CreateKeyRequest(BaseModel):
    user_id: str = Field(default=...)  # pro koho je klíč vytvořen
    name: Optional[str] = None
    expires_in_days: Optional[int] = Field(default=14, ge=1, le=360)
    rate_limit_per_minute: Optional[int] = Field(default=None, ge=1)

class AdminCreateKeyRequest(BaseModel):
    user_id: str = Field(default=...)  # pro koho je klíč vytvořen
    name: Optional[str] = None
    expires_in_days: Optional[int] = Field(default=14, ge=1, le=14)
    rate_limit_per_minute: Optional[int] = Field(default=None, ge=1)

class CreateKeyResponse(BaseModel):
    id: str
    user_id: str
    token: str  # plaintext (once!)
    prefix: str
    name: Optional[str]
    is_active: bool
    created_at: datetime
    expires_at: Optional[datetime]

class ApiKeyInfo(BaseModel):
    id: str
    prefix: str
    name: Optional[str]
    is_active: bool
    created_at: datetime
    expires_at: Optional[datetime]
    last_used_at: Optional[datetime]
    rate_limit_per_minute: Optional[int]

class UsagePoint(BaseModel):
    bucket: str
    requests: int
    prompt_tokens: Optional[int] = 0
    completion_tokens: Optional[int] = 0
    total_tokens: Optional[int] = 0
    stream_bytes: Optional[int] = 0
    cost_usd: Optional[float] = 0.0

# ----- ensure user -----
async def _ensure_user(db: AsyncSession, p: Principal) -> UserModel:
    from sqlalchemy import select
    res = await db.execute(select(UserModel).where(or_(UserModel.oid == p.oid, UserModel.email == p.email)))
    user = res.scalars().first()
    if not user:
        user = UserModel(oid=p.oid, email=p.email, display_name=p.name)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        # lehký update jména/emailu
        upd = False
        if p.email and p.email != user.email:
            user.email = p.email
            upd = True
        if p.name and p.name != user.display_name:
            user.display_name = p.name
            upd = True
        if upd:
            await db.commit()
    return user

# ---------- Routes ----------
@router.post("/apikeys", response_model=CreateKeyResponse)
async def create_api_key(
    req: CreateKeyRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_session),
):
    user = await _ensure_user(db, principal)
    plaintext, prefix, h = generate_api_key()
    expires_at = None
    if req.expires_in_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=req.expires_in_days)
    
    if user.is_admin is False:
        req.user_id = user.id  # non-admin může vytvářet klíče jen sám sobě
    # if user.id != req.user_id and user.is_admin is not True:
    #     raise HTTPException(403, "Cannot create key for another user")

    key = ApiKeyModel(
        user_id=user.id,
        prefix=prefix,
        key_hash=h,
        name=req.name,
        is_active=True,
        expires_at=expires_at,
        rate_limit_per_minute=req.rate_limit_per_minute,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return CreateKeyResponse(
        id=key.id,
        user_id=key.user_id,
        token=plaintext,  # show once!
        prefix=prefix,
        name=key.name,
        is_active=key.is_active,
        created_at=key.created_at,
        expires_at=key.expires_at,
    )

@router.get("/apikeys", response_model=list[ApiKeyInfo])
async def list_my_keys(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_session),
):
    user = await _ensure_user(db, principal)
    from sqlalchemy import select
    res = await db.execute(select(ApiKeyModel).where(ApiKeyModel.user_id == user.id).order_by(ApiKeyModel.created_at.desc()))
    keys = res.scalars().all()
    return [
        ApiKeyInfo(
            id=k.id, prefix=k.prefix, name=k.name, is_active=k.is_active,
            created_at=k.created_at, expires_at=k.expires_at,
            last_used_at=k.last_used_at, rate_limit_per_minute=k.rate_limit_per_minute
        )
        for k in keys
    ]

@router.post("/apikeys/{key_id}/disable")
async def disable_key(
    key_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_session),
):
    # owner-only (unless admin)
    user = await _ensure_user(db, principal)

    from sqlalchemy import select
    query = select(ApiKeyModel, UserModel).join(UserModel, ApiKeyModel.user_id == UserModel.id).where( or_(ApiKeyModel.id == key_id, UserModel.id == user.id) )
    if user.is_admin:
        query = select(ApiKeyModel, UserModel).join(UserModel, ApiKeyModel.user_id == UserModel.id).where( ApiKeyModel.id == key_id) 

    res = await db.execute(query)
    row = res.first()
    if not row:
        raise HTTPException(403, "Unauthorized or key not found")
    key: ApiKeyModel = row[0]

    key.is_active = False
    await db.commit()
    return {"ok": True}

@router.post("/apikeys/{key_id}/enable")
async def enable_key(
    key_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_session),
):
    from sqlalchemy import select
    user = await _ensure_user(db, principal)
    query = select(ApiKeyModel, UserModel).join(UserModel, ApiKeyModel.user_id == UserModel.id).where(or_(ApiKeyModel.id == key_id, UserModel.id == user.id))
    if user.is_admin:
        query = select(ApiKeyModel, UserModel).join(UserModel, ApiKeyModel.user_id == UserModel.id).where( ApiKeyModel.id == key_id)

    res = await db.execute(query)
    row = res.first()
    if not row:
        raise HTTPException(404, "Unauthorized or key not found")
    key: ApiKeyModel = row[0]
    key.is_active = True
    await db.commit()
    return {"ok": True}

class UsageQuery(BaseModel):
    key_id: Optional[str] = None
    user_id: Optional[str] = None
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    bucket: Literal["hour", "day"] = "day"

@router.post("/usage", response_model=list[UsagePoint])
async def my_usage(
    q: UsageQuery,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_session),
):
    # ownership check
    from sqlalchemy import select
    user = await _ensure_user(db, principal)
    query = select(ApiKeyModel, UserModel).join(UserModel, ApiKeyModel.user_id == UserModel.id)
    if q.key_id:
        if user.is_admin:
            query = query.where( ApiKeyModel.id == q.key_id)
        else:
            query = query.where(and_(ApiKeyModel.id == q.key_id, UserModel.id == user.id))
    if q.user_id:
        query = query.where(ApiKeyModel.user_id == q.user_id)
    res = await db.execute(query)
    row = res.first()
    if not row:
        raise HTTPException(404, "Unauthorized or key not found")
    key: ApiKeyModel = row[0]
    since = q.since or (datetime.now(timezone.utc) - timedelta(days=30))
    until = q.until or datetime.now(timezone.utc)

    rows = await usage_timeseries_for_key(
        db, api_key_id=q.key_id, user_id=q.user_id, since=since, until=until, bucket=q.bucket
    )
    # Pydantic casting
    result = [UsagePoint(**r) for r in rows]
    return result


class UserInfo(BaseModel):
    id: str
    oid: Optional[str]
    email: Optional[str]
    display_name: Optional[str]
    is_active: Optional[bool] = True
    is_admin: Optional[bool] = False
    created_at: Optional[datetime] = None

@router.get("/users", response_model=list[UserInfo])
async def list_users(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_session),
):
    from sqlalchemy import select
    res = await db.execute(select(UserModel))
    users = res.scalars().all()
    return [
        UserInfo(
            id=u.id,
            oid=u.oid,
            email=u.email,
            display_name=u.display_name,
            is_active=getattr(u, "is_active", True),
            is_admin=getattr(u, "is_admin", False),
            created_at=getattr(u, "created_at", None)
        ) for u in users
    ]
