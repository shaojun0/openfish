# DESIGN · AI 维护的校验套件（AI-maintained checks）

> 版本 v1 · 面向 openfish 仓库内实现。权威规格仍是
> `docs/agent-hub/DEVELOPMENT.md`；本文只补充「让 AI 自己写并维护校验」这一
> 能力的**边界、治理与实现落点**。与本文冲突时以 §3.1 依赖政策为准。
>
> **不修改** `DESIGN-git-thin-layer.md` 与 `RESEARCH-ai-capabilities.md`。

## 0. 一句话

普通用户不必自己写检查：AI 在仓库里**写一套校验套件**（`.agent/checks/`），
随代码演进；但**同一 run 绝不能既产出修复、又削弱判它的检查**。裁决权始终在
人的合并动作上，agent 没有合并/批准能力。

---

## 1. 硬不变量（本文档的核心）

> **A check suite that gates a fix must be read-only with respect to that fix.
> 同一个 agent run 绝不能既产出修复、又削弱判它的检查。**

它由三条**代码机制**（不是文档约定）落实：

| 机制 | 落点 | 断言内容 |
| --- | --- | --- |
| **冻结**：fix 从 base/approved 快照解析套件，事后重跑用同一份 `FrozenSuite`，绝不从改过的 working tree 重新解析 | `services/check_suite.py:freeze_suite`、`services/agent_runner.py`（`frozen_user` / `frozen_ai`） | 事后重跑用的是同一份冻结套件 |
| **路径守卫**：fix 不得写 `.agent/checks/**`、用户测试路径、`.agent/review-policy.yml`；curator 只许写 `.agent/checks/**`（+ 测试文件）。守卫在 `commit()` **之前**跑，违规直接 `failed`，不产生分支/PR | `services/check_suite.py:fix_guard_violations` / `curator_guard_violations`、`services/agent_runner.py:_guard_task_paths` | 改套件 → failed 且无 commit/push/PR |
| **未验证 ≠ 通过**：无可解析套件 → `unverified`；`unvalidated` 检查只报告不门控。`on_green` 对 `unverified` 一律不 push、不开 PR | `services/gates.py`（三态 `STATUS_*`）、`services/check_trust.py:assess_assurance` | 无套件 → `unverified`，不 push、不开 PR |

这三条原本由 `backend/scripts/check_agent_checks.py` 离线断言；该目录已整体删除，
类似断言现在只能由仓库的校验套件提供（`.agent/checks/` → `.agent/review-policy.yml`
的 `checks:` → manifest 自动发现），解析不到就是 `unverified`。

### 1.1 历史缺陷（P1.0）

`SubprocessRunnerAdapter` 旧实现：checkout 里没有可解析的离线门禁脚本时
`_checkout_scripts()` 返回 `None`，`run_gates(scripts_dir=None)` 回退到
**runner 镜像内置的**门禁目录，并以 `cwd=checkout` 运行 openfish 的门禁 →
外来仓库假绿 → `pr_policy=on_green` 推分支、开 PR，实际什么都没验证。
（那个镜像内置的门禁目录**已被整体删除**，这条假绿路径连素材都不剩。）

修复：`SubprocessRunnerAdapter.run_gates()` 只在 checkout（或显式 operator
覆盖）里解析套件；解析不到就是 `unverified`。**永远不会**回退到
`services.gates.REPO_ROOT`。gate 断言：构建一个只有 README 的临时仓库，
`run_gates` 必须返回 `state == "unverified"`，且结果里没有镜像自带的 gate。

---

## 2. 两套解耦的校验套件（治理模型）

**没有「每次改动都要人批准」的流程。** 取而代之的是两套**互相独立**的套件。

| | 用户套件（repository / user suite） | AI 套件（AI-maintained suite） |
| --- | --- | --- |
| 位置 | 仓库自带测试/CI；或人在 `.agent/review-policy.yml` 的 `checks:` 里声明 | `.agent/checks/**`（`checks.yml` 或 `check_*.py`） |
| 谁维护 | 用户；**AI 永不修改** | AI curator（`checks` 任务），无需人工批准 |
| 地位 | **权威**：它决定 `on_green` | **advisory + ratchet**：补充信号，绝不覆盖用户失败 |
| 改动守卫 | fix 不得写用户测试路径与 policy 文件 | fix 不得写 `.agent/checks/**`；curator 只写这里 |

