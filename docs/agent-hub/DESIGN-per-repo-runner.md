# openfish 逻辑 per-repo runner 设计（B1）

> **状态**：`repo_runners` 模型、`ensure_schema` 迁移、`RepoRunnerService` 与
> `AgentTask.runner_id` 已在基座落地并验证（`models/agent_hub.py`、
> `models/agent_hub_migrate.py`、`services/repo_runner.py`、`services/agent_queue.py`）。
> 本文件记录**执行边界**的设计与语义，包含验收门禁与未决问题。
> **规格关系**：本文件细化 `DEVELOPMENT.md` §9.2 的沙箱一节，并闭合 §13 开放问题 6
> 中「repo:push 粒度 / per-repo runner」的选择（**B1 = 逻辑 runner**）。
> **与鉴权设计的关系**：身份账号体系见
> [`DESIGN-git-thin-layer.md`](./DESIGN-git-thin-layer.md)；两者关注点不同、不冲突，
> 见本文 §13。

---

## 0. 结论（TL;DR）

1. **进程池不拆，配置按仓库拆。** 平台仍然只跑**一个共享的 runner 进程池**
   （`docker compose --profile runner --scale runner=N`），**不挂宿主 docker
   socket**，不为每个仓库起守护进程。每个仓库多出来的东西只有**一行配置**：
   `repo_runners`。
2. **一行一仓库。** `repo_runners.repo_id` 唯一，所以「仓库 X 用哪份凭据 / 写哪个
   目录 / 限几个并发 / 声明什么出网策略」永远只有一个答案。
3. **任务绑定 runner。** `enqueue` 时把当前 `RepoRunner.id` 写进
   `AgentTask.runner_id`（软引用，无 FK）；`claim` 用 `NOT EXISTS` 跳过「该仓库的
   runner 被禁用」的任务。老任务 `runner_id IS NULL` 照常可领，不因新增列而卡死。
4. **凭据 fail-closed。** 默认回退到部署级 `FORGEJO_RUNNER_TOKEN`；仓库专属 token 用
   `services.git_identity.TokenCipher`（Fernet，`GIT_IDENTITY_KEY`）封装后落库。
   **明文永不进数据库 / 日志 / argv / `.git/config`**；密文解不开时**报错，不回退**。
5. **工作区按 runner 隔离。** 默认 `AGENT_WORK_ROOT/runners/<runner_id>`，路径经
   `safe_workspace_subdir()` 校验，越界路径直接拒绝（不静默改写成「看起来安全」）。
6. **配额优先级固定。** 显式入参 > `repo_runners.max_concurrency`（> 0 时）>
   `AGENT_MAX_IN_FLIGHT_PER_REPO`；`0` 一律表示「继承」。
7. **`egress_policy` 是声明，不是执行。** 平台只**持久化**策略并在任务上标注；
   真正的网络分段（`internal` 网络、防火墙、出口白名单）由**部署侧**执行。
   不把「写了 allowlist」说成「平台已经拦住了」。
8. **迁移无 Alembic。** 走 `models/agent_hub_migrate.ensure_schema(engine)`，幂等，
   SQLite 与 PostgreSQL 同路径，返回「本次实际改了什么」。

---

## 1. 目标与非目标

### 1.1 目标

| # | 目标 | 验收信号 |
|---|---|---|
| G1 | 每个仓库有且只有一行执行配置 | `repo_runners.repo_id` 唯一；`ensure()` 幂等且不覆盖既有设置 |
| G2 | 任务领取受仓库级开关与配额约束 | 禁用 runner 的任务不被 `claim`；超出 `max_concurrency` 的入队返回 0（抑制） |
| G3 | 凭据按仓库解析，且只以密文落库 | `credential()` 优先返回 repo 密文解密结果，否则共享 token；解密失败抛错不回退 |
| G4 | 工作区不越界 | `safe_workspace_subdir()` 拒绝绝对路径 / `..` / 反斜杠 / 控制字符 / 非法字符集 |
| G5 | 出网策略有唯一存放处且语义诚实 | `egress_policy ∈ {inherit, internal, allowlist}`，文档明确「平台只持久化」 |
| G6 | 新 schema 幂等补全，老库平滑升级 | `ensure_schema()` 二次调用零改动；`check_database.py` 在 SQLite/PG 都过 |

