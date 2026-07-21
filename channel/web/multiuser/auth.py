"""
Auth middleware for multi-user support.

Provides helpers for:
- Checking if multi-user mode is active
- Getting the current user from a request
- RBAC checks (require_admin)
- Session cookie management
"""

from __future__ import annotations

import json
import time
from typing import Optional, Dict

import web

from common.log import logger
from channel.web.multiuser.db import get_multiuser_db, MultiUserDB


# ---------------------------------------------------------------------------
# Cookie / session key
# ---------------------------------------------------------------------------

_SESSION_COOKIE = "mu_session"
_SESSION_EXPIRE_SECONDS = 86400 * 7  # 7 days


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_multiuser_enabled() -> bool:
    """Return True if multi-user mode is enabled via config or mu_users database."""
    from config import conf
    if conf().get("multi_user", False) is True:
        return True
    try:
        db = get_multiuser_db()
        return db.count_users() > 0
    except Exception:
        return False


def get_current_user() -> Optional[Dict]:
    """Return the currently logged-in user dict, or None.

    Reads the mu_session cookie and looks up the server-side session.
    """
    session_id = web.cookies().get(_SESSION_COOKIE, "")
    if not session_id:
        return None
    db = get_multiuser_db()
    session = db.get_session(session_id)
    if session is None:
        return None
    user = db.get_user_by_id(session["user_id"])
    if user is None:
        return None
    # Strip password_hash from the dict before returning
    user.pop("password_hash", None)
    return user


def get_current_user_id() -> int:
    """Return the current user's ID, or 0 if not logged in."""
    user = get_current_user()
    return user["id"] if user else 0


def require_login() -> Dict:
    """Require a logged-in user. Raises 401 if not authenticated.

    Returns the user dict on success.
    """
    user = get_current_user()
    if not user:
        raise web.HTTPError(
            "401 Unauthorized",
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps({"status": "error", "message": "请先登录 / Please login first"}),
        )
    return user


def require_admin() -> Dict:
    """Require a logged-in admin user. Raises 401/403 if not.

    Returns the user dict on success.
    """
    user = require_login()
    if user.get("role") != "admin":
        raise web.HTTPError(
            "403 Forbidden",
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps({"status": "error", "message": "需要管理员权限 / Admin access required"}),
        )
    return user


def set_session_cookie(session_id: str, expires_at: int) -> None:
    """Set the mu_session cookie on the response."""
    web.setcookie(
        _SESSION_COOKIE,
        session_id,
        expires=expires_at,
        path="/",
        httponly=True,
        samesite="Lax",
    )


def clear_session_cookie() -> None:
    """Remove the mu_session cookie."""
    web.setcookie(_SESSION_COOKIE, "", expires=-1, path="/")


def login_user(username: str, password: str) -> Optional[Dict]:
    """Authenticate a user and create a session.

    Returns dict with user and session_id on success, None on failure.
    """
    db = get_multiuser_db()
    user = db.authenticate(username, password)
    if not user:
        return None
    session = db.create_session(user["id"], _SESSION_EXPIRE_SECONDS)
    set_session_cookie(session["id"], session["expires_at"])
    user.pop("password_hash", None)
    return {"user": user, "session_id": session["id"]}


def logout_current_user() -> None:
    """Destroy the current user's session."""
    session_id = web.cookies().get(_SESSION_COOKIE, "")
    if session_id:
        db = get_multiuser_db()
        db.delete_session(session_id)
    clear_session_cookie()


def ensure_first_user_is_admin() -> None:
    """If no users exist, any registration will create an admin.

    This is called during registration to enforce the first-user-is-admin rule.
    """
    db = get_multiuser_db()
    return db.user_count() == 0