实现：`services/check_suite.py:resolve_suites()` 独立解析两套，返回
`SuitePair(user, ai)`；runner 分别执行（`adapter.run_gates` 跑用户套件、
`adapter.run_ai_gates` 跑 AI 套件），结果**分两块**进入 payload 与 PR 正文，
**不合并成一份匿名绿名单**（`services/check_trust.py:render_two_suites`）。

### 2.1 Ratchet（只紧不松）

curator 可以自由**增改** AI 套件；但**放松/删除/退休一个 active check 必须在
另一个独立的 curator run 里完成**，且留下可见 diff 与 provenance
（author=ai, model, task_id, base_sha, suite hash, reason）。同一 run 里
「一边受益一边放松」被 §1 的路径守卫直接判失败。

> **当前强制程度（诚实说明）**：`.agent/checks/**` 的 fix 写入被机械拦截；
> 「放松 active check 必须是独立 run」目前靠 **两个独立 PR + provenance 记录**
> 实现（`SuiteProvenance`、`check_runs.weakened`），没有独立的 diff 语义分析器。
> 见 §10 未实现清单。

---

## 3. 解析顺序

`services/check_suite.py:resolve_suite()`（单套件，Phase 1 契约）与
`resolve_suites()`（两套解耦，治理路径）共用同一批 provider：

**AI 套件**（`resolve_ai_suite`）：

1. `.agent/checks/checks.yml`（manifest；`validated` 默认 **false**）
2. 否则 `.agent/checks/check_*.py`（每个文件一个 check，默认 unvalidated）

**用户套件**（`resolve_user_suite`）：

1. `.agent/review-policy.yml` 的 `checks:`（人写的命令，`validated`）
2. manifest 零配置发现（`services/check_discovery.py`，**纯读、不执行**）：
   - `package.json` scripts → `npm run {test,lint,build,typecheck}`
   - `pyproject.toml`/`pytest.ini`/`tests/` → `pytest`；`[tool.ruff]`→`ruff`；`[tool.mypy]`→`mypy`
   - `go.mod` → `go build ./...` / `go vet ./...` / `go test ./...`（三条独立 check）
   - `Cargo.toml` → `cargo check` / `cargo test`
   - `Makefile` → 定义了才跑 `test`/`check`/`lint`
3. 都没有 → 空套件 → `unverified`

单套件 `resolve_suite()` 保持 Phase 1 顺序：`.agent/checks/` → policy `checks:`
→ 自动发现 → `unverified`（先 AI 后用户，因为 Phase 1 只有一套）。

manifest 示例（`.agent/checks/checks.yml`）：

```yaml
version: 1
checks:
  - id: unit-tests
    command: pytest -q
    cwd: .
    timeout: 300
    validated: false      # 只有通过证伪验证（或人显式声明）才可门控
```

命令进 `argv`（`str.split()` / `shlex.split()` 语义），**从不**交给 shell，
不存在注入面。

### 3.1 冻结与 provenance

`freeze_suite()` 对套件求 `suite_fingerprint()`（HTTP-free 的稳定 SHA-256：
顺序、argv、cwd、timeout、validation；不含相对 checkout 的路径与 `reason`）。
结果文档 `run.suite_hash` / `run.ai_suite_hash` 记录它；DB 侧把
snapshot + hash + provenance + validation + run history 记为**索引/缓存**
（§9.3），仓库树仍是 source of truth。

---

## 4. 责任矩阵（每行都有机械 guard，或明确标注无）

