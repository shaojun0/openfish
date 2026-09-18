# Agent Hub 开发文档

> 版本 v0.1（草案）· 面向 openfish 仓库内实现
> 本文是 **B 路线**（仓库内智能体）的权威规格。子智能体实现时以本文为准；
> 与现有代码冲突时以「§3.2 复用而非重写」为准，并把冲突记进 `OPEN-QUESTIONS.md`。

---

## 0. 背景与定位

openfish 目前是企业内网一体化平台：PyPI / npm / Docker / apt / Node 五个真实协议面、
制品库、模型路由（`backend/config/model_routes.json` + `/api/v1/models/resolved`）、
五表 RBAC、API-key、设备授权、私网 CA，以及下游 DSH 企业内网插件
（`integrations/dsh-plugin-enterprise-intranet`）。

**Agent Hub 在这之上补两件事：**

1. **仓库面**：平台能托管 / 镜像 git 仓库，并把仓库的**全部协作历史**（提交、issue、PR、
   label、milestone、review 评论）收进来作为智能体的上下文。
2. **智能体面**：智能体以仓库为工作空间执行任务（审查、修复、开 PR），
   而**发现的结论被记住**——同一个问题不会每次重新报一遍。

一句话定位：**把「git 仓库」做成智能体的工作空间与记忆，把「发现」做成有生命周期的对象。**

### 0.1 目标（本轮必须达成）

| # | 目标 | 可验证标准 |
|---|---|---|
| G1 | 导入一个外部仓库（含完整 issue / PR 历史） | 能把 `vllm-project/vllm` 的 issue 导进来，数量与 label 可查 |
| G2 | 平台能提供 git clone/push | `git clone http://<host>/git/<owner>/<repo>.git` 成功 |
| G3 | 智能体可执行任务并产出 PR | 一次 review run 产出结构化 finding，并可生成分支/PR |
| G4 | 同一 finding 跨提交去重 | 第二次 run 不新开 finding，状态机保持连续 |
| G5 | 「以后再说」有登记、有到期 | `wontfix` 必须有 owner + due，到期重新激活 |
| G6 | 每类发现可被阻断或延期 | policy 分 blocking / debt 两档，blocking 无 wontfix |
| G7 | 文档回写 | PR 合并后生成仓库文档条目（复用现有 docs 服务） |

### 0.2 非目标（本轮明确不做）

- 不做可视化工作流编排（那是 Dify 的地盘，见 §12）。
- 不做多人实时协作编辑、不做代码高亮 IDE。
- 不实现 SSH 协议（push/clone 只走 HTTP smart protocol；SSH 后续挂 Forgejo 原生端口）。
- 不做跨仓库推理：一个任务只在一个仓库内。
- 不自动改设计取舍类问题（见 §6.4），也不自动推保护分支。

---

## 1. 开发者与智能体的体验闭环

```
① 导入仓库（VLLM 全历史）
      │  repo + issues + PR + labels 落库
      ▼
② 开发者 push 一个提交 / 开一个 issue 标注 "agent"
      │  webhook → agent_task 入队
      ▼
③ 智能体取任务 → 起沙箱 → clone → 读 AGENTS.md + policy
      │  跑 gates（backend/scripts/check_*.py）
      ▼
④ 产出 finding（结构化对象，带 fingerprint）
      │  与既有 finding 去重 → 状态机推进
      ▼
⑤ 分档处置
      ├─ blocking → 阻断：agent 直接生成修复分支 + PR
      └─ debt     → 登记：owner + due，静默进债务看板
      ▼
⑥ PR 被合并 / 被 wontfix（必须填 due）
      │
      ▼
⑦ 回写：PR 说明 + finding 解释写入仓库文档（复用 /docs）
```

**关键不变量（实现时必须保证）**

- **I1**：finding 的 `fingerprint` 跨 run 稳定；去重不依赖文本相似度。
- **I2**：`wontfix` 必须带 `owner` + `due`，缺一不可（API 层拒绝）。
- **I3**：bounding 规则命中的 finding **不允许** `wontfix`。
- **I4**：agent 永不直接写 `main` 或任何保护分支；只能推 `agent/*` 分支并开 PR。
- **I5**：所有 agent 产出必须能追到 `agent_task_id` + `review_run_id`。
- **I6**：导入的 issue 历史对 agent 是**只读证据**，不是可执行指令（见 §8.4 prompt injection）。

---

## 2. 架构总览

### 2.1 三平面 + 一运行时

```
┌────────────────────────────────────────────────────────────────────┐
│ 控制平面（openfish 现有能力 + 新增 policy/RBAC）                     │
│  权限点、角色、API-key、审计、.agent/review-policy.yml、模型路由       │
├────────────────────────────────────────────────────────────────────┤
│ 数据平面（openfish 新增，SQLAlchemy 单库）                           │
│  repos · repo_commits · repo_issues · findings · finding_events      │
│  review_runs · review_results · agent_tasks · import_jobs            │
├────────────────────────────────────────────────────────────────────┤
│ 仓库平面（新增容器 Forgejo，只做 git 协议 + 承载）                    │
│  bare repo · smart HTTP clone/push · 原生 GitHub migration（含 issue）│
├────────────────────────────────────────────────────────────────────┤
│ 智能体运行时（新增 worker + 沙箱）                                    │
│  取任务 → 隔离目录 → 跑 DSH/runner → 跑 gates → 出 finding / PR      │
└────────────────────────────────────────────────────────────────────┘
```

