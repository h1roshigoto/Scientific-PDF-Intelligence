"""SQLite identity, session, rate and quota state shared by all processes."""

import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
from hmac import compare_digest, digest
from secrets import token_hex, token_urlsafe
from time import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .auth_config import AuthConfig, UserQuota
from .auth_models import SECRET, USER_ID, User, normalized_email, strong_password
from .upload_store import private_sqlite_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 user_id TEXT PRIMARY KEY, email TEXT NOT NULL, email_key TEXT NOT NULL UNIQUE,
 password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','blocked')),
 role TEXT NOT NULL CHECK(role IN ('user','admin')),
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, last_login_at INTEGER
);
CREATE TABLE IF NOT EXISTS invitations (
 code_hash TEXT PRIMARY KEY, creator TEXT NOT NULL REFERENCES users, expires_at INTEGER NOT NULL,
 redeemed_by TEXT REFERENCES users, redeemed_at INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
 session_id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users,
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
 revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id, revoked_at);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS counters (
 category TEXT NOT NULL, subject TEXT NOT NULL, window INTEGER NOT NULL,
 expires_at INTEGER NOT NULL, used INTEGER NOT NULL, PRIMARY KEY(category, subject, window)
);
CREATE INDEX IF NOT EXISTS counters_expiry ON counters(expires_at);
CREATE TABLE IF NOT EXISTS user_quotas (
 user_id TEXT NOT NULL REFERENCES users, name TEXT NOT NULL,
 value INTEGER NOT NULL CHECK(typeof(value)='integer' AND value BETWEEN 1 AND 9007199254740991),
 PRIMARY KEY(user_id, name)
);
"""
PUBLIC_COLUMNS = "user_id, email, display_name, status, role, created_at, updated_at, last_login_at"
HASHER = PasswordHasher()


class AuthError(Exception):
    def __init__(self, status: int, code: str, retry_after: int | None = None):
        self.status = status
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


@dataclass(frozen=True)
class Session:
    user: User
    session_id: str


def _hash(secret: str) -> str:
    return sha256(secret.encode()).hexdigest()


def _verify(password_hash: str, password: str) -> bool:
    try:
        return HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError):
        return False


class AuthStore:
    def __init__(self, config: AuthConfig):
        self.config = config

    def initialize(self):
        self.config.db.parent.mkdir(parents=True, exist_ok=True)
        self.config.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
        self.dummy_hash = HASHER.hash(token_urlsafe(32))

    @contextmanager
    def connect(self, *, write=False):
        path = private_sqlite_path(self.config.db.parent, self.config.db.name)
        with closing(sqlite3.connect(path, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _user(db, user_id):
        row = db.execute(f"SELECT {PUBLIC_COLUMNS} FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return User(**dict(row)) if row else None

    def user(self, user_id):
        with self.connect() as db:
            return self._user(db, user_id)

    def active_users(self):
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT user_id FROM users WHERE status = 'active' ORDER BY user_id")]

    def add_user(self, account, *, invitation=None, bootstrap=False):
        password = account.password.get_secret_value()
        try:
            strong_password(password, account.email)
        except ValueError as exc:
            raise AuthError(422, "weak_password") from exc
        code = invitation or ""
        code_hash = _hash(code)
        if not bootstrap:
            if not re.fullmatch(SECRET, code):
                raise AuthError(400, "invalid_invitation")
            with self.connect() as db:
                if not self._invitation_valid(db, code_hash, int(time())):
                    raise AuthError(400, "invalid_invitation")
        password_hash = HASHER.hash(password)
        with self.connect(write=True) as db:
            now = int(time())
            if bootstrap:
                if db.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
                    raise AuthError(409, "admin_already_bootstrapped")
            elif not self._invitation_valid(db, code_hash, now):
                raise AuthError(400, "invalid_invitation")
            email_key = normalized_email(account.email)
            if db.execute("SELECT 1 FROM users WHERE email_key = ?", (email_key,)).fetchone():
                raise AuthError(409, "account_conflict")
            user_id = token_hex(16)
            db.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL)",
                       (user_id, account.email, email_key, password_hash, account.display_name,
                        "admin" if bootstrap else "user", now, now))
            if not bootstrap:
                db.execute("UPDATE invitations SET redeemed_at = ?, redeemed_by = ? WHERE code_hash = ?",
                           (now, user_id, code_hash))
            return self._user(db, user_id)

    @staticmethod
    def _invitation_valid(db, code_hash, now):
        return db.execute("""SELECT 1 FROM invitations JOIN users ON creator = user_id
                           WHERE code_hash = ? AND expires_at > ? AND redeemed_at IS NULL AND status = 'active'""",
                          (code_hash, now)).fetchone()

    def invite(self, admin, ttl):
        if not 1 <= ttl <= 7 * 86400:
            raise AuthError(422, "invalid_invitation_lifetime")
        code = token_urlsafe(32)
        with self.connect(write=True) as db:
            self._require_admin(db, admin)
            db.execute("INSERT INTO invitations VALUES (?, ?, ?, NULL, NULL)",
                       (_hash(code), admin, int(time()) + ttl))
        return code

    @staticmethod
    def _require_admin(db, user_id):
        user = AuthStore._user(db, user_id)
        if not user or user.role != "admin" or user.status != "active":
            raise AuthError(403, "admin_required")

    def block(self, admin, user_id):
        with self.connect(write=True) as db:
            self._require_admin(db, admin)
            now = int(time())
            changed = db.execute("""UPDATE users SET status = 'blocked', updated_at = ?
                                  WHERE user_id = ? AND role = 'user'""", (now, user_id))
            if changed.rowcount != 1:
                raise AuthError(404, "user_not_found")
            db.execute("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                       (now, user_id))

    def _credentials(self, email, password):
        with self.connect() as db:
            row = db.execute("SELECT user_id, password_hash, status FROM users WHERE email_key = ?",
                             (normalized_email(email),)).fetchone()
        valid = _verify(row["password_hash"] if row else self.dummy_hash, password)
        if not valid or not row or row["status"] != "active":
            raise AuthError(401, "invalid_credentials")
        return row

    def admin_credentials(self, email, password):
        row = self._credentials(email, password)
        with self.connect() as db:
            self._require_admin(db, row["user_id"])
        return row["user_id"]

    def login(self, email, password, previous_cookie):
        row = self._credentials(email, password)
        user_id, old_hash = row["user_id"], row["password_hash"]
        new_hash = HASHER.hash(password) if HASHER.check_needs_rehash(old_hash) else old_hash
        session_id, secret = token_hex(16), token_urlsafe(32)
        with self.connect(write=True) as db:
            now = int(time())
            changed = db.execute("""UPDATE users SET password_hash = ?, last_login_at = ?, updated_at = ?
                                  WHERE user_id = ? AND status = 'active' AND password_hash = ?""",
                                 (new_hash, now, now, user_id, old_hash))
            if changed.rowcount != 1:
                raise AuthError(401, "invalid_credentials")
            previous = self._session_row(db, previous_cookie, now)
            if previous:
                db.execute("UPDATE sessions SET revoked_at = ? WHERE session_id = ?",
                           (now, previous["session_id"]))
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, NULL)",
                       (session_id, _hash(secret), user_id, now, now + self.config.lifetime, now))
            return self._user(db, user_id), f"{session_id}.{secret}"

    def _session_row(self, db, cookie, now):
        session_id, _, secret = (cookie or "").partition(".")
        if not re.fullmatch(USER_ID, session_id) or not re.fullmatch(SECRET, secret):
            return None
        row = db.execute("""SELECT sessions.* FROM sessions JOIN users USING(user_id)
                           WHERE session_id = ? AND revoked_at IS NULL AND expires_at > ?
                           AND last_seen_at > ? AND status = 'active'""",
                         (session_id, now, now - self.config.idle)).fetchone()
        return row if row and compare_digest(row["secret_hash"], _hash(secret)) else None

    def session(self, cookie):
        now = int(time())
        with self.connect() as db:
            row = self._session_row(db, cookie, now)
            if not row:
                return None
            user = self._user(db, row["user_id"])
            if user.status != "active":
                return None
        if now - row["last_seen_at"] >= min(60, max(1, self.config.idle // 4)):
            with self.connect(write=True) as db:
                row = self._session_row(db, cookie, int(time()))
                if not row:
                    return None
                db.execute("UPDATE sessions SET last_seen_at = max(last_seen_at, ?) WHERE session_id = ?",
                           (now, row["session_id"]))
        return Session(user, row["session_id"])

    def sessions(self, session):
        now = int(time())
        with self.connect() as db:
            rows = db.execute("""SELECT session_id, created_at, expires_at, last_seen_at FROM sessions
                               WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
                               AND last_seen_at > ? ORDER BY created_at DESC""",
                              (session.user.user_id, now, now - self.config.idle))
            return [{**dict(row), "current": row["session_id"] == session.session_id} for row in rows]

    def revoke(self, user_id, session_id):
        with self.connect(write=True) as db:
            updated = db.execute("""UPDATE sessions SET revoked_at = coalesce(revoked_at, ?)
                                  WHERE user_id = ? AND session_id = ?""", (int(time()), user_id, session_id))
            if updated.rowcount != 1:
                raise AuthError(404, "session_not_found")

    def change_password(self, session, current, new):
        user = session.user
        row = self._credentials(user.email, current)
        try:
            strong_password(new, user.email)
            if new == current:
                raise ValueError("Password unchanged")
        except ValueError as exc:
            raise AuthError(422, "weak_password") from exc
        password_hash = HASHER.hash(new)
        with self.connect(write=True) as db:
            now = int(time())
            changed = db.execute("""UPDATE users SET password_hash = ?, updated_at = ?
                                  WHERE user_id = ? AND status = 'active' AND password_hash = ?
                                  AND EXISTS (SELECT 1 FROM sessions WHERE user_id = users.user_id
                                  AND session_id = ? AND revoked_at IS NULL AND expires_at > ?
                                  AND last_seen_at > ?)""",
                                 (password_hash, now, user.user_id, row["password_hash"],
                                  session.session_id, now, now - self.config.idle))
            if changed.rowcount != 1:
                raise AuthError(401, "session_expired")
            db.execute("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                       (now, user.user_id))

    def quotas(self, user_id):
        limits = asdict(self.config.quotas)
        with self.connect() as db:
            rows = db.execute("SELECT name, value FROM user_quotas WHERE user_id = ?", (user_id,))
            limits.update((name, value) for name, value in rows if name in limits)
        limits["max_active_jobs"] = min(limits["max_active_jobs"], 64)
        return UserQuota(**limits)

    def consume(self, entries, *, code="rate_limited"):
        """Atomically consume all requested fixed-window counters, or none."""
        now = int(time())
        with self.connect(write=True) as db:
            db.execute("DELETE FROM counters WHERE expires_at <= ?", (now,))
            for category, subject, limit, period in entries:
                window = now // period * period
                updated = db.execute("""INSERT INTO counters VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(category, subject, window) DO UPDATE SET used = used + 1 WHERE used < ?""",
                    (category, self.counter_key(subject), window, window + period, limit))
                if updated.rowcount != 1:
                    raise AuthError(429, code, window + period - now)

    def counter_key(self, subject):
        # Quota subjects are already random user IDs. Keep their accounting
        # stable across server-secret rotation while hiding IPs and email keys.
        return subject if re.fullmatch(USER_ID, subject) else digest(
            self.config.secret, subject.encode(), "sha256").hex()

    def used(self, category, user_id, period):
        with self.connect() as db:
            window = int(time()) // period * period
            row = db.execute("SELECT used FROM counters WHERE category = ? AND subject = ? AND window = ?",
                             (category, self.counter_key(user_id), window)).fetchone()
            return row[0] if row else 0
