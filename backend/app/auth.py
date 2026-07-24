"""Server-enforced authentication and authorization for BridgeGuard.

Production can verify Cloudflare Access application JWTs on every request. A
signed-cookie password mode remains available for local and non-Cloudflare
deployments. In both modes, roles are resolved from server-side email lists.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Iterable

import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from jwt import PyJWKClient
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


COOKIE_NAME = "bridgeguard_session"
ROLE_LEVEL = {"viewer": 0, "editor": 1, "admin": 2}
BUILTIN_ADMIN_EMAILS = {
    "bsidhom.ahmed@gmail.com",
}
BUILTIN_EDITOR_EMAILS = {
    "douglas.tintle@dot.nj.gov",
    "vijay.sampat@dot.nj.gov",
    "harjit.bal@dot.nj.gov",
    "mantawy@rowan.edu",
    "bahria76@rowan.edu",
}


def _csv(name: str) -> set[str]:
    return {
        item.strip().lower()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    }


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AuthUser:
    email: str
    role: str


class LoginBody(BaseModel):
    email: str
    password: str


def auth_disabled() -> bool:
    return _truthy("BRIDGEGUARD_AUTH_DISABLED")


def auth_mode() -> str:
    if auth_disabled():
        return "disabled"
    mode = os.getenv("BRIDGEGUARD_AUTH_MODE", "password").strip().lower()
    if mode not in {"password", "cloudflare"}:
        raise RuntimeError("BRIDGEGUARD_AUTH_MODE must be 'password' or 'cloudflare'")
    return mode


def _secret() -> bytes:
    value = os.getenv("BRIDGEGUARD_SESSION_SECRET", "").strip()
    if len(value) < 32:
        raise RuntimeError("BRIDGEGUARD_SESSION_SECRET must contain at least 32 characters")
    return value.encode("utf-8")


def _configured_password() -> str:
    value = os.getenv("BRIDGEGUARD_PASSWORD", "")
    if len(value) < 12:
        raise RuntimeError("BRIDGEGUARD_PASSWORD must contain at least 12 characters")
    return value


def role_for_email(email: str) -> str | None:
    """Return the configured role for an email, or None when it is not allowed."""
    email = email.strip().lower()
    if not email or "@" not in email:
        return None
    if (
        email in BUILTIN_ADMIN_EMAILS
        or email in _csv("BRIDGEGUARD_OWNER_EMAIL")
        or email in _csv("BRIDGEGUARD_ADMIN_EMAILS")
    ):
        return "admin"
    if email in BUILTIN_EDITOR_EMAILS or email in _csv("BRIDGEGUARD_EDITOR_EMAILS"):
        return "editor"
    allowed_emails = _csv("BRIDGEGUARD_ALLOWED_EMAILS")
    allowed_domains = {domain.lstrip("@") for domain in _csv("BRIDGEGUARD_ALLOWED_DOMAINS")}
    domain = email.rsplit("@", 1)[1]
    if email in allowed_emails or domain in allowed_domains:
        return "viewer"
    return None


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_session(user: AuthUser, now: int | None = None) -> str:
    now = int(now or time.time())
    ttl = max(300, int(os.getenv("BRIDGEGUARD_SESSION_TTL_SECONDS", "28800")))
    payload = {
        "email": user.email,
        "role": user.role,
        "iat": now,
        "exp": now + ttl,
        "nonce": secrets.token_urlsafe(12),
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64encode(hmac.new(_secret(), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_session(token: str, now: int | None = None) -> AuthUser | None:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _b64encode(
            hmac.new(_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        payload = json.loads(_b64decode(encoded))
        if int(payload["exp"]) <= int(now or time.time()):
            return None
        email = str(payload["email"]).strip().lower()
        current_role = role_for_email(email)
        if current_role is None or current_role != payload.get("role"):
            return None
        return AuthUser(email=email, role=current_role)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, RuntimeError):
        return None


_jwks_lock = threading.Lock()
_jwks_clients: dict[str, PyJWKClient] = {}


def _cloudflare_settings() -> tuple[str, str]:
    team_domain = os.getenv("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "").strip().lower()
    team_domain = team_domain.removeprefix("https://").rstrip("/")
    audience = os.getenv("CLOUDFLARE_ACCESS_AUD", "").strip()
    if not team_domain.endswith(".cloudflareaccess.com") or not audience:
        raise RuntimeError(
            "CLOUDFLARE_ACCESS_TEAM_DOMAIN and CLOUDFLARE_ACCESS_AUD are required"
        )
    return team_domain, audience


def _jwks_client(team_domain: str) -> PyJWKClient:
    with _jwks_lock:
        client = _jwks_clients.get(team_domain)
        if client is None:
            client = PyJWKClient(
                f"https://{team_domain}/cdn-cgi/access/certs",
                cache_keys=True,
                lifespan=3600,
            )
            _jwks_clients[team_domain] = client
        return client


def verify_cloudflare_request(request: Request) -> AuthUser | None:
    """Verify Cloudflare's application JWT and map its email to an app role."""
    token = request.headers.get("Cf-Access-Jwt-Assertion", "").strip()
    if not token:
        return None
    try:
        team_domain, audience = _cloudflare_settings()
        signing_key = _jwks_client(team_domain).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=f"https://{team_domain}",
            options={"require": ["exp", "iat", "iss", "aud", "email"]},
        )
        email = str(claims["email"]).strip().lower()
        role = role_for_email(email)
        return AuthUser(email, role) if role else None
    except (jwt.PyJWTError, KeyError, TypeError, ValueError, RuntimeError):
        return None