| 主体 | 可以 | 不可以 | 「不可以」的机械 guard |
| --- | --- | --- | --- |
| **AI / `fix`** | 改业务代码、commit、push `agent/*`、开 PR | 改 `.agent/checks/**`、改用户测试、改 `.agent/review-policy.yml`、合并、批准、改分支保护 | `fix_guard_violations()`（commit 前）→ `AgentRunnerError`；`RestrictedForgejoClient` 白名单；`assert_pushable()`（I4）；`scan_forbidden_api()` 静态审计 |
| **AI / `checks` 策展人** | 增改 AI 套件、开**独立** PR | 改用户套件、在「本次 run 会从中受益」时放松 check、合并 | `curator_guard_violations()`（commit 前）；`RestrictedForgejoClient`；§2.1 ratchet 的独立 PR + provenance |
| **用户** | 改一切（含两套 check）、批准、合并 | — | 合并是唯一放行手段（§5） |
| **openfish 平台** | 跑两套 check、记录 provenance、标注保证等级、开 PR、调度 | 调用任何合并/批准 API、代替用户决策 | `MAY_AUTO_MERGE = False`；`scan_forbidden_api()` 静态审计；客户端能力白名单 |

**无法机械强制的部分**（诚实标注）：

- 「策展人不得为本次 run 的利益放松 check」是一个**语义**判断。代码只能保证
  「放松发生在一个独立 run、有独立 PR、有 provenance」（§2.1），不能证明动机。
- 「用户的合并动作」发生在 Forgejo UI，openfish 无法也**不应**代替；平台侧只能
  保证自己不发合并请求（§5）。

---

## 5. 红线：永不自动合并

四层，纵深防御：

1. **能力删除（代码）**：注入 agent 路径的 Forgejo 客户端被
   `RestrictedForgejoClient` 包住，白名单只有
   `create_pull_request`（+ 预留的评论方法），任何 merge/approve/protection
   调用抛 `AgentSurfaceError`。
2. **静态审计（离线自检）**：`scan_forbidden_api()` 用 `ast` 扫
   `services/agent_runner.py`、`services/agent_worker.py`、
   `services/repo_import.py`，字符串常量里的 API 路径与标识符里的方法名都查；
   自检断言结果为空，并断言真实 `ForgejoClient` 不含任何
   危险方法。
3. **Forgejo 侧（真正的红线）**：默认分支开启分支保护，要求人工批准、禁止
   直接 push；bot 账号不是 org owner、不授予 admin。配置步骤见
   `docker/forgejo/README.md` §9。即使 openfish 代码有 bug，也翻不过去。
4. **I4 + 诚实标注**：`assert_pushable()` 只放行 `agent/*`；PR 正文固定带
   `HUMAN_REVIEW_NOTICE`：「不会自动合并。本 PR 由 agent 产出，必须经人工 review
   后由人合并」。

`MAY_AUTO_MERGE` 是一个恒为 `False` 的模块常量，`Assurance.may_auto_merge`
硬编码引用它——没有任何配置、policy 或测试能从这里打开自动合并。

---

## 6. 证伪性标准（P2.2）

一个 check 只有在

1. **当前修订上通过**，且
2. **至少一个已知坏修订上失败**

之后才算 `validated`，才可门控。`unvalidated` 检查照常运行、照常报告，但**永不
门控**（`services/gates.py:run_suite`：只有 `validated` 且失败才算 `failed`；
全是 unvalidated → 整个套件 `unverified`）。

已知坏修订来自 `services/check_validation.py` 的**可插拔变异播种**：

- python：翻转比较（`>`→`<=`）、`if cond:`→`if False:`、删 `return`；
- javascript/typescript：翻转 `===`/`!==`、守卫永假；
- go：翻转比较。

播种只发生在 `shutil.copytree` 出来的临时副本（`COPY_IGNORE` 跳过 `.git` /
`node_modules` / `.venv`），**绝不碰真实 checkout**；executor / seeders / copier
全部可注入，因此完全离线可测。

**没有可播种的变异 → 该 check 保持 `unvalidated`，不得门控**（安全方向）。

### 6.1 运行历史

`CheckRunRecord`（check_id / state / suite_hash / commit_sha / exit_code /
duration_ms / at / validation）→ `summarize_history()` 得到
`runs` / `failures` / `flake_rate` / `never_failed`。两个用途：

- L3 晋升判据（§7）；
- 治理 finding：`checks.never-failed`（从未失败过的检查无法证明能发现回归）。

持久化：生产用 **DB 表** `check_runs`（`services/check_store.DbCheckHistoryStore`）；
`services/check_validation.CheckHistoryStore` 只是离线/测试用的内存替身，JSONL
store 已**移除**（多副本共享一个文件既不能并发写也不能查询）。见 §9「持久化」。

---

## 7. 信任阶梯（机械获得信任，取代人工批准）

`services/check_trust.py:assess_assurance()`：

| 等级 | 条件 | 允许 |
| --- | --- | --- |
| **L0** | 没有可门控的校验（两套都 `unverified`） | 只报告，不 push、不开 PR |
| **L1** | 有 `validated` 的 AI 检查且通过 | 开**带标签**的 PR（`ai-checks/L1`），绝不自动合并 |
| **L2** | 用户套件存在 | 以用户套件裁决；AI 套件只作补充信号（`ai-checks/L2`） |
| **L3** | L1 + 满足客观晋升标准（**策略显式开启，默认关闭**） | 该 AI 检查也获得门控权（`ai-checks/L3`），**仍不自动合并** |

关键性质（gate 断言）：

- 用户套件失败 + AI 全绿 → **不能**开 PR（AI 不能覆盖用户失败）；
- 用户套件通过 + AI 失败 → 以用户套件为准（AI 不能把绿的翻红，也不能被掩盖）；
- L3 **默认关闭**：`DEFAULT_L3_CRITERIA.enabled is False`，即使
  `l3_eligible=True` 也只到 L1；
- **任何等级 `may_auto_merge` 恒为 False**。

### 7.1 L3 判据（默认关闭；**只授门控与标签，永不授合并**）

`L3Criteria(enabled=False, min_detection_rate=1.0, min_runs=20,
max_flake_rate=0.0, require_never_weakened=True)`，由
`check_l3_eligible(validations, history, criteria)` 判定，全部是**已记录历史**
的客观属性，可审计：

- 每个候选 AI 检查都 `validated`；
- `detection_rate ≥ min_detection_rate`（变异检出率）；
- 运行次数 `≥ min_runs`；
- `flake_rate ≤ max_flake_rate`；
- `require_never_weakened`：历史里没有任何 `weakened=True` 的运行记录
  （弱化行为必须显式记进 run history，才可能被这条判据拦下）。

**本次实现状态**：判据、policy 开关与「默认关闭」的断言已落地；L3 **未启用**，
也没有任何部署默认打开它。开启方式是 policy（后续：`defaults.ai_checks.promote`）。

---

## 8. 报告与治理 finding

### 8.1 分两块报告

`Result.gates` = **用户/仓库套件**；`Result.ai_gates` = **AI 套件**；
`RunMeta` 分别记录 `suite_*` 与 `ai_suite_*`（hash / source / state）以及
`assurance_level` / `assurance_label`。PR 正文由
`render_two_suites()` 生成两块带标题的表格 + 保证等级 + 「不会自动合并」声明。

### 8.2 治理 finding（进现有 ledger）

`services/check_trust.py:governance_findings()` 产出 §9.5 形状的 finding
（`level=debt`, `autofix=False`）：

| rule_id | 触发 |
| --- | --- |
| `checks.no-automated-verification` | 两套都为空 |
| `checks.never-failed` | 某个 `validated` 检查历史 0 失败 |
| `checks.ai-suite-diverges` | AI 套件与用户套件覆盖不一致 |

---

## 9. curator 闭环（A–D，已实现）

### 9.1 A —— `checks` 任务类型可入队 + 旧库安全迁移

- `models/agent_hub.py:TASK_KIND` 现在含 `checks`；`agent_tasks.kind` 的
  `ck_agent_tasks_kind` CHECK 随之扩大。
- `models/agent_hub_migrate.py:evolve_check_constraints()` 是**新的第 4 步**，
  在 `ensure_schema` 里自动运行：
  - 先 introspection（`Inspector.get_check_constraints`）判断 live 约束是否已经
    接受全部必需值；是则**什么都不做**（第二次 `ensure_schema` 完全 no-op）；
  - **SQLite**：文档化的表重建 —— `PRAGMA foreign_keys=OFF`（事务外）→ 建
    `agent_tasks__openfish_new`（DDL 由模型编译而来，含新 CHECK）→ 复制全部
    列/行 → `DROP TABLE agent_tasks` → `RENAME` → 重建每个索引 → `COMMIT` →
    `PRAGMA foreign_keys=ON` → `PRAGMA foreign_key_check`。因为从不把
    `agent_tasks` 改名为别的名字，`review_runs.agent_task_id` 的外键始终指向
    `agent_tasks`；`id`、所有列、所有行原样保留。
  - **PostgreSQL**：按 introspection 到的真实约束名做 `DROP CONSTRAINT IF EXISTS`
    + `ADD CONSTRAINT ck_agent_tasks_kind CHECK (...)`，存在性检查过。
- 报告新增 `updated_constraints` 键，日志写明拓宽了哪个约束。

### 9.2 B —— curator 触发（`defaults.curator`）

- policy 新字段（pydantic）：`defaults.curator: off | bootstrap | auto = bootstrap`
  与 `defaults.curator_min_interval_seconds: int = 3600`。
- `routes/repo_webhook.py:plan_actions()`（纯函数）在默认分支 push 上，当
  `curator in (bootstrap, auto)` 且 `curator_allowed` 时追加一个 `checks`
  `QueuedAction`（priority 4，低于 review）。它与 `auto_review` **相互独立**：
  仓库可以关掉自动 review、但仍然 bootstrap 它的校验套件。
- **去重 + 限流**（`services/check_store.curator_should_enqueue`）：同一 repo 已有
  `queued|leased|running` 的 `checks` 任务 → 拒绝第二个；**`bootstrap` 是一次性**
  的——该仓库已有 AI 快照（`check_suite_snapshots.kind='ai'`）就不再提案；
  `auto` 跳过这条、只受冷却窗口（默认 3600s）约束。按 **suite hash** 的去重在
  runner 侧由 `proposal_in_flight_for_hash()` 提供（hash 只有 clone 之后才知道）。
  门禁读取失败时**fail-closed**（拒绝入队，绝不 spam PR）。
- 路由把 DB 判定结果作为 `curator_allowed` 传进纯函数，因此 gate 可以离线驱动
  全部分支（off / bootstrap / auto / 被拒 / 非默认分支）。

### 9.3 C —— DB 持久化（树是真相，DB 是索引/缓存）

三张新表（`models/agent_hub.py`，由 `create_missing_tables` 建立）：

| 表 | 内容 |
| --- | --- |
| `check_suite_snapshots` | 冻结套件：`repo_id, kind(user\|ai), source, suite_hash, checks(JSON), base_sha, author(human\|ai), model, task_id, status(proposed\|active\|retired), created_at/updated_at`；`(repo_id, kind, suite_hash)` 唯一 |
| `check_validations` | 证伪裁决：`repo_id, check_id, base_sha, detection_rate, faults_seeded, validated, status, reason, created_at` |
| `check_runs` | 运行历史：`repo_id, check_id, suite_hash, revision, state(passed\|failed\|unverified\|error), exit_code, duration_ms, weakened, created_at` |

**仓库树始终是 source of truth**：这些行都是它的派生物，删库重 clone 就能重建；
runner 仍然从 checkout/ `.agent/checks/**` 解析套件，DB 只被 *记录*，不被用来
决定「有哪些检查」。`DbCheckHistoryStore` 是生产实现（每次调用自开 session，
多副本安全）；内存 `CheckHistoryStore` 只服务离线 gate；**JSONL store 已删除**。

### 9.4 D —— curator handler + 提案 PR

- `models.agent_hub.TASK_KIND` 里的 `checks` 现在真的可 enqueue；
  `services/agent_worker.py:SUPPORTED_KINDS` 含 `checks`，handler 用与 review
  相同的 `AGENT_REVIEW_COMMAND` 让模型在 `.agent/checks/**` 写提案；命令未配置
  时 `build_review_fn` 直接抛错，任务 `failed`（绝不静默退休）。
- 模型写完文件后、**commit 之前**，runner 调
  `services/check_curator.curate_workspace()`：对每个提案 check 跑 §6 的证伪验证器，
  然后 `apply_validations()` 把 `checks.yml` 的 `validated` **按证据**改写——
  模型自称的 `validated: true` 会被无证据地改回 `false`（gate 断言）。
- 只有 proven 的 check 才会在树里 `validated: true`，下一个 fix 解析时才可能门控；
  本次 curator run 用的是 **frozen base 套件**，提案无法给自己放行。
- 路径守卫：curator 只能写 `.agent/checks/**` + 测试文件（`curator_guard_violations`）。
- PR 打标：标题与正文都带 `[openfish-checks-proposal]`，正文列每个 check 的证伪
  结果、manifest 路径、套件指纹，并写明「需人工 review、不会自动合并」，head 是
  `agent/checks-<task_id>`，base 是默认分支。
- **provenance 落库**：handler 在 run 后把提案快照（author=ai, model, task_id,
  base_sha, status=proposed）、每条 validation、以及用户/AI 两套的 run history
  写进 §9.3 的表；写入是 best-effort（它是缓存，失败不能让成功的 run 变 failed）。

---

## 10. 未实现 / 需要活环境（诚实清单）

- **Forgejo 分支保护是部署步骤**：`docker/forgejo/README.md` §9 给了精确设置，
  但「默认分支真的开了保护」只能在活的 Forgejo 上验收；离线 gate 只能证明
  openfish 侧没有合并能力（§5）。
- **L3 未启用**（判据与默认关闭断言已实现）；开启需要 policy，且只授门控/标签，
  永不授合并。
- **`auto` 模式的「新源码没有对应 check」判定尚未实现**：`auto` 目前与
  `bootstrap` 的差别是**不一次性**（每次默认分支 push 都可能提案，只受冷却窗口
  约束），但还没有做「本次 push 新增的源文件是否缺少对应 check」的 diff 覆盖分析。
  真正的覆盖判定需要在 runner 内比对 diff 与套件覆盖，是下一步。
- **ratchet 的语义分析**（判断某次改动是不是「放松」）未实现：目前靠
  `.agent/checks/**` 的 fix 写入被机械拦截 + 独立提案 PR + `weakened` 历史标记 +
  人工 review。`weakened=True` 目前没有自动写入者（需人工/后续工具标），因此
  `require_never_weakened` 现在只在有记录时才生效。
- **curator 提案不按 `on_green` 门控**（决策 2 的 carve-out）：新 check 在
  validated 之前本来就不能门控，因此提案在 `on_green` 下仍会开 PR——但**仍然
  打标、仍然只能人工合并**。
- **DB 是缓存**：`check_suite_snapshots.status` 的 `active` 目前没有自动写入者
  （快照以 `proposed` 记录）；「哪个快照是 active」仍以 runner 从 base 快照解析
  为准，DB 只是 provenance。

---

## 11. 验收（离线）

后端**不再有**仓库自带门禁（`backend/scripts/` 已整体删除）：校验套件按
`.agent/checks/` → `.agent/review-policy.yml` 的 `checks:` → manifest 自动发现解析，
由 `services/gates.py` 的 `run_suite()` 执行，解析不到即 `unverified`。

```bash
make gates-frontend     # 人 / agent / CI 的同一入口（只剩前端 smoke）
```

本设计的验收面（原 `check_agent_checks.py` 的离线断言，该脚本已随门禁目录整体
删除，清单保留作为回归清单）：假绿修复（外来仓库 → `unverified`）、三态、
解析顺序与纯发现、冻结 + 路径守卫、`OPENFISH_TASK_KIND`、`checks:` schema、
**agent 无合并能力**、信任阶梯 L0–L3（含默认关闭）、两套解耦与治理 finding、
证伪验证器与运行历史、**A 旧库 CHECK 迁移（数据保留 + 幂等）**、
**B curator 触发/去重/限流**、**C DB 快照/验证/历史**、
**D curator 闭环**（假模型命令 + 假 Forgejo + 真本地 git：enqueue → clone →
模型写提案 → 证伪验证改写 validated → 守卫 → commit → push `agent/checks-*` →
带标记的提案 PR → provenance 落库）。

无法离线验证、需要活环境的：真实 Forgejo（clone/push/PR/分支保护）、真实模型
endpoint（`AGENT_REVIEW_COMMAND`）、真实 PostgreSQL（迁移的 PG 分支只做了
DDL/存在性检查，未在真 PG 上跑）、真实 docker runner。
