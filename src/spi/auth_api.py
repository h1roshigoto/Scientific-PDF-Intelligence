"""Same-origin browser sessions, CSRF protection and account endpoints."""

import logging
import re
import sqlite3
from dataclasses import asdict
from hmac import compare_digest, digest
from ipaddress import ip_address
from secrets import token_urlsafe
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect

from .auth_config import flag
from .auth_models import AccountSession, SECRET, USER_ID, Credentials, PasswordChange, Registration, User, normalized_email
from .auth_store import AuthError, AuthStore, Session
from .tenant_storage import DAY, tenant_lease, tenant_queue, tenant_store, worker_slot

router = APIRouter()
logger = logging.getLogger(__name__)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
RATE_LIMITS = {
    "csrf": (90, 60), "session": (90, 60), "register": (6, 600), "upload": (12, 60),
    "process": (30, 60), "review": (30, 60), "export": (30, 60), "upload_read": (240, 60),
    "password": (5, 900),
}


def error_response(request, exc):
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
    return JSONResponse({"detail": exc.code}, status_code=exc.status, headers=headers)


def invalid_request(request, exc):
    # FastAPI's default validation response includes the raw input.
    return JSONResponse({"detail": "invalid_request"}, status_code=422)


def unavailable(request, exc):
    logger.error("Multiuser storage unavailable (%s)", type(exc).__name__)
    return JSONResponse({"detail": "auth_unavailable"}, status_code=503)


def _proxy_identity(request, config):
    if request.client is None:
        raise AuthError(400, "invalid_client")
    try:
        peer = ip_address(request.client.host)
        scheme = request.url.scheme
        if any(peer in allowed for allowed in config.trusted_proxies):
            forwarded_ip = request.headers.getlist("x-forwarded-for")
            forwarded_proto = request.headers.getlist("x-forwarded-proto")
            if len(forwarded_ip) != 1 or len(forwarded_proto) != 1:
                raise ValueError
            peer = ip_address(forwarded_ip[0])
            scheme = forwarded_proto[0]
        if scheme not in {"http", "https"}:
            raise ValueError
    except ValueError as exc:
        raise AuthError(400, "invalid_client") from exc
    if config.development and not peer.is_loopback:
        raise AuthError(403, "local_development_only")
    if scheme != "https" and (not config.development or config.secure):
        raise AuthError(403, "https_required")
    return str(peer)


def _csrf_context(request, config):
    cookie = request.cookies.get(config.cookie, "")
    session_id, _, secret = cookie.partition(".")
    if re.fullmatch(USER_ID, session_id) and re.fullmatch(SECRET, secret):
        return "session:" + cookie
    nonce = request.cookies.get(config.nonce_cookie, "")
    return "anonymous:" + nonce if re.fullmatch(SECRET, nonce) else None


def _csrf_token(config, context):
    return digest(config.secret, context.encode(), "sha256").hex()


class SecurityMiddleware:
    def __init__(self, app, config):
        self.app = app
        self.config = config
        self.deployed = flag("SPI_DEPLOYMENT")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        if (self.deployed and scope["path"] == "/__ops/ready"
                and request.client and request.client.host == "127.0.0.1"):
            return await self.app(scope, receive, send)
        started = False

        async def secure_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                headers.update(SECURITY_HEADERS)
                if not self.config.development:
                    headers["Strict-Transport-Security"] = "max-age=31536000"
            await send(message)

        try:
            hosts = request.headers.getlist("host")
            if len(hosts) != 1 or hosts[0].lower() not in self.config.hosts:
                raise AuthError(400, "invalid_host")
            request.state.client_ip = _proxy_identity(request, self.config)
            if request.method in UNSAFE_METHODS:
                if request.headers.getlist("origin") not in [[origin] for origin in self.config.origins]:
                    raise AuthError(403, "invalid_origin")
                context = _csrf_context(request, self.config)
                token = request.headers.get("x-spi-csrf", "")
                if not context or not re.fullmatch(r"[a-f0-9]{64}", token) or not compare_digest(
                        token, _csrf_token(self.config, context)):
                    raise AuthError(403, "invalid_csrf")
                if request.url.path.rstrip("/") != "/uploads" or request.method != "POST":
                    body = bytearray()
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > 64 * 1024:
                            raise AuthError(413, "request_too_large")
                        body.extend(chunk)

                    async def replay():
                        return {"type": "http.request", "body": bytes(body), "more_body": False}

                    receive = replay
        except ClientDisconnect:
            return
        except AuthError as exc:
            return await error_response(request, exc)(scope, receive, secure_send)
        try:
            await self.app(scope, receive, secure_send)
        except Exception as exc:
            logger.error("Multiuser request failed (%s)", type(exc).__name__)
            if started:
                # ASGI cannot replace headers once a download has started.
                raise RuntimeError("Response interrupted") from None
            response = JSONResponse({"detail": "internal_error"}, status_code=500)
            await response(scope, receive, secure_send)


def auth_store(request: Request) -> AuthStore:
    return request.app.state.auth


Database = Annotated[AuthStore, Depends(auth_store)]


