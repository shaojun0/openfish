# openfish Git 功能重构设计 — 薄中间层

> **状态**：待评审（本文件只是方案，未改任何代码）
> **目标形态**：git 仓库角色 = 独立 docker 容器（Forgejo，已存在，不动）；openfish 只做
> **前端 + 认证 + 转发 + AI Agent**。
> **规格关系**：本文件重写 `DEVELOPMENT.md` 决策 D1 的边界。评审通过后需回写
> §2.2 / §2.3 / §5.2 / §8.2 / §9.2，并同步 `ARCHITECTURE.md`。
> **缺陷基线**：`../../../openfish-git-defects.md`（P0×4、P1×9、P2×5）。

---

## 0. 结论（TL;DR）

1. **git 仓库容器已经存在**：`docker/docker-compose.yml` 的 `forgejo` 服务
   （`codeberg.org/forgejo/forgejo:15`、`openfish-forgejo`、只 `expose:3000`、不发布宿主端口，
   数据落 `docker/forgejo/`）。这一层**保持不动**，它是「独立容器扮演 git 仓库」的答案。
2. 要改的是**openfish 里那部分 "自己也当 git 客户端" 的代码**。当前 openfish 除了前端/认证/转发
   之外，还额外背了：① 自建 bare 镜像并以 `git log` 读历史；② 用 admin token 给每个用户铸
   Forgejo token 并 Fernet 落库；③ 前端教用户换票。这三块正是缺陷审计里 P0/P1 的集中地。
3. 重构后的边界一句话：**openfish 认证，nginx 注入身份，Forgejo 认身份并管仓库；openfish 不再
   持有任何 git 对象、不再为任何人铸 git 凭据、不再保存 git 仓库数据。**
4. AI Agent 仍然留在 openfish：它通过 **Forgejo REST API**（提交/issue/PR）+ **git CLI 作为客户端**
   （只在自己沙箱的临时工作树里 clone/commit/push）工作——git CLI 在这里是「使用 git」而不是
   「实现 git」，属于允许保留的部分。
5. 收益：删除约 **1200 行**自有 git 逻辑（`git_identity.py` 795 行、`GitCommitReader` 174 行、
   `/git-credential` 及辅助 ~136 行、`GitIdentity` 模型 66 行、cli/指纹零头 ~60 行）外加 557 行
   门禁，再加一张表与 4 个环境变量；消灭缺陷 #3、#6、#7、#10–#13、#17 的成因；
   `make gates` 全绿之外新增一条**边界门禁** `check_git_boundary.py`。

---

## 1. 目标与非目标

### 1.1 目标

| # | 目标 | 验收信号 |
|---|---|---|
| G1 | git 协议、bare 仓库、issue/PR 迁移、Web UI 全部只由 Forgejo 承担 | openfish 进程内不存在 `git clone --bare` / `git log` 调用 |
| G2 | 认证只做一次：openfish 的账号体系是唯一入口，Forgejo 信任 openfish | 浏览器与 git CLI 都用 openfish 凭据；Forgejo 不再有自己的登录步骤 |
| G3 | 转发是纯代理：`/git/*` 的字节不经过业务逻辑，只在入口做认证注入 | nginx `location /git/` 只多一个 `auth_request` |
| G4 | openfish 不保存仓库数据/凭据 | `git_identities` 表、`data/forgejo-mirror/`、`FORGEJO_ADMIN_TOKEN`、`GIT_IDENTITY_KEY` 全部消失 |
| G5 | AI Agent 能力不回退 | runner 仍能 clone→review→commit→push `agent/*`→开 PR，且推送可归因到任务 |
| G6 | 缺陷审计中的相关条目被结构性消除，而不是打补丁 | 见 §10.3 的缺陷对照表 |

### 1.2 非目标

- 不换 git 系统（Forgejo 保留；若将来要换 Gitea/Gogs，见 §4.D-A 的迁移成本）。
- 不重写 openfish 的制品中心（pypi/npm/docker/debian/docs）与现有 RBAC。
- 不引入 k8s / Celery / Redis；不新增大依赖（见 §4.D-E）。
- 不在本轮做「Forgejo 的 CI/Actions 接管 agent 运行」——agent 运行时仍在 openfish 的 runner 容器。

---

## 2. 现状解剖

### 2.1 谁在做什么（重构前）

