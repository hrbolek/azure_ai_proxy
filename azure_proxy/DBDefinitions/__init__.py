# db.py
from __future__ import annotations
import os, json, math, hashlib, secrets
from dataclasses import field
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Optional, Iterable, Literal
from uuid import uuid4

import sqlalchemy
from sqlalchemy import (
    String, Integer, Boolean, DateTime, ForeignKey, Float, Text,
    Index, func, select, and_, or_, case
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    Mapped, mapped_column, relationship, declarative_base, 
    MappedAsDataclass
)

from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import MappedAsDataclass

from .BaseModel import BaseModel
from .UserModel import UserModel
from .ApiKeyModel import ApiKeyModel
from .UsageModel import UsageModel


# ---------- DB engine ----------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./proxy.db")

# asyncEngine = create_async_engine(DATABASE_URL, future=True, echo=False)
# AsyncSessionMaker = async_sessionmaker(asyncEngine, expire_on_commit=False)

asyncEngine = None
AsyncSessionMaker = None

async def init_db() -> None:
    """Create tables if not exist."""
    async with asyncEngine.begin() as conn:
        await conn.run_sync(BaseModel.metadata.create_all)

async def get_session_maker() -> AsyncSession:
    global AsyncSessionMaker
    global asyncEngine
    if asyncEngine is None:
        asyncEngine = create_async_engine(DATABASE_URL, future=True, echo=False)
        
        async with asyncEngine.begin() as conn:
            try:
                # await conn.run_sync(BaseModel.metadata.drop_all)
                await conn.run_sync(BaseModel.metadata.create_all)
                print("BaseModel.metadata.create_all finished")
            except sqlalchemy.exc.NoReferencedTableError as e:
                print(e)
                print("Unable automaticaly create tables")
                raise

    if AsyncSessionMaker is None:
        AsyncSessionMaker = async_sessionmaker(asyncEngine, expire_on_commit=False)

    return AsyncSessionMaker

async def get_session() -> AsyncIterator[AsyncSession]:
    global AsyncSessionMaker
    global asyncEngine
    if asyncEngine is None:
        asyncEngine = create_async_engine(DATABASE_URL, future=True, echo=False)
        
        async with asyncEngine.begin() as conn:
            try:
                # await conn.run_sync(BaseModel.metadata.drop_all)
                await conn.run_sync(BaseModel.metadata.create_all)
                print("BaseModel.metadata.create_all finished")
            except sqlalchemy.exc.NoReferencedTableError as e:
                print(e)
                print("Unable automaticaly create tables")
                raise

    if AsyncSessionMaker is None:
        AsyncSessionMaker = async_sessionmaker(asyncEngine, expire_on_commit=False)

    async with AsyncSessionMaker() as s:
        print("DB session opened")
        yield s
        await s.commit()
        print("DB session committed and closed")

# ---------- Security / hashing ----------
API_KEY_PREFIX_LEN = int(os.getenv("API_KEY_PREFIX_LEN", "8"))
API_KEY_BYTES = int(os.getenv("API_KEY_BYTES", "32"))  # entropy ~ 256 bits
API_KEY_PEPPER = os.getenv("API_KEY_PEPPER", "")       # optional server-side secret for hashing

def hash_token(raw_token: str) -> str:
    # cheap & compatible; můžeš nahradit za bcrypt/argon2
    return hashlib.sha256((API_KEY_PEPPER + raw_token).encode("utf-8")).hexdigest()