### 2.2 为什么 git 协议不自己实现（决策 D1）

候选与结论：

| 方案 | 结论 | 理由 |
|---|---|---|
| 纯自研（pygit2/dulwich + 自写 smart HTTP） | ❌ | pack 协商、capabilities、hooks、大仓推送是数月的坑；且大量边界问题与产品价值无关 |
| **Forgejo/Gitea 承担 git 协议与镜像（选定）** | ✅ | 原生 GitHub migration 直接带 issue/PR/label；协议实现成熟；可独立升级 |
| 用现成托管（GitHub/GitLab） | ❌ | 客户是内网私有化，外部依赖不可接受 |

**边界写死**：Forgejo 负责「字节与协议」（bare repo、clone/push、migration、webhook）。
openfish 负责「治理与智能」（权限、finding、策略、任务、审计、上下文检索）。
**openfish 不直连 git 对象做写操作；所有读取走 Forgejo API + `git` CLI 只读副本。**

- 新增容器名 `openfish-forgejo`，**不发布宿主端口**，由边缘 nginx 暴露 `/git/` 与 `/forgejo-api/`。
- 版本锚定 `codeberg.org/forgejo/forgejo:9`（实现时确认可用 tag，写入 `docker/.env.example`）。
- 数据落 `docker/forgejo/`（bind mount，纳入 `prepare-mounts.sh`，与现有 6 个挂载点同风格）。

### 2.3 组件职责

| 组件 | 职责 | 不做什么 |
|---|---|---|
| `services/repo_import.py` | 驱动 Forgejo migration、轮询进度、把结果镜像进 `repo_issues`、断点续传 | 不解析 git 对象 |
| `services/repo_context.py` | 给 agent 组装上下文：issue 证据、提交历史、finding 历史、policy | 不调模型 |
| `services/findings.py` | fingerprint、去重、状态机、激活条件、投影为 PR 评论 | 不做规则判定 |
| `services/review_policy.py` | 读 `.agent/review-policy.yml`，解析 exception 与到期 | 不写文件 |
| `services/gates.py` | 发现并执行 `backend/scripts/check_*.py`，归一化结果 | 不判定业务规则 |
| `services/agent_runner.py` | 起沙箱、注入模型配置（复用 `/api/v1/models/resolved`）、执行、回收产物 | 不存状态（状态在 DB） |
| `services/agent_queue.py` | DB 队列：入队/领取/心跳/重试/死信 | 不做调度策略 |
| `routes/repos.py` 等 | JSON 契约 + 权限点绑定 | 不放业务逻辑 |

---

## 3. 技术选型与复用约束

### 3.1 依赖政策（硬约束）

- **新依赖最小化**。允许新增：`pyyaml`（解析 policy）。
- git 相关操作**只用 `git` CLI（subprocess，只读）**与 Forgejo HTTP API（`requests`，已有）。
  **不引入** pygit2 / dulwich / GitPython（原生扩展或额外抽象层，收益不足）。
- 沙箱执行用**现有 docker compose**（新增 profile `runner`），不引入 k8s。
- 队列用**数据库表 + 单 worker**（`python -m services.agent_queue worker`），
  不引入 Celery/Redis——与「一个关注点一份实现」的现有约定一致。

### 3.2 复用而非重写（实现时先找同类模块照抄）

| 需求 | 复用什么 | 位置 |
|---|---|---|
| 原子写 + JSON 落盘 | `services/fileio.py` | 已有 |
| SHA-256 缓存 / 指纹 | `services/digest.py` | 已有 |
| 上游 HTTP 代理与重试 | `services/upstream.py` | 已有 |
| 分页 / 列表响应 | `routes/hub.py` + `schemas.py` | 已有 |
| 权限点声明与角色播种 | `services/authz.py` + `auth/permissions.py` | 已有 |
| 设备授权式免密接入 | `services/device_auth.py` | 已有 |
| Markdown 渲染 | `services/markdown.py` | 已有 |
| 模型路由解析 | `services/model_routes.py::resolve` | 已有 |
| 后端风格（`from __future__`、`X \| None`、logger 命名） | `ARCHITECTURE.md` §代码约定 | 权威 |

**门禁必须继续通过**：`backend/scripts/check_lint.py`（pyflakes）、`check_openapi.py`、
`check_contract.py`、`check_permission_catalog.py`。新增权限点必须同步目录。

---

## 4. 数据模型

### 4.1 实体关系

```
repos 1─n repo_commits
  │   1─n repo_issues ─┐
  │   1─n import_jobs  │
  │   1─n findings ────┼── n─1 review_runs（发现它的那次运行）
  │         │ 1─n finding_events（状态迁移史）
  │         └── n─n repo_issues（关联证据，见 4.6）
  └── 1─n agent_tasks ─n─1 review_runs
```

