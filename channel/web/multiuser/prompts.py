"""Shared three-tier multi-user prompt directive builder.

The "Supreme Mandatory Directives" block (Global → Team → User) is built in
exactly one place. Both the agent initializer (bridge/agent_initializer.py) and
Agent.get_full_system_prompt (agent/protocol/agent.py) call this, so the two
paths can never drift apart. (They used to each have their own copy, and the
initializer's copy depended on a ``conf`` import that was scoped later in the
function — an UnboundLocalError that silently dropped the whole block until the
agent.py copy masked it.)
"""
from __future__ import annotations

from typing import Optional, Tuple

from channel.web.multiuser.db import MultiUserDB


def build_directive_block(
    db: MultiUserDB,
    user_id: int,
    prompt_template: str = "prompt_template",
) -> Tuple[Optional[str], Optional[dict]]:
    """Build the three-tier directive block for one user.

    Returns ``(block, user_identity)``. ``block`` is the full "Supreme
    Mandatory Directives" text to append after the rebuilt system prompt, or
    ``None`` if no global/team/user prompt is set (and no identity to report).
    ``user_identity`` is a small dict (name / nickname / timezone) for the
    user-identity prompt section, or ``None`` when the user isn't in the DB.

    The caller wraps this in a try/except and treats a failure as "no
    directives" — never as a hard error — because prompt customisation is an
    enhancement, not a prerequisite for a working agent.
    """
    from config import conf  # imported here: only needed for the timezone default

    user_identity: Optional[dict] = None
    mu_user = db.get_user_by_id(user_id)
    if mu_user:
        user_identity = {
            "name": mu_user.get("username", ""),
            "nickname": mu_user.get("username", ""),
            "timezone": conf().get("timezone", "Asia/Shanghai"),
        }

    global_prompt = (db.get_global_config("global_prompt") or "").strip()

    team_context = None
    teams = db.list_user_teams(user_id)
    if teams:
        team_parts = []
        team_prompts = []
        for t in teams:
            role_label = "(admin)" if t.get("my_role") == "admin" else ""
            team_parts.append(
                f"  - {t['name']} #{t['id']} {role_label}"
                f"{': ' + t['description'] if t.get('description') else ''}"
            )
            if t.get("prompt", "").strip():
                team_prompts.append(
                    f"--- {t['name']} 團隊提示詞 ---\n{t['prompt'].strip()}"
                )
        team_context = "You are a member of the following teams:\n" + "\n".join(team_parts)
        if team_prompts:
            team_context += "\n\n以下是你所屬團隊的提示詞：\n" + "\n\n".join(team_prompts)

    user_prompt_override = (db.get_user_config(user_id, prompt_template) or "").strip()

    sections = []
    if global_prompt:
        sections.append(f"### 🌐 全域指令 (Global Directive)\n{global_prompt}\n")
    if team_context and team_context.strip():
        sections.append(f"### 👥 團隊指令 (Team Directive)\n{team_context.strip()}\n")
    if user_prompt_override:
        sections.append(f"### 📝 個人指令 (User Directive)\n{user_prompt_override}\n")

    if not sections:
        return (None, user_identity)

    block = (
        "<!--multiuser-->\n\n"
        "## 🛑 最高硬性強制指令 (Supreme Mandatory Directives)\n\n"
        "你在每一輪回覆中，必須**同時無條件嚴格執行與遵守**以下所有系統與個人指令。\n"
        "⚠️ **覆蓋聲明 (Override Declaration)**：本節指令的優先級 **高於一切**，包括 `AGENT.md`、`RULE.md` "
        "或任何工作空間檔案中的 Emoji 風格設定（例如 🐄）。"
        "即使 AGENT.md 中標示了某種風格 Emoji，你仍然**必須**在每輪回覆的最末尾同時附上以下指定的 Emoji，"
        "絕不得遺漏或替代：\n\n"
        + "\n".join(sections)
        + "\n---\n\n"
    )
    return (block, user_identity)