### 1.2 非目标

- **不做每仓库一容器**：不引入 docker socket、不引入 k8s、不引入每仓库常驻进程
  （理由见 §2）。
- **不实现网络分段**：平台不写 iptables / 不建 `internal` 网络、不代理出口流量；
  只持久化 `egress_policy` / `egress_allowlist`（见 §8）。
- **不承担用户身份**：用户 git 身份、账号体系与反向代理认证属于薄中间层设计，
  不在本设计范围内（见 §13）。
- **不新增依赖**：沿用 `cryptography`（Fernet）、SQLAlchemy、现有服务模块。
- **不让 runner 容器拿到平台密钥**：本设计不改变 `docker/runner/README.md` §9 的
  凭据边界，**不把 `GIT_IDENTITY_KEY` 塞进 runner 环境**；repo 凭据的解密必须发生在
  受信侧，绝不是任务自带的 `check_*.py` 子进程。当前 worker 在 runner 容器内解析
  凭据这一事实与该边界存在缺口，见 §5.2 / §12.7。

---

## 2. 为什么是「逻辑 runner」而非「每仓库一容器」

候选形态与取舍：

| 形态 | 隔离强度 | 代价 | 结论 |
|---|---|---|---|
| **A. 全局单 runner 配置** | 最低：所有仓库共用一份凭据 / 目录 / 配额 | 一个仓库的噪声能饿死其余仓库；凭据不可按仓库收回 | 现状，不够 |
| **B1. 共享进程池 + 每仓库配置**（本设计） | 执行边界按仓库区分：凭据、工作区前缀、配额、出网声明 | 一行配置 + 领取路径一次 `NOT EXISTS`；进程池不拆 | **选择** |
| B2. 每仓库一容器 | 进程/文件系统级隔离最强 | 需要 docker socket 或 Docker API 服务；每个活跃仓库一个常驻容器；编排、镜像分发、日志回收全部翻倍 | 非目标 |
| B3. 每任务一容器 | 同上且更细 | 与 B2 同因，且 `RunnerAdapter` 要换实现 | 留作后续（见 §12） |

选 B1 的三个理由：

1. **不挂 docker socket 是硬约束。** 「按仓库/按任务起容器」要么挂 socket，要么把
   Docker API 暴露成服务，二者都扩大攻击面；而 runner 会执行仓库自带的
   `check_*.py`，是**最不该**拿到控制面能力的进程。
2. **隔离需求目前落在「执行边界」而不是「内核边界」。** 一个任务只在一个仓库内
   （`AGENTS.md` §0.2）；需要按仓库区分的是**用谁的凭据、写哪里、占多少并发、
   被声明成什么出网档位**，这些用一行配置表达就够，不需要第二套编排。
3. **队列天然支持共享池。** `services/agent_queue.py` 已具备原子领取、租约心跳、
   过期回收、`FOR UPDATE SKIP LOCKED` / `BEGIN IMMEDIATE`；B1 只是把「该不该领、
   以什么配置跑」接到这条既有路径上，不新增 broker、不新增状态。

> 形态标签说明：本文的 **B1** 指「逻辑 per-repo runner」。
> `DESIGN-git-thin-layer.md` §4.D-B 里的 **B1** 是「认证回退方案」，两者同名不同物，
> 引用时请带上下文。

---

## 3. 数据模型：`repo_runners`

`models/agent_hub.py` 定义，`models/agent_hub_migrate.py` 的 `AGENT_HUB_TABLES`
纳入建表，**一个仓库恰好一行**：