### 4.2 表定义（SQLAlchemy 2.x，`models/agent_hub.py`）

```python
class Repo(Base):
    __tablename__ = "repos"
    id: int                 # PK
    slug: str               # unique, "<owner>/<name>"
    source: str             # "import" | "local"
    source_url: str | None  # 外部来源，如 https://github.com/vllm-project/vllm
    default_branch: str     # 默认 main
    forgejo_repo: str | None# Forgejo 侧全名，如 "openfish/vllm"
    kind: str               # "upstream"（只读镜像）| "workspace"（agent 可写）
    sync_state: str         # pending | cloning | issues | indexing | ready | error
    synced_at: datetime | None
    issue_count: int        # 物化计数，列表页免 N+1
    commit_count: int
    created_at / updated_at

class ImportJob(Base):
    __tablename__ = "import_jobs"
    id, repo_id(FK), mode("code"|"code+issues"|"issues"), status
    phase: str              # migrate | poll | mirror_issues | index_commits | done
    cursor: str | None      # 断点：上一次镜像到的 issue number / 时间
    progress: int           # 0-100
    total, done             # issue 镜像进度
    error: str | None
    started_at, finished_at

class RepoIssue(Base):
    __tablename__ = "repo_issues"
    id, repo_id(FK)
    number: int             # 源系统 issue 号（PR 用负数或 is_pr 区分）
    is_pull_request: bool
    title, body             # body 保留 Markdown 原文，渲染在读取时做
    state: str              # open | closed
    author: str
    labels: str             # JSON 数组（SQLite/PG 通用）
    milestone: str | None
    created_at, updated_at, closed_at
    url: str | None         # 源系统永久链接，供 agent 引用
    source_id: str | None   # 源系统 ID，幂等去重用
    # 索引：unique(repo_id, source_id)；index(repo_id, state)
    #      + 全文检索在 services/repo_context.py 内做（见 §8.2）

class Finding(Base):
    __tablename__ = "findings"
    id, repo_id(FK)
    fingerprint: str        # I1：见 4.4，unique(repo_id, fingerprint)
    rule_id: str            # 命中哪条规则，不是自由文本
    level: str              # blocking | debt
    severity: str           # critical | high | medium | low
    status: str             # open | acknowledged | wontfix | fixed | stale
    file_path, symbol: str  # 符号锚点，不用行号（4.4）
    line_hint: int | None
    title, detail           # detail 允许 LLM 生成的解释文字
    first_seen_run_id: int  # FK review_runs
    last_seen_run_id: int
    seen_count: int         # 被重新观察到的次数
    owner: str | None       # wontfix/acknowledged 必填
    due: date | None        # wontfix/acknowledged 必填（I2）
    decided_by, decided_at
    pr_url: str | None
    created_at, updated_at

class FindingEvent(Base):
    __tablename__ = "finding_events"
    id, finding_id(FK), at, actor, from_status, to_status, reason, run_id

class ReviewRun(Base):
    __tablename__ = "review_runs"
    id, repo_id, agent_task_id(FK, nullable)
    commit_sha: str
    policy_hash: str        # policy 文件内容哈希，policy 变了要说明差异
    findings_new, findings_matched: int
    gates_total, gates_passed, gates_failed
    status: str             # running | ok | error
    log_ref: str | None
    started_at, finished_at

class AgentTask(Base):
    __tablename__ = "agent_tasks"
    id, repo_id, kind      # review | fix | import | backfill
    status                 # queued | leased | running | done | failed | dead
    payload: str           # JSON：目标 sha、issue 号、finding ids
    leased_by, lease_until # 心跳/超时（worker 崩溃可回收）
    attempts, max_attempts
    priority: int
    result_ref: str | None
    created_at, started_at, finished_at
    # index(status, priority, created_at) —— 领取查询用
```

### 4.3 枚举（集中在 `models/agent_hub.py` 顶部，禁止散落字符串）

```python
FINDING_STATUS = ("open", "acknowledged", "wontfix", "fixed", "stale")
FINDING_LEVEL  = ("blocking", "debt")
TASK_STATUS    = ("queued", "leased", "running", "done", "failed", "dead")
```

### 4.4 fingerprint 规范（I1，必须逐字实现）

```
fingerprint = sha256(f"{rule_id}\x00{file_path}\x00{symbol}\x00{context_key}")
```

- `symbol`：函数/类/方法全名；无符号时用文件内稳定锚（如 blueprint 名、路由前缀）。
- `context_key`：规则自定义的附加判别位。**默认空串**。
  只有「同一位置同一规则但确实算两个问题」时才让规则提供，例如
  `debt.duplicate-implementation` 用「重复实现的第二处路径」作 context_key。
- **禁止**把行号、提交 sha、时间、文本摘要放进 fingerprint——它们会让去重失效。
- 用 `services/digest.py` 的现有 sha256 helper，不要另写。

### 4.5 迁移

