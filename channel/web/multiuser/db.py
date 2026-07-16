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

CREATE TABLE IF NOT EXISTS mu_kb_shares (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL,
    shared_with_id  INTEGER NOT NULL,
    permission      TEXT    NOT NULL DEFAULT 'read',
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES mu_users(id) ON DELETE CASCADE,
    FOREIGN KEY (shared_with_id) REFERENCES mu_users(id) ON DELETE CASCADE,
    UNIQUE(owner_id, shared_with_id)
);

CREATE INDEX IF NOT EXISTS idx_mu_kb_shares_owner
    ON mu_kb_shares (owner_id);

CREATE INDEX IF NOT EXISTS idx_mu_kb_shares_target
    ON mu_kb_shares (shared_with_id);

CREATE TABLE IF NOT EXISTS mu_teams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    description     TEXT    NOT NULL DEFAULT '',
    prompt          TEXT    NOT NULL DEFAULT '',
    created_by      INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    FOREIGN KEY (created_by) REFERENCES mu_users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS mu_team_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id         INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    role            TEXT    NOT NULL DEFAULT 'member',
    joined_at       INTEGER NOT NULL,
    FOREIGN KEY (team_id) REFERENCES mu_teams(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES mu_users(id) ON DELETE CASCADE,
    UNIQUE(team_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_mu_team_members_team
    ON mu_team_members (team_id);

CREATE INDEX IF NOT EXISTS idx_mu_team_members_user
    ON mu_team_members (user_id);

CREATE TABLE IF NOT EXISTS mu_user_configs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    config_key      TEXT    NOT NULL,
    config_value    TEXT    NOT NULL DEFAULT '',
    updated_at      INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES mu_users(id) ON DELETE CASCADE,
    UNIQUE(user_id, config_key)
);

CREATE INDEX IF NOT EXISTS idx_mu_user_configs_user
    ON mu_user_configs (user_id);
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
        """Create a new user. Returns user dict or None if username exists.

        Also creates the user's knowledge directory at ``knowledge/users/{id}/``
        under the workspace root.
        """
        user = None
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
                user = self._get_user_by_username(conn, username)
            except sqlite3.IntegrityError:
                return None
            finally:
                conn.close()

        # Create user's knowledge directory after successful registration
        if user:
            self._ensure_user_knowledge_dir(user["id"])
        return user

    def _ensure_user_knowledge_dir(self, user_id: int) -> None:
        """Create ``knowledge/users/{user_id}/`` directory for a user."""
        try:
            from config import conf
            workspace = conf().get("agent_workspace", "~/cow")
            workspace = os.path.expanduser(workspace)
            user_kb_dir = os.path.join(workspace, "knowledge", "users", str(user_id))
            os.makedirs(user_kb_dir, exist_ok=True)
            logger.debug(f"[MultiUserDB] Created knowledge directory for user {user_id}: {user_kb_dir}")
        except Exception as e:
            logger.warning(f"[MultiUserDB] Failed to create knowledge dir for user {user_id}: {e}")

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

    def verify_password(self, user_id: int, password: str) -> bool:
        """Verify a user's password by user ID. Returns True if correct."""
        with self._lock:
            conn = self._get_conn()
            try:
                user = self._get_user_by_id(conn, user_id)
                if not user:
                    return False
                return _verify_password(password, user["password_hash"])
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

    # -- knowledge share management ---------------------------------------

    def create_share(self, owner_id: int, shared_with_id: int,
                     permission: str = "read") -> Optional[Dict]:
        """Share knowledge base with another user. Returns share dict or None."""
        with self._lock:
            conn = self._get_conn()
            try:
                now = int(time.time())
                conn.execute(
                    "INSERT INTO mu_kb_shares (owner_id, shared_with_id, permission, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (owner_id, shared_with_id, permission, now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT id, owner_id, shared_with_id, permission, created_at "
                    "FROM mu_kb_shares WHERE owner_id = ? AND shared_with_id = ?",
                    (owner_id, shared_with_id),
                ).fetchone()
                return dict(row) if row else None
            except sqlite3.IntegrityError:
                return None
            finally:
                conn.close()

    def remove_share(self, share_id: int, owner_id: int) -> bool:
        """Remove a share. Returns True if found and deleted."""
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM mu_kb_shares WHERE id = ? AND owner_id = ?",
                    (share_id, owner_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def list_shares_by_owner(self, owner_id: int) -> List[Dict]:
        """List all shares created by a user (who they shared with)."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT s.id, s.owner_id, s.shared_with_id, s.permission, "
                    "s.created_at, u.username AS shared_with_username "
                    "FROM mu_kb_shares s "
                    "JOIN mu_users u ON u.id = s.shared_with_id "
                    "WHERE s.owner_id = ? "
                    "ORDER BY s.created_at DESC",
                    (owner_id,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def list_shares_for_user(self, user_id: int) -> List[Dict]:
        """List all shares targeting a user (knowledge shared with them)."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT s.id, s.owner_id, s.shared_with_id, s.permission, "
                    "s.created_at, u.username AS owner_username "
                    "FROM mu_kb_shares s "
                    "JOIN mu_users u ON u.id = s.owner_id "
                    "WHERE s.shared_with_id = ? "
                    "ORDER BY s.created_at DESC",
                    (user_id,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_shared_user_ids(self, user_id: int) -> List[int]:
        """Return list of user IDs whose knowledge is shared with this user."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT owner_id FROM mu_kb_shares WHERE shared_with_id = ?",
                    (user_id,),
                ).fetchall()
                return [r[0] for r in rows]
            finally:
                conn.close()

    # -- team management --------------------------------------------------

    def create_team(self, name: str, description: str, created_by: int, prompt: str = '') -> Optional[Dict]:
        """Create a new team. Also adds creator as admin member. Returns team dict or None."""
        with self._lock:
            conn = self._get_conn()
            try:
                # Check if referenced user exists; if not (single-user mode), skip FK enforcement
                user_exists = conn.execute(
                    "SELECT 1 FROM mu_users WHERE id = ?", (created_by,)
                ).fetchone() is not None
                if not user_exists:
                    conn.execute("PRAGMA foreign_keys = OFF")

                now = int(time.time())
                cur = conn.execute(
                    "INSERT INTO mu_teams (name, description, prompt, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (name, description, prompt, created_by, now, now),
                )
                team_id = cur.lastrowid
                # Add creator as team admin
                conn.execute(
                    "INSERT INTO mu_team_members (team_id, user_id, role, joined_at) "
                    "VALUES (?, ?, 'admin', ?)",
                    (team_id, created_by, now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT id, name, description, created_by, created_at, updated_at "
                    "FROM mu_teams WHERE id = ?", (team_id,)
                ).fetchone()
                return dict(row) if row else None
            except sqlite3.IntegrityError:
                return None
            finally:
                conn.close()

    def delete_team(self, team_id: int) -> bool:
        """Delete a team and all memberships."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM mu_team_members WHERE team_id = ?", (team_id,))
                cur = conn.execute("DELETE FROM mu_teams WHERE id = ?", (team_id,))
                conn.commit()
                # Remove team knowledge directory
                self._ensure_team_knowledge_dir_cleanup(team_id)
                return cur.rowcount > 0
            finally:
                conn.close()

    def get_team(self, team_id: int) -> Optional[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT id, name, description, prompt, created_by, created_at, updated_at "
                    "FROM mu_teams WHERE id = ?", (team_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def list_teams(self) -> List[Dict]:
        """List all teams with member count."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT t.id, t.name, t.description, t.prompt, t.created_by, t.created_at, "
                    "(SELECT COUNT(*) FROM mu_team_members m WHERE m.team_id = t.id) AS member_count "
                    "FROM mu_teams t ORDER BY t.name ASC"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def list_user_teams(self, user_id: int) -> List[Dict]:
        """List teams a user belongs to."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT t.id, t.name, t.description, t.prompt, t.created_by, t.created_at, "
                    "m.role AS my_role "
                    "FROM mu_teams t "
                    "JOIN mu_team_members m ON m.team_id = t.id "
                    "WHERE m.user_id = ? "
                    "ORDER BY t.name ASC",
                    (user_id,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_user_team_ids(self, user_id: int) -> List[int]:
        """Return list of team IDs a user belongs to (for search filtering)."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT team_id FROM mu_team_members WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
                return [r[0] for r in rows]
            finally:
                conn.close()

    def update_team(self, team_id: int, name: str = None, description: str = None, prompt: str = None) -> bool:
        """Update team name/description/prompt. Returns True if found."""
        with self._lock:
            conn = self._get_conn()
            try:
                now = int(time.time())
                fields = []
                params = []
                if name is not None:
                    fields.append("name = ?")
                    params.append(name)
                if description is not None:
                    fields.append("description = ?")
                    params.append(description)
                if prompt is not None:
                    fields.append("prompt = ?")
                    params.append(prompt)
                if not fields:
                    return False
                fields.append("updated_at = ?")
                params.append(now)
                params.append(team_id)
                cur = conn.execute(
                    f"UPDATE mu_teams SET {', '.join(fields)} WHERE id = ?",
                    params,
                )
                conn.commit()
                return cur.rowcount > 0
            except sqlite3.IntegrityError:
                return False
            finally:
                conn.close()

    # -- team membership --------------------------------------------------

    def add_team_member(self, team_id: int, user_id: int, role: str = "member") -> bool:
        """Add a user to a team. Returns True if successful."""
        with self._lock:
            conn = self._get_conn()
            try:
                # Check if referenced user exists; if not (single-user mode), skip FK enforcement
                user_exists = conn.execute(
                    "SELECT 1 FROM mu_users WHERE id = ?", (user_id,)
                ).fetchone() is not None
                if not user_exists:
                    conn.execute("PRAGMA foreign_keys = OFF")

                now = int(time.time())
                conn.execute(
                    "INSERT INTO mu_team_members (team_id, user_id, role, joined_at) "
                    "VALUES (?, ?, ?, ?)",
                    (team_id, user_id, role, now),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False
            finally:
                conn.close()

    def remove_team_member(self, team_id: int, user_id: int) -> bool:
        """Remove a user from a team. Returns True if found."""
        with self._lock:
            conn = self._get_conn()
            try:
                # Cannot remove last admin
                if self._is_last_team_admin(conn, team_id, user_id):
                    return False
                cur = conn.execute(
                    "DELETE FROM mu_team_members WHERE team_id = ? AND user_id = ?",
                    (team_id, user_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def update_team_member_role(self, team_id: int, user_id: int, role: str) -> bool:
        """Change a member's role. Returns True if found."""
        with self._lock:
            conn = self._get_conn()
            try:
                if role != "admin" and self._is_last_team_admin(conn, team_id, user_id):
                    return False
                cur = conn.execute(
                    "UPDATE mu_team_members SET role = ? WHERE team_id = ? AND user_id = ?",
                    (role, team_id, user_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def list_team_members(self, team_id: int) -> List[Dict]:
        """List all members of a team with their usernames."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT m.id, m.user_id, m.role, m.joined_at, u.username "
                    "FROM mu_team_members m "
                    "JOIN mu_users u ON u.id = m.user_id "
                    "WHERE m.team_id = ? "
                    "ORDER BY m.joined_at ASC",
                    (team_id,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def _is_last_team_admin(self, conn, team_id: int, user_id: int) -> bool:
        """Check if user is the last admin of a team."""
        row = conn.execute(
            "SELECT COUNT(*) FROM mu_team_members "
            "WHERE team_id = ? AND role = 'admin'",
            (team_id,),
        ).fetchone()
        if row and row[0] <= 1:
            # Check if this user IS an admin
            is_admin = conn.execute(
                "SELECT 1 FROM mu_team_members "
                "WHERE team_id = ? AND user_id = ? AND role = 'admin'",
                (team_id, user_id),
            ).fetchone()
            return is_admin is not None
        return False

    # -- knowledge directory helpers for teams ----------------------------

    def _ensure_team_knowledge_dir(self, team_id: int) -> None:
        """Create ``knowledge/teams/{team_id}/`` directory for a team."""
        try:
            from config import conf
            workspace = conf().get("agent_workspace", "~/cow")
            workspace = os.path.expanduser(workspace)
            team_kb_dir = os.path.join(workspace, "knowledge", "teams", str(team_id))
            os.makedirs(team_kb_dir, exist_ok=True)
            logger.debug(f"[MultiUserDB] Created knowledge directory for team {team_id}: {team_kb_dir}")
        except Exception as e:
            logger.warning(f"[MultiUserDB] Failed to create knowledge dir for team {team_id}: {e}")

    def _ensure_team_knowledge_dir_cleanup(self, team_id: int) -> None:
        """Remove team knowledge directory on team deletion."""
        try:
            from config import conf
            workspace = conf().get("agent_workspace", "~/cow")
            workspace = os.path.expanduser(workspace)
            team_kb_dir = os.path.join(workspace, "knowledge", "teams", str(team_id))
            import shutil
            if os.path.isdir(team_kb_dir):
                shutil.rmtree(team_kb_dir, ignore_errors=True)
                logger.debug(f"[MultiUserDB] Removed knowledge directory for team {team_id}")
        except Exception as e:
            logger.warning(f"[MultiUserDB] Failed to cleanup knowledge dir for team {team_id}: {e}")

    # -- user config (prompt override, etc.) ------------------------------

    def set_user_config(self, user_id: int, config_key: str, config_value: str) -> bool:
        """Set a user-specific config value (upsert)."""
        with self._lock:
            conn = self._get_conn()
            try:
                now = int(time.time())
                conn.execute(
                    "INSERT INTO mu_user_configs (user_id, config_key, config_value, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(user_id, config_key) "
                    "DO UPDATE SET config_value = excluded.config_value, updated_at = ?",
                    (user_id, config_key, config_value, now, now),
                )
                conn.commit()
                return True
            except Exception as e:
                logger.warning(f"[MultiUserDB] set_user_config error: {e}")
                return False
            finally:
                conn.close()

    def get_user_config(self, user_id: int, config_key: str) -> Optional[str]:
        """Get a user-specific config value."""
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT config_value FROM mu_user_configs "
                    "WHERE user_id = ? AND config_key = ?",
                    (user_id, config_key),
                ).fetchone()
                return row[0] if row else None
            finally:
                conn.close()

    def get_all_user_configs(self, user_id: int) -> Dict[str, str]:
        """Get all configs for a user as a dict."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT config_key, config_value FROM mu_user_configs "
                    "WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
                return {r[0]: r[1] for r in rows}
            finally:
                conn.close()

    def delete_user_config(self, user_id: int, config_key: str) -> bool:
        """Delete a user-specific config."""
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM mu_user_configs WHERE user_id = ? AND config_key = ?",
                    (user_id, config_key),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    # -- global config (user_id = -1 is the global/system sentinel) -------

    def set_global_config(self, config_key: str, config_value: str) -> bool:
        """Set a global config value (upsert, user_id=-1 sentinel)."""
        return self.set_user_config(-1, config_key, config_value)

    def get_global_config(self, config_key: str) -> Optional[str]:
        """Get a global config value."""
        return self.get_user_config(-1, config_key)

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
            # Migrate: add prompt column to mu_teams if missing (legacy databases)
            try:
                conn.execute("ALTER TABLE mu_teams ADD COLUMN prompt TEXT NOT NULL DEFAULT ''")
                conn.commit()
                logger.debug("[MultiUserDB] Migration: added prompt column to mu_teams")
            except sqlite3.OperationalError:
                pass  # Column already exists
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
                # Bootstrap: if no users exist, create default admin
                if _db_instance.user_count() == 0:
                    _db_instance.create_user("admin", "123456", role="admin")
                    logger.info("[MultiUserDB] Bootstrapped default admin user (admin / 123456)")
    return _db_instance


def reset_multiuser_db() -> None:
    """Reset the singleton (mainly for testing)."""
    global _db_instance
    with _db_lock:
        _db_instance = None
