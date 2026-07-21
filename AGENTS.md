# feat-multiuser — 多使用者認證、對話隔離與知識庫分享系統

> **Phase 1-2** — 後端核心 & 知識庫隔離 (已完成)  \
> **Phase 3** — Team Scope & 三層 Prompt 繼承 (✅ 已完成)  \
> **Phase 4** — RBAC Manager 角色 & 企業功能 (規劃中)  \
> **戰略基礎** — [三層 Scope (global/team/user) + Prompt 繼承 + RBAC 擴充](#0-戰略願景-strategic-vision)  \
> Branch: `feat-multiuser`  \
> Base: `main` (upstream `anomixer/CowAgent`)

---

## 目錄

0. [戰略願景](#0-戰略願景-strategic-vision)
1. [動機與目標](#1-動機與目標)
2. [系統架構](#2-系統架構)
3. [資料庫層 — `multiuser/db.py`](#3-資料庫層--multiuserdbpy)
4. [認證中間件 — `multiuser/auth.py`](#4-認證中間件--multiuserauthpy)
5. [Route Handler — `web_channel.py`](#5-route-handler--web_channelpy)
6. [對話隔離管線](#6-對話隔離管線)
7. [向後相容設計](#7-向後相容設計)
8. [安全考量](#8-安全考量)
9. [知識庫隔離與分享機制](#9-知識庫隔離與分享機制)
10. [前端 UI 變更](#10-前端-ui-變更)
11. [已修改檔案索引](#11-已修改檔案索引)
12. [Phase 2 完成項目與展望](#12-phase-2-完成項目與展望)
13. [Phase 3 — Team Scope & 三層 Prompt 繼承](#13-phase-3--team-scope--三層-prompt-繼承)

---

## 0. 戰略願景 (Strategic Vision)

> 本分支的發展方向，奠基於 playerr (anomixer) 在 2026-07-09 寫給原作者 zhayujie 的信中提出的架構。
> 完整內容請參閱 [知識庫](../sources/letter-to-zhayujie-2026-07-09.md)。

### 核心架構：三層 Scope

```
┌─────────────────────────────────────────┐
│  🌐 global                               │
│  全域共享知識庫 / 系統設定 / 公共技能     │
│  由 Admin 管理                            │
│  ┌───────────────────────────────────┐   │
│  │  👥 team                           │   │
│  │  部門或群組共享空間                 │   │
│  │  由 Manager 管理                   │   │
│  │  ┌─────────────────────────────┐   │   │
│  │  │  👤 user                     │   │   │
│  │  │  個人私有對話/記憶/知識庫    │   │   │
│  │  │  完全隔離，預設私有          │   │   │
│  │  └─────────────────────────────┘   │   │
│  └───────────────────────────────────┘   │
└─────────────────────────────────────────┘
```

**原則**：
- 記憶與知識庫**預設收在 user scope**，再視需要 promote 到 team / global
- 避免記憶串味，也不至於把所有東西切得太死

### Prompt / Persona 繼承架構

```
Admin 基礎 system prompt  /  品牌規範  /  安全邊界
        │
        ▼
Team  部門層級覆蓋  /  共用 context
        │
        ▼
User  個人化微調  /  私有 context
```

### RBAC 角色模型（三層）

| 角色 | 權限範圍 |
|------|----------|
| **admin** | 全域設定、模型、知識庫、成員管理、審計 |
| **manager** | team scope 內的共享資源管理 |
| **user** | 自己的內容 + 被授權的 team/global 資源 |

### 商業化願景：CowAgent Pro

| 版本 | 功能 |
|------|------|
| **基礎企業版** | 多用戶登入 + 基礎知識庫隔離 |
| **專業企業版** | + team scope、prompt override、audit log |
| **企業定制版** | + SSO、私有化部署、客製整合、SLA 支援 |

### Channel 綁定（未來方向）

不同 team 可綁定不同 Slack / 企微 / 飛書機器人；同一組織下不同 user 綁自己的 channel 身份，使 CowAgent 從個人助理進化為組織級 Agent Infrastructure。

---

## 1. 動機與目標

原本的 CowAgent Web Channel 使用**單一靜態密碼**（`web_password`）保護整台機器。所有共用此密碼的人共用同一個 session 空間、同一組對話歷史、同一份知識庫。這有幾個問題：

- **無法區分不同使用者**的對話與知識
- **共用密碼**難以管理與撤銷（換密碼 = 通知所有人）
- **不適合**企業/團隊部署場景

### 目標

- 引入 **username + password** 的使用者帳號系統
- 支援 **RBAC**（Admin / User 兩種角色）
- 實作**對話隔離**：使用者只看得到自己的 Session
- **向後相容**：沒人註冊時仍可沿用舊有單密碼模式
- **零外部依賴**：只用 Python 標準函式庫

---

## 2. 系統架構

```
外部請求
    │
    ▼
web_channel.py  URL Router
    │
    ├── AuthCheckHandler  ← 判斷目前是哪種模式 + 是否已登入
    ├── AuthLoginHandler  ← 支援 legacy 密碼 / multiuser username+password
    ├── AuthLogoutHandler
    │
    ├── RegisterHandler    ← (新) 註冊，第一人自動成 admin
    ├── MeHandler          ← (新) 查詢目前登入者
    ├── AdminUsersHandler  ← (新) Admin 管理使用者列表/新增
    ├── AdminUserDetailHandler ← (新) Admin 刪除/修改使用者
    │
    ├── SessionsHandler    ← 對話列表（多使用者模式按 user_id 過濾）
    │
    └── MessageHandler     ← 發送訊息（context 帶入 user_id）
            │
            ▼
        AgentBridge
            │
            ├── _pre_persist_user_message()   ← 讀取 context["user_id"]
            ├── _persist_messages()            ← 傳 user_id 給 store
            │
            ▼
        ConversationStore.append_messages()
            │                          ┌─────────────────┐
            └── session.user_id ──────►│ conversations.db │
                                       │   sessions 表    │
                                       │   user_id 欄位   │
                                       └─────────────────┘

multiuser/
├── db.py       ──►  mu_users / mu_sessions 表 (獨立 SQLite)
└── auth.py     ──►  認證邏輯 + cookie 管理
```

### 雙模式流程圖

```
系統啟動
    │
    ▼
AuthCheckHandler.GET()
    │
    ├── mu_users 表格是空的？ ──► Legacy 模式 (單密碼)
    │                              │
    │                              ▼
    │                          auth/login ← 只收 password
    │                          auth/check ← 回傳 auth_required + authenticated
    │
    └── mu_users 有資料？  ──► Multi‑user 模式
                               │
                               ▼
                           auth/login ← 收 username + password
                           auth/register ← 註冊（第一人自動 admin）
                           auth/me ← 查目前使用者
                           auth/users ← Admin CRUD
                           auth/change-password ← 修改密碼
                           auth/check ← 回傳 user + multiuser: true
```

### 知識庫隔離與分享流程

```
使用者上傳知識庫檔案
    │
    ▼
MemoryManager.sync()
    │
    ├── knowledge/users/{user_id}/*.md  ──► scope="user", user_id=該使用者
    └── knowledge/*.md (其他路徑)        ──► scope="shared", user_id=null
            │
            ▼
記憶查詢 (search)
    │
    ├── 只搜自己的知識庫 (user_id=自己)
    └── 若有分享 (shared_user_ids): 也併入分享者的知識庫
            │
            ▼
KnowledgeShareHandler
    ├── POST /api/knowledge/shares      ──► 建立知識庫分享
    ├── GET  /api/knowledge/shares      ──► 列出（我分享的 + 別人分享給我的）
    └── DELETE /api/knowledge/shares/:id ──► 移除分享
```

### 三層 Prompt 注入流程 (Phase 3, 雙層保障機制)

**第一層：System Prompt 末端注入 (Section 9 — `extra_system_suffix`)**

```
agent_initializer.py  initialize_agent()
    │
    ├── 1. 載入 Global Prompt（從 mu_global_configs 表）
    │
    ├── 2. 載入 Team Prompt（從 mu_teams.prompt 欄位）
    │
    ├── 3. 載入 User Prompt（從 mu_user_configs, user_id=該使用者）
    │
    └── 設定 agent.extra_system_suffix = <!--multiuser--> 區塊
         base system prompt (from workspace)
            + 🌐 全域提示詞  (admin 設定，對所有人生效)
            + 👥 團隊資訊    (成員身份 + 團隊 prompt)
            + 📝 使用者提示詞 (個人微調)
            + [Section 9] 🛑 最高硬性強制指令 (Override Declaration)
```

**第二層：Ephemeral Reminder Injection（每次 API Call 前注入）**

> **核心突破**：LLM 對「最後幾個 token」給予最高的 recency attention。
> 即使 AGENT.md / BOOTSTRAP.md 裡有衝突的風格設定，最後看到的指令才是 LLM 真正遵從的。

```
AgentStreamExecutor._call_llm_stream()  (agent_stream.py)
    │
    ├── messages = self._prepare_messages()   ← 真實對話歷史
    │
    ├── 若 agent.extra_system_suffix 含 <!--multiuser-->：
    │      messages = messages + [EPHEMERAL {role:user, content: 強制提醒}]
    │      （不寫入 self.messages，不污染對話歷史）
    │
    └── LLMRequest(messages=messages, system=self.system_prompt)
              ↑ LLM 最後看到的就是強制提醒
```

**保障範圍**：

| 情境 | 保障狀態 |
|------|----------|
| onboarding 第 1 輪（BOOTSTRAP.md 在線） | ✅ Ephemeral 蓋過 BOOTSTRAP.md 劇本 |
| onboarding 中間（tool call 寫入 AGENT.md） | ✅ 每個 LLM turn 都注入 |
| onboarding 後一般對話 | ✅ AGENT.md emoji 設定不影響 |
| Single User（無 extra_system_suffix） | ✅ 完全跳過，AGENT.md 自然行為保留 |

> **快取刷洗**：當 Admin 或 User 在 UI 更新 Prompt 時，`UserConfigHandler` / `GlobalConfigHandler` / `TeamDetailHandler` 會調用 `AgentBridge.clear_agent_cache()`，確保下一次對話即刻套用最新 Prompt。


---
---

## 3. 資料庫層 — `multiuser/db.py`

**路徑**: `channel/web/multiuser/db.py`  \
**依賴**: 僅 `sqlite3`, `hashlib`, `hmac`, `os`, `time`, `threading`, `logging`

### 表格結構

#### `mu_users`

| 欄位 | 型態 | 說明 |
|------|------|------|
| `id` | INTEGER PK AUTOINCREMENT | 使用者 ID |
| `username` | TEXT UNIQUE NOT NULL | 使用者名稱 (3-32 字元) |
| `password_hash` | TEXT NOT NULL | PBKDF2-SHA256 雜湊 (附帶 salt) |
| `role` | TEXT NOT NULL DEFAULT 'user' | `admin` 或 `user` |
| `created_at` | INTEGER | Unix timestamp |
| `updated_at` | INTEGER | Unix timestamp |

#### `mu_sessions`

| 欄位 | 型態 | 說明 |
|------|------|------|
| `id` | TEXT PK | 亂數 session token |
| `user_id` | INTEGER NOT NULL | 對應 `mu_users.id` |
| `created_at` | INTEGER | Unix timestamp |
| `expires_at` | INTEGER | 過期時間戳 (預設 7 天) |

#### `mu_teams` (Phase 3)

| 欄位 | 型態 | 說明 |
|------|------|------|
| `id` | INTEGER PK AUTOINCREMENT | 團隊 ID |
| `name` | TEXT UNIQUE NOT NULL | 團隊名稱 |
| `description` | TEXT NOT NULL DEFAULT '' | 團隊描述 |
| `prompt` | TEXT NOT NULL DEFAULT '' | 團隊共享的提示詞模板 |
| `created_by` | INTEGER NOT NULL | FK → mu_users.id |
| `created_at` | INTEGER | Unix timestamp |
| `updated_at` | INTEGER | Unix timestamp |

#### `mu_team_members` (Phase 3)

| 欄位 | 型態 | 說明 |
|------|------|------|
| `id` | INTEGER PK AUTOINCREMENT | 成員記錄 ID |
| `team_id` | INTEGER NOT NULL | FK → mu_teams.id |
| `user_id` | INTEGER NOT NULL | FK → mu_users.id |
| `role` | TEXT NOT NULL DEFAULT 'member' | `admin` 或 `member` |
| `joined_at` | INTEGER | Unix timestamp |
| UNIQUE(team_id, user_id) | 避免重複加入 |

#### `mu_user_configs` (Phase 3)

| 欄位 | 型態 | 說明 |
|------|------|------|
| `id` | INTEGER PK AUTOINCREMENT | 設定記錄 ID |
| `user_id` | INTEGER NOT NULL | FK → mu_users.id |
| `config_key` | TEXT NOT NULL | 設定名稱 (如 prompt_template) |
| `config_value` | TEXT NOT NULL DEFAULT '' | 設定值 |
| `updated_at` | INTEGER | Unix timestamp |
| UNIQUE(user_id, config_key) | 避免衝突 |

#### `mu_global_configs` (Phase 3)

| 欄位 | 型態 | 說明 |
|------|------|------|
| `config_key` | TEXT PK | 設定名稱 (如 global_prompt) |
| `config_value` | TEXT NOT NULL DEFAULT '' | 設定值 |
| `updated_at` | INTEGER | Unix timestamp |

#### `mu_kb_shares` (Phase 2)

| 欄位 | 型態 | 說明 |
|------|------|------|
| `id` | INTEGER PK AUTOINCREMENT | 分享記錄 ID |
| `owner_id` | INTEGER NOT NULL | 知識庫擁有者（FK → mu_users.id） |
| `shared_with_id` | INTEGER NOT NULL | 被分享者（FK → mu_users.id） |
| `permission` | TEXT NOT NULL DEFAULT 'read' | 權限：`read`（目前僅支援唯讀） |
| `created_at` | INTEGER | Unix timestamp |
| UNIQUE(owner_id, shared_with_id) | 防止重複分享 |

### 密碼雜湊機制

```python
hash = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, iterations=600000)
```

- **PBKDF2-SHA256** 搭配 **16 bytes 隨機 salt**
- **600,000 次疊代**（符合 OWASP 2023 建議 > 600K）
- 使用 `hmac.compare_digest` 進行**常數時間比對**，防 timing attack
- 零外部依賴（只用 `hashlib` + `hmac`）

### 公開 API

```python
class MultiUserDB:
    # ── User management ──
    def create_user(username, password, role="user") -> dict | None
    def authenticate(username, password) -> dict | None
    def get_user_by_id(user_id) -> dict | None
    def get_user_by_username(username) -> dict | None
    def list_users() -> list[dict]
    def update_user_role(user_id, new_role) -> bool
    def update_user_password(user_id, new_password) -> bool
    def delete_user(user_id) -> bool
    def count_users() -> int

    # ── Sessions ──
    def create_session(user_id) -> str
    def get_session(session_token) -> dict | None
    def delete_session(session_token) -> bool
    def cleanup_expired_sessions() -> int

    # ── Teams (Phase 3) ──
    def create_team(name, description, prompt="", created_by) -> dict | None
    def get_team(team_id) -> dict | None
    def list_teams() -> list[dict]
    def list_user_teams(user_id) -> list[dict]
    def update_team(team_id, name=None, description=None, prompt=None) -> bool
    def delete_team(team_id) -> bool
    def add_team_member(team_id, user_id, role="member") -> dict | None
    def remove_team_member(team_id, user_id) -> bool
    def update_team_member_role(team_id, user_id, role) -> bool
    def list_team_members(team_id) -> list[dict]
    def get_user_team_ids(user_id) -> list[int]
    def is_team_admin(team_id, user_id) -> bool

    # ── User config (Phase 3) ──
    def set_user_config(user_id, config_key, config_value) -> bool
    def get_user_config(user_id, config_key) -> str | None
    def get_all_user_configs(user_id) -> dict
    def delete_user_config(user_id, config_key) -> bool

    # ── Global config (Phase 3, 獨立 mu_global_configs 表) ──
    def set_global_config(config_key, config_value) -> bool
    def get_global_config(config_key) -> str | None

    # ── Knowledge shares (Phase 2) ──
    def create_share(owner_id, shared_with_id, permission="read") -> dict | None
    def remove_share(share_id, owner_id) -> bool
    def list_shares_by_owner(user_id) -> list[dict]
    def list_shares_for_user(user_id) -> list[dict]
    def get_shared_user_ids(user_id) -> list[int]

    # ── Conversation isolation ──
    def get_user_conversation_sessions(user_id, ...) -> dict

    # ── Migrations ──
    def ensure_conversation_user_id_column()
```

### 對話隔離查詢

`get_user_conversation_sessions()` 在 multi-user 模式時取代原本的 `store.list_sessions()`，用 **SQL JOIN** 只撈出屬於該使用者的 session：

```sql
SELECT s.* FROM sessions s
WHERE s.user_id = ?
  AND s.channel_type = ?
ORDER BY s.last_active DESC
LIMIT ? OFFSET ?
```

### 現有資料遷移

`ensure_conversation_user_id_column()` 在初始化時檢查 `conversations.db` 的 `sessions` 表是否有 `user_id` 欄位，沒有的話自動 `ALTER TABLE ADD COLUMN`。**現有資料不會遺失**。

Phase 3 另加 migration：`ALTER TABLE mu_teams ADD COLUMN prompt TEXT NOT NULL DEFAULT ''`

---

## 4. 認證中間件 — `multiuser/auth.py`

**路徑**: `channel/web/multiuser/auth.py`  \
**依賴**: `db.py`, `web.py` (web 框架)

### 功能函式

```python
def is_multiuser_enabled() -> bool
    """檢查 mu_users 是否有資料；有的話切換為多使用者模式"""

def get_current_user() -> dict | None
    """從 cookie 'mu_session' 解析出目前登入的使用者資訊"""

def require_login()
    """若未登入則 raise web.forbidden() (401)"""

def require_admin() -> dict
    """若無管理權限則 raise web.forbidden() (403)；成功回傳 user dict"""

def login_user(username, password) -> dict | None
    """驗證帳密 → 建立 session → 設定 cookie → 回傳 user"""
    # 回傳 {"user": {...}, "session_token": "..."}

def logout_current_user()
    """清除 cookie + 刪除 session"""

def set_session_cookie(session_token)
    """設定 httponly cookie 'mu_session'，Secure 旗標只在 HTTPS 時啟用"""

def clear_session_cookie()
    """清除 mu_session cookie"""

def ensure_first_user_is_admin() -> bool
    """檢查是否為第一個使用者（註冊流程用）"""
```

### Cookie 安全設定

```python
web.setcookie(
    "mu_session", session_token,
    expires=expire_seconds,
    path="/",
    httponly=True,           # JavaScript 無法讀取
    samesite="Lax",          # 防 CSRF
)
```

- **httponly**: 防止 XSS 竊取 session
- **samesite=Lax**: 防止 CSRF 攻擊
- **Secure**: 只在 HTTPS 時啟用（自動偵測）
- **Session 有效期限**: 7 天（可在 `conf().get("web_session_expire_days", 7)` 調整）

---

## 5. Route Handler — `web_channel.py`

**路徑**: `channel/web/web_channel.py`  \
**改動量**: +~420 行（Phase 1-3 累計）

### 新增 Route

| Method | Route | Handler | 權限 | Phase | 說明 |
|--------|-------|---------|------|:----:|------|
| POST | `/api/auth/register` | `RegisterHandler` | 公開 | P1 | 註冊（第一人 = admin） |
| GET | `/api/auth/me` | `MeHandler` | 登入 | P1 | 查詢目前使用者 |
| POST | `/api/auth/change-password` | `ChangePasswordHandler` | 登入 | P2 | 修改密碼 |
| GET | `/api/auth/users` | `AdminUsersHandler` | Admin | P1 | 使用者列表 |
| POST | `/api/auth/users` | `AdminUsersHandler` | Admin | P1 | 新增使用者 |
| GET/PUT/DELETE | `/api/auth/users/:id` | `AdminUserDetailHandler` | Admin | P1 | 使用者 CRUD |
| GET/PUT | `/api/auth/my-config` | `UserConfigHandler` | 登入 | P3 | 個人設定 (prompt_template) |
| GET/PUT | `/api/auth/global-config` | `GlobalConfigHandler` | Admin | P3 | 全域設定 (global_prompt) |
| GET/POST | `/api/teams` | `TeamsHandler` | Admin | P3 | 團隊列表/建立 |
| GET/PUT/DELETE | `/api/teams/:id` | `TeamDetailHandler` | Admin | P3 | 團隊 CRUD |
| GET/POST | `/api/teams/:id/members` | `TeamMembersHandler` | Admin | P3 | 成員列表/新增 |
| PUT/DELETE | `/api/teams/:id/members/:uid` | `TeamMemberDetailHandler` | Admin | P3 | 成員角色/踢出 |
| POST | `/api/teams/:id/members/leave` | `TeamMemberLeaveHandler` | 登入 | P3 | 退出團隊 |
| GET/POST | `/api/knowledge/shares` | `KnowledgeShareHandler` | 登入 | P2 | 知識庫分享 |
| DELETE | `/api/knowledge/shares/:id` | `KnowledgeShareDetailHandler` | 登入 | P2 | 移除分享 |

### 修改的 Handler

#### `AuthCheckHandler.GET`

**Legacy 模式** (維持不變):
```json
{"status": "success", "auth_required": true, "authenticated": true/false}
```

**Multi-user 模式**:
```json
{
  "status": "success",
  "auth_required": true,
  "authenticated": true,
  "user": {"id": 1, "username": "playerr", "role": "admin"},
  "multiuser": true
}
```

### `_check_auth()` — web_password 繞過

在 multiuser 模式下，`_check_auth()` 在**最頂頭**就回傳 `True`，完全繞過 `web_password` 的檢查邏輯：

```python
def _check_auth():
    if is_multiuser_enabled():
        return True
    ...
```

### `ConfigHandler.GET` — 新增 `multiuser` 標誌

```json
{
  "status": "success",
  "multiuser": true,
  "web_password_masked": "...",
  ...
}
```

### `ConfigHandler.POST` — 安全閥

```python
if is_multiuser_enabled():
    updates.pop("web_password", None)
```

#### `AuthLoginHandler.POST`

根據 `is_multiuser_enabled()` 自動決定驗證方式：

- **Legacy**: 收 `password`，比對 `web_password`
- **Multi-user**: 收 `username` + `password`，呼叫 `mu_login_user()`

---

## 6. 對話隔離管線

對話 session 的 `user_id` 歸屬流程：

```
使用者發送訊息
    │
    ▼
post_message()
    ├── 偵測 multiuser 模式
    ├── 讀取 get_current_user()["id"]
    └── context["user_id"] = mu_user["id"]
           │
           ▼
AgentBridge.agent_reply()
    │
    ├── _pre_persist_user_message(session_id, query, context, ...)
    │       └── store.append_messages(..., user_id=user_id)
    │
    └── _persist_messages(session_id, new_messages, channel_type, user_id)
            └── store.append_messages(..., user_id=user_id)
                   │
                   ▼
            ConversationStore.append_messages()
                └── UPDATE sessions SET user_id = ? WHERE session_id = ? AND user_id = 0
```

**關鍵設計**: `AND user_id = 0` 確保只有「第一次建立」時會寫入，後續訊息不會覆蓋已存在的 owner。

## 7. 向後相容設計

### 切換邏輯

```
mu_users 有資料？ ─YES─► Multi-user 模式
       │
      NO
       │
       ▼
web_password 有設定？ ─YES─► Legacy 單密碼模式
       │
      NO
       │
       ▼
無須認證（開放模式）
```

### 重點

1. **使用者沒註冊前**，系統行為跟原來完全一樣
2. **一旦有人註冊**，自動切換為 multi-user 模式，legacy 密碼登入失效
3. **現有對話 session** 因為 `user_id = 0`（預設值），不會被任何人看到（安全）
4. Admin 可以透過 API 手動將舊 session 指定給某個 user
- **Legacy → Multi-user 是不可逆的**（一旦有人註冊就回不去了）
- 若想 reset，刪除 `multiuser.db` 檔案即可

---

## 8. 安全考量

| 層面 | 實作 |
|------|------|
| 密碼儲存 | PBKDF2-SHA256, 600K iterations, 16 bytes salt |
| Timing attack 防護 | `hmac.compare_digest()` 常數時間比對 |
| Session 劫持 | `httponly` cookie + `samesite=Lax` |
| XSS | Cookie 無法被 JavaScript 讀取 |
| CSRF | `samesite=Lax` 阻擋跨站請求 |
| Admin 操作 | 所有管理 API 都經 `require_admin()` 檢查 |
| 不能刪除自己 | `DELETE /api/auth/users/:id` 檢查 `id != current_user["id"]` |
| 不能降級自己 | `PUT` role 檢查，避免最後一個 admin 把自己降級 |
| 最小權限 | 一般 user 無法存取 admin API |

### 已知限制

- 無 rate limiting（建議在 Nginx/Caddy 層做）
- 無 email 驗證
- 無 2FA
- Session 被 stolen 後無法單一撤銷

---

## 9. 知識庫隔離與分享機制

### 知識庫目錄隔離

每個使用者的知識庫檔案存放在 `knowledge/users/{user_id}/` 目錄下：

```python
# knowledge/users/{user_id}/... → scope="user", user_id=整数
if rel_parts[:2] == ["knowledge", "users"] and rel_parts[2].isdigit():
    user_id = int(rel_parts[2])
    files_to_scan.append((file_path, "knowledge", "user", user_id))
else:
    files_to_scan.append((file_path, "knowledge", "shared", None))
```

**註冊時自動建立目錄**: `MultiUserDB.create_user()` 呼叫 `_ensure_user_knowledge_dir(user_id)`。

### 搜尋管線 — shared_user_ids 傳遞鏈

```
MemoryManager.search(user_id="1")
    ├── get_shared_user_ids(user_id=1) → [2, 3]  # 從 mu_kb_shares 查
    ├── storage.search_vector(..., shared_user_ids=[2, 3])
    └── storage.search_keyword(..., shared_user_ids=[2, 3])
```

所有搜尋方法都新增了 `shared_user_ids: Optional[List[int]] = None` 參數。

---
## 10. 前端 UI 變更

### HTML 結構 (`chat.html`) — +113 行

**登入遮罩層 (Login Overlay)**
- 支援雙模式顯示：Legacy / Multi-user
- 完整的**註冊表單**（可切換登入/註冊）
- 密碼顯示切換按鈕（眼睛圖示）

**側邊欄 (Sidebar)**
- 新增「👥 使用者管理」選單項目（`role=admin` 才顯示）

**頂部標題列 (Header)**
- 使用者下拉選單：頭像縮寫、角色標籤、個人設定、使用者管理、退出登入

**主要內容區**
- `#view-profile` 容器 → 個人設定頁面（含 Global Prompt 編輯器，admin only）
- `#view-users` 容器 → 管理員使用者管理頁面

### JavaScript 邏輯 (`console.js`) — +~750 行

**i18n 翻譯擴充** — 三種語系共補了 40+ 個字串（含 global prompt）

**認證流程重寫 (~300 行)**：雙模式登入、註冊、使用者下拉選單

**Admin 使用者管理**：列表、新增、修改角色、刪除

**個人設定頁面**：
- 修改密碼
- User Prompt 編輯器（所有人）
- Global Prompt 編輯器（admin only）— `/api/auth/global-config`

**Global Prompt 前端函數**：
| 函數 | 說明 |
|------|------|
| `loadGlobalPrompt()` | GET `/api/auth/global-config?key=global_prompt` |
| `saveGlobalPrompt()` | PUT `/api/auth/global-config` |
| `clearGlobalPrompt()` | PUT 空值清除 |

---

## 11. 已修改檔案索引

### v2.0 (2026-07-19) 安全性強化與路徑修正

| 類別 | 項目 | 說明 |
|------|------|------|
| **Fix** | DB 路徑統一 | `get_default_db_path()` 從 `__file__` 改為 `agent_workspace`（`~/cow/sessions/`） |
| **Security** | 保護主 admin（id=1） | 其他 admin 無法刪除或降級第一個管理員 |
| **Fix** | Team Members 不能改 role | role 只能在加入時設定，加入後 locked |
| **Feat** | Edit Team UI | Team Members 頁面新增編輯按鈕，可改名稱與 Prompt |
| **Fix** | `config-template.json` | 補上 `"multi_user": false` |
| **Fix** | User Prompt 標題混淆 | 原「系統提示詞」改為「個人提示詞」/「Personal Prompt」，避免與 Global Prompt 混淆 |
| **Fix** | user 看團隊成員數為 0 | `list_user_teams()` SQL 補 `member_count` subquery |
| **Fix** | Global Prompt 存檔 FK 失敗 | `set_global_config()` 原本用 `user_id=-1` sentinel 撞 FK，改獨立 `mu_global_configs` table |
| **Fix** | agent_initializer.py 縮排全壞（1空格→4空格） | 三層Prompt commit 意外把所有縮排從4空格變1空格，導致 IndentationError，Agent 模式 fallback |
| **Fix** | `initialize_agent()` 不認 `team_ids` | `agent_bridge.py` 傳了不存在的參數，TypeError 被 try/except 吃掉 |
| **Fix** | Agent 模式 fallback 到 normal mode | 修好縮排後，三層 Prompt 注入恢復正常運作 |

### 新增檔案

| 檔案 | 行數 | 說明 |
|------|------|------|
| `channel/web/multiuser/__init__.py` | 0 | Package marker |
| `channel/web/multiuser/db.py` | ~970 | 資料庫層 (mu_users, mu_sessions, mu_teams, mu_team_members, mu_user_configs, mu_kb_shares) |
| `channel/web/multiuser/auth.py` | ~200 | 認證中間件 |

### 修改檔案

| 檔案 | 說明 |
|------|------|
| `channel/web/web_channel.py` | +15 routes；移除 `TeamMemberDetailHandler.PUT`（禁止 role 變更）；保護主 admin |
| `channel/web/chat.html` | 登入/註冊 UI、使用者選單、profile/users view 容器 |
| `channel/web/static/js/console.js` | 前端邏輯 + i18n + Global Prompt admin UI；移除 role 下拉；保護主 admin UI；新增 Edit Team；Prompt 標題改名 |
| `channel/web/multiuser/db.py` | `list_user_teams()` 補 `member_count` subquery（user 看團隊人數正確） |
| `config-template.json` | 新增 `"multi_user": false` |
| `bridge/agent_bridge.py` | `_pre_persist_user_message` + `_persist_messages` 串接 user_id |
| `bridge/agent_initializer.py` | 三層 Prompt 注入 (Global → Team → User) |
| `agent/memory/conversation_store.py` | `append_messages` 新增 `user_id` 參數 |
| `agent/memory/storage.py` | 搜尋方法新增 `shared_user_ids` 參數 |
| `agent/memory/manager.py` | sync() 掃描 `knowledge/users/{user_id}/`；search() 傳遞 `shared_user_ids` |
| `README.md` | 新增英文 Multi-User 說明章節 |
| `docs/zh/README-Hant.md` | 新增繁體中文多使用者說明章節 |
| `AGENTS.md` | 本文件 |

---

## 12. Phase 2 完成項目與展望

### Phase 2 已實作 ✅

| 類別 | 項目 | 狀態 |
|------|------|:----:|
| **後端** | 知識庫目錄隔離 `knowledge/users/{id}/` | ✅ |
| **後端** | `MemoryManager.sync()` 掃描使用者知識庫 | ✅ |
| **後端** | 搜尋管線 `shared_user_ids` 傳遞鏈（4 種 search method） | ✅ |
| **後端** | `mu_kb_shares` 分享表 + 完整 CRUD | ✅ |
| **後端** | `KnowledgeShareHandler` + route | ✅ |
| **後端** | `ChangePasswordHandler`（multiuser + legacy 雙模式） | ✅ |
| **後端** | `create_user()` 自動建立知識庫目錄 | ✅ |
| **前端** | 登入/註冊 UI（雙模式） | ✅ |
| **前端** | Admin 使用者管理頁面（CRUD） | ✅ |
| **前端** | i18n 翻譯（zh / zh-Hant / en）30+ 字串 | ✅ |
| **文件** | AGENTS.md Phase 2 完整記錄 | ✅ |

---

## 13. Phase 3 — Team Scope & 三層 Prompt 繼承

### 已實作 ✅

| 類別 | 項目 | 狀態 |
|------|------|:----:|
| **DB** | `mu_teams` / `mu_team_members` / `mu_user_configs` 三表 DDL + CRUD | ✅ |
| **DB** | `mu_teams.prompt` 欄位 + ALTER TABLE migration | ✅ |
| **DB** | `get_global_config()` / `set_global_config()`（user_id=-1 sentinel） | ✅ |
| **API** | Team CRUD API（TeamsHandler, TeamDetailHandler） | ✅ |
| **API** | Team Member CRUD（TeamMembersHandler, TeamMemberDetailHandler） | ✅ |
| **API** | Team Member Leave（TeamMemberLeaveHandler） | ✅ |
| **API** | UserConfigHandler（GET/PUT /api/auth/my-config） | ✅ |
| **API** | GlobalConfigHandler（GET/PUT /api/auth/global-config） | ✅ |
| **API** | TeamsHandler POST / TeamDetailHandler PUT 收 prompt 欄位 | ✅ |
| **Bridge** | 三層 Prompt 注入：Global → Team → User | ✅ |
| **前端** | Team 管理 UI（建立、編輯、成員管理、退出） | ✅ |
| **前端** | Team Prompt 編輯器（建立/編輯時設定） | ✅ |
| **前端** | User Prompt 編輯器（個人 profile） | ✅ |
| **前端** | Global Prompt 編輯器（admin profile） | ✅ |
| **前端** | 分享 UI 整合（knowledge shares tab） | ✅ |
| **Memory** | per-user memory scope（`MemorySearchTool` 傳遞 user_id） | ✅ |
| **Memory** | per-user language preference（`UserConfigHandler`） | ✅ |
| **文件** | AGENTS.md Phase 3 完整記錄 | ✅ |

### 三層 Prompt 注入架構

```
base system prompt (from workspace files)
    + 🌐 全域提示詞  (admin 設定，對所有人生效)
    + 👥 團隊資訊    (所屬團隊列表 + 團隊 shared prompt)
    + 📝 使用者提示詞 (個人微調)
```

每一層都是 **append** 而非覆蓋，越個人化的 prompt 越在後面、權重越高。

### 2026-07-20 — Prompt 注入實測與 Bug 確認 🐮

#### 今日測試摘要

**目標**: 驗證三層 Prompt (Global → Team → User) 在 multiuser 模式下是否正確注入

**測試環境**:
- 本地開發機 `~/cowagent-multiuser` branch `feat-multiuser`
- 測試機 `~/cowagent` branch `feat-multiuser`（從 github pull）
- 設定 admin user prompt: `personal prompt, append a dog emoji 🐶 to every end of chat`
- 設定 global prompt: `global prompt, append a cat emoji 🐱 to every end of chat`

#### Prompt 注入實測演化與終極架構

| 方法 | commit | 結果 | 原因 |
|:-----|:-------|:-----|:------|
| **A. ContextFile 插入 position 1**（不改 AGENT.md 硬碟） | `d75fb416` ~ `9ebd3d51` | ❌ LLM 完全忽略 prompt，跑 BOOTSTRAP.md onboarding | ContextFile 雖在 system prompt 中，但 LLM 沒 `read` 它就回話 |
| **B. AGENT.md 硬碟 prepend + `<!--multiuser-->` marker** | `05278d5b` | ⚠️ 磁碟污染風險 | 會修改實體磁碟 `AGENT.md`，多使用者並行連線會有 Race Condition |
| **C. ContextFile 插入 AGENT.md 頂端** | `ccdb6758` | ⚠️ 部分生效 (🐱無🐶有) | 位於 System Prompt 中段 (Section 6)，會被 Section 8 結尾風格語法 (`🐄`) 產生的近因效應覆蓋 |
| **D. `extra_system_suffix` 頂級注入 (Section 9)** | `f463fe04` ~ `1ad1e30a` | ✅ **終極修復 (100% 成功)** | 放在 System Prompt 最末端 Section 9，結合最高硬性強制指令 (`Supreme Mandatory Directives`) 達到最強注意力權重 |

**關鍵教訓與 Prompt 最佳實踐**：
1. **`extra_system_suffix` 才是王道** — 三層 Prompt (Global/Team/User) 必須放在 System Prompt 的最末端 (Section 9)，避開中段 `AGENT.md` 或 Section 8 預設風格的近因效應。
2. **`user_id` 必須全鏈條持久化** — 在 `Agent.__init__`、`create_agent`、`AgentInitializer` 與 `get_full_system_prompt()` 動態重建時皆須持有 `user_id`，避免第 1 輪 LLM 思考動態重構 Prompt 時抹除三層 Prompt。
3. **Few-Shot 歷史慣性處置** — 新對話 100% 呈現最新 Prompt 效果；舊對話包含歷史回答慣性，需在 Section 9 提示詞中加入「歷史慣性排除規則」，且開新對話 (New Chat) 可獲 100% 乾淨效果。

#### 🟢 Bug 修復與重構進度（2026-07-21 完成）

> **已全面修正**：三層 Prompt 繼承與多使用者對話隔離相關問題已被徹底修復並驗證。

##### Bug #1: Prompt 跨使用者錯亂、動態抹除與近因效應 (Fixed ✅)
- **狀態**: ✅ 已全面修復 (Section 9 Extra System Suffix Injection)
- **修復方案**:
  1. 徹底移除改寫磁碟 `AGENT.md` 的行為，改在 **`extra_system_suffix` (Section 9 最末端)** 將 Global -> Team -> User 封裝注入。避開磁碟 Race Condition 並且獲取大模型最高注意力權重 (`f463fe04`)。
  2. 修復 `agent.user_id` 在 `AgentStreamExecutor` 動態 `get_full_system_prompt()` 重建時漏傳導致三層 Prompt 被 Wipe 的 Bug (`96a447d0`)。
  3. `get_agent()` 自動同步既有 Session 的 `user_id`，確保舊 Agent 實例能即時繼承使用者設定 (`1ad1e30a`)。
  4. 新增 `AgentBridge.clear_agent_cache()` 機制，當 Admin/User 在 Web UI 設定保存最新 Prompt 時，即時刷洗快取。

##### Bug #2: 既有對話歷史被刪與數據持久化 Warning (Fixed ✅)
- **狀態**: ✅ 已修復
- **修復方案**: 在 `db.py` 的 `CREATE TABLE IF NOT EXISTS sessions` DDL 中直接定義 `user_id INTEGER NOT NULL DEFAULT 0` 欄位，解決 `no such column: user_id` 報錯與 Sessions 載入異常。

##### Bug #3: 無法新增對話與對話隔離 (Fixed ✅)
- **狀態**: ✅ 已修復並驗證
- **修復方案**: 強化 `web_channel.py` 請求層與 `AgentBridge` 之間的 `user_id` 綁定，多使用者模式下對話與 Session 創建按 `user_id` 嚴格隔離。

#### 測試驗證檢查清單（已通過 ✅）
- [x] admin 開對話 → 🐶 (user prompt) + 🐱 (global prompt) 同時生效
- [x] user2 開對話 → 不該有 🐶（admin 的 prompt），只該有 🐱（global）+ user2 自己的 prompt
- [x] 所有對話 session 重開頁面後要還在
- [x] 新增對話按鈕要正常運作
- [x] 多個 user 切換後 prompt 不混雜

---

## 路線圖

#### ✅ Phase 1 — 後端核心
多使用者認證、Session 管理、RBAC（admin/user）、對話隔離、向後相容

#### ✅ Phase 2 — 知識庫隔離與分享
知識庫目錄隔離、搜尋管線 `shared_user_ids` 傳遞、知識庫分享 CRUD、前端 UI

#### ✅ Phase 3 — Team Scope & 三層 Prompt 繼承
Team CRUD + 成員管理 + 三層 Prompt 注入 (Global/Team/User) + 個人/全域設定 + Section 9 Extra System Suffix 優化

#### 🔲 Phase 4 — RBAC Manager 角色 & 企業功能

| 類別 | 項目 | 優先級 |
|------|------|:------:|
| **後端** | Manager 角色新增（team scope 管理員） | 🔴 高 |
| **後端** | Team 層級知識庫隔離 `knowledge/teams/{id}/` | 🔴 高 |
| **前端** | Manager view：管理 team 成員與共享資源 | 🟡 中 |
| **安全** | Rate Limiting 內建支援 | 🟡 中 |
| **安全** | Session 黑名單（管理員可強制登出特定使用者） | 🟡 中 |
| **測試** | 完整測試全角色 + 分享流程 | 🟡 中 |

#### 🔲 Phase 5 — CowAgent Pro 企業功能

| 類別 | 項目 | 優先級 |
|------|------|:------:|
| **認證** | OAuth / SSO 整合（LDAP, Google, GitHub） | 🟡 中 |
| **認證** | API Token 支援（機器對機器呼叫） | 🟢 低 |
| **監控** | Audit Log 操作日誌（誰做了什麼 + 時間戳 + IP） | 🟡 中 |
| **Channel** | Channel 與 team/user 綁定（Slack/企微/飛書/Telegram/Discord） | 🟢 低 |
| **部署** | 私有化部署安裝腳本 + Docker Compose 範本 | 🟢 低 |
| **商業化** | CowAgent Pro 版本功能標誌（feature flag） | 🟢 低 |

---

> **Author**: CowAgent 🐮  \
> **Date**: 2026-07-21  \
> **Base**: `anomixer/CowAgent`  \
> **Branch**: `feat-multiuser`  \
> **Status**: Phase 1 ✅ + Phase 2 ✅ + Phase 3 ✅ (三層 Prompt 繼承與 Section 9 最終極優化完成) 🎉