```python
class RepoRunner(Base):
    __tablename__ = "repo_runners"
    id: int                    # PK
    repo_id: int               # FK repos.id, unique, index, ondelete=CASCADE
    name: str                  # 展示名，默认取仓库 slug，回退 repo-<id>
    enabled: bool              # False = 该仓库的任务不可领取（默认 True）
    max_concurrency: int       # 0 = 继承 AGENT_MAX_IN_FLIGHT_PER_REPO
    workspace_subdir: str      # "" = "runners/<id>"（相对 AGENT_WORK_ROOT）
    egress_policy: str         # inherit | internal | allowlist
    egress_allowlist: str | None   # 逗号分隔主机名；仅 allowlist 时有意义
    credential_kind: str       # shared | repo
    credential_username: str | None
    credential_ciphertext: str | None  # Fernet 密文，绝无明文
    credential_expires_at: datetime | None
    credential_rotated_at: datetime | None
    last_task_at: datetime | None      # 每仓库「最近活跃」信号
    created_at / updated_at
```

两条 CHECK 约束把枚举钉死在数据库上（SQLAlchemy 声明与迁移清单同名）：

```
ck_repo_runners_credential_kind:  credential_kind IN ('shared', 'repo')
ck_repo_runners_egress_policy:    egress_policy  IN ('inherit', 'internal', 'allowlist')
```

`to_dict()` 的边界：

- **不出现** `credential_ciphertext`——控制台只能知道「有没有」，不能读到密文；
- **出现** `has_credential: bool`（等价于密文非空）；
- 其余可展示字段（`enabled`、`max_concurrency`、`workspace_subdir`、`egress_*`、
  `credential_kind` / `credential_username` / 过期与轮换时间、`last_task_at`）原样输出。

`services/repo_runner.py` 是**凭据字段的唯一写入者**；其他模块只读，
避免出现第二份「怎么存 token」的实现。

---

## 4. 调度绑定：`AgentTask.runner_id` 与「禁用的 runner 不可领取」

### 4.1 软引用

`AgentTask.runner_id: Mapped[int | None]` 是**软引用**（`Integer`，无 FK）：

- runner 行被重置/删除时，任务历史不被级联删除，排障仍看得到「当时是哪台逻辑
  runner 跑的」；
- 老库可以把它当**普通可空列** ALTER 上去（见 §9），不需要回填。

### 4.2 入队：解析、绑定、配额

`AgentQueue.enqueue()` 的路径（`services/agent_queue.py`）：

1. 先做 `(repo_id, kind, dedup_key)` 的活跃任务去重（原样保留）；
2. 按 `repo_id` 查 `RepoRunner`：
   - 有行 → 把 `runner.id` 写进 `AgentTask.runner_id`；
   - 无行 → `runner_id = None`（该仓库尚未配置逻辑 runner，任务照常可跑）；
3. **配额优先级**（见 §7）：显式入参 > `runner.max_concurrency`（> 0）>
   `AGENT_MAX_IN_FLIGHT_PER_REPO`；
4. 超限时返回 `0`（**抑制**），调用方据此区分「已入队（id > 0）」与「被抑制（0）」，
   不会把一场风暴误当成成功。

### 4.3 领取：禁用的 runner 跳过

`AgentQueue.claim()` 在候选查询里加一个跨方言的 `NOT EXISTS`：

```python
runner_runnable = ~(
    select(RepoRunner.id)
    .where(RepoRunner.repo_id == AgentTask.repo_id,
           RepoRunner.enabled.is_(False))
    .exists()
)
```

语义：

- **禁用的 runner 不可领取**：即使有排队任务，`claim` 也跳过；
- **没有 runner 行照常可领**：`NOT EXISTS` 为真，不因未配置而阻塞；
- **老任务 `runner_id IS NULL` 照常可领**：判断按 `repo_id` 关联当前配置，
  不依赖任务上快照的 `runner_id`，所以禁用是**立即生效**的，不必等老任务出队；
