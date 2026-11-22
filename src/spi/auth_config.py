"""Explicit authentication modes and fail-closed multiuser configuration."""

import os
import re
from base64 import urlsafe_b64decode
from dataclasses import dataclass, field
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlsplit

from .upload_store import PROJECT_ROOT, _overlap, _path_aliases, _positive_limit, private_path, private_sqlite_path


def auth_mode() -> str:
    mode = os.getenv("SPI_AUTH_MODE", "private")
    if mode not in {"private", "multiuser"}:
        raise ValueError("SPI_AUTH_MODE must be private or multiuser")
    return mode


def flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "true" if default else "false").lower()
    if value not in {"1", "0", "true", "false"}:
        raise ValueError(f"Invalid boolean for {name}")
    return value in {"1", "true"}


def values(name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in os.getenv(name, "").split(",") if part.strip())


def loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def check_separate(*paths: Path) -> None:
    cwd = Path.cwd()
    checkout = next((parent for parent in (cwd, *cwd.parents) if (parent / ".git").exists()), PROJECT_ROOT)
    protected = (PROJECT_ROOT, checkout, Path(os.getenv("SPI_STATE_DIR", "state")), cwd / "outputs")
    seen = set().union(*(_path_aliases(path, path.resolve()) for path in protected))
    for path in paths:
        aliases = _path_aliases(path, path.resolve())
        if not path.is_absolute() or _overlap(aliases, seen):
            raise ValueError("Data paths must be absolute and separate from one another and the official Store")
        seen.update(aliases)


@dataclass(frozen=True)
class UserQuota:
    max_upload_files: int
    max_total_storage_bytes: int
    max_pdf_bytes: int
    max_active_jobs: int
    max_queued_jobs: int
    max_model_requests: int
    max_exports_per_period: int
    max_retries: int

    @classmethod
    def from_env(cls):
        defaults = (100, 500 * 1024**2, 25 * 1024**2, 1, 4, 80, 30, 3)
        quota = cls(*(_positive_limit(f"SPI_USER_{key.upper()}", default)
                      for key, default in zip(cls.__dataclass_fields__, defaults, strict=True)))
        if quota.max_active_jobs > 64:
            raise ValueError("SPI_USER_MAX_ACTIVE_JOBS must be at most 64")
        return quota


@dataclass(frozen=True)
class AuthConfig:
    db: Path
    root: Path
    origins: tuple[str, ...]
    hosts: frozenset[str]
    trusted_proxies: tuple
    development: bool
    secure: bool
    secret: bytes = field(repr=False)
    idle: int
    lifetime: int
    global_jobs: int
    job_requests: int
    quotas: UserQuota

    @property
    def cookie(self):
        return "__Host-spi-session" if self.secure else "spi-dev-session"

    @property
    def nonce_cookie(self):
        return "__Host-spi-csrf" if self.secure else "spi-dev-csrf"

    @classmethod
    def from_env(cls):
        if auth_mode() != "multiuser":
            raise ValueError("SPI_AUTH_MODE=multiuser is required")
        if (flag("SPI_PUBLIC_DEMO_ENABLED") or not flag("SPI_RATE_LIMIT_ENABLED", True)
                or not flag("SPI_AUTH_CSRF_ENABLED", True)):
            raise ValueError("Multiuser requires rate limiting and CSRF, with official/demo routes disabled")
        development = flag("SPI_AUTH_LOCAL_DEVELOPMENT")
        secure = flag("SPI_AUTH_COOKIE_SECURE", True)
        if not secure and not development:
            raise ValueError("Production requires Secure cookies")
        origins = values("SPI_AUTH_ALLOWED_ORIGINS")
        hosts = set()
        for origin in origins:
            parsed = urlsplit(origin)
            if (not parsed.hostname or parsed.username or parsed.password or parsed.netloc != parsed.netloc.lower()
                    or not re.fullmatch(r"(?:[a-z0-9.-]+|\[[a-f0-9:]+\])(?::[0-9]+)?", parsed.netloc)
                    or parsed.port == 0 or origin != f"{parsed.scheme}://{parsed.netloc}"
                    or parsed.scheme not in ({"http", "https"} if development else {"https"})
                    or (development and not loopback(parsed.hostname))):
                raise ValueError("Invalid SPI_AUTH_ALLOWED_ORIGINS")
            hosts.add(parsed.netloc)
        if not origins:
            raise ValueError("SPI_AUTH_ALLOWED_ORIGINS is required")
        proxies = tuple(ip_network(item, strict=True) for item in values("SPI_AUTH_TRUSTED_PROXIES"))
        if any(net.prefixlen != net.max_prefixlen for net in proxies):
            raise ValueError("Trusted proxies must be individual IP addresses")
        secret_text = os.getenv("SPI_AUTH_SECRET", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret_text):
            raise ValueError("SPI_AUTH_SECRET must be a random token_urlsafe(32) value")
        secret = urlsafe_b64decode(secret_text + "=")
        if len(set(secret)) < 16:
            raise ValueError("SPI_AUTH_SECRET must be generated with a cryptographic RNG")
        db = Path(os.environ.get("SPI_AUTH_DB", ""))
        root = Path(os.environ.get("SPI_MULTIUSER_STORAGE_ROOT", ""))
        legacy = [Path(value).absolute() for name in ("SPI_UPLOAD_DIR", "SPI_UPLOAD_STATE_DIR",
                  "SPI_UPLOAD_PROCESS_STATE_DIR") if (value := os.getenv(name, "").strip())]
        check_separate(db, root)
        if any(_overlap(_path_aliases(path, path.resolve()), _path_aliases(old, old.resolve()))
               for path in (db, root) for old in legacy):
            raise ValueError("Multiuser storage must be separate from the legacy upload Store")
        private_sqlite_path(db.parent, Path(db.name))
        private_path(root, Path(), directory=True)
        idle = _positive_limit("SPI_AUTH_SESSION_IDLE_SECONDS", 1800)
        lifetime = _positive_limit("SPI_AUTH_SESSION_MAX_SECONDS", 43200)
        jobs = _positive_limit("SPI_GLOBAL_MAX_ACTIVE_JOBS", 2)
        requests = _positive_limit("SPI_UPLOAD_PROCESS_MAX_MODEL_REQUESTS", 8)
        if idle > lifetime or lifetime > 30 * 86400 or jobs > 64 or requests > 32:
            raise ValueError("Invalid session lifetime, global worker slots (1–64) or model budget (1–32)")
        return cls(db, root, origins, frozenset(hosts), proxies, development, secure, secret,
                   idle, lifetime, jobs, requests, UserQuota.from_env())