def current_session(request: Request, db: Database) -> Session:
    cookie = request.cookies.get(db.config.cookie)
    session = db.session(cookie)
    binding = request.headers.get("x-spi-session-id")
    if session is None or (binding is not None and binding != session.session_id):
        raise AuthError(401, "session_required")
    return session


Principal = Annotated[Session, Depends(current_session)]


def rate(db, request, category, user_id=None):
    limit, period = RATE_LIMITS[category]
    entries = [(category, "ip:" + request.state.client_ip, limit * (4 if user_id else 1), period)]
    if user_id:
        entries.append((category, user_id, limit, period))
    db.consume(entries)


def _set_cookie(response, db, name, value, lifetime):
    response.set_cookie(name, value, max_age=lifetime, secure=db.config.secure,
                        httponly=True, samesite="lax", path="/")


@router.get("/auth/config")
def get_config():
    return {"mode": "multiuser"}


@router.get("/auth/csrf")
def csrf(request: Request, response: Response, db: Database):
    rate(db, request, "csrf")
    context = _csrf_context(request, db.config)
    if context is None:
        nonce = token_urlsafe(32)
        _set_cookie(response, db, db.config.nonce_cookie, nonce, 1800)
        context = "anonymous:" + nonce
    return {"csrf_token": _csrf_token(db.config, context)}


@router.post("/auth/register", status_code=201)
def register(account: Registration, request: Request, db: Database) -> User:
    rate(db, request, "register")
    with worker_slot(db.config.root / "auth-hash-slots", 4) as acquired:
        if not acquired:
            raise AuthError(429, "auth_busy", 2)
        return db.add_user(account, invitation=account.invitation_code.get_secret_value())


@router.post("/auth/login")
def login(account: Credentials, request: Request, response: Response, db: Database) -> AccountSession:
    db.consume([
        ("login", "global", 120, 60),
        ("login", "ip:" + request.state.client_ip, 20, 300),
        ("login", "email:" + normalized_email(account.email), 8, 300),
    ])
    with worker_slot(db.config.root / "auth-hash-slots", 4) as acquired:
        if not acquired:
            raise AuthError(429, "auth_busy", 2)
        user, cookie = db.login(account.email, account.password.get_secret_value(), request.cookies.get(db.config.cookie))
    _set_cookie(response, db, db.config.cookie, cookie, db.config.lifetime)
    _set_cookie(response, db, db.config.nonce_cookie, "", 0)
    return AccountSession(**user.model_dump(), session_id=cookie.partition(".")[0])


@router.get("/auth/me")
def me(session: Principal) -> AccountSession:
    return AccountSession(**session.user.model_dump(), session_id=session.session_id)


@router.get("/auth/sessions")
def sessions(session: Principal, db: Database):
    return db.sessions(session)


@router.delete("/auth/sessions/{session_id}", status_code=204)
def revoke(session_id: Annotated[str, Path(pattern=USER_ID)], session: Principal, request: Request, db: Database):
    rate(db, request, "session", session.user.user_id)
    db.revoke(session.user.user_id, session_id)
    response = Response(status_code=204)
    if session_id == session.session_id:
        _set_cookie(response, db, db.config.cookie, "", 0)
    return response


@router.post("/auth/logout", status_code=204)
def logout(session: Principal, db: Database):
    db.revoke(session.user.user_id, session.session_id)
    response = Response(status_code=204)
    _set_cookie(response, db, db.config.cookie, "", 0)
    return response


@router.post("/auth/change-password", status_code=204)
def change_password(passwords: PasswordChange, session: Principal, request: Request, db: Database):
    rate(db, request, "password", session.user.user_id)
    with worker_slot(db.config.root / "auth-hash-slots", 4) as acquired:
        if not acquired:
            raise AuthError(429, "auth_busy", 2)
        db.change_password(session, passwords.current_password.get_secret_value(), passwords.new_password.get_secret_value())
    response = Response(status_code=204)
    _set_cookie(response, db, db.config.cookie, "", 0)
    return response


@router.get("/account/usage")
def usage(session: Principal, request: Request, db: Database):
    owner = session.user.user_id
    config = db.config
    rate(db, request, "session", owner)
    quota = db.quotas(owner)
    with tenant_lease(config, owner):
        uploads = tenant_store(config, owner, quota)
        files, size = uploads.usage()
        jobs = tenant_queue(config, uploads, quota).counts()
    model = db.used("quota_model", owner, DAY)
    exports = db.used("quota_export", owner, DAY)
    return {"documents": files, "storage_bytes": size, "limits": asdict(quota),
            "active_jobs": jobs.get("processing", 0), "queued_jobs": jobs.get("queued", 0),
            "model_requests_remaining": max(0, quota.max_model_requests - model),
            "exports_remaining": max(0, quota.max_exports_per_period - exports), "period_seconds": DAY}


def install_auth(app, config):
    db = AuthStore(config)
    db.initialize()
    app.state.auth = db
    app.include_router(router)
    app.add_exception_handler(AuthError, error_response)
    app.add_exception_handler(RequestValidationError, invalid_request)
    for exc in (sqlite3.Error, OSError):
        app.add_exception_handler(exc, unavailable)
