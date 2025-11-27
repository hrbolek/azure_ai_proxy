# gui.py
from __future__ import annotations
import os, asyncio, json
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse
        
from authlib.integrations.starlette_client import OAuth
from nicegui import ui, app as ngapp

from .ExpiringDict import ExpiringDict
TENANT_ID = os.getenv("AZURE_TENANT_ID", "")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")  # pro public flow lze vynechat
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-please")
REDIRECT_PATH = os.getenv("GUI_REDIRECT_PATH", "/auth")  # musí být registrováno v Entra

session_store: dict[str, dict] = ExpiringDict()


def init_gui(fastapi_app: FastAPI) -> None:
    """Připojí NiceGUI k existujícímu FastAPI a zaregistruje stránky + OIDC."""
    # 1) Session pro OAuth (Authlib)
    fastapi_app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

    # 2) Napoj NiceGUI na FastAPI
    ui.run_with(
        fastapi_app,
        favicon="💙",
        mount_path="/mgmt",
        dark=None,
        tailwind=True,
        storage_secret="SUPER-SECRET"
    )

    # 3) OAuth client (Entra ID OpenID provider)
    oauth = OAuth()
    oauth.register(
        name="entra",
        server_metadata_url=f"https://login.microsoftonline.com/{TENANT_ID}/v2.0/.well-known/openid-configuration",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET or None,
        client_kwargs={"scope": "openid profile email offline_access"},
        redirect_uri=PUBLIC_BASE_URL + REDIRECT_PATH,
    )

    # 4) Interní ASGI klient – volání management API bez externího HTTP
    transport = httpx.ASGITransport(app=fastapi_app)
    api = httpx.AsyncClient(transport=transport, base_url="http://internal", timeout=20.0)

    def _auth_header() -> dict:
        tok = ngapp.storage.user.get("id_token") or ngapp.storage.user.get("access_token")
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    async def _api_get(path: str, cookies={}):
        r = await api.get(path, headers=_auth_header(), cookies=cookies)
        r.raise_for_status()
        return r.json()

    # async def _api_post(path: str, cookies={}, json_body: dict | None = None):
    #     r = await api.post(
    #         path, 
    #         headers=_auth_header(), 
    #         cookies=cookies,
    #         json=json_body or {}
    #     )
    #     r.raise_for_status()
    #     return r.json() if r.content else {"ok": True}
    
    async def _api_post(path: str, cookies=None, json_body: dict | None = None):
        r = await api.post(path, headers=_auth_header(), cookies=cookies or {}, json=json_body or {})
        if r.is_error:
            # uvidíš konkrétní Pydantic errors
            detail = r.text
            try:
                detail = r.json()
            except Exception:
                pass
            raise RuntimeError(f"{r.status_code} {r.request.url} -> {detail}")
        return r.json() if r.content else {"ok": True}
    
    # --------- OIDC routy ---------
    @fastapi_app.get("/auth/login")
    async def login(request: Request):
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host   = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
        # TODO predelat
        redirect = f"{scheme}://{host}{REDIRECT_PATH}"
        print(f"Login redirect: {redirect}")
        return await oauth.entra.authorize_redirect(request, redirect) # PUBLIC_BASE_URL + REDIRECT_PATH



    @fastapi_app.get(REDIRECT_PATH)
    async def auth_callback(request: Request):
        print("In auth callback")
        token = await oauth.entra.authorize_access_token(request)
        
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        is_https = scheme == "https"
    
        # 1) claims z ID tokenu (Authlib to většinou dává sem)
        claims = token.get("userinfo") or token.get("id_token_claims") or {}

        user_oid = claims.get("oid")
        tenant_id = claims.get("tid")
        upn = claims.get("preferred_username") or claims.get("email")
        name = claims.get("name")

        # 2) Fallback: když z nějakého důvodu 'oid' v id_tokenu není, dotaz na Graph /me
        # if not user_oid and token.get("access_token"):
        #     async with httpx.AsyncClient(timeout=10) as client:
        #         r = await client.get(
        #             "https://graph.microsoft.com/v1.0/me?$select=id,displayName,userPrincipalName",
        #             headers={"Authorization": f"Bearer {token['access_token']}"}
        #         )
        #         r.raise_for_status()
        #         me = r.json()
        #         user_oid = me.get("id")
        #         name = name or me.get("displayName")
        #         upn = upn or me.get("userPrincipalName")

        # 3) Ulož do session store (server-side)
        sid = str(uuid4())
        session_store[sid] = {
            "id_token": token.get("id_token"),
            "access_token": token.get("access_token"),
            "name": name or upn or "user",
            "user_oid": user_oid,
            "tenant_id": tenant_id,
            "upn": upn,
        }

        # 4) Redirect zpět do UI s HTTP-only cookie
        resp = RedirectResponse(url="/mgmt", status_code=302)
        # ngapp.storage.user["id_token"] = token.get("id_token")
        # ngapp.storage.user["access_token"] = token.get("access_token")
        # ngapp.storage.user["name"] = claims.get("name") or claims.get("preferred_username")
        # zpět do UI

        
        resp.set_cookie(
            "sid", sid,
            httponly=True, 
            secure=is_https, 
            samesite="lax", 
            path="/",
            max_age=60*30,  # 30 min nebo dle potřeby
        )
        return resp        

    @fastapi_app.get("/auth/logout")
    async def logout(request: Request):
        ngapp.storage.user.clear()
        from starlette.responses import RedirectResponse
        return RedirectResponse("/mgmt")

    # --------- NiceGUI stránka: /mgmt ---------
    @ui.page("/users")
    async def users_page():
        from nicegui import Client
        client: Client = ui.context.client
        request: Request = client.request
        sid = request.cookies.get('sid')
        sess = None
        user_id = None
        if sid and sid in session_store:
            sess = session_store[sid]
            # Tohle už je UI kontext → můžeme použít storage.user
            ngapp.storage.user['id_token'] = sess.get('id_token')
            ngapp.storage.user['access_token'] = sess.get('access_token')
            ngapp.storage.user['name'] = sess.get('name', 'user')
            user_id = sess.get('user_oid')
            # volitelně: session z paměti smazat / obnovit expiraci
            # del SESSIONS[sid]
        else:
            ui.run_javascript("window.location.href='/auth/login'")
            return   
        with ui.card().classes("w-full"):
            ui.label("Users").classes("text-lg mb-2")
            columns = [
                {"name": "email", "label": "Email", "field": "email"},
                {"name": "display_name", "label": "Display Name", "field": "display_name"},
                {"name": "is_active", "label": "Active", "field": "is_active"},
                {"name": "is_admin", "label": "Admin", "field": "is_admin"},
                {"name": "created_at", "label": "Created", "field": "created_at"},
            ]
            table = ui.table(
                columns=columns, 
                rows=[], 
                row_key="id", 
                selection='single',   
                pagination=10).classes("w-full")
            async def refresh_users():
                rows = await _api_get("/management/users", cookies={"sid": sid} if sid else {})
                table.rows = rows
                table.update()
            await refresh_users()
            async def load_usage():
                sel = table.selected
                if not sel:
                    ui.notify("Select a key to load usage", type="warning")
                    return
                sel = sel[0]
                payload = {"user_id": sel["id"], "bucket": "day"}
                rows = await _api_post(
                    "/management/usage", 
                    json_body=payload,
                    cookies={"sid": sid} if sid else {}
                )
                xs = [r["bucket"] for r in rows]
                reqs = [r["requests"] for r in rows]
                toks = [r.get("total_tokens") or 0 for r in rows]
                chart.options["xAxis"]["data"] = xs
                chart.options["series"][0]["data"] = reqs
                chart.options["series"][1]["data"] = toks
                chart.update()

            with ui.row().classes("gap-2"):
                ui.button("Load usage for selected key", on_click=load_usage)
                ui.button("Show statistics for keys", on_click=lambda: ui.run_javascript("window.location.href='/mgmt'"))

        with ui.card().classes("w-full mt-4"):
            ui.label("Usage (last 30 days)").classes("text-lg mb-2")
            chart = ui.echart(
                {
                    "tooltip": {"trigger": "axis"},
                    "xAxis": {"type": "category", "data": []},
                    "yAxis": {"type": "value"},
                    "legend": {"data": ["requests", "total_tokens"]},
                    "series": [
                        {"name": "requests", "type": "bar", "data": []},
                        {"name": "total_tokens", "type": "line", "data": []},
                    ],
                }
            ).classes("w-full h-80")

    @ui.page("/")
    async def mgmt_page():
        from nicegui import Client
        
        client: Client = ui.context.client
        request: Request = client.request      
        sid = request.cookies.get('sid')         
        sess = None
        user_id = None
        if sid and sid in session_store:
            sess = session_store[sid]
            # Tohle už je UI kontext → můžeme použít storage.user
            ngapp.storage.user['id_token'] = sess.get('id_token')
            ngapp.storage.user['access_token'] = sess.get('access_token')
            ngapp.storage.user['name'] = sess.get('name', 'user')
            user_id = sess.get('user_oid')
            # volitelně: session z paměti smazat / obnovit expiraci
            # del SESSIONS[sid]
        else:
            ui.run_javascript("window.location.href='/auth/login'")

        ui.page_title("Proxy Management")
        with ui.header().classes("items-center justify-between"):
            ui.label("Azure OpenAI Proxy - Management")
            with ui.row().classes("items-center"):
                if ngapp.storage.user.get("id_token") or ngapp.storage.user.get("access_token"):
                    ui.label(f"Signed in as {ngapp.storage.user.get('name', 'user')}")
                    ui.button("Sign out", on_click=lambda: ui.navigate.to("/auth/logout"))
                    # ui.link('Sign in with Entra ID', '/auth/login', new_tab=False)
                else:
                    ui.button("Sign in with Entra ID", color="primary", on_click=lambda: ui.run_javascript("window.location.href='/auth/login'"))
                    # ui.link('Sign in with Entra ID', '/auth/login', new_tab=False, )

        if not (ngapp.storage.user.get("id_token") or ngapp.storage.user.get("access_token")):
            ui.label("Please sign in to manage API keys.").classes("text-grey-7 mt-8")
            return

        # --- sekce klíčů ---
        with ui.card().classes("w-full"):
            ui.label("API Keys").classes("text-lg mb-2")

            columns = [
                {"name": "prefix", "label": "Prefix", "field": "prefix"},
                {"name": "name", "label": "Name", "field": "name"},
                {"name": "is_active", "label": "Active", "field": "is_active"},
                {"name": "created_at", "label": "Created", "field": "created_at"},
                {"name": "expires_at", "label": "Expires", "field": "expires_at"},
                {"name": "last_used_at", "label": "Last used", "field": "last_used_at"},
                {"name": "rate_limit_per_minute", "label": "RPM", "field": "rate_limit_per_minute"},
            ]
            table = ui.table(
                columns=columns, 
                rows=[], 
                row_key="id", 
                selection='single',   
                pagination=10).classes("w-full")

            async def refresh_keys():
                with client:
                    try:
                        rows = await _api_get(
                            "/management/apikeys",
                            cookies={"sid": sid} if sid else {}
                        )
                        table.rows = rows
                        table.update()
                        
                        ui.notify("Keys refreshed", type="positive")
                    except Exception as e:
                        ui.notify(f"Failed to load keys: {e}", type="negative")

            async def open_create_key_dialog():
                create_button = None
                with ui.dialog() as d, ui.card():
                    name = ui.input("Name")
                    days = ui.number("Expires in days", value=90, min=1, max=3650)
                    rpm = ui.number("Rate limit (per minute)", value=1, min=1)
                    async def create():
                        nonlocal create_button
                        create_button.disabled = True
                        create_button.update()
                        payload = {
                            "user_id": user_id or "me",
                            "name": name.value or None,
                            "expires_in_days": int(days.value) if days.value else None,
                            "rate_limit_per_minute": int(rpm.value) if rpm.value else 1,
                        }
                        resp = await _api_post(
                            "/management/apikeys", 
                            json_body=payload,
                            cookies={"sid": sid} if sid else {}
                        )
                        plaintext = resp["token"]
                        ui.notify("API key created. Token will be shown once below.", type="positive", close_button="OK")
                        ui.markdown(f"**Save this token now:** `{plaintext}`").classes("mt-2")
                        create_button.text = "Close"
                        create_button.on_click(lambda: d.close())
                        create_button.update()

                        await refresh_keys()

                    with ui.row().classes("justify-end mt-2"):
                        create_button = ui.button("Create", on_click=create, color="primary")
                        ui.button("Cancel", on_click=d.close)
                d.open()

            async def enable_disable(selected: dict, enable: bool):
                if not selected:
                    ui.notify("Select a key in the table first", type="warning")
                    return
                item = selected[0]
                item_id = item.get("id")
                print("Enable/disable", item, item_id, enable)
                path = f"/management/apikeys/{item_id}/" + ("enable" if enable else "disable")
                await _api_post(
                    path,
                    cookies={"sid": sid} if sid else {}
                )
                await refresh_keys()

            with ui.row().classes("gap-2 mb-2"):
                ui.button("Refresh", on_click=refresh_keys)
                ui.button("Create key", color="primary", on_click=open_create_key_dialog)
                ui.button("Enable", on_click=lambda: enable_disable(table.selected, True))
                ui.button("Disable", on_click=lambda: enable_disable(table.selected, False))
                ui.button("Show statistics for users", on_click=lambda: ui.run_javascript("window.location.href='/mgmt/users'"))

        # --- sekce spotřeby ---
        with ui.card().classes("w-full mt-4"):
            ui.label("Usage (last 30 days)").classes("text-lg mb-2")
            chart = ui.echart(
                {
                    "tooltip": {"trigger": "axis"},
                    "xAxis": {"type": "category", "data": []},
                    "yAxis": {"type": "value"},
                    "legend": {"data": ["requests", "total_tokens"]},
                    "series": [
                        {"name": "requests", "type": "bar", "data": []},
                        {"name": "total_tokens", "type": "line", "data": []},
                    ],
                }
            ).classes("w-full h-80")

            async def load_usage():
                sel = table.selected
                if not sel:
                    ui.notify("Select a key to load usage", type="warning")
                    return
                sel = sel[0]
                payload = {"key_id": sel["id"], "bucket": "day"}
                rows = await _api_post(
                    "/management/usage", 
                    json_body=payload,
                    cookies={"sid": sid} if sid else {}
                )
                xs = [r["bucket"] for r in rows]
                reqs = [r["requests"] for r in rows]
                toks = [r.get("total_tokens") or 0 for r in rows]
                chart.options["xAxis"]["data"] = xs
                chart.options["series"][0]["data"] = reqs
                chart.options["series"][1]["data"] = toks
                chart.update()

            with ui.row().classes("gap-2"):
                ui.button("Load usage for selected key", on_click=load_usage)

        # úvodní načtení
        asyncio.create_task(refresh_keys())