| 平面 | 承载者 | 位置 | 重构后 |
|---|---|---|---|
| git 协议 / bare repo / Web UI | **Forgejo 容器** | `docker/docker-compose.yml:204`、`docker/forgejo/` | **保留** |
| 反向代理 | nginx | `docker/nginx/nginx.conf:132`（`/git/`）、`:142`（`/forgejo-api/`） | 保留 + 加 `auth_request` |
| 仓库导入流水线 | openfish | `services/repo_import.py`（1993 行） | 保留骨架，删 git 读 |
| **bare 镜像 + 读提交** | openfish | `repo_import.py:904-1077` `GitCommitReader`、`_git_commits:1555`、`fingerprint_source_paths:1939` | **删除**，只走 API |
| **每用户铸 Forgejo token** | openfish | `services/git_identity.py`（795 行）、`GitIdentity` 表、`/git-credential` | **删除**，改代理注入身份 |
| 仓库 REST + 凭据端点 | openfish | `routes/repos.py:892-1030` | 删 `/git-credential`，加内部认证端点 |
| Webhook 触发 agent | openfish | `routes/repo_webhook.py`（766 行） | 保留，按 E1–E7 修 |
| Agent 执行 | openfish runner 容器 | `services/agent_runner.py:864-1085` `SubprocessRunnerAdapter` | 保留 git CLI，换凭据来源 |
| 前端 | Vue SPA | `frontend/src/views/RepoDetail.vue` | 改 clone 指引 |
| 下游客户端 | DSH 插件 | `integrations/dsh-plugin-enterprise-intranet/lib/index.js`（1298 行） | 简化 helper，不再换票 |

### 2.2 当前认证链路（重构前，即缺陷来源）

```
用户 --平台API Key--> GET /api/v1/repos/<slug>/git-credential   (repo:push)
                             │
                             ├─ 懒创建 Forgejo 账号 of-<user_id>-<hash>   ← 需要 FORGEJO_ADMIN_TOKEN
                             ├─ admin API 铸 access token                ← 账号级，无法按仓库收窄(C4)
                             ├─ Fernet(GIT_IDENTITY_KEY) 加密落库         ← C5/C6/C7/C10
                             └─ 返回 {username, password=<Forgejo token>}
用户 --Basic--> Forgejo /git/...                               ← Forgejo 校验它自己的 token
```

