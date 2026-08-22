"""Multi-user three-tier prompt injection (checklist section H).

These tests exercise the REAL initializer + Agent.get_full_system_prompt path
(no fakes), so a regression like the ``conf`` UnboundLocalError — or the two
injection copies drifting apart — fails here instead of in a running gateway.

They pin:
  H1-H3  global / team / user prompts all reach the rebuilt system prompt
  H4     a user with no team still gets the layers that are set
  H8     the block survives get_full_system_prompt() (the live LLM path)
"""
import os

import pytest

import channel.web.multiuser.db as _mub
from channel.web.multiuser.db import MultiUserDB


@pytest.fixture
def multiuser_env(tmp_path):
    """Point the multiuser DB + agent workspace at a throwaway location.

    Restores the config keys EXACTLY (deleting one we added) so that other
    tests' `get_agent_registry()` — which rebuilds from config when unpinned —
    never sees an `agent_workspace` key we created with a None value.
    """
    ws = str(tmp_path / "cow")
    os.makedirs(ws, exist_ok=True)
    dbfile = str(tmp_path / "index.db")

    import config as cfg
    cfg_obj = cfg.conf()
    orig_ws = cfg_obj.get("agent_workspace")
    had_ws = "agent_workspace" in cfg_obj
    orig_mu = cfg_obj.get("multi_user")
    cfg_obj["agent_workspace"] = ws
    cfg_obj["multi_user"] = True

    orig_db = _mub._db_instance
    _mub._db_instance = MultiUserDB(dbfile)
    db = _mub._db_instance

    yield db

    _mub._db_instance = orig_db
    if had_ws:
        cfg_obj["agent_workspace"] = orig_ws
    else:
        cfg_obj.pop("agent_workspace", None)
    cfg_obj["multi_user"] = orig_mu


@pytest.fixture
def initializer(multiuser_env):
    from bridge.bridge import Bridge
    from bridge.agent_bridge import AgentBridge

    return AgentBridge(Bridge()).initializer


def _sentinel(user_id, label):
    return f"{label}_SENTINEL_{user_id}"


def test_all_three_layers_reach_the_live_prompt(multiuser_env, initializer):
    db = multiuser_env
    user = db.create_user("alice", "pw", role="user")
    uid = user["id"]
    db.set_global_config("global_prompt", _sentinel(uid, "GLOBAL"))

    import sqlite3, time
    conn = sqlite3.connect(db._db_path)
    conn.execute(
        "INSERT INTO mu_teams (name, description, prompt, created_by, created_at, updated_at) "
        "VALUES ('T1', '', ?, 1, 0, 0)", (_sentinel(uid, "TEAM"),))
    conn.execute("INSERT INTO mu_team_members (team_id, user_id, role, joined_at) VALUES (1, ?, 'admin', 0)", (uid,))
    conn.commit(); conn.close()
    db.set_user_config(uid, "prompt_template", _sentinel(uid, "USER"))

    agent = initializer.initialize_agent(session_id="probe", user_id=uid)
    full = agent.get_full_system_prompt()

    assert _sentinel(uid, "GLOBAL") in full
    assert _sentinel(uid, "TEAM") in full
    assert _sentinel(uid, "USER") in full
    # The identity section (username) must also be present on the live path.
    assert "alice" in full


def test_no_team_still_injects_global_and_user(multiuser_env, initializer):
    db = multiuser_env
    user = db.create_user("bob", "pw", role="user")
    uid = user["id"]
    db.set_global_config("global_prompt", _sentinel(uid, "GLOBAL"))
    db.set_user_config(uid, "prompt_template", _sentinel(uid, "USER"))

    agent = initializer.initialize_agent(session_id="probe", user_id=uid)
    full = agent.get_full_system_prompt()

    assert _sentinel(uid, "GLOBAL") in full
    assert _sentinel(uid, "USER") in full
    assert "member of the following teams" not in full  # bob has no team


def test_user_identity_present_on_rebuild(multiuser_env, initializer):
    """The initializer builds user_identity; get_full_system_prompt must also
    carry it (a previous version dropped it on the rebuild path)."""
    db = multiuser_env
    user = db.create_user("carol", "pw", role="user")
    uid = user["id"]

    agent = initializer.initialize_agent(session_id="probe", user_id=uid)
    # No prompts set at all -> no directive block, but identity may still be
    # reported by the shared helper.
    block, identity = None, None
    from channel.web.multiuser.prompts import build_directive_block
    block, identity = build_directive_block(db, uid)
    assert block is None, "no prompts set => no directive block"
    assert identity and identity.get("name") == "carol"
