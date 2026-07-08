"""
Multi-user database layer.

Manages users table + server-side sessions in the existing conversations.db.

Tables:
    mu_users        — registered users (id, username, password_hash, role, ...)
    mu_sessions     — server-side login sessions (id, user_id, created_at, expires_at)

Thread-safe via a single re-entrant lock.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
import os
from pathlib import Path
from typing import Optional, Dict, Any, List

from common.log import logger


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS mu_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    role            TEXT    NOT NULL DEFAULT 'user',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mu_sessions (
    id              TEXT    PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES mu_users(id)
);

CREATE INDEX IF NOT EXISTS idx_mu_sessions_user
    ON mu_sessions (user_id);

CREATE INDEX IF NOT EXISTS idx_mu_sessions_expires
    ON mu_sessions (expires_at);
"""

_MIGRATION_ADD_USER_ID_TO_SESSIONS = """
ALTER TABLE sessions ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0;
"""

# ---------------------------------------------------------------------------
# Password utilities (zero external dependencies)
# ---------------------------------------------------------------------------

_PWD_ALGORITHM = "sha256"
_PWD_SALT_BYTES = 16
_PWD_HASH_ITERATIONS = 100000


def _generate_salt() -> str:
    return secrets.token_hex(_PWD_SALT_BYTES)


def _hash_password(password: str, salt: str = None) -> str:
    """Return ``algorithm$salt$hash`` string."""
    import hashlib
    if salt is None:
        salt = _generate_salt()
    h = hashlib.pbkdf2_hmac(
        _PWD_ALGORITHM,
        password.encode("utf-8"),
        salt.encode("utf-8"),
        _PWD_HASH_ITERATIONS,
    )
    return f"{_PWD_ALGORITHM}${salt}${h.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Verify password against a stored hash string."""
    try:
        algorithm, salt, _ = stored.split("$", 2)
        if algorithm != _PWD_ALGORITHM:
            return False
        expected = _hash_password(password, salt)
        return hmac.compare_digest(expected, stored)
    except (ValueError, AttributeError):
        return False


import hmac  # noqa: E402 (needed for _verify_password)


# ---------------------------------------------------------------------------
# Database manager
# ---------------------------------------------------------------------------

class MultiUserDB:
    """Thread-safe manager for the multi-user tables in conversations.db."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    # -- public helpers ---------------------------------------------------

    @classmethod
    def get_default_db_path(cls) -> str:
        """Return the default path to conversations.db under the workspace."""
        from config import conf
        data_root = conf().get("data_root", "")
        if data_root:
            return os.path.join(data_root, "sessions", "conversations.db")
        # Fallback: use workspace root
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "sessions", "conversations.db"
        )

    # -- user CRUD --------------------------------------------------------

    def create_user(self, username: str, password: str, role: str = "user") -> Optional[Dict]:
        """Create a new user. Returns user dict or None if username exists."""
        with self._lock:
            conn = self._get_conn()
            try:
                now = int(time.time())
                password_hash = _hash_password(password)
                conn.execute(
                    "INSERT INTO mu_users (username, password_hash, role, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (username, password_hash, role, now, now),
                )
                conn.commit()
                return self._get_user_by_username(conn, username)
            except sqlite3.IntegrityError:
                return None
            finally:
                conn.close()

    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                return self._get_user_by_id(conn, user_id)
            finally:
                conn.close()

    def get_user_by_username(self, username: str) -> Optional[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                return self._get_user_by_username(conn, username)
            finally:
                conn.close()

    def authenticate(self, username: str, password: str) -> Optional[Dict]:
        """Verify credentials. Returns user dict on success, None on failure."""
        with self._lock:
            conn = self._get_conn()
            try:
                user = self._get_user_by_username(conn, username)
                if user and _verify_password(password, user["password_hash"]):
                    return user
                return None
            finally:
                conn.close()

    def list_users(self) -> List[Dict]:
        """Return all users (password_hash excluded)."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT id, username, role, created_at, updated_at FROM mu_users "
                    "ORDER BY id ASC"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def update_user_role(self, user_id: int, role: str) -> bool:
        """Change a user's role. Returns True if found."""
        with self._lock:
            conn = self._get_conn()
            try:
                now = int(time.time())
                cur = conn.execute(
                    "UPDATE mu_users SET role = ?, updated_at = ? WHERE id = ?",
                    (role, now, user_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def update_user_password(self, user_id: int, new_password: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            try:
                now = int(time.time())
                password_hash = _hash_password(new_password)
                cur = conn.execute(
                    "UPDATE mu_users SET password_hash = ?, updated_at = ? WHERE id = ?",
                    (password_hash, now, user_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def delete_user(self, user_id: int) -> bool:
        """Delete a user and all their sessions. Returns True if found."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM mu_sessions WHERE user_id = ?", (user_id,))
                cur = conn.execute("DELETE FROM mu_users WHERE id = ?", (user_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def user_count(self) -> int:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute("SELECT COUNT(*) FROM mu_users").fetchone()
                return row[0] if row else 0
            finally:
                conn.close()

    # -- session management -----------------------------------------------

    def create_session(self, user_id: int, expire_seconds: int = 86400) -> Dict:
        """Create a login session. Returns session dict with id."""
        with self._lock:
            conn = self._get_conn()
            try:
                session_id = secrets.token_urlsafe(32)
                now = int(time.time())
                expires_at = now + expire_seconds
                conn.execute(
                    "INSERT INTO mu_sessions (id, user_id, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, user_id, now, expires_at),
                )
                conn.commit()
                return {
                    "id": session_id,
                    "user_id": user_id,
                    "created_at": now,
                    "expires_at": expires_at,
                }
            finally:
                conn.close()

    def get_session(self, session_id: str) -> Optional[Dict]:
        """Get a valid (non-expired) session. Returns None if invalid/expired."""
        if not session_id:
            return None
        with self._lock:
            conn = self._get_conn()
            try:
                now = int(time.time())
                row = conn.execute(
                    "SELECT s.id, s.user_id, s.created_at, s.expires_at "
                    "FROM mu_sessions s "
                    "WHERE s.id = ? AND s.expires_at > ?",
                    (session_id, now),
                ).fetchone()
                if row:
                    return dict(row)
                return None
            finally:
                conn.close()

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM mu_sessions WHERE id = ?", (session_id,))
                conn.commit()
            finally:
                conn.close()

    def delete_user_sessions(self, user_id: int) -> None:
        """Delete all sessions for a user (force logout everywhere)."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM mu_sessions WHERE user_id = ?", (user_id,))
                conn.commit()
            finally:
                conn.close()

    def cleanup_expired_sessions(self) -> int:
        """Remove expired sessions. Returns count of deleted rows."""
        with self._lock:
            conn = self._get_conn()
            try:
                now = int(time.time())
                cur = conn.execute("DELETE FROM mu_sessions WHERE expires_at <= ?", (now,))
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    # -- conversation session user_id migration ---------------------------

    def ensure_conversation_user_id_column(self) -> None:
        """Add user_id column to the conversations sessions table if missing."""
        with self._lock:
            conn = self._get_conn()
            try:
                # Check if column exists
                cols = [
                    row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
                ]
                if "user_id" not in cols:
                    conn.execute(_MIGRATION_ADD_USER_ID_TO_SESSIONS)
                    conn.commit()
                    logger.info("[MultiUserDB] Added user_id column to sessions table")
            except sqlite3.OperationalError:
                # sessions table may not exist yet (fresh install) — that's fine
                pass
            finally:
                conn.close()

    def migrate_session_owner(self, session_id: str, user_id: int) -> bool:
        """Assign a conversation session to a user. Returns True if found."""
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE sessions SET user_id = ? WHERE session_id = ?",
                    (user_id, session_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def get_user_conversation_sessions(self, user_id: int,
                                       channel_type: str = "web",
                                       page: int = 1,
                                       page_size: int = 50) -> Dict:
        """List conversation sessions for a specific user."""
        with self._lock:
            conn = self._get_conn()
            try:
                offset = (page - 1) * page_size
                rows = conn.execute(
                    "SELECT session_id, channel_type, title, context_start_seq, "
                    "created_at, last_active, msg_count "
                    "FROM sessions "
                    "WHERE channel_type = ? AND user_id = ? "
                    "ORDER BY last_active DESC "
                    "LIMIT ? OFFSET ?",
                    (channel_type, user_id, page_size, offset),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE channel_type = ? AND user_id = ?",
                    (channel_type, user_id),
                ).fetchone()[0]
                sessions = [dict(r) for r in rows]
                return {
                    "sessions": sessions,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": max(1, (total + page_size - 1) // page_size),
                }
            finally:
                conn.close()

    # -- internals --------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        try:
            conn.executescript(_DDL)
            conn.commit()
            self.ensure_conversation_user_id_column()
            logger.debug(f"[MultiUserDB] Initialized at {self._db_path}")
        except Exception as e:
            logger.error(f"[MultiUserDB] Init error: {e}")
            raise
        finally:
            conn.close()

    def _get_user_by_id(self, conn, user_id: int) -> Optional[Dict]:
        row = conn.execute(
            "SELECT id, username, password_hash, role, created_at, updated_at "
            "FROM mu_users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def _get_user_by_username(self, conn, username: str) -> Optional[Dict]:
        row = conn.execute(
            "SELECT id, username, password_hash, role, created_at, updated_at "
            "FROM mu_users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Singleton helper
# ---------------------------------------------------------------------------

_db_instance: Optional[MultiUserDB] = None
_db_lock = threading.Lock()


def get_multiuser_db() -> MultiUserDB:
    """Get or create the singleton MultiUserDB instance."""
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:
                db_path = MultiUserDB.get_default_db_path()
                _db_instance = MultiUserDB(db_path)
    return _db_instance


def reset_multiuser_db() -> None:
    """Reset the singleton (mainly for testing)."""
    global _db_instance
    with _db_lock:
        _db_instance = None
