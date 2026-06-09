from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import Response

from app.config import AUTH_CONFIG


ALLOWED_PATH_PREFIXES = ("/login", "/logout", "/static")
API_PATH_PREFIXES = ("/api", "/aws/api")


def validate_auth_config() -> None:
    if not AUTH_CONFIG.secret:
        raise RuntimeError("COMPLTD_AUTH_SECRET must be set")
    if not AUTH_CONFIG.users:
        raise RuntimeError(
            "Set at least one username/password pair using COMPLTD_ADMIN_* or COMPLTD_USER_*"
        )


def authenticate(username: str, password: str) -> bool:
    for user in AUTH_CONFIG.users:
        if hmac.compare_digest(user.username, username) and hmac.compare_digest(user.password, password):
            return True
    return False


def set_auth_cookie(response: Response, username: str) -> None:
    response.set_cookie(
        key=AUTH_CONFIG.cookie_name,
        value=_create_cookie_value(username),
        max_age=AUTH_CONFIG.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(AUTH_CONFIG.cookie_name, path="/")


def get_authenticated_username(request: Request) -> str | None:
    cookie_value = request.cookies.get(AUTH_CONFIG.cookie_name)
    if not cookie_value:
        return None
    return _parse_cookie_value(cookie_value)


async def auth_middleware(request: Request, call_next):
    username = get_authenticated_username(request)
    request.state.user = username

    if _is_allowed_path(request.url.path):
        return await call_next(request)
    if username:
        return await call_next(request)
    if request.url.path.startswith(API_PATH_PREFIXES):
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    login_target = quote(str(request.url.path))
    if request.url.query:
        login_target = quote(f"{request.url.path}?{request.url.query}")
    return RedirectResponse(url=f"/login?next={login_target}", status_code=303)


def _is_allowed_path(path: str) -> bool:
    return path == "/" or path == "/login" or path == "/logout" or path.startswith(ALLOWED_PATH_PREFIXES)


def _create_cookie_value(username: str) -> str:
    expires_at = int(time.time()) + AUTH_CONFIG.session_max_age_seconds
    payload = f"{username}:{expires_at}"
    signature = _sign(payload)
    raw = f"{payload}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _parse_cookie_value(cookie_value: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(cookie_value.encode("ascii")).decode("utf-8")
        username, expires_at_text, signature = raw.rsplit(":", 2)
        payload = f"{username}:{expires_at_text}"
        if not hmac.compare_digest(signature, _sign(payload)):
            return None
        if int(expires_at_text) < int(time.time()):
            return None
    except Exception:
        return None

    for user in AUTH_CONFIG.users:
        if user.username == username:
            return username
    return None


def _sign(payload: str) -> str:
    return hmac.new(AUTH_CONFIG.secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