问题：平台凭据只是「换票的钥匙」，真正的 git 授权被复制成了第二套账号体系，且这套体系
在 Forgejo 侧是账号级、无法按仓库收窄；再加上
[Gitea/Forgejo 的 repo-scope 在 git smart HTTP 的 Bearer 路径上根本不生效](https://api.osv.dev/v1/vulns/GHSA-cc8w-r4qh-3v65)
（`CheckRepoScopedToken()` 只在 Basic 路径执行），所谓「最小 scope」在 git 面上并不成立。

### 2.3 当前提交读取链路（重构前）

```
_step_index_commits (repo_import.py:1504)
   ├─ 首选: ForgejoClient.stream_commits(...)   ← 已经是 API
   └─ 回退: GitCommitReader.ensure_mirror() + git log   ← 这条才是要删的
              git clone --bare --filter=blob:none <base>/<repo>.git  ← 落盘 + 可能带凭据
```

好消息：**API 路径已经是主路径**（`repo_import.py:1515`），删掉回退不改主流程语义。

---

## 3. 目标架构

```
                          浏览器
                            │  (openfish 会话 cookie / Basic API Key)
                            ▼
        ┌──────────────────────────────────────────────────────────┐
        │  nginx（唯一对外入口）                                     │
        │   /                → Vue SPA（前端）                       │
        │   /api/v1/*        → Flask 后端（认证/RBAC/看板/任务）      │
        │   /auth/*          → Flask（登录/设备授权）                 │
        │   /internal/*      → 仅内部 auth_request，对外 404          │
        │   /git/*  ──auth_request──► /internal/git-auth             │
        │           └─────────────────────────────────► Forgejo:3000 │
        │                （注入 X-WEBAUTH-USER / EMAIL / FULLNAME）   │
        │   /forgejo-api/* ─────────────────────────► Forgejo:3000  │
        └───────────────┬───────────────────────────┬───────────────┘
                        │                           │
        ┌───────────────▼──────────┐   ┌────────────▼─────────────────┐
        │ Flask / openfish         │   │  Forgejo 容器（独立 git 仓库） │
        │  · 认证 + RBAC           │   │  · bare repo / smart HTTP     │
        │  · 代理注入（internal）   │   │  · Web UI / issue / PR / 分支保护│
        │  · AI Agent 调度与看板    │   │  · 无宿主端口，只信 nginx 头   │
        │  · issue/commit 上下文缓存 │   └────────────▲─────────────────┘
        └───────────────┬──────────┘                │
                        │ 服务账号 token（env）       │
        ┌───────────────▼──────────────────────────┐ │
        │ runner 容器（AI Agent 沙箱）              │ │
        │  git clone/commit/push（客户端行为）──────┘ │
        │  Forgejo REST 开 PR ───────────────────────┘
        └──────────────────────────────────────────┘
```

### 3.1 职责边界（硬边界）

| 能力 | openfish | Forgejo | runner |
|---|---|---|---|
| 代码 / 分支 / tag / LFS | ❌ 不存 | ✅ 唯一事实源 | ❌ 只临时 checkout |
| git 协议（clone/push） | ❌ 只代理 | ✅ 实现 | ✅ 作为客户端调用 |
| 提交历史读取 | ✅ 读 API 后缓存 | ✅ 提供 API | ✅ `git log` 本地 |
| issue / PR 数据 | ✅ 缓存进 `repo_issues`（供检索） | ✅ 事实源 | ❌ |
| 用户身份 | ✅ 唯一账号体系 | ✅ 信任代理头 | ❌ |
| 分支保护 | ✅ 默认分支保护规则下发 | ✅ 执行 | ✅ 另有 `assert_pushable` |
| 令牌 | ❌ 不再铸用户令牌 | ✅ 校验身份 | ❌ 用服务账号 |
| AI review / finding / 看板 | ✅ | ❌ | ✅ 执行 |

---

## 4. 关键设计决策

### D-A. git 仓库角色 = 独立容器（保持 Forgejo）

**结论**：保留 `forgejo` 服务，不改容器职责，不改数据落盘方式。

**理由**：Forgejo 已满足「独立 docker 容器 + 开源 + 自带 smart HTTP / Web UI / issue / PR /
迁移 / webhook / 分支保护」。换 Gitea 是同源代码、收益仅是体积；换 Gogs 会丢迁移与 PR API；
换 Soft Serve/gitolite 会丢 issue/PR/REST，AI Agent 的上下文平面与 PR 回写全部要自建。
**换系统在本轮是纯成本、零收益。**

### D-B. 认证薄层：openfish 认证 + 反向代理头，取代 admin 铸票 ★核心决策

**结论**：开启 Forgejo 的 **reverse-proxy authentication**；nginx 在 `/git/*` 上先做
`auth_request` 到 openfish，openfish 用**现有账号体系**（会话 cookie 或 API Key）认证后，
把身份写进响应头，nginx 注入 `X-WEBAUTH-USER` / `X-WEBAUTH-EMAIL` / `X-WEBAUTH-FULLNAME`。

```
浏览器/CLI ──► nginx /git/<owner>/<repo>.git/info/refs
                 │ auth_request = /internal/git-auth (internal)
                 │     ├─ 无 Authorization 且无会话 → 200 + 空头（当匿名）
                 │     ├─ 会话 cookie 有效          → 200 + X-Openfish-User: of-1-ab12cd34ef
                 │     ├─ Basic password = 平台 API Key 有效且含 repo:push → 200 + 同一个头
                 │     └─ 凭据存在但无效            → 401（fail-closed）
                 └─ proxy_set_header X-WEBAUTH-USER $openfish_user; → Forgejo
```

**为什么是它**：

1. 它把「认证」彻底留在 openfish（用户诉求），Forgejo 退化成纯仓库 + 信任代理。
2. 用户不再需要第二套账号，也**不再需要平台铸任何 token**，C4–C7、C10 直接消失。
3. git CLI 的 Basic 里可以直接放**平台 API Key**——这正好把旧前端那句「git 与 API 共用一个
   凭据」的**错误文案变成真的**，而且不经过任何凭据存储。
4. 禁用用户 = openfish 认证失败 = nginx 不注入身份 = Forgejo 当匿名拒绝，**天然 fail-closed**，
   比现在 `revoke()` 远端删除失败仍标记已吊销（C5）更安全。

**前置条件（必须实测确认）**：Forgejo 的 reverse-proxy auth 对 **git 路由**的支持是
2026-07 才修好的（[forgejo#13640](https://codeberg.org/forgejo/forgejo/pulls/13640)，
回移到 v16.0）。当前 compose pin 的是 `forgejo:15`（LTS）。实施第一步就是：
升级到含该修复的 tag（v16.0.1+ 或 v15 的对应补丁版本），并用
`curl -H 'X-WEBAUTH-USER: <user>' .../info/refs?service=git-upload-pack` 对**私有仓库**实测
返回 200。

**回退方案（若 pin 的版本不支持 git 路由的代理认证）**：
- 方案 B1（推荐回退）：用户用 **Forgejo 自己签发的 PAT** 做 git 凭据（在已经 SSO 的 Forgejo Web UI
  里自助创建）。openfish 只做 Web SSO + 文档，**零铸票代码**；代价是 CLI push 多一步「建 PAT」。
- 方案 B2（不推荐）：保留 `/git-credential` 换票端点，但改为用户自己的 OAuth2 授权码换取，而不是
  admin 铸票。仍会保留一张 token 表和一次落库，且受 §2.2 的 scope 缺陷影响。

**配置要点**（写入 `docker/forgejo/forgejo.env.example`）：

```ini
[service]
ENABLE_REVERSE_PROXY_AUTHENTICATION = true
ENABLE_REVERSE_PROXY_AUTO_REGISTRATION = true
ENABLE_REVERSE_PROXY_EMAIL = true
ENABLE_REVERSE_PROXY_FULL_NAME = true
REVERSE_PROXY_AUTHENTICATION_USER = X-WEBAUTH-USER
REVERSE_PROXY_TRUSTED_PROXIES = <nginx 容器网段>
REVERSE_PROXY_LIMIT = 1
DISABLE_REGISTRATION = true
```

安全约束：`X-WEBAUTH-*` 只信 nginx；runner 与 backend 直连 `forgejo:3000` 时**不**在信任网段内，
无法伪造；nginx 必须无条件覆盖客户端传来的同名头（`proxy_set_header` 本身即覆盖）。

### D-C. 转发：nginx 直通，openfish 只在入口认证

**结论**：`/git/*` 保持纯字节代理，只加 `auth_request` 与身份头注入；Forgejo 的 Web UI 也在
`/git/` 下（`ROOT_URL` 已配 `/git/`），因此**代码浏览 UI 复用 Forgejo 现成的**，openfish 的 SPA
只做「链接出去 + 展示平台信息」。

**理由**：用户要的「前端」是 openfish 自己的控制台；代码浏览是 Forgejo 的强项，重造没有收益。
`/forgejo-api/` 保持只对内部/排障开放，生产建议直接关掉该 location（backend 走 compose 内网）。

### D-D. openfish 不持有 git 对象

**结论**：删除 bare 镜像与 `git log` 读取；`index_commits` 只保留 API 路径。

- 删 `GitCommitReader`（`repo_import.py:904-1077`）、`_git_commits`（:1555）、
  `fingerprint_source_paths`（:1939）、`ImportConfig.mirror_dir` / `git_base_url` 中的镜像用途。
- `_step_index_commits`（:1504）去掉 `except → git` 回退：API 失败即让任务 `partial`/`error`，
  **不允许静默改走本地 git**（宁缺勿脏）。
- `repo_commits` 表**保留**：它是 AI 上下文缓存，不是仓库事实源；数据全部来自
  `GET /repos/{owner}/{repo}/commits`。
- 物理清理：`backend/data/forgejo-mirror/`、`GIT_MIRROR_DIR`。

### D-E. Forgejo 客户端库：先用现有 `requests` 薄客户端，把 pyfj 列为候选

**结论**：

| 选项 | 评估 | 决定 |
|---|---|---|
| 保留 `ForgejoClient`（`requests`，手写） | 已在门禁覆盖内；`DEVELOPMENT.md` §3.1 明确允许 `requests` + Forgejo HTTP API；离线部署零新增 wheel | ✅ 本轮保留并**缩减到实际用到的端点** |
| [`forgejo-python`](https://pypi.org/project/forgejo-python/)（import `pyfj`） | v0.1.0（2026-09-14 发布）、Beta、pydantic+httpx、按 Forgejo 16 API 生成、类型完整 | ⏳ 观望，v1.0 或团队愿意承担 httpx + 内网 wheel 时替换 |
| [`forj`](https://pypi.org/project/forj/) | 是 **CLI** 不是库，定位不符 | ❌ |

**理由**：本轮要删的是 **git 对象访问 + 铸票**，不是 HTTP 客户端；为一次发布 4 天的 0.1.0 库
引入 httpx 与生成代码，违反 AGENTS.md「新依赖最小化」。但 D-E 是**可逆**的：把 `ForgejoClient`
收敛成一个接口（`stream_issues` / `stream_commits` / `migrate` / `open_pr` / `ensure_branch`），
将来换成 pyfj 只改一个文件。

### D-F. AI Agent 的凭据

| 用途 | 凭据 | 存放 |
|---|---|---|
| backend 调 Forgejo（migration、issue/commit 镜像、开 PR、分支保护） | **服务账号 token** `FORGEJO_SERVICE_TOKEN` | 仅环境变量，不落库、不进日志 |
| runner 容器 push `agent/*` | 独立的 `FORGEJO_RUNNER_TOKEN`（同服务账号或专用 bot） | 仅 runner 容器 env；git 通过 `credential.helper` + env 读取，**不进 argv / 不进 `.git/config`** |
| 用户的 git 读写 | 平台会话/API Key → nginx 注入身份 | 无 |

**理由**：把「每用户一个 token」换成「一个可轮换的服务凭据 + 用户身份由代理头表达」，是本次
重构减少攻击面最大的单点。服务账号权限用 Forgejo org/team 收窄，push 限制在 `agent/*`。

### D-G. Webhook：保留，作为 AI Agent 的触发器

**结论**：`/api/v1/repos/webhook` 保留（这是「AI Agent」能力的一部分），但按缺陷审计重写：

| 缺陷 | 处理 |
|---|---|
| E1 任意 PR 污染 finding | 只在 `merged` 且按 run/PR 精确回填 |
| E2 无去重 | 持久化 `X-Forgejo-Delivery`，重复投递直接 200 |
| E3 删除分支入队空 SHA | 用 payload `after`，`deleted` / 全 0 直接跳过 |
| E4 未知仓库 404→无限重试 | 改 200 + `handled:false` |
| E5 `one_or_none` 500 | 改 `first()` |
| E6 两个候选列恒等 | 删掉退化分支或真正区分 slug / forgejo_repo |
| E7 label 回退不合协议 | 去掉 payload 顶层 labels 回退 |

### D-H. 数据模型取舍

| 表/字段 | 决定 | 说明 |
|---|---|---|
| `git_identities` | **删除** | 身份由代理头表达，无需映射表 |
| `repos.forgejo_repo` | 保留（建议改名 `host_repo`） | 仍是 slug → 仓库全名的映射 |
| `repos.kind`（upstream/workspace） | 保留 | 决定只读/可写与 UI |
| `repo_issues` / `repo_commits` | 保留 | AI 上下文缓存，来源改为纯 API |
| `import_jobs` | 保留 | 流水线断点 |
| `findings` / `agent_tasks` / `review_runs` | 不动 | 与 git 无关 |

### D-I. 前端与下游客户端

- `RepoDetail.vue` / i18n：clone 命令改为「平台用户名:API Key@host/git/owner/name.git」；
  删掉「换票」叙述，说明「push 受平台 RBAC + Forgejo 分支保护双重约束」。
- DSH 插件 `openfish-git-credential`：**不再调用 `/git-credential`**。保留 host 精确校验
  （C8 必须修）；把「内嵌长期 API Key 到生成脚本」改为从插件进程已有的 0600 配置读取，
  或直接引导用户用 `credential.helper store` / netrc。
- `cli.py disable-user`：删掉 `_revoke_git_identity`；可选用服务 token 调 Forgejo
  `PATCH /admin/users/{username}` 停用账号（生命周期管理，非铸票）。

---

## 5. 模块级改造清单

### 5.1 删除

| 路径 | 规模 | 原因 |
|---|---|---|
| `backend/services/git_identity.py` | 795 行 | admin 铸票 + Fernet 落库模型整体废弃 |
| `backend/models/agent_hub.py::GitIdentity` | ~65 行 | 同上 |
| `backend/scripts/check_git_identity.py` | 557 行 | 被测对象消失 |
| `backend/routes/repos.py::git_credential` + `_git_identity_service` | ~120 行 | 端点废弃 |
| `backend/services/repo_import.py::GitCommitReader` | 174 行 | openfish 不再碰 git 对象 |
| `repo_import.py::_git_commits` / `fingerprint_source_paths` | ~35 行 | 同上 |
| `backend/data/forgejo-mirror/` | 运行态 | 物理清理 |
| `cli.py::_revoke_git_identity` | ~25 行 | 铸票模型副产品 |

### 5.2 修改

| 路径 | 改动 |
|---|---|
| `backend/services/repo_import.py` | `_step_index_commits` 去 git 回退；删 `mirror_dir`/`git_base_url` 镜像用途；`FORGEJO_ADMIN_TOKEN` → `FORGEJO_SERVICE_TOKEN` |
| `backend/routes/repo_webhook.py` | E1–E7 修复 + delivery 去重 |
| `backend/services/agent_runner.py` | `SubprocessRunnerAdapter` 的 clone/push 凭据改 env+helper；`assert_pushable` 保留；开 PR 用服务 token |
| `backend/routes/repos.py` | 删 `/git-credential`；新增内部 `GET /internal/git-auth`；clone URL 展示沿用 `FORGEJO_PUBLIC_BASE_URL` |
| `backend/config/hub.py` | Forgejo 配置从 `repo_import.CONFIG_ITEMS` 收敛成正式 settings 组（S4 待办） |
| `docker/nginx/nginx.conf` | `/git/` 加 `auth_request`；`location = /internal/git-auth { internal; }`；可选关闭 `/forgejo-api/` 对外 |
| `docker/docker-compose.yml` | 环境变量增删（§6）；forgejo 服务配置调整 |
| `docker/forgejo/forgejo.env.example` | 反向代理认证配置；`FORGEJO_IMAGE` tag 升级 |
| `frontend/src/views/RepoDetail.vue` + `locales/{zh-CN,en-US}.ts` | clone/凭据文案 |
| `integrations/dsh-plugin-enterprise-intranet/lib/index.js` | helper 简化 + host 校验 |
| `docs/agent-hub/DEVELOPMENT.md` / `README.md` | 回写 D1 边界、§5.2、§8.2、§9.2 |
| `docker/forgejo/README.md` | 认证模式说明 |

### 5.3 新增

| 路径 | 作用 |
|---|---|
| `backend/routes/internal.py`（或并入 `repos.py`） | `GET /internal/git-auth`：认证 → 返回 `X-Openfish-User/Email/Fullname`；对外 404 |
| `backend/services/git_identity_map.py`（可选，~40 行） | 由 `user_id + external_id` 纯函数推导 Forgejo 用户名（`of-<id>-<sha256[:10]>`），**无状态、无落库** |
| `backend/scripts/check_git_boundary.py` | 边界门禁（§10.2） |
| `docs/agent-hub/DESIGN-git-thin-layer.md` | 本文件 |

---

## 6. 配置与环境变量变更

| 变量 | 变更 | 说明 |
|---|---|---|
| `FORGEJO_BASE_URL` | 保留 | 内网 API 基址 |
| `FORGEJO_PUBLIC_BASE_URL` | 保留 | 对外 `/git` 前缀（仅生成 URL） |
| `FORGEJO_OWNER` | 保留 | 承载镜像的 org |
| `FORGEJO_WEBHOOK_SECRET` | 保留 | webhook HMAC |
| `FORGEJO_ADMIN_TOKEN` | **删除** | 不再有 admin 铸票 |
| `GIT_IDENTITY_KEY` | **删除** | 不再有 Fernet 落库 |
| `GIT_MIRROR_DIR` | **删除** | 不再有本地镜像 |
| `FORGEJO_GIT_BASE_URL` | **删除** | 镜像专用，runner 用 `FORGEJO_BASE_URL` |
| `FORGEJO_SERVICE_TOKEN` | **新增** | backend → Forgejo API |
| `FORGEJO_RUNNER_TOKEN` | **新增** | runner push |
| `FORGEJO_TRUSTED_PROXIES` | **新增** | 传给容器配置，限制代理头来源 |
| `FORGEJO_PUBLIC_UI_URL` | 可选新增 | SPA 里「浏览代码」的外链 |

---

## 7. 数据迁移

`models/agent_hub_migrate.py` 是无 Alembic 的幂等 top-up，只能建表/加列，**不删表**。因此：

1. 从 `AGENT_HUB_TABLES` 移除 `GitIdentity`（新库不再建）。
2. 新增一次性清理：`DROP TABLE IF EXISTS git_identities`（在 `ensure_schema` 的
   「已存在则清」分支里执行，或放进 `cli.py` 的升级命令），并在日志里显式记录。
3. 环境变量校验：启动时若仍设置 `FORGEJO_ADMIN_TOKEN` / `GIT_IDENTITY_KEY`，
   **打 warning 提示已废弃**，不静默忽略（避免运维以为还在生效）。

---

## 8. 接口契约变更（破坏性）

| 变更 | 类型 | 影响 |
|---|---|---|
| `GET /api/v1/repos/<slug>/git-credential` | **删除** | 前端、DSH 插件、外部脚本 |
| `GET /internal/git-auth` | 新增（内部） | 仅 nginx；对外 404 |
| `Repos` 响应里的凭据相关文案 | 改 | OpenAPI description 同步 |
| `repo_webhook` 响应语义 | 改 | 未知仓库 200、重复投递 200 `duplicate:true` |
| runner `push`/`open_pr` 内部协议 | 不变 | `RunnerAdapter` Protocol 不动 |

`openapi/` 与 `backend/scripts/check_contract.py`、`check_openapi.py` 必须同步，否则门禁红。

---

## 9. 安全模型

### 9.1 信任链

```
客户端凭据 ──► openfish 认证（唯一判定点）
                   │ 通过 → 注入 X-WEBAUTH-*（nginx 覆盖客户端同名头）
                   ▼
              Forgejo 只信 nginx 网段（REVERSE_PROXY_TRUSTED_PROXIES）
```

- 伪造路径：直连 `forgejo:3000` 并自带 `X-WEBAUTH-USER`。缓解：forgejo 不发布宿主端口；
  runner/backend 不在信任网段；生产可把 forgejo 放到只与 nginx 共享的网络。
- 越权路径：平台 `repo:push` 只表示「可以拿到身份」，具体能推哪个仓库、哪个分支由 Forgejo
  RBAC + 分支保护决定；默认分支开启保护，只允许服务账号/合并队列。
- 凭据泄漏面：不再有 token 落库、不再有 URL 内嵌 token、不再有生成的 helper 脚本内嵌长期 key。
  runner 侧沿用 `credential.helper` + env 注入（缺陷审计 C1 的修复方式）。

### 9.2 消除的缺陷（对照 `openfish-git-defects.md`）

| 原缺陷 | 消除方式 |
|---|---|
| #3 admin token 进 clone URL / 日志 / `.git/config` / argv | 删除 `GitCommitReader` 与 admin token，**整条链路不存在** |
| #4 插件 helper 不校验 host | 保留校验并简化 helper（仍要修） |
| #6 `--depth 1` 导致只读 1 个 commit | 不再用本地镜像 |
| #7 `ensure_mirror(refresh=True)` 被短路 | 镜像不存在 |
| #10 账号级 token 无法按仓库收窄 / 文案不实 | 不再铸 token；身份走代理，授权走 Forgejo RBAC |
| #11 `revoke()` fail-open | 无 token 可吊销；停用用户即认证失败 |
| #12 `_mint` 删除失败导致永久故障 | 无 mint |
| #13 插件默认关 TLS 校验 | helper 重写时默认开启 |
| #16 `.gitignore` 否定规则失效 | 独立小修（保留在切片里） |
| #17 镜像路径清洗过弱 | 无镜像 |

> #1/#2（runner 未接线、fix 无 commit）与 #5/#8/#9（webhook）属于独立缺陷，本设计**不声称**自动
> 修复，分别落在 §11 的 S3 与 S4。

---

## 10. 验收标准

### 10.1 端到端场景（必须在真容器上跑）

1. **匿名读公开仓库**：`git clone http://host/git/<owner>/<public>.git` 成功，无认证头。
2. **用户读私有仓库**：`git clone http://any:<OPENFISH_API_KEY>@host/git/<owner>/<private>.git`
   成功；错误 key 返回 401。
3. **用户推非保护分支**：能推 `feature/x`；Forgejo 侧 author 是**该平台用户**（不是 bot）。
4. **用户推默认分支**：被 Forgejo 分支保护拒绝（I4 从 runner 内断言升级为仓库级强制）。
5. **浏览器 SSO**：登录 openfish → 打开 `/git/<owner>/<repo>` 直接是已登录的 Forgejo 页面，
   不再出现 Forgejo 登录表单。
6. **agent 闭环**：webhook 入队 → runner clone → review → commit → push `agent/x` → 开 PR，
   PR 作者是服务账号，描述含 gates 汇总。
7. **停用用户**：`cli.py disable-user` 后，其 API Key 克隆私有仓库立刻 401。
8. **泄漏扫描**：`ps`、`.git/config`、容器日志、`/work` 内均搜不到任何 token。

### 10.2 新增边界门禁 `check_git_boundary.py`（离线）

- `repo_import.py` 内不出现 `subprocess` / `GitCommitReader` / `--bare` / `git log`。
- 全仓库不出现 `FORGEJO_ADMIN_TOKEN` / `GIT_IDENTITY_KEY` / `git_identities`。
- `nginx.conf` 的 `location /git/` 块内含 `auth_request`。
- `routes/repos.py` 无 `git-credential` 路由。
- runner 的 clone URL 不携带凭据（凭据只从 env + helper 来）。

### 10.3 既有门禁

`make gates`（`services.gates` 自动发现 + 前端 smoke）与 `make contract-gate` 全绿；
`check_agent_repos.py` / `check_agent_runtime.py` 的断言随新协议更新。

---

## 11. 实施切片（每片可独立验收）

| 切片 | 内容 | 依赖 | 风险 |
|---|---|---|---|
| **S1 认证薄层** | Forgejo 升级 + 代理认证配置；nginx `auth_request`；`/internal/git-auth`；删 `git_identity.py` / 表 / 端点 / cli 钩子；前端文案 | 需实测 Forgejo 版本支持 | 高（涉及登录路径） |
| **S2 去掉本地 git** | 删 `GitCommitReader` / `_git_commits` / `fingerprint_source_paths` / `GIT_MIRROR_DIR`；`index_commits` 只走 API；清镜像目录 | 无 | 低 |
| **S3 Agent 凭据与闭环** | 服务账号 token；runner clone/commit/push；开 PR；`assert_pushable` + 分支保护；接线 worker（缺陷 #1/#2） | S1 | 中 |
| **S4 Webhook 修复** | E1–E7 + delivery 去重 | 无 | 低 |
| **S5 下游与文档** | DSH 插件 helper；`DEVELOPMENT.md` / `README.md` / `docker/forgejo/README.md` | S1 | 低 |
| **S6 清理与门禁** | 删表迁移；env 废弃 warning；`check_git_boundary.py`；`check_git_identity.py` 删除 | S1–S4 | 低 |

**建议先做 S1 的可行性实测（0.5 天）**：起一个临时 Forgejo（v16.0.1+），开反向代理认证，
用 `curl -H 'X-WEBAUTH-USER: …'` 验证私有仓库 `/info/refs` 与 `git-receive-pack` 都能通过。
这一条不成立，整个 D-B 要切到 B1 回退方案，后续切片顺序不变。

---

## 12. 风险与未决问题

| # | 项 | 影响 | 处置 |
|---|---|---|---|
| R1 | `forgejo:15` 是否含 git 路由反向代理认证 | D-B 的前提 | S1 先实测；不支持则升 v16.0.1+ 或切 B1 |
| R2 | 反向代理认证**不支持 API** | backend 不能用代理头调 Forgejo | 已用服务 token，无影响 |
| R3 | 代理头信任范围配错 → 伪造身份 | 严重 | `REVERSE_PROXY_TRUSTED_PROXIES` 收紧到 nginx；forgejo 不发布端口 |
| R4 | 一次性 DROP `git_identities` | 旧 token 记录丢失 | 迁移前导出备份；旧 token 本就要作废 |
| R5 | 分支保护规则需人工/脚本初始化 | I4 是否真强制 | 提供 `cli.py` 或 compose 初始化脚本下发保护规则 |
| R6 | pyfj 是否替换手写客户端 | 代码量 vs 新依赖 | 推迟到 v1.0；接口先收敛（D-E） |
| R7 | AI Agent 的服务账号是一个高权凭据 | 泄漏影响面 | org/team 收窄 + `agent/*` 限制 + 定期轮换 + runner 容器无外网 |

### 待你确认的问题

1. **D-B 是否采纳**（代理认证 SSO，git 复用平台 API Key）？若你更希望「用户自助 Forgejo PAT」
   （B1 回退），openfish 会更薄，但 CLI 首次使用多一步。
2. **S1 是否允许先做一次 Forgejo 版本实测**（起临时容器验证 git 路由反向代理认证）？
3. **D-E**：接受「本轮保留 `requests` 薄客户端、pyfj 观望」，还是要求直接换 pyfj？
4. **分支保护**由谁初始化：openfish `cli.py` 下发，还是运维在 Forgejo 后台手工配置一次？