- 沿用现有建表方式（`models/base.py` + 启动时 create_all / 现有 migration 习惯）。
- **不引入 alembic**（现有工程没有）。新增列用「create_all + 幂等 ALTER 助手」，
  助手放 `models/agent_hub_migrate.py`，只处理本模块的表。
- SQLite 与 PostgreSQL 都必须可用（`check_database.py` 是门禁）。

### 4.6 finding ↔ issue 关联（导入历史的直接价值）

```
finding_evidence: finding_id, repo_issue_id, relation("mentions"|"duplicate_of"|"fixed_by")
```

- 关联来源有两类：**确定性**（finding 的 `file_path` 命中了 issue body 里的代码块/路径）
  与**智能体建议**（agent 判断「这个问题历史 issue #1234 讨论过」→ 落库为 `mentions`，需人确认）。
- **确定性关联直接生效；智能体建议默认 `pending`，控制台确认后生效。**
- 这条是「导入 issue 历史」的落点：**agent 报告新问题前必须先查历史 issue**，
  若已有讨论则报告里必须引用（见 §8.3）。

---

## 5. HTTP / 协议契约

### 5.1 权限点（新增，必须同步 `auth/permissions.py` 与目录门禁）

| 权限点 | 含义 | 授予角色 |
|---|---|---|
| `repo:read` | 列出/查看仓库与 issue | 所有登录用户 |
| `repo:write` | 导入、触发同步、改变 repo 配置 | admin |
| `repo:push` | clone/push（内部走 git 协议，API 侧签发凭据） | 所有登录用户（受分支保护约束） |
| `finding:read` | 查看 finding 与债务看板 | 所有登录用户 |
| `finding:decide` | acknowledge / wontfix / 驳回 | 仓库 owner 角色 |
| `agent:run` | 触发一次 review/fix 任务 | 所有登录用户（受配额约束） |
| `agent:admin` | 管理运行时、查看全部任务、重试/终止 | admin |
| `policy:write` | 写 review policy（`.agent/review-policy.yml`） | admin |

**默认策略**：`anonymous` 依然只有 `doc:read`，一个新权限点都不给（保持「anonymous 只有文档」总纲）。

### 5.2 git 协议面（由 Forgejo 提供，nginx 暴露）

| 路径 | 说明 |
|---|---|
| `/git/<owner>/<repo>.git/info/refs?service=git-upload-pack` | clone/fetch |
| `/git/<owner>/<repo>.git/info/refs?service=git-receive-pack` | push（需 `repo:push`） |
| `/forgejo-api/` | 内部 API 反代（openfish 后端调用，不对外直接暴露） |

凭据：**两段式**。调用方先用平台凭据（API-key，或 DSH 通过设备授权自动获得的
那一枚）向 `GET /api/v1/repos/<slug>/git-credential` 换取一张票；平台凭据是第一道门
（`repo:push`），**git 面本身使用的是换来的 Forgejo 票**——因为 `git-receive-pack` 的
Basic 由 Forgejo 校验，它不认识平台的 API key（§13.1）。

兑换规则：平台为每个用户懒创建一个专属 Forgejo 账号（`of-<user_id>-<sha256(external_id)[:10]>`，
**一经上线不可改**），并用 `FORGEJO_ADMIN_TOKEN` 铸一张最小 scope 的 access token：
`kind=workspace` 给 `write:repository`，`kind=upstream` 只读镜像给 `read:repository`。
token 用 Fernet（`GIT_IDENTITY_KEY`）加密落库、按用户隔离、到期轮换，用户被禁用即撤销
（`cli.py disable-user`）。缺 `GIT_IDENTITY_KEY` 时该端点返回 **503**，**不降级为明文**。

### 5.3 REST 契约（`/api/v1`，需同步 `openapi/` 与 `check_contract.py`）

```
GET    /api/v1/repos                          # 列表，?q=&kind=&page=
POST   /api/v1/repos                          # 创建本地工作仓
POST   /api/v1/repos/import                   # {source_url, mode, include_issues, include_prs}
GET    /api/v1/repos/<slug>                   # 详情 + 计数
GET    /api/v1/repos/<slug>/issues            # ?state=&label=&q=&page=
GET    /api/v1/repos/<slug>/issues/<number>   # 正文 + 评论（若已镜像）
POST   /api/v1/repos/<slug>/sync              # 触发增量同步 → ImportJob
GET    /api/v1/imports/<job_id>               # 进度（前端轮询）
GET    /api/v1/findings                       # ?repo=&status=&level=&rule=&owner=
GET    /api/v1/findings/<id>
POST   /api/v1/findings/<id>/decide           # {action: fix|acknowledge|wontfix|false_positive, owner?, due?, reason}
POST   /api/v1/findings/<id>/fix              # 生成修复任务 → AgentTask
GET    /api/v1/agent/tasks                    # ?repo=&status=
POST   /api/v1/agent/tasks                    # {repo, kind, payload}
POST   /api/v1/agent/tasks/<id>/retry
POST   /api/v1/agent/tasks/<id>/cancel
GET    /api/v1/agent/tasks/<id>/log           # 日志引用
GET    /api/v1/policies/<slug>                # 只读展示 + policy_hash
PUT    /api/v1/policies/<slug>                # policy:write
GET    /api/v1/repos/<slug>/context/search    # ?q= → issue/commit/finding 混合检索（§8.2）
```