def cloudflare_logout_url() -> str:
    team_domain, _ = _cloudflare_settings()
    return f"https://{team_domain}/cdn-cgi/access/logout"


def _required_role(method: str, path: str) -> str:
    """Map an API request to its minimum role.

    Reads and AI queries are available to viewers. Data mutations require an
    editor. Raw database administration and runtime configuration require admin.
    """
    admin_prefixes: Iterable[str] = (
        "/api/table/",
        "/api/table-schema/",
        "/api/table-rows/",
    )
    admin_exact = {
        "/api/tables",
        "/api/clear-data",
        "/api/sql-query",
        "/api/system-info",
        "/api/rag-settings",
    }
    if path in admin_exact or any(path.startswith(prefix) for prefix in admin_prefixes):
        return "admin"
    if method in {"PUT", "PATCH", "DELETE"}:
        return "editor"
    path_parts = [part for part in path.split("/") if part]
    if (
        method == "POST"
        and len(path_parts) == 5
        and path_parts[:2] == ["api", "bridges"]
        and path_parts[-1] == "point-cloud"
    ):
        return "editor"
    editor_posts = (
        "/api/load-json",
        "/api/inspection-reports/apply",
        "/api/custom-tables",
        "/api/forecasts/build-training-db",
        "/api/forecasts/train",
        "/api/vision-reset",
    )
    if method == "POST" and (
        path.endswith("/reports") or any(path.startswith(prefix) for prefix in editor_posts)
    ):
        return "editor"
    return "viewer"


def _has_role(user: AuthUser, required: str) -> bool:
    return ROLE_LEVEL.get(user.role, -1) >= ROLE_LEVEL[required]


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        try:
            mode = auth_mode()
        except RuntimeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        if mode == "disabled":
            request.state.user = AuthUser("local@bridgeguard", "admin")
            return await call_next(request)
        if request.method == "OPTIONS" or not path.startswith("/api/"):
            return await call_next(request)
        if path == "/api/auth/config":
            return await call_next(request)
        if path == "/api/auth/login" and mode == "password":
            return await call_next(request)
        if mode == "cloudflare":
            user = verify_cloudflare_request(request)
        else:
            token = request.cookies.get(COOKIE_NAME, "")
            user = verify_session(token) if token else None
        if user is None:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        required = _required_role(request.method, path)
        if not _has_role(user, required):
            return JSONResponse(
                {"detail": f"{required.capitalize()} access required"}, status_code=403
            )
        request.state.user = user
        return await call_next(request)


router = APIRouter(prefix="/api/auth", tags=["authentication"])
_attempt_lock = threading.Lock()
_failed_attempts: dict[str, list[float]] = {}


@router.get("/config")
def auth_config():
    """Expose only the active login mechanism so the SPA can render it correctly."""
    return {"mode": auth_mode()}


def _check_rate_limit(client: str) -> None:
    now = time.time()
    with _attempt_lock:
        recent = [stamp for stamp in _failed_attempts.get(client, []) if now - stamp < 900]
        _failed_attempts[client] = recent
        if len(recent) >= 5:
            raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again later.")


def _record_failure(client: str) -> None:
    with _attempt_lock:
        _failed_attempts.setdefault(client, []).append(time.time())


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    if auth_mode() != "password":
        raise HTTPException(status_code=409, detail="Password login is disabled")
    client = request.client.host if request.client else "unknown"
    _check_rate_limit(client)
    try:
        configured_password = _configured_password()
        _secret()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Authentication is not configured: {exc}") from exc
    email = body.email.strip().lower()
    role = role_for_email(email)
    if role is None or not hmac.compare_digest(body.password, configured_password):
        _record_failure(client)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    with _attempt_lock:
        _failed_attempts.pop(client, None)
    user = AuthUser(email, role)
    secure_default = os.getenv("BRIDGEGUARD_ENV", "development").lower() == "production"
    response.set_cookie(
        COOKIE_NAME,
        issue_session(user),
        httponly=True,
        secure=_truthy("BRIDGEGUARD_COOKIE_SECURE", secure_default),
        samesite="strict",
        max_age=max(300, int(os.getenv("BRIDGEGUARD_SESSION_TTL_SECONDS", "28800"))),
        path="/",
    )
    return {"email": user.email, "role": user.role}


@router.get("/me")
def me(request: Request):
    user: AuthUser = request.state.user
    return {"email": user.email, "role": user.role}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    if auth_mode() == "cloudflare":
        return {"status": "signed_out", "logout_url": cloudflare_logout_url()}
    return {"status": "signed_out", "logout_url": None}