- `ClaimedTask` 携带 `runner_id`，worker 后续据此解析该仓库的凭据 / 工作区 / 出网声明。

> 语义选择说明：禁用按**仓库当前配置**判定，而不是按任务入队时的快照。这样
> 「停掉一个仓库」是即时运维动作；代价是重新启用后，此前被跳过的排队任务会恢复可领
> ——这是期望行为（任务没有丢失）。

---

## 5. 凭据模型：shared 回退 vs repo-scoped 密文

`RepoRunnerService.credential(repo_id) -> RunnerCredential | None` 的解析顺序：

| 情形 | 返回 | `source` |
|---|---|---|
| `credential_kind='repo'` 且有密文，解密成功 | 解密出的 token | `"repo"` |
| 上述之外，`FORGEJO_RUNNER_TOKEN` 非空 | 共享 token | `"shared"` |
| 两者都没有 | `None` | — |

### 5.1 shared（默认，历史行为）

- 读 `shared_runner_token()`：`FORGEJO_RUNNER_TOKEN`，未设置返回 `""`（不是 `None`）；
- 用户名用约定值 `x-access-token`（Forgejo 接受访问令牌作密码，用户名任意）；
- 这是所有未配置仓库的默认路径，行为与既有 `agent_worker.runner_token()` 一致。

### 5.2 repo-scoped（Fernet 密文）

- 写入：`set_credential(repo_id, token=…, username=…, expires_at=…)`；
  **先取到 cipher，再写库**——缺 `GIT_IDENTITY_KEY` 时抛 `RepoRunnerError`，
  什么都不落库，绝不退化成明文或共享 token；
- 加密：`services.git_identity.TokenCipher`（Fernet，key 来自 `GIT_IDENTITY_KEY`）；
- 落库：`credential_kind='repo'`、`credential_ciphertext=<密文>`、
  `credential_username`、`credential_expires_at`、`credential_rotated_at`；
- 清除：`clear_credential()` 把 `credential_kind` 拨回 `shared`、清空密文 / 用户名 /
  过期时间，于是自动回退到共享 token。

**已知缺口（诚实标注）**：`services/agent_worker.py` 在 profile `runner` 的容器内
解析凭据，而该容器的 `x-runner-env` **刻意不含** `GIT_IDENTITY_KEY`
（`docker/docker-compose.yml` §9.2、`docker/runner/README.md` §9）。因此
`credential_kind='repo'` 目前在沙箱 runner 内会因取不到 cipher 而 **fail-closed**
（共享 token 路径不受影响）。解密封装应该发生在哪个进程尚未定，见 §12.7。

### 5.3 fail-closed

`credential()` 是**只读**的：

- 不创建行（`get` 语义），配置缺失就是缺失；
- `credential_kind='repo'` 但**解密失败或解出空串** → 抛 `RepoRunnerError`，
  **拒绝回退到共享 token**。若静默回退，任务会以**错误的主体**完成 push / 开 PR，
  比失败更危险；
- 「repo 密文存在但不清除」与「解密失败」是两件事：前者继续用 repo，后者必须报错。

### 5.4 明文边界

- 明文只存在于 `RunnerCredential.token` 这个不可变值里，生命周期不超出调用方；
- 日志只用 `services.agent_runner.mask_secrets()` 掩码，**永不**打印明文或密文原文；
- token **不进** DB 明文列、不进日志、不进 argv、不进 `.git/config`；
  注入 git 走环境变量 / credential helper（与 `docker/runner/README.md` §6 一致）。

---

## 6. 工作区隔离：`runners/<id>`

`workspace_root(repo_id, base)` = `Path(base) / safe_workspace_subdir(runner.workspace_subdir,
runner_id=runner.id)`：