def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext, prefix, hash)."""
    # URL-safe, bez oddělovačů
    raw = secrets.token_urlsafe(API_KEY_BYTES).replace("-", "").replace("_", "")
    prefix = raw[:API_KEY_PREFIX_LEN]
    return raw, prefix, hash_token(raw)



# ---------- API key auth helpers ----------
class ApiKeyAuthError(Exception): ...

async def get_api_key_by_token(db: AsyncSession, raw_token: str) -> Optional[ApiKeyModel]:
    """Najde aktivní ApiKey podle plaintext tokenu (prefix + hash compare)."""
    if not raw_token:
        return None
    prefix = raw_token[:API_KEY_PREFIX_LEN]
    q = await db.execute(
        select(ApiKeyModel).where(
            ApiKeyModel.prefix == prefix,
            ApiKeyModel.is_active == True,  # noqa: E712
        )
    )
    candidates: Iterable[ApiKeyModel] = q.scalars().all()
    target_hash = hash_token(raw_token)
    for k in candidates:
        # konstantní čas by tady řešil hmac.compare_digest
        if secrets.compare_digest(k.key_hash, target_hash):
            # expirace?
            if k.expires_at and k.expires_at < datetime.now(timezone.utc):
                return None
            return k
    return None

async def require_api_key(
    db: AsyncSession,
    token: Optional[str],
) -> ApiKeyModel:
    """Ověří klíč z hlavičky a vrátí ApiKey; jinak vyhodí ApiKeyAuthError."""
    if not token:
        raise ApiKeyAuthError("Missing API key")
    key = await get_api_key_by_token(db, token)
    if not key:
        raise ApiKeyAuthError("Invalid or inactive API key")
    return key

# ---------- Usage recording ----------
# async def record_usage(
#     db: AsyncSession,
#     *,
#     api_key: ApiKeyModel,
#     ts: Optional[datetime] = None,
#     route: Optional[str] = None,
#     deployment: Optional[str] = None,
#     status: Optional[int] = None,
#     stream: bool = False,
#     prompt_tokens: Optional[int] = None,
#     completion_tokens: Optional[int] = None,
#     total_tokens: Optional[int] = None,
#     stream_bytes: Optional[int] = None,
#     cost_usd: Optional[float] = None,
#     meta: Optional[dict] = None,
# ) -> UsageModel:
#     u = UsageModel(
#         api_key_id=api_key.id,
#         ts=ts or datetime.now(timezone.utc),
#         route=route,
#         deployment=deployment,
#         status=status,
#         stream=stream,
#         prompt_tokens=prompt_tokens,
#         completion_tokens=completion_tokens,
#         total_tokens=total_tokens,
#         stream_bytes=stream_bytes,
#         cost_usd=cost_usd,
#         meta_json=json.dumps(meta, ensure_ascii=False) if meta else None,
#     )
#     db.add(u)
#     api_key.last_used_at = datetime.now(timezone.utc)
#     await db.commit()
#     await db.refresh(u)
#     return u

# ---------- Aggregations (consumption over time) ----------
Bucket = Literal["hour", "day"]

def _date_bucket_expr(bucket: Bucket):
    # portable-ish bucket expr (SQLite vs Postgres)
    if DATABASE_URL.startswith("sqlite"):
        if bucket == "hour":
            return func.strftime("%Y-%m-%dT%H:00:00Z", UsageModel.ts)
        return func.strftime("%Y-%m-%d", UsageModel.ts)
    # default: Postgres-friendly date_trunc
    return func.to_char(func.date_trunc(bucket, UsageModel.ts), "YYYY-MM-DD\"T\"HH24:00:00Z" if bucket=="hour" else "YYYY-MM-DD")

async def usage_timeseries_for_key(
    db: AsyncSession,
    *,
    api_key_id: str,
    since: datetime,
    until: datetime,
    bucket: Bucket = "day",
):
    b = _date_bucket_expr(bucket).label("bucket")

    # q = (
    #     select(UsageModel)#.filter_by(api_key_id=api_key_id)
    # )
    # res = await db.execute(q)
    # rows = res.scalars().all()
    # print(f"api_key_id: {api_key_id}, {type(api_key_id)}")
    # for row in rows:
    #     print(f"usage row: {row}, {row.api_key_id==api_key_id}")

    q = (
        select(
            b,
            func.count().label("requests"),
            func.sum(UsageModel.prompt_tokens).label("prompt_tokens"),
            func.sum(UsageModel.completion_tokens).label("completion_tokens"),
            func.sum(UsageModel.total_tokens).label("total_tokens"),
            func.sum(UsageModel.stream_bytes).label("stream_bytes"),
            func.sum(UsageModel.cost_usd).label("cost_usd"),
        )
        .where(
            UsageModel.api_key_id == api_key_id,
            UsageModel.ts >= since, 
            UsageModel.ts < until
        )
        .group_by(b)
        .order_by(b.asc())
    )
    res = await db.execute(q)
    rows = res.mappings().all()
    result = [dict(r) for r in rows]
    print(result)
    return  result

async def backupDB(asyncSessionMaker, filename="./systemdata.backup.json"):
    import sqlalchemy
    import dataclasses
    import json

    from .BaseModel import BaseModel
    data = []
    dbModels = [mapper.class_ for mapper in BaseModel.registry.mappers]
    async with asyncSessionMaker() as session:
        for model in dbModels:
            sqlquery = sqlalchemy.select(model)
            rows = await session.execute(sqlquery)
            # vsechny radky do dict
            rowsdict = {}
            for row in rows:
                # print(row)
                asdict = dataclasses.asdict(row[0])
                id = asdict.get("id", None)
                if id is None: continue
                rowsdict[id] = asdict
            # vsechny primarní klice do ids
            ids = set(rowsdict.keys())
            todo = set()
            done = set()
            chunk_id = 0
            while len(done) < len(ids):
                for row in rowsdict.values():
                    id = row.get("id", None)
                    if id in done: continue
                    skip_this_id = False
                    for key, value in row.items():
                        if key == "id": continue
                        # if not isinstance(value, IDType): continue
                        if value is None: continue
                        if value not in ids: continue
                        if value not in done: 
                            # print(row, key, value)
                            skip_this_id = True
                            break
                            # primarni klic je zpracovatelny, nemame zavislost na nezpracovanych klicich
                    if skip_this_id: continue
                    row["_chunk"] = chunk_id
                    todo.add(id)
                print(f"{model.__tablename__} chunk {chunk_id} todo/done/all {len(todo)}/{len(done)}/{len(ids)}")
                if len(todo) == 0: break
                done = done.union(todo)
                todo = set()
                chunk_id += 1
            data.append({
                model.__tablename__: list(rowsdict.values())
            })
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False, default=str)
    
    print("backup done", flush=True)