# feat-multiuser — 多使用者認證、對話隔離與知識庫分享系統

> **Phase 1-2** — 後端核心 & 知識庫隔離 (已完成)  \
> **Phase 3** — Team Scope (開發中)  \
> **Phase 4-5** — Prompt 繼承 & Pro 企業功能 (規劃中)  \
> **戰略基礎** — [三層 Scope (global/team/user) + Prompt 繼承 + RBAC 擴充](#0-戰略願景-strategic-vision)  
> Branch: `feat-multiuser`  
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

---

## 3. 資料庫層 — `multiuser/db.py`

**路徑**: `channel/web/multiuser/db.py`  
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
    def create_user(username, password, role="user") -> dict | None
    def authenticate(username, password) -> dict | None
    def get_user_by_id(user_id) -> dict | None
    def get_user_by_username(username) -> dict | None
    def list_users() -> list[dict]
    def update_user_role(user_id, new_role) -> bool
    def update_user_password(user_id, new_password) -> bool
    def delete_user(user_id) -> bool
    def count_users() -> int
    def create_session(user_id) -> str           # 回傳 session token
    def get_session(session_token) -> dict | None
    def delete_session(session_token) -> bool
    def cleanup_expired_sessions() -> int
    # 對話隔離
    def get_user_conversation_sessions(user_id, channel_type, page, page_size) -> dict
    # 資料庫初始化 + 遷移
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

---

## 4. 認證中間件 — `multiuser/auth.py`

**路徑**: `channel/web/multiuser/auth.py`  
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

**路徑**: `channel/web/web_channel.py`  
**改動量**: +347 行（Phase 1: +218, Phase 2: +129）

### 新增 Route（Phase 1 — 認證 & 使用者管理）

| Method | Route | Handler | 權限 | 說明 |
|--------|-------|---------|------|------|
| POST | `/api/auth/register` | `RegisterHandler` | 公開 | 註冊（第一人 = admin） |
| GET | `/api/auth/me` | `MeHandler` | 登入 | 查詢目前使用者 |
| POST | `/api/auth/change-password` | `ChangePasswordHandler` | 登入 | 修改密碼（支援 multiuser & legacy） |
| GET | `/api/auth/users` | `AdminUsersHandler` | Admin | 使用者列表 |
| POST | `/api/auth/users` | `AdminUsersHandler` | Admin | 新增使用者 |
| PUT | `/api/auth/users/:id` | `AdminUserDetailHandler` | Admin | 修改角色/密碼 |
| DELETE | `/api/auth/users/:id` | `AdminUserDetailHandler` | Admin | 刪除使用者 |

### 新增 Route（Phase 2 — 知識庫分享）

| Method | Route | Handler | 權限 | 說明 |
|--------|-------|---------|------|------|
| GET | `/api/knowledge/shares` | `KnowledgeShareHandler` | 登入 | 列出我分享的 + 別人分享給我的 |
| POST | `/api/knowledge/shares` | `KnowledgeShareHandler` | 登入 | 建立知識庫分享（指定 shared_with_id + permission） |
| DELETE | `/api/knowledge/shares/:id` | `KnowledgeShareDetailHandler` | 登入+擁有者 | 移除知識庫分享 |

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
        # Multiuser mode: web_password is bypassed entirely.
        # Authentication is handled by mu_session (RequireLogin / RequireAdmin).
        return True
    ...
```

這確保了：
- `ConfigHandler`、`KnowledgeListHandler` 等由 `@_require_auth()` 保護的 handler，在 multiuser 模式下不再檢查 `cow_auth_token`，統一交由 `mu_session` cookie 管理
- 原有的 `get_current_user()` fallback 邏輯已移除（已被頂頭 bypass 取代，更乾淨）
- Legacy 模式下行為完全不變

### `ConfigHandler.GET` — 新增 `multiuser` 標誌

```json
{
  "status": "success",
  "multiuser": true,
  "web_password_masked": "...",
  ...
}
```

前端 `initConfigView()` 可用 `data.multiuser`（或全域 `isMultiuserMode`）判斷是否要灰掉密碼欄位。

### `ConfigHandler.POST` — 安全閥

```python
# Multi-user mode: never allow web_password to be changed via config
if is_multiuser_enabled():
    updates.pop("web_password", None)
```

即使前端繞過 UI 直接發送 `web_password` 更新，後端也會靜默忽略，多一層防護。

#### `AuthLoginHandler.POST`

根據 `is_multiuser_enabled()` 自動決定驗證方式：

- **Legacy**: 收 `password`，比對 `web_password`
- **Multi-user**: 收 `username` + `password`，呼叫 `mu_login_user()`

### Route 順序注意

`web.py` 的 URL mapping 是用 tuple 順序比對的，所以：
```
'/api/auth/users/(.*)', 'AdminUserDetailHandler',   # 有參數 → 放前面
'/api/auth/users', 'AdminUsersHandler',              # 無參數 → 放後面
```

這樣 `/api/auth/users/123` 會進 `AdminUserDetailHandler`，`/api/auth/users` 會進 `AdminUsersHandler`。

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
    │       │
    │       ├── 讀取 context["user_id"]
    │       └── store.append_messages(..., user_id=user_id)
    │
    └── _persist_messages(session_id, new_messages, channel_type, user_id)
            │
            ├── 接收從 context 傳來的 user_id
            └── store.append_messages(..., user_id=user_id)
                   │
                   ▼
            ConversationStore.append_messages()
                │
                └── INSERT OR IGNORE INTO sessions (...)
                └── UPDATE sessions SET user_id = ? WHERE session_id = ? AND user_id = 0
```

**關鍵設計**: `AND user_id = 0` 確保只有「第一次建立」時會寫入，後續訊息不會覆蓋已存在的 owner。

### SessionsHandler 隔離

```python
if is_multiuser_enabled():
    user = get_current_user()
    if user:
        db = get_multiuser_db()
        result = db.get_user_conversation_sessions(user_id=user["id"])
        return json.dumps({"status": "success", **result})
```

使用 `db.get_user_conversation_sessions()` 做 SQL-level 過濾，只回傳屬於該使用者的 session。

---

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

### 切換注意事項

- **Legacy → Multi-user 是不可逆的**（一旦有人註冊就回不去了）
- 如果需要繼續用 legacy 模式，不要註冊任何使用者
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
| 密碼暴力破解 | 無 rate limiting → 建議在前端或反向代理層加上 |
| Admin 操作 | 所有管理 API 都經 `require_admin()` 檢查 |
| 不能刪除自己 | `DELETE /api/auth/users/:id` 檢查 `id != current_user["id"]` |
| 不能降級自己 | `PUT` role 檢查，避免最後一個 admin 把自己降級 |
| 最小權限 | 一般 user 無法存取 admin API |

### 已知限制 (Phase 1)

- 無 rate limiting（建議在 Nginx/Caddy 層做）
- 無 email 驗證
- 無 2FA
- Session 被 stolen 後無法單一撤銷（可改用 redis 實作黑名單）

---

## 9. 知識庫隔離與分享機制

### 知識庫目錄隔離

每個使用者的知識庫檔案存放在 `knowledge/users/{user_id}/` 目錄下，由 `MemoryManager.sync()` 在掃描時自動辨識：

```python
# agent/memory/manager.py — sync() 掃描邏輯
knowledge_dir = Path(workspace_dir).resolve() / "knowledge"
if knowledge_dir.exists():
    for file_path in knowledge_dir.rglob("*.md"):
        rel_str = str(rel.relative_to(workspace))
        rel_parts = rel_str.replace("\\\\", "/").split("/")

        # knowledge/users/{user_id}/... → scope="user", user_id=整数
        if rel_parts[:2] == ["knowledge", "users"] and rel_parts[2].isdigit():
            user_id = int(rel_parts[2])
            files_to_scan.append((file_path, "knowledge", "user", user_id))
        else:
            # 其他 → scope="shared"（共用知識庫）
            files_to_scan.append((file_path, "knowledge", "shared", None))
```

**註冊時自動建立目錄**: `MultiUserDB.create_user()` 在成功建立使用者後，會呼叫 `_ensure_user_knowledge_dir(user_id)` 建立該使用者的知識庫目錄，確保開箱即用。

### 搜尋管線 — shared_user_ids 傳遞鏈

```
MemoryManager.search(user_id="1")
    │
    ├── get_shared_user_ids(user_id=1) → [2, 3]  # 從 mu_kb_shares 查
    │
    ├── storage.search_vector(..., shared_user_ids=[2, 3])
    │       └── SQL: WHERE scope IN (...) AND (scope='shared' OR user_id IN ('1','2','3'))
    │
    └── storage.search_keyword(..., shared_user_ids=[2, 3])
            └── _search_fts5/_search_like/_search_fts5_trigram
                    └── SQL: 同樣的 user_id IN (...) 條件
```

所有的搜尋方法（`search_vector`, `search_keyword`, `_search_fts5`, `_search_like`, `_search_fts5_trigram`）都新增了 `shared_user_ids: Optional[List[int]] = None` 參數，在 SQL 層將分享者的知識庫一併納入搜尋範圍。

### 知識庫分享 CRUD

| API | 說明 |
|-----|------|
| `POST /api/knowledge/shares` | 建立分享（body: `{"shared_with_id": 2, "permission": "read"}`） |
| `GET /api/knowledge/shares` | 列出自己的分享（回傳 `owned` + `received` 兩組列表） |
| `DELETE /api/knowledge/shares/:id` | 移除分享（僅擁有者可操作） |

### mu_kb_shares 表格操作

```python
# db.py 公開 API
db.create_share(owner_id=1, shared_with_id=2, permission="read")  → dict | None
db.remove_share(share_id=5, owner_id=1)                           → bool
db.list_shares_by_owner(user_id=1)          # 我分享給誰（含對方 username）
db.list_shares_for_user(user_id=2)          # 誰分享給我（含對方 username）
db.get_shared_user_ids(user_id=2)           # 只回傳 ID 列表，供 search 使用
```

---

## 10. 前端 UI 變更

### HTML 結構 (`chat.html`) — +113 行

**登入遮罩層 (Login Overlay)**
- 支援雙模式顯示：
  - **Legacy 模式**: 只顯示密碼輸入框（維持原樣）
  - **Multi-user 模式**: 顯示 username + password 雙欄位 + 註冊切換連結
- 完整的**註冊表單**（可切換登入/註冊），含表單切換函數 `showRegisterForm()` / `showLoginForm()`
- 密碼顯示切換按鈕（眼睛圖示）

**側邊欄 (Sidebar)**
- 新增「👥 使用者管理」選單項目（預設 `hidden`，`role=admin` 才顯示）

**頂部標題列 (Header)**
- 新增**使用者下拉選單**：
  - 頭像縮寫圓圈（取 username 前兩個字）
  - 使用者名稱 + 角色標籤
  - 「個人設定」→ 修改密碼
  - 「使用者管理」→ admin view（僅 admin 可見）
  - 「退出登入」

**主要內容區**
- 新增 `#view-profile` 容器 → 個人設定頁面
- 新增 `#view-users` 容器 → 管理員使用者管理頁面

### JavaScript 邏輯 (`console.js`) — +661/-29 行

**i18n 翻譯擴充** — 三種語系共補了 30+ 個字串：

| 語系 | 新增內容 |
|------|---------|
| `zh` | 登入、註冊、使用者管理、個人設定、分享相關 |
| `zh-Hant` | 同上，完整繁中翻譯 |
| `en` | 同上 |

**全域狀態變數**

```javascript
let currentUser = null;       // {id, username, role}
let isMultiuserMode = false;  // 是否為多使用者模式
let isAdmin = false;          // 是否有管理權限
```

**認證流程重寫 (~300 行)**

| 函數 | 說明 |
|------|------|
| `showLoginScreen()` | 雙模式偵測：multiuser 顯示 username 欄位 / legacy 只用密碼 |
| `showLoginForm()` / `showRegisterForm()` | 表單切換 |
| Login form submit | 雙模式 payload：`{username, password}` 或 `{password}` |
| Register form submit | 完整註冊流程（含驗證），註冊後自動登入 |
| `toggleUserMenu()` / `closeUserMenu()` | 使用者下拉選單 |
| `setupUserMenu(user)` | 初始化選單（頭像縮寫、admin 選項顯示控制） |
| `navigateTo()` 覆寫 | 切換到 profile/users view 時 lazy-render |

**Admin 使用者管理 (`renderUsersView`)**

- 使用者列表 table（ID、名稱、角色 select、刪除按鈕）
- `fetchUsers()` — 呼叫 `GET /api/auth/users`
- `renderUserList()` — 渲染使用者表格
- `submitAddUser()` — 彈出 modal 新增使用者
- `updateUserRole()` — 直接修改角色（admin/user）
- `deleteUser()` — 刪除確認後刪除

**個人設定頁面 (`renderProfileView`)**

- 顯示目前使用者資訊（ID、名稱、角色、註冊時間）
- 修改密碼表單（舊密碼 → 新密碼 → 確認新密碼）
- `submitPasswordChange()` — 呼叫 `POST /api/auth/change-password`

**安全設定頁面灰化（Settings → 訪問密碼）**

Multiuser 模式下，`initConfigView()` 會以全域 `isMultiuserMode` 變數判斷，自動灰化 `web_password` 相關元件：

```javascript
if (isMultiuserMode) {
    pwdInput.disabled = true;           // 輸入框無法編輯
    pwdInput.placeholder = '由多使用者帳密管理';
    pwdInput.classList.add('opacity-50', 'cursor-not-allowed');
    pwdSaveBtn.classList.add('hidden'); // 隱藏存檔按鈕
    // 提示文字改為「密碼驗證由多使用者帳號系統管理」
}
```

安全閥還包含：
- `savePasswordConfig()` 頂頭 `if (isMultiuserMode) return;` 直接跳過
- ConfigHandler.POST 後端 `updates.pop("web_password", None)` 雙重防護

---

## 11. 已修改檔案索引

### 新增檔案（Phase 1）

| 檔案 | 行數 | 說明 |
|------|------|------|
| `channel/web/multiuser/__init__.py` | 0 | Package marker |
| `channel/web/multiuser/db.py` | ~577 | 資料庫層 (mu_users, mu_sessions, mu_kb_shares, CRUD, 密碼雜湊, 對話隔離, 知識庫分享) |
| `channel/web/multiuser/auth.py` | ~200 | 認證中間件 (cookie, session, RBAC) |

### 修改檔案（Phase 1 & Phase 2）

| 檔案 | +/- 行 | 說明 |
|------|--------|------|
| `channel/web/web_channel.py` | +353/-9 | 新增 8 個 handler（含 KnowledgeShare）+ route + import + SessionsHandler 隔離 + `_check_auth()` 頂頭 bypass + ConfigHandler multiuser 標誌 + POST 安全閥 |
| `channel/web/chat.html` | +113/-0 | 登入/註冊 UI、使用者選單、admin view、profile view 容器 |
| `channel/web/static/js/console.js` | +680/-29 | 完整前端邏輯：雙模式登入、使用者管理、修改密碼、i18n 翻譯、Settings 密碼欄位灰化 + `savePasswordConfig()` 安全跳過 |
| `bridge/agent_bridge.py` | +12/-3 | `_pre_persist_user_message` + `_persist_messages` 串接 user_id |
| `agent/memory/conversation_store.py` | +9/-0 | `append_messages` 新增 `user_id` 參數 |
| `agent/memory/storage.py` | +70/-4 | 所有搜尋方法（vector/FTS5/like/trigram）新增 `shared_user_ids` 參數 |
| `agent/memory/manager.py` | +35/-2 | `sync()` 掃描 `knowledge/users/{user_id}/`；`search()` 傳遞 `shared_user_ids` |

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
| **前端** | 使用者下拉選單（頭像、名稱、角色） | ✅ |
| **前端** | Admin 使用者管理頁面（CRUD） | ✅ |
| **前端** | 個人設定頁面（修改密碼） | ✅ |
| **前端** | i18n 翻譯（zh / zh-Hant / en）30+ 字串 | ✅ |
| **文件** | AGENTS.md Phase 2 完整記錄 | ✅ |

### 路線圖：對齊三層 Scope 戰略

#### ✅ Phase 1 — 後端核心（已完成）
多使用者認證、Session 管理、RBAC（admin/user）、對話隔離、向後相容

#### ✅ Phase 2 — 知識庫隔離與分享（已完成）
知識庫目錄隔離、搜尋管線 `shared_user_ids` 傳遞、知識庫分享 CRUD、前端 UI（登入/註冊/管理/個人設定）、i18n 三語系

#### 🟡 Phase 3 — Team Scope（開發中）
| 類別 | 項目 | 優先級 |
|------|------|:------:|
| **DB** | `mu_teams` / `mu_team_members` / `mu_user_configs` 三表 DDL + CRUD | 🟢 完成 |
| **Storage** | `MemoryChunk` scope 擴充 team、`team_id` 欄位 + migration + 4 種 search method WHERE 子句 | 🟢 完成 |
| **Manager** | `sync()` 掃描 `knowledge/teams/{id}/`、`search()` 傳遞 `team_ids` | 🟡 進行中 |
| **API** | Team CRUD API + Prompt API handlers | 🔴 待做 |
| **Bridge** | Global + User 提示詞合併 | 🔴 待做 |
| **前端** | Team 管理 UI + Prompt 編輯器 | 🔴 待做 |
| **文件** | AGENTS.md Phase 3 更新 | 🟡 進行中 |
| **分享** | 分享 UI 整合到前端（目前僅有後端 API） | 🟡 中 |

#### 🔲 Phase 4 — Prompt 繼承與 RBAC 擴充
| 類別 | 項目 | 優先級 |
|------|------|:------:|
| **DB** | `mu_prompts` 表（scope: global/team/user, 繼承鏈） | 🔴 高 |
| **後端** | Prompt 繼承引擎：Admin 基底 → Team 覆蓋 → User 微調 | 🔴 高 |
| **後端** | Manager 角色新增（team scope 管理員） | 🔴 高 |
| **API** | Prompt CRUD API + 繼承狀態查詢 | 🔴 高 |
| **前端** | Prompt 編輯器（含繼承預覽、版本對比） | 🔴 高 |
| **前端** | Manager view：管理 team 成員與共享資源 | 🟡 中 |
| **安全** | Rate Limiting 內建支援 | 🟡 中 |
| **安全** | Session 黑名單（管理員可強制登出特定使用者） | 🟡 中 |
| **測試** | 完整測試全角色 + 繼承 + 分享流程 | 🟡 中 |

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

## 開發備註

### 測試方式

```bash
# 1. 啟動後端
python app.py

# 2. 註冊第一個使用者 (自動成為 admin)
curl -X POST http://localhost:9899/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}'

# 3. 檢查登入狀態
curl -X GET http://localhost:9899/auth/check

# 4. 註冊第二個使用者
curl -X POST http://localhost:9899/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "user1", "password": "user1234"}'

# 5. [Admin] 列出使用者
curl -X GET http://localhost:9899/api/auth/users

# 6. [Admin] 修改使用者角色
curl -X PUT http://localhost:9899/api/auth/users/2 \
  -H "Content-Type: application/json" \
  -d '{"role": "admin"}'

# 7. 用不同使用者登入測試對話隔離
```

### 注意事項

- `multiuser.db` 建立在 `get_data_root()` 目錄下（與 `config.json` 同層）
- 密碼驗證用 `secret` 參數傳遞（`@web.data()`），Log 中不會明文記錄密碼
- 啟動時會自動執行 `ensure_conversation_user_id_column()` 做 migration

---

> **Author**: CowAgent 🐮  
> **Date**: 2026-07-08  
> **Base**: `anomixer/CowAgent`  
> **Branch**: `feat-multiuser`  
> **Status**: Phase 1 ✅ + Phase 2 ✅