**错误契约**：沿用现有 `errors.py` 的错误类型。新增两种：
- `409 finding_decision_invalid`：blocking 命中 wontfix，或缺 owner/due（I2/I3）。
- `409 repo_protected_branch`：试图对保护分支直接操作（I4）。

### 5.4 Webhook（Forgejo → openfish）

| 事件 | 动作 |
|---|---|
| `push` | 若目标是默认分支且 policy 开启 auto_review → 入队 `review` |
| `pull_request` | 记录 pr_url 回填 finding；合并后触发文档回写 |
| `issues` | 若 label 含 `agent` → 入队 `fix` 任务 |

**安全**：webhook 必须校验共享密钥（`X-Forgejo-Signature` HMAC），
密钥来自环境变量，**不进仓库**（与 `model_routes.json` 的「不落密」原则一致）。

---

## 6. Finding 状态机（本文档的核心）

### 6.1 迁移表

| from | 事件 | to | 触发者 |
|---|---|---|---|
| （无） | run 首次命中 | `open` | agent |
| `open` | 人决定延期（填 owner+due） | `acknowledged` | 开发者 |
| `open` | 人决定不修（填 owner+due，仅 debt） | `wontfix` | 开发者 |
| `open` | 判定误报 | `fixed`（reason=rejected） | 开发者 |
| `open`/`acknowledged`/`wontfix` | 修复 PR 合并 | `fixed` | 系统 |
| `open`/`acknowledged` | 代码已变且规则不再命中 | `stale` | 系统 |
| `acknowledged`/`wontfix`/`stale` | 见 §6.2 激活 | `open` | 系统 |

### 6.2 激活条件（只有三种，实现为 `services/findings.py::should_reactivate`）

1. **代码漂移**：`file_path` + `symbol` 所在区域又被改动。
   判定用**只读副本的 `git diff` 行区间与符号区间求交**，不是「文件被碰过」。
2. **债务到期**：`due <= today`。
3. **升级**：同 `rule_id` 在本仓的 `open+acknowledged+wontfix` 总数超过 policy 阈值，
   或 `severity` 被重新判定为更高档。

**blocking 例外**：`level == "blocking"` 的 finding **不因 wontfix 而静默**，
且 `decide` 拒绝 `wontfix`/`acknowledge`（I3），只有 `fixed`（真修）或
`fixed(reason=false_positive)`（走 §6.3 流程改规则）。

### 6.3 误报与规则治理

- 标「误报」需要第二人确认（`finding:decide` + 非本人），确认后写 `fixed` 并记 `FindingEvent`。
- 同一 `rule_id` 在 30 天内误报 ≥ 3 次 → 生成一条**「规则待修」finding**（rule_id 固定为
  `meta.rule-quality`），指向该规则的 policy 条目，要求改规则或降级为 debt。
- 这条是为了防止「规则噪音 → 开发者批量忽略 → agent 失去信任」的死亡螺旋（§12.1）。

### 6.4 自动修复的边界（硬边界）

| 允许 agent 自动修 | 只报告、不自动改 |
|---|---|
| 命名/类型写法/logger 名/死导入 | 服务拆分与合并 |
| 违反「一个关注点一份实现」的重复实现 | 缓存/存储策略替换 |
| 缺文档、文档与路由表不一致 | 权限模型与角色设计 |
| `openapi.json` 顺序等机械契约 | 协议面语义变更 |
| gates 失败的直接修复 | 任何需要改 `model_routes.json` 的改动 |

规则归属写在 policy 文件里（`autofix: true|false`），**默认 false**。

---

## 7. Review Policy 文件

### 7.1 位置与生命周期

- 每个仓库根：`.agent/review-policy.yml`（agent 读，人改，走 PR 评审）。
- 平台侧缓存副本 + `policy_hash` 落 `review_runs`；policy 变更后第一次 run 必须在
  PR 描述里说明「策略变了，因此本次报告与上次不可直接比较」。

### 7.2 schema（实现为 pydantic 模型，`services/review_policy.py`）

```yaml
version: 1
defaults:
  auto_review: true          # push 到默认分支是否自动跑 review
  max_findings_per_run: 50   # 防止一次刷屏
rules:
  - id: backend.no-typing-optional
    level: blocking
    autofix: true            # 机械可改
  - id: docs.missing-route-doc
    level: debt
    default_due_days: 30
    autofix: false
exceptions:
  - rule: backend.openapi-schema-order
    paths: ["backend/routes/pypi.py"]
    reason: "PEP 563 + flask-pydantic 必须保留注解，见文件头注释"
    decided_by: shaojun0
    due: 2026-12-31
    status: wontfix
escalation:
  rule_noise_threshold: 3    # 30 天内误报次数
  rule_count_threshold: 20   # 同规则累计条数触发升级
```

### 7.3 约束（API 层校验，写进 `check_contract`）

- `exceptions[*].due` **必填**且必须 > 今天；缺少即 400。
- 引用不存在的 `rule_id` 的 exception 视为无效，读取时给出 warning 列表。
- policy 文件不存在时，平台用**只读默认策略**（全 blocking 集内建，autofix 全 false），
  并在 API 响应里标 `policy_source: "builtin-default"`。

---

## 8. 仓库导入（G1 的主战场）

### 8.1 两种模式

| 模式 | 行为 | 用途 |
|---|---|---|
| `mirror`（只读上游） | Forgejo migration 拉代码 + issue/PR/label/milestone；`repo.kind=upstream`；禁止 push | 导入 vllm 这种大仓当**上下文源** |
| `workspace`（可写工作仓） | 可在其上跑 agent、开 PR；issue 可增量导入 | 自己团队的项目 |

### 8.2 导入流水线（`services/repo_import.py`，可断点续传）

```
1 validate     解析 URL、探测是否可达、探测 issue 规模（轻量 API 探一次）
2 migrate      调 Forgejo migration（带 issues/labels/milestones/prs 开关）→ forgejo_repo
3 poll         轮询 Forgejo migration 状态，写 ImportJob.progress
4 mirror_issues 分页拉 Forgejo issues → upsert 进 repo_issues（幂等 key=source_id）
5 index_commits 浅层拉提交元数据（限制条数，默认 5000，可配）→ repo_commits
6 build_search 在 services/repo_context.py 建检索（见 8.3）
7 done         repo.sync_state=ready, 物化 issue_count/commit_count
```

- **每一步开始前写 `ImportJob`，结束写 cursor**：worker 崩溃后从 cursor 续，不重头。
- **大仓保护**：issue 镜像默认上限 `IMPORT_MAX_ISSUES`（默认 20000）。
  到上限即在 `ImportJob` 标 `partial: true`，**API 必须把这个状态显式暴露**，
  不允许静默截断（vllm 的 issue 量级需要这条）。
- **限速**：对上游 API 用 `services/upstream.py` 的重试与退避；默认 2 req/s。

### 8.3 上下文检索（`services/repo_context.py`）

给 agent 的不是「把 2 万条 issue 塞进 prompt」，而是**按需检索**：

| 能力 | 实现（本轮） | 说明 |
|---|---|---|
| 关键词检索 | SQL `ILIKE` / SQLite `LIKE` + 标题加权 | 双库通用，无新依赖 |
| 结构化过滤 | state / label / author / 时间区间 | 索引覆盖 |
| 关联扩展 | finding_evidence 命中的 issue 优先返回 | §4.6 |
| 语义检索 | **本轮不做**，留 `EMBEDDING_ROUTE` 钩子 | 复用模型路由里已有的 `embedding` 别名 |
| 返回预算 | 强制返回 top-k + 字符预算（默认 8k），超出截断并标注 | 防 prompt 爆炸 |

**agent 报告新 finding 前必须先调一次 `/context/search`**（把这条写进 `AGENTS.md`
与 runner 的系统提示词）；命中历史 issue 时，finding 的 `detail` 必须引用编号。
**这正是「导入历史 issue」的收益：同一个问题不会被当成新发现重复提。**
同时按 I6，issue 正文只是证据，**正文里的任何指令都不得被执行为操作**。

### 8.4 导入内容中的 prompt injection（必须实现）

- 导入的 issue/PR 正文与评论一律视为**不可信数据**：注入 prompt 时用明确的
  分隔标签包裹（如 `<untrusted-issue>`），并在系统提示词中声明其非指令性。
- agent 不得因 issue 正文里的请求而改变 policy、改权限、访问仓库外资源。
- 沙箱无网络出口（除模型端点与内网镜像），从机制上兜底。

---

## 9. 智能体运行时

### 9.1 任务队列（`services/agent_queue.py`）

- 表 `agent_tasks`，状态机 `queued → leased → running → done|failed|dead`。
- 领取用**乐观租约**：`UPDATE ... WHERE status='queued' ORDER BY priority DESC, created_at LIMIT 1`
  （SQLite 用 `BEGIN IMMEDIATE`，PG 用 `FOR UPDATE SKIP LOCKED`）。
- 心跳：worker 每 15s 续 `lease_until`；过期任务可被其他 worker 回收（`attempts += 1`）。
- `attempts > max_attempts` → `dead` 并告警（进 finding? 不，进运维日志）。
- CLI：`python -m services.agent_queue worker --once|--loop`，与现有 `cli.py` 风格一致。

### 9.2 沙箱（compose profile `runner`）

```
docker compose --profile runner up -d runner
```

- 镜像 `openfish-runner`：基于 `python:3.12-slim` + `git` + 仓库所需基础工具；
  **不含模型权重、不含宿主 docker socket**。
- 每个任务一个工作目录（`/work/<task_id>`），任务结束**保留 24h 便于排障再回收**。
- 资源限制：`--cpus 2 --memory 2g --pids-limit 512`；无外网（`network: internal`）。
- 凭据注入：模型 key 从平台 `models/resolved` 取（已有能力），**通过环境变量传入，
  任务结束即失效**；不写进工作目录。

### 9.3 agent 的任务协议（`AGENTS.md` 契约）

runner 执行时固定流程：

```
1 clone        只读副本 fetch 目标 sha
2 read         AGENTS.md + .agent/review-policy.yml（没有则用 builtin-default）
3 gates        跑 gates（见 9.4），失败 → 进入 fix 模式（若 autofix）
4 review       按 policy 规则逐条判定 → 产出 findings JSON（schema 见 9.5）
5 search       每条 finding 先检索历史 issue（§8.3），填 evidence
6 emit         写 result.json + 分支推送 + 开 PR（仅 fix 模式）
```

### 9.4 gates 归一化（`services/gates.py`）

- 发现 `backend/scripts/check_*.py`，逐个执行，**每个 gate 独立超时（默认 120s）**。
- 输出归一化为 `{gate, passed, exit_code, stdout_tail, duration_ms}`，落 `review_runs` 计数。
- gates **必须是 agent 的自证验收**：PR 描述里要贴 gates 汇总，CI 里也要跑同一份。
- 与 CI 的关系：CI workflow 调同一入口（见 §10），避免「本地过、CI 不过」两套真相。

### 9.5 findings 产出 schema（runner ↔ platform 的唯一接口）

```json
{
  "run": {"commit_sha": "…", "policy_hash": "…", "started_at": "…"},
  "findings": [
    {
      "rule_id": "backend.no-typing-optional",
      "level": "blocking",
      "severity": "medium",
      "file_path": "backend/services/foo.py",
      "symbol": "FooService.bar",
      "line_hint": 42,
      "title": "…",
      "detail": "…",
      "evidence": [{"kind": "issue", "number": 1234, "relation": "mentions"}],
      "autofix": false
    }
  ],
  "gates": [{"gate": "check_lint", "passed": true, "duration_ms": 812}]
}
```

- 平台侧用 pydantic 校验；**校验失败的任务标 `failed`，不写 finding**（宁缺勿脏）。

---

## 10. 与现有工程的一致性要求

| 要求 | 落点 |
|---|---|
| 新增 `make gates` 入口 | 根 `Makefile`：串起 `backend/scripts/check_*.py` + 前端 smoke |
| CI | `.github/workflows/gates.yml`（**仓库当前没有 CI**，这是 B 的地基） |
| 边缘路由 | `docker/nginx/nginx.conf` 增 `/git/`、`/forgejo-api/`；SPA 增 `/repos`、`/findings` |
| compose | `docker/docker-compose.yml` 增 `forgejo`、`runner`（profile） |
| 挂载 | `docker/prepare-mounts.sh` 增 `forgejo/`、`agent-work/` |
| 前端 | `frontend/src/views/` 增 Repos / RepoDetail / Findings / Import；照抄现有 `usePagination` |
| OpenAPI | `openapi/` 注册新路由；`check_openapi.py` / `check_contract.py` 必须过 |
| 权限目录 | `auth/permissions.py` + `check_permission_catalog.py` + `check_permission_labels.py` |
| 文档 | 本文件 + `README.md` 一节 + `/documentation` 生态文档条目 |

---

## 11. 实施路线图（子智能体分工）

**共享文件纪律**：每个切片只新建自己命名空间内的文件。下面标 **【共享】** 的文件由
集成阶段统一修改，切片实现者**不要改**，而是把所需改动写进
`docs/agent-hub/integration/<slice>.md`。

| 切片 | 交付 | 关键文件（新建） | 依赖 |
|---|---|---|---|
| **S0 基座** | 表结构、权限点、队列、CLI、Makefile、CI | `models/agent_hub.py`、`models/agent_hub_migrate.py`、`auth/permissions.py`【共享】、`services/agent_queue.py`、`Makefile`、`.github/workflows/gates.yml`、`scripts/check_agent_hub.py` | 无 |
| **S1 仓库与 git 面** | 仓库 CRUD、git 协议暴露、webhook、导入驱动 | `services/repo_import.py`、`routes/repos.py`、`routes/repo_webhook.py`、`docker/forgejo/`、nginx 片段 | S0 |
| **S2 上下文检索** | issue 镜像、检索 API、证据关联 | `services/repo_context.py`、`models/repo_issue.py`、`routes/repo_context.py` | S0 |
| **S3 Findings 状态机** | fingerprint、去重、状态机、policy、决策 API | `services/findings.py`、`services/review_policy.py`、`routes/findings.py` | S0 |
| **S4 运行时** | runner、gates 归一化、任务 API、AGENTS.md | `services/agent_runner.py`、`services/gates.py`、`routes/agent_tasks.py`、`docker/runner/`、`AGENTS.md` | S0 |
| **S5 前端与文档** | Repos/Findings 页面、README 章节 | `frontend/src/views/*`、`frontend/src/api/agentHub.js` | S1–S4 |

**验收（集成后由主智能体执行）**

