# feat-multiuser — 多使用者認證與對話隔離系統

> **Phase 1** — 後端核心 (Backend Core)  
> Branch: `feat-multiuser`  
> Base: `main` (upstream `anomixer/CowAgent`)

---

## 目錄

1. [動機與目標](#1-動機與目標)
2. [系統架構](#2-系統架構)
3. [資料庫層 — `multiuser/db.py`](#3-資料庫層--multiuserdbpy)
4. [認證中間件 — `multiuser/auth.py`](#4-認證中間件--multiuserauthpy)
5. [Route Handler — `web_channel.py`](#5-route-handler--web_channelpy)
6. [對話隔離管線](#6-對話隔離管線)
7. [向後相容設計](#7-向後相容設計)
8. [安全考量](#8-安全考量)
9. [已修改檔案索引](#9-已修改檔案索引)
10. [Phase 2 展望](#10-phase-2-展望)

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
                           auth/register ← 註冊
                           auth/me ← 查目前使用者
                           auth/users ← Admin CRUD
                           auth/check ← 回傳 user + multiuser: true
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
**改動量**: +218 行

### 新增 Route

| Method | Route | Handler | 權限 | 說明 |
|--------|-------|---------|------|------|
| POST | `/api/auth/register` | `RegisterHandler` | 公開 | 註冊（第一人 = admin） |
| GET | `/api/auth/me` | `MeHandler` | 登入 | 查詢目前使用者 |
| GET | `/api/auth/users` | `AdminUsersHandler` | Admin | 使用者列表 |
| POST | `/api/auth/users` | `AdminUsersHandler` | Admin | 新增使用者 |
| PUT | `/api/auth/users/:id` | `AdminUserDetailHandler` | Admin | 修改角色/密碼 |
| DELETE | `/api/auth/users/:id` | `AdminUserDetailHandler` | Admin | 刪除使用者 |

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

## 9. 已修改檔案索引

### 新增檔案

| 檔案 | 行數 | 說明 |
|------|------|------|
| `channel/web/multiuser/__init__.py` | 0 | Package marker |
| `channel/web/multiuser/db.py` | ~300 | 資料庫層 (表格, CRUD, 密碼雜湊, 對話隔離) |
| `channel/web/multiuser/auth.py` | ~200 | 認證中間件 (cookie, session, RBAC) |

### 修改檔案

| 檔案 | +/- 行 | 說明 |
|------|--------|------|
| `channel/web/web_channel.py` | +218/-6 | 新增 6 個 handler + route + import + SessionsHandler 隔離 |
| `bridge/agent_bridge.py` | +12/-3 | `_pre_persist_user_message` + `_persist_messages` 串接 user_id |
| `agent/memory/conversation_store.py` | +9/-0 | `append_messages` 新增 `user_id` 參數 |

---

## 10. Phase 2 展望

### 前端 (chat.html)

- **登入頁面** — username + password 輸入框，取代原本的密碼輸入
- **註冊頁面** — 第一次啟動時顯示註冊表單
- **使用者管理頁面** — Admin 可看到使用者列表、新增/刪除/修改
- **個人設定** — 修改密碼、頭像等

### 知識庫隔離

- 每個使用者的知識庫放在 `knowledge/{user_id}/` 目錄
- 系統知識（共用的）放在 `knowledge/shared/`
- `knowledge-wiki` 技能需增加使用者上下文參數

### 知識庫分享機制

- 使用者可以選擇性分享知識頁面給其他使用者
- 類似 Google Docs 的權限模型（view / edit / admin）
- 透過 `mu_kb_shares` 表格管理分享關係

### 其他可擴充

- **OAuth / SSO** 整合 (LDAP, Google, GitHub)
- **API Token** 支援（機器對機器呼叫）
- **Rate Limiting** 內建支援
- **Session 黑名單**（管理員可強制登出特定使用者）
- **操作日誌** Audit Log

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
> **Status**: Phase 1 Complete ✅