- `workspace_subdir=""` → 默认 `runners/<runner_id>`（在 `AGENT_WORK_ROOT` 下）；
- 首次调用会经 `ensure()` 建行，因为默认路径是 **id 派生**的，没有行就没有 id。
- `update()` 拒绝任何解析后与**其它 runner 工作区相同**的 `workspace_subdir`（包括
  显式路径占用别人的 `runners/<id>` 默认值），保证仓库之间前缀唯一；
  `workspace_root()` 解析后校验结果仍在 `AGENT_WORK_ROOT` 内——路径里预先埋下的
  符号链接不能把 checkout 重定向出去——并按 `0700` 创建目录。

`safe_workspace_subdir()` 的规则（越界即抛 `RepoRunnerError`，不静默改写）：

| 输入 | 结果 |
|---|---|
| `""` / `None` | `runners`（无 id）或 `runners/<id>`（有 id） |
| `/abs` 或 `\abs` | 拒绝（绝对路径） |
| 含 `\` | 拒绝（反斜杠） |
| 含控制字符（`< 0x20` 或 `0x7F`） | 拒绝 |
| 任一段为 `""` / `.` / `..` | 拒绝 |
| 段内出现 `[A-Za-z0-9._-]` 之外的字符 | 拒绝 |
| `a/b` 形式的多段相对路径 | 接受 |

与既有 `agent_runner.workdir_for(root, task_id, attempt)` 的关系：后者保证**任务之间**
不撞车（`/work/<task_id>` 或 `/work/<task_id>-attempt<n>`）；本设计保证**仓库之间**
落在不同前缀。两者叠加后，共享 `/work` 挂载上的目录形如
`/work/runners/<runner_id>/<task_id>`，既不跨任务也不跨仓库。

> 迁移期注意：既有部署的目录是 `/work/<task_id>`。切换到 runner 前缀属于**目录布局
> 变更**，不能只改解析而不迁移；见 §12 未决问题。

---

## 7. 配额：`max_concurrency` 与 `AGENT_MAX_IN_FLIGHT_PER_REPO`

入队时的每仓库在飞上限，**严格按以下优先级**取第一个可用值：

```
显式入参 max_in_flight_per_repo
  > repo_runners.max_concurrency（仅当 > 0）
    > AGENT_MAX_IN_FLIGHT_PER_REPO（configured_max_in_flight_per_repo()）