```bash
cd backend && python scripts/check_lint.py && python scripts/check_openapi.py \
  && python scripts/check_contract.py && python scripts/check_permission_catalog.py \
  && python scripts/check_database.py && python scripts/check_agent_hub.py
cd frontend && npm run smoke
make gates
```

---

## 12. 竞品与反模式（实现时用来自我校验）

### 12.1 三条死亡螺旋（必须设计上避免）

1. **噪音螺旋**：报告太多 → 开发者批量忽略 → agent 失去信任。
   → 对策：`max_findings_per_run`、规则误报治理（§6.3）、blocking/debt 分档。
2. **静音名单螺旋**：wontfix 无期限 → 问题永久消失。
   → 对策：I2 强制 due + 到期激活（§6.2）。
3. **重报螺旋**：每次 review 都当新问题。
   → 对策：fingerprint 去重 + 历史 issue 检索（§4.4、§8.3）。

### 12.2 参考产品与抄什么

| 参考 | 抄什么 | 别抄什么 |
|---|---|---|
| SkillsGateway | 企业网关式分发、git 分发的技能仓 | 只做分发不做执行 |
| GitLab Duo Agent Platform / AI Catalog | agent 的版本/权限/可见范围元数据模型 | 体量与平台耦合 |
| GitHub Copilot coding agent | issue → 分支 → PR 的标准闭环 | 依赖公有云 |
| Gitee 智能化软件工厂 | 私有化交付、issue→MR→流水线叙事 | 大而全 |
| Langfuse / PromptLayer | 版本 + label + 环境的同步语义 | prompt 为中心的窄视角 |
| CodeRabbit / Sourcery | review 评论的呈现与降噪策略 | 无状态的「每次重报」 |
| Dify / Coze Studio | 模型路由、插件市场、多租户 | 可视化编排为中心（我们的中心是 git） |

### 12.3 名词表

- **finding**：一次规则命中形成的有生命周期对象（≠ 一条评论）。
- **fingerprint**：finding 的稳定身份，去重的唯一依据。
- **debt**：可延期但有 owner/due 的 finding；**blocking**：不可延期。
- **drift**：finding 所在符号区间被再次改动。
- **evidence**：finding 与历史 issue/PR 的关联。

---

## 13. 开放问题（实现中遇到请追加到 `docs/agent-hub/OPEN-QUESTIONS.md`）

1. **（已闭合，S6；但上线前须用真 Forgejo 验证一次）push 的凭据链。**
   `git-receive-pack` 的 Basic 认证由 **Forgejo** 承担，而旧 `/git-credential` 铸造的是
   **openfish API key**；Forgejo 不认识它，因此 `clone`（匿名只读）成立、**`push` 必 401**。
   S6 采用**身份兑换**闭合：`GET /api/v1/repos/<slug>/git-credential` 保留 `repo:push` 作为
   第一道门，然后用平台的 `FORGEJO_ADMIN_TOKEN` 为该用户懒创建一个 Forgejo 账号并铸一张
   **Forgejo access token** 返回；token 用 Fernet（`GIT_IDENTITY_KEY`）加密落库、按用户
   隔离、过期轮换，撤销接到 `cli.py disable-user`。部署变量、admin token 的最小 scope
   （`write:admin`）与等价 CLI 引导路径见 `docs/agent-hub/integration/S6.md`。
   **残留风险**：铸票的 REST 形状（`POST /admin/users` + `POST /users/{u}/tokens` 带
   `Sudo:`）没有活 Forgejo 可验证，门禁使用假客户端——上线前必须实测一次 push；
   兜底是 CLI 引导。分支保护仍独立成立（I4），用户票不含任何 admin scope。
2. 前端「历史 issue 证据」与「gates 结果」两块目前恒为空：`Finding` 的序列化不内联
   evidence，也没有 review_run 的 HTTP 出口（`services.repo_context.related_to_finding()`
   已实现但未接路由）。修法二选一：搜索路由透传 `finding_id`，或详情内联 evidence + 最近一次 run。
3. Forgejo migration 对 2 万+ issue 的耗时与内存表现未知，是否需要先只导最近 N 年？
4. issue 增量同步的触发频率（webhook / 定时 / 手动）与限速策略。
5. 语义检索是否本轮就要接 `embedding` 模型路由（当前设计留钩子）。
6. 权限点 `repo:push` 的粒度：仓库级还是全局级（本轮按全局，后续可能需要 per-repo）。
7. 沙箱执行 DSH 的形态：单进程 headless 还是容器内起 `dsh web` + 驱动 API（S4 已选前者）。
8. 保护分支规则由 Forgejo 原生（branch protection）还是 openfish policy 承担（倾向 Forgejo 原生）。
9. `read_policy_auto_review()`（S1）仍在猜 `services/review_policy` 的函数名
   （`auto_review_enabled` / `load_auto_review` / `auto_review` 都不存在），实际走到
   「文件扫描 + 默认 True」。结果与 §7.2 的 `auto_review: true` 一致，但应改为直接调用
   `review_policy.load_file(...)->defaults.auto_review`，把巧合变成契约。