```

| 值 | 含义 |
|---|---|
| 显式入参 `None` | 不参与，交给下一级 |
| `repo_runners.max_concurrency = 0` | **继承** 环境变量（不是「无限」） |
| `repo_runners.max_concurrency > 0` | 该仓库的硬上限 |
| `AGENT_MAX_IN_FLIGHT_PER_REPO = 0` / 未设 | 无上限 |

判定口径：统计该 `repo_id` 下状态属于 `ACTIVE_TASK_STATUSES`
（`queued` / `leased` / `running`）的任务数；达到上限则 `enqueue` 返回 `0`。
上限是**生产者侧**的抑制，不改变消费侧的领取逻辑（§4.3 的禁用判定独立成立）。

设计取舍：用「抑制入队」而不是「入队后排队」是为了防止单个吵仓库把全局队列塞满、
饿死其它仓库；代价是被抑制的任务需要生产者在下一轮再试（或由 webhook 重投），
这与既有的 `dedup_key` 去重行为一致。

---

## 8. `egress_policy` 的声明语义

**平台只持久化声明，不执行网络分段。** 这一点必须写在最显眼处，避免把「配了
allowlist」误读成「平台已经拦住了出口」：

| `egress_policy` | 平台侧行为 | 部署侧**必须自行**完成 |
|---|---|---|
| `inherit` | 只是默认值；不覆盖部署级网络策略 | 沿用 runner 容器的网络配置。**注意：当前 compose 给 runner 的是普通 `bridge`，默认带 NAT 公网出口，出口并未封堵**（`docker/docker-compose.yml` 的 `networks: openfish`；`DEVELOPMENT.md` §9.2 把网络隔离列为「待补」）。所以 `inherit` 现在是**最宽**的一档，不是安全默认 |
| `internal` | 持久化「该仓库只允许内网」 | `internal: true` 网络或等价的防火墙规则 |
| `allowlist` | 持久化去重后的逗号分隔主机名 | 出口白名单（代理 / egress gateway / 防火墙）真正放行这些主机 |

约束：

- `egress_allowlist` 经 `_normalize_allowlist()` 归一（`;` 视作 `,`、去空白、去重）；
  空值存 `None`；
- `update(egress_allowlist="")` 表示**清空**，与「不改动」（传 `None`）区分；
- 平台**不校验**主机名是否真实可达，也不在 runner 进程内做拦截；
  「声明与执行不一致」是部署配置错误，不是平台静默兜底。

> 诚实性条款：在本条目的执行侧落地之前，`egress_policy` 的验收只覆盖
> 「值合法、可持久化、可读取、可展示」，**不覆盖**「网络真的被分段」。
> 后者由 `DESIGN-git-thin-layer.md` 的边界门禁与部署清单各自负责。

---

## 9. 迁移：`ensure_schema` 幂等

`models/agent_hub_migrate.ensure_schema(engine)` 是无 Alembic 的顶层补全入口，
本设计的三处落点：

1. `RepoRunner` 在 `AGENT_HUB_TABLES` 中 → 新库**建表**；
2. `("agent_tasks", "runner_id", "INTEGER")` 在 `_COLUMNS` 中 → 老库**加列**
   （可空、无 server default，SQLite/PG 都能 `ADD COLUMN`）；
3. 两条 CHECK 在 `_CHECK_CONSTRAINTS` 中 → 枚举集将来变大时**重写约束**而不是
   被旧约束拒绝（SQLite 走建表重建、PG 走 `DROP`+`ADD`，先探测再动）。

幂等性保证：

- 建表只建缺失的表；加列先 `inspect` 再 `ALTER`；索引 `IF NOT EXISTS` + 先探测；
  约束先判断 live 是否已接纳全部取值；
- 返回 `{"created_tables": [...], "added_columns": [...], "added_indexes": [...],
  "updated_constraints": [...]}`——空报告即「数据库已是最新」，二次调用零改动；
- 只碰 Agent Hub 自己的十五张表，不误建/误改其他模块的表。

`check_database.py` 是这条路径的门禁：SQLite 与 PostgreSQL 都必须可用。

---

## 10. 门禁 `scripts/check_repo_runners.py`

本设计的离线验收门禁，与其余 `check_*.py` 同一发现机制（`backend/scripts/` 下自动
发现，`services/gates.py` 与根 `Makefile` 的 `make gates` 跑同一批）：

```bash
cd backend
.venv/bin/python scripts/check_repo_runners.py
```

门禁必须离线可跑（注入内存 SQLite / 夹具 session，不需要活服务），并 pin 住以下契约：

> 交付状态：门禁与 `services/repo_runner.py` 同属本设计的实现切片；下表即它的验收
> 规格。落盘后它会被 `backend/scripts/check_*.py` 的发现机制自动纳入 `make gates`。

| # | 断言 | 对应章节 |
|---|---|---|
| 1 | `repo_runners` 表存在；`repo_id` 唯一且有索引；两列 CHECK 的允许集与 `RUNNER_CREDENTIAL_KINDS` / `RUNNER_EGRESS_POLICIES` 一致 | §3 |
| 2 | `RepoRunner.to_dict()` **不含**密文字段、**含** `has_credential` | §3 |
| 3 | `ensure()` 幂等：二次调用返回同一行，且**不覆盖**已改过的 `enabled` / `max_concurrency` / `egress_policy` | §3 |
| 4 | `credential()`：repo 密文可解时 `source='repo'`；无 repo 凭据时回退 `source='shared'`；两者皆无时 `None`；**密文损坏时抛 `RepoRunnerError` 且不回退** | §5 |
| 5 | `set_credential()` 在缺 `GIT_IDENTITY_KEY` 时**不落库**；落库后 DB 中**不含明文**（夹具可断言密文 != 明文） | §5 |
| 6 | `clear_credential()` 后 `credential_kind='shared'`、密文为 `None`，`credential()` 回到共享 token | §5 |
| 7 | `safe_workspace_subdir()` 的拒绝矩阵（绝对路径 / `..` / 反斜杠 / 控制字符 / 非法字符）与默认值 `runners/<id>` | §6 |
| 8 | `workspace_root()` = `base/runners/<id>`，且会按需建行 | §6 |
| 9 | `enqueue` 把 `runner_id` 写对；配额优先级为「显式 > runner > env」 | §4.2 / §7 |
| 10 | 禁用 runner 的任务不被 `claim`；无 runner 行、`runner_id IS NULL` 的任务仍可领 | §4.3 |
| 11 | 迁移清单包含 `repo_runners` 建表、`agent_tasks.runner_id` 加列、两条 CHECK；`ensure_schema` 二次调用空报告 | §9 |

门禁纪律：**不为了变绿弱化断言**；新增一项能力就在本表加一行并在门禁里加断言，
版本化在 `scripts/check_repo_runners.py` 一处。

---

## 11. 验收场景

按顺序在**离线夹具**（1–6）与**真容器**（7；8 待 §12.7 闭合后）上验证：

1. **默认即共享。** 未配置任何 runner，入队一个 review 任务 → `runner_id` 为
   `None`（无行）或指向默认行；`credential()` 返回 `source='shared'`。
2. **禁用即时生效。** `update(repo_id, enabled=False)` 后：
   新任务不绑定可领取路径；已排队的任务在 `claim` 中被跳过；`enabled=True` 后恢复。
3. **配额优先。** `max_concurrency=1` 时，同仓库第二个活跃任务入队返回 `0`；
   显式入参覆盖它；`max_concurrency=0` 时回到 `AGENT_MAX_IN_FLIGHT_PER_REPO`。
4. **per-repo 凭据。** `set_credential(token="…")` 后：DB 里查不到明文；
   `credential().source == 'repo'`、token 与写入一致；`clear_credential()` 后回到
   `shared`。
5. **fail-closed。** 把 `credential_ciphertext` 篡改成乱码 → `credential()` 抛
   `RepoRunnerError`，**不**返回共享 token。
6. **工作区越界被拒。** `update(workspace_subdir="../escape")` →
   `RepoRunnerError`，且**没有**落库任何半成品改动。
7. **迁移幂等（真库）。** 对既有部署跑两次 `ensure_schema`：第一次报告
   `repo_runners` 建表与 `agent_tasks.runner_id` 加列（若缺失），第二次空报告；
   `check_database.py` 在 SQLite 与 PostgreSQL 都过。
8. **端到端（依赖 §12.7 闭合）。** 先定下 repo 凭据的解密封装位置，再配一个仓库
   专属凭据 + `runners/1` 工作区，跑一次 fix 任务：clone / push 用该凭据，工作树落在
   `AGENT_WORK_ROOT/runners/<runner_id>/<task_id>`，PR 打开后 `last_task_at` 更新。

---

## 12. 未决问题

1. **Forgejo 侧 ACL。** 平台把「仓库专属 token」当成执行凭据，但**平台不强制**
   Forgejo 上这枚 token 的作用域：签发时是否只给该仓库（或该 org/team）的写权限，
   由签发者 / Forgejo 配置决定。若一枚 token 实际能写别的仓库，本设计的「按仓库隔离
   凭据」就只是**平台视角**的隔离。待办：与薄中间层的服务账号模型一起，明确
   per-repo token 的 Forgejo 侧最小 scope，并在文档里写清「服务账号权限由 Forgejo
   侧收窄」。
2. **按仓库物理隔离。** 本设计只保证逻辑边界；将来若某个仓库需要进程/文件系统级
   隔离，可把 `docker/runner` 镜像当模板，由平台按仓库（或按任务）`docker run`
   一份——届时换 `RunnerAdapter` 的 docker 实现，`services/repo_runner.py` 的解析
   语义不变。属于 B2/B3 形态，不在本轮。
3. **目录布局迁移。** 从 `/work/<task_id>` 切到 `/work/runners/<runner_id>/<task_id>`
   需要一次性迁移或双读兼容期；24h 回收规则（`.done`）必须同时覆盖旧布局，
   否则老目录永不回收。
4. **token 过期策略（已决）。** `credential_expires_at` 在 `credential()` 里判定：
   过期即抛 `RepoRunnerError`（fail-closed，与 §5.3 一致），过期 token 不会再被
   交给 clone / push / 开 PR。轮换仍由运维显式触发；「临近过期告警」尚未实现。
5. **`egress_policy` 的执行侧接线。** 平台已能持久化声明，但把声明翻译成
   `internal` 网络 / 出口白名单的部署自动化尚未落地（§8 的诚实性条款）。
6. **`last_task_at` 的用途。** 现在只是「最近活跃」信号；是否据此做空闲回收
   （如长期无任务的 runner 行清理、工作区回收）未定。
7. **repo 凭据的解密封装位置（已知实现缺口）。** `RepoRunnerService.credential()`
   当前由 `services/agent_worker.py` 在 profile `runner` 的容器内调用，而该容器
   刻意不带 `GIT_IDENTITY_KEY`（§5.2 的诚实标注）。候选修法二选一：
   ① 在**受信侧**（backend / 独立的凭据服务）解密后经内部通道把凭据交给 worker，
   不让沙箱持有主密钥；② 为执行平面引入一枚**专用**封装密钥，并确认它不进入任务
   子进程（`services/sandbox_env.py` 的白名单）。在选定前，仓库专属凭据只在能提供
   该 cipher 的进程内可用——沙箱 runner 内的 repo 凭据解析会 fail-closed，这是
   **实现缺口**而不是文档笔误。

---

## 13. 与 `DESIGN-git-thin-layer.md` 的关系

两份设计**关注点不同，因此不冲突**：

| | 薄中间层（`DESIGN-git-thin-layer.md`） | 本设计（B1 逻辑 per-repo runner） |
|---|---|---|
| 管什么 | **身份账号体系**：谁是谁、谁能以谁的 git 身份读写 | **执行边界**：一个 agent 任务用哪份服务凭据、写哪里、占多少并发、出网声明 |
| 典型问题 | 「用户 push 时如何认证」「是否还要 openfish 铸 Forgejo token」 | 「仓库 X 的任务用哪枚 token、工作区在哪、并发上限多少」 |
| 承载者 | openfish 认证 + nginx 注入身份头 + Forgejo 信任代理 | `repo_runners` 一行 + 共享进程池的解析逻辑 |
| 变更对象 | 删除 `/git-credential`、`GitIdentity`、admin 铸票 | 新增 `repo_runners`、`AgentTask.runner_id`、领取/配额语义 |

衔接点只有一个：**at-rest 封装原语**。本设计当前用
`services.git_identity.TokenCipher`（Fernet，`GIT_IDENTITY_KEY`）封装**服务凭据**
（repo-scoped runner token），而薄中间层要删除的是**用户 token 的落库与铸造**。
两者都用到「加密存储」这一能力，但对象不同：

- 薄中间层落地后，`git_identities` 表与用户铸票逻辑消失，**用户**不再有密文；
- 执行平面仍然需要一枚**服务凭据**的密文封装，因此封装原语（或其后继）需要保留，
  只是不再服务用户身份。

因此正确的读法是：**薄中间层管身份、本设计管执行**；若两份设计同时实施，
唯一需要对齐的接口是「封装原语保留在哪里、密钥叫什么」，而不是「谁取代谁」。
在薄中间层真正合并前，本设计沿用现状（`TokenCipher` + `GIT_IDENTITY_KEY`），
不提前假设其删除。两者的共同底线一致：**不挂 docker socket、token 不进日志 /
argv / `.git/config`、默认 fail-closed**。
