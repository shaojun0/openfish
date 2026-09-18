# openfish AI 能力调研 — 竞品能做什么，我们还可以做什么

> **性质**：调研 + 机会清单，不含实现承诺。
> **立场**：openfish 是**内网私有化**的「制品中心 + Agent Hub」，差异化不在「多写几个 review 评论」，
> 而在**治理闭环（finding 债务账本 / 误报规则治理 / 策略即文件）**与**制品中心邻接**
> （npm/pypi/docker/debian 自建镜像 + DSH 客户端）。
> **方法**：检索 2026 年在售/在用的 AI 研发工具，按能力归类，再逐条映射到 openfish 的
> 代码平面 / 上下文平面 / 执行平面 / 控制平面。
> **配套**：架构边界见 `DESIGN-git-thin-layer.md`；规格权威 `DEVELOPMENT.md`。

---

## 0. 一句话结论

竞品 2026 年的主战场已经从「**在 PR 下留评论**」转向三件事：

1. **代码库级上下文**（graph/索引 + 跨仓库影响），不是只看 diff；
2. **分诊与优先级**（PR 太多，人类审不过来，先排 P0）；
3. **自治闭环 + 验证**（agent 自己改、自己跑、自己验，人类只做终审）。

openfish 已经具备 2 和 3 的**治理底座**（finding 状态机、owner+due、误报规则治理、gates、
`agent/*` 分支约束、I6 注入边界），缺的是 1（语义/图上下文）和「分诊」的产品化。
**下一步的最优增量不是再造 reviewer，而是把已有的 finding/gates/制品中心变成上游输入。**

---

## 1. 竞品地图（按能力簇）

| 簇 | 代表产品 | 它们真正的能力 | 来源 |
|---|---|---|---|
| **AI PR review** | CodeRabbit、Greptile、Qodo、GitHub Copilot code review、Cursor Bugbot | 逐行建议、变更讲解、自定义规则、从团队评论学习、安全审查 | [CodeRabbit](https://www.coderabbit.ai/)、[Greptile](https://www.greptile.com/)、[Qodo](https://www.qodo.ai/features/qodo-git/)、[对比综述](https://futureagi.com/blog/best-ai-code-review-tools-2026/) |
| **PR 分诊 / 变更管理** | CodeRabbit Triage、Graphite、Trunk | 按严重度排 P0/P1、change stack 分解、merge queue、flaky 隔离 | [CodeRabbit Triage](https://www.coderabbit.ai/triage)、[Trunk](https://docs.trunk.io/) |
| **代码库上下文引擎** | Greptile、Qodo Context Engine、Sourcegraph Amp/Cody、Augment | 建代码图谱、跨仓库依赖、PR 历史感知、ticket 上下文 | [Greptile](https://www.greptile.com/)、[Qodo](https://www.qodo.ai/features/qodo-git/)、[repo-context 竞品分析](https://raw.githubusercontent.com/invariance-ai/gps/main/docs/competitive-landscape.md) |
| **自治 coding agent** | GitHub Copilot coding agent、Devin、OpenAI Codex、Google Jules、Cursor Cloud Agents、**OpenHands（OSS）** | issue → 计划 → 改多文件 → 跑测试 → 提 PR；沙箱执行 | [Copilot coding agent](https://github.com/agentpatterns-ai/website/blob/main/tools/copilot/coding-agent.md)、[OpenHands 评测](https://theaiagentindex.com/blog/openhands-review-2026)、[企业部署综述](https://northflank.com/blog/enterprise-ai-coding-agent-deployment) |
| **安全 / 供应链** | Semgrep（Code/Secrets/Supply Chain/**Guardian**/Agentic Workflows）、Snyk、ZeroPath、Corgea、DryRun | 规则+AI 混合检测、AI triage、自动修复 PR、面向 AI 生成代码的实时扫描 | [Semgrep](https://semgrep.dev/products/semgrep-code)、[Guardian](https://semgrep.dev/products/semgrep-guardian/)、[Agentic Workflows](https://semgrep.dev/products/semgrep-agentic-workflows/)、[能力矩阵](https://matrix.dryrun.security/index.html) |
| **测试 / 质量闭环** | Trunk（flaky 检测+隔离）、Qodo testgen、TestAgent | flaky 指纹与 quarantine、批处理+bisection、单测生成、覆盖率 | [Trunk](https://docs.trunk.io/)、[TestAgent 论文](https://dl.acm.org/doi/10.1145/3803437.3806428) |
| **Forgejo/Gitea 生态** | `opencode-review-gitea`、Gitea Actions 生态 | 在 Gitea Actions 里跑 OpenCode，抓 PR diff 并回帖结构化 review | [opencode-review-gitea](https://github.com/ccsert/opencode-review-gitea) |

**证据与边界**：AI review 的**误报率**是行业公认痛点（[Copilot 缺陷预测能力的实证评估](https://ieeexplore.ieee.org/document/11600404)），
且 AI 生成代码把 PR 量推到「50+/天」，人的 review 带宽成了瓶颈（[Trunk](https://docs.trunk.io/)）。
这解释了为什么 2026 年的产品都在做**分诊**和**自治验证**，而不是堆评论。

---

## 2. openfish 已经覆盖了什么（避免重复造）

| 竞品能力 | openfish 现状 | 差距 |
|---|---|---|
| 自动 review | `AgentRunner` 六步（clone→read→gates→review→search→emit） | 触发面窄（只默认分支 push），且**生产中 worker 尚未接线**（缺陷 A1/A2） |
| finding 去重 | `fingerprint` + `finding_events` 状态机 | 无跨仓库去重 |
| 规则治理 | `review-policy.yml` + 「同规则 30 天 ≥3 次误报 → 生成规则待修 finding」 | 规则需手写；无自然语言规则 |
| 只报不吵 | `level=blocking` 不能静默、`debt` 必须 owner+due | **比多数竞品更先进**，是核心卖点 |
| 沙箱执行 | runner 容器（无外网、无 socket、cap_drop、24h 回收） | 未接 worker |
| 上下文检索 | 关键词 ILIKE + 结构化过滤 + 字符预算 | **无语义检索、无代码图谱**（留了 `EMBEDDING_ROUTE` 钩子） |
| 注入防御 | I6：issue 正文是不可信数据、`<untrusted-issue>` 包裹 | 可作为对内合规卖点 |
| 制品中心 | npm/pypi/docker/debian/docs 自建镜像 + 离线中继 | 与 agent 尚未联动 |

---

## 3. 可新增的 AI 功能清单（按平面）

> 优先级：**P0** = 直接补当前断点/最高杠杆；**P1** = 差异化；**P2** = 锦上添花或有前置条件。
> 「落点」指 openfish 现有模块，避免新造第二套实现（AGENTS.md 硬约束 2）。

### 3.1 代码平面（review 与变更）

| # | 功能 | 竞品参考 | openfish 落点 | 优先级 |
|---|---|---|---|---|
| C1 | **PR 级审查**（不只 push）：审 diff、给行内建议、走 review 状态 | CodeRabbit / Greptile / Qodo | `repo_webhook.plan_actions` 加 `pull_request` 触发；`review` 任务已支持 | **P0** |
| C2 | **风险分诊**：每条 finding/每个 PR 打风险分与建议审查顺序 | [CodeRabbit Triage](https://www.coderabbit.ai/triage) | `findings` 已有 level/severity/seen_count/due，可直接算分并排序看板 | **P0** |
| C3 | **自定义规则自然语言化**：`.agent/review-policy.yml` 支持 `rule: "禁止在 service 层直接 requests.get"` | [Greptile custom rules](https://www.greptile.com/) | `services/review_policy.py` 已有 schema，扩一条 `nl_rules` 即可 | **P1** |
| C4 | **从人的决策中学习**：误报/不修/修复的决定反哺规则（自动降噪） | [Greptile learning](https://www.greptile.com/) | `finding_events` 已记录 actor/decision，规则误报率已可算 | **P1** |
| C5 | **需求对齐校验**：PR 是否真正实现了关联 issue 的要求 | [Qodo requirement validation](https://www.qodo.ai/features/qodo-git/) | 已镜像 issue + finding↔issue 关联；加一个 prompt 契约即可 | **P1** |
| C6 | **跨仓库影响分析**：改了共享包，列出受影响的仓库 | [Qodo cross-repo](https://www.qodo.ai/features/qodo-git/) | 制品中心知道谁发布了什么；`repos` 表已有全量仓库 | **P1** |
| C7 | **变更讲解 / walkthrough + PR 描述自动生成** | CodeRabbit / Qodo | `_pr_body()` 已存在，扩成「变更摘要 + gates 汇总 + 风险」 | **P1** |
| C8 | **测试影响分析**：这个改动该跑哪些测试 | [Trunk impacted targets](https://docs.trunk.io/) | `services/gates.py` 可加「按改动路径选 gate」 | **P2** |
| C9 | **大 PR 拆分建议 / change stack** | Graphite / Qodo | 新功能，需前端配合 | **P2** |

### 3.2 上下文平面（openfish 最该补的一块）

| # | 功能 | 竞品参考 | openfish 落点 | 优先级 |
|---|---|---|---|---|
| X1 | **语义检索**（embedding）替代/补充关键词 | Greptile / Qodo Context Engine / Sourcegraph | `repo_context.py` 已留 `EMBEDDING_ROUTE` 钩子，模型路由已有 `embedding` 别名 | **P0** |
| X2 | **代码图谱/依赖图**：文件-函数-依赖关系 | [Greptile graph index](https://www.greptile.com/) | 可从 `repo_commits` + 现有 AST/import 分析增量建 | **P1** |
| X3 | **跨仓库 finding 去重**：同一问题在多仓出现只报一次 | 各家的 codebase intelligence | `fingerprint` 去掉 repo 维度做全局视图 | **P1** |
| X4 | **决策记忆（ADR）**：从历史 commit/PR/issue 提取「当初为什么这么改」 | Greptile knowledge base | `repo_issues` + `repo_commits` 已在本地，做摘要索引 | **P1** |
| X5 | **新人问答**：对着代码库问「X 在哪、为什么」 | Sourcegraph / Greptile | 复用 X1 + 现有 `/context/search` 契约 | **P2** |

### 3.3 执行平面（自治闭环）

| # | 功能 | 竞品参考 | openfish 落点 | 优先级 |
|---|---|---|---|---|
| E1 | **把 worker 接上真实 runner**（当前是 placeholder，任务空转） | 所有 | `agent_queue.Worker(handler=AgentRunner)` + 注入 `findings.ingest` | **P0**（缺陷 A1/A2） |
| E2 | **SWE-agent 式迭代**：gates 失败→读日志→改→重跑，而非一次成 | [OpenHands](https://theaiagentindex.com/blog/openhands-review-2026) | `AgentRunner.run()` 加有界重试循环（现有 gates 结果可作反馈） | **P1** |
| E3 | **flaky 测试治理**：指纹识别 + quarantine + AI 修 | [Trunk](https://docs.trunk.io/) | gates 结果已在 `review_runs` 落库，可做跨 run 失败指纹 | **P1** |
| E4 | **依赖升级 / 漏洞修复 PR** | Dependabot + Semgrep autofix | 制品中心已有内网依赖镜像与版本信息 | **P1** |
| E5 | **单测生成**（用覆盖率是否上升做主 gate） | Qodo testgen / [TestAgent](https://dl.acm.org/doi/10.1145/3803437.3806428) | `run_gates` 后加覆盖率对比 | **P1** |
| E6 | **CI 失败分诊**：归类「你的代码 / flaky / 环境」并给修复建议 | Trunk / CodeRabbit | webhook 加 CI 事件；gates 输出已有结构 | **P2** |
| E7 | **文档 / 变更日志 / 发布说明生成** | Qodo / 各家 | `DOC_TASK_KIND`（merged PR → doc 回写）已有骨架 | **P2** |
| E8 | **PR 内交互问答**（@agent 提问） | Greptile / Copilot | 需要 Forgejo PR 评论回帖通道 | **P2** |

### 3.4 控制平面 / 治理（openfish 的护城河）

| # | 功能 | 竞品参考 | 说明 | 优先级 |
|---|---|---|---|---|
| G1 | **AI review 质量评测集**：自建内网 PR 基准，量化 precision/recall/误报率 | [Qodo benchmark](https://www.qodo.ai/ai-code-review-benchmark/)、[Greptile benchmarks](https://www.greptile.com/) | 用 `finding_events` 的人为判定做**规则级误报率**，这是竞品给不了的 | **P0** |
| G2 | **成本/配额/审计**：每任务 token 与花费上限、模型路由 | 企业版普遍有 | `model_routes` 已有；补任务级预算与超限策略 | **P1** |
| G3 | **渐进放权**：autofix 白名单从「只报告」到「自动修」的分级 | §6.4 已有边界 | 用 G1 的误报率数据决定某规则能否升级为 autofix | **P1** |
| G4 | **内网供应链扫描**：Semgrep CE 规则 + AI triage + autofix | [Semgrep](https://semgrep.dev/products/semgrep-code)、[Guardian](https://semgrep.dev/products/semgrep-guardian/) | 完全可离线；产物可进finding 账本 | **P1** |
| G5 | **Agent 可观测**：finding→task→run→模型→成本全链路 | I5 已有追溯要求 | 看板加「这条 finding 花了多少」 | **P2** |

---

## 4. 不建议做 / 反模式

| 反模式 | 为什么 |
|---|---|
| **追求评论数量** | 误报是行业第一痛点（[实证](https://ieeexplore.ieee.org/document/11600404)）；openfish 的「只报不吵 + owner+due」正是解药，别退回去 |
| **无审批的全自动 merge** | `blocking` 不可静默是设计边界（I3）；自治只到「开 PR」为止 |
| **把 issue 正文当指令** | I6 已有明确边界；引入更多外部文本源时要保持包裹与不执行 |
| **每仓库一个自研 reviewer** | 违反 AGENTS.md 硬约束 2；应共用 `AgentRunner` + policy |
| **为大而全引入重依赖**（k8s/Celery/新 agent 框架） | 与 §3.1 依赖政策冲突；OpenHands 这类可作**参考实现**而非嵌入依赖 |
| **用 AI 替代 gates** | gates 是确定性契约，AI 是概率判断，两者互补不可互换 |

---

## 5. 建议的下一步（三个最小切片）

1. **补断点（P0）**：接 `AgentRunner` 进 worker + `findings.ingest`（E1），并把
   `review` 从「仅默认分支 push」扩到 PR（C1）。没有这一步，下面全是空中楼阁。
2. **上下文升级（P0）**：接 `EMBEDDING_ROUTE` 做语义检索（X1）+ 风险分诊（C2）。
   这是投入产出比最高的一步：直接降低重复 finding、提升 review 命中率。
3. **治理度量（P0）**：用 `finding_events` 建规则级误报率看板（G1），
   再把数据接进 C4（自动降噪）与 G3（渐进放权）。这一步把 openfish 和
   「又一个 AI reviewer」彻底区分开。

> 若要更进一步，**跨仓库影响分析（C6）+ 依赖升级 PR（E4）** 是 openfish 独有的组合拳——
> 竞品要么没有内网制品中心，要么没有 air-gap 部署形态。

---

## 6. 参考来源

- CodeRabbit — <https://www.coderabbit.ai/>、Triage <https://www.coderabbit.ai/triage>
- Greptile — <https://www.greptile.com/>
- Qodo / PR-Agent — <https://www.qodo.ai/features/qodo-git/>、benchmark <https://www.qodo.ai/ai-code-review-benchmark/>
- Trunk（flaky + merge queue）— <https://docs.trunk.io/>
- Semgrep — <https://semgrep.dev/products/semgrep-code>、Guardian <https://semgrep.dev/products/semgrep-guardian/>、Agentic Workflows <https://semgrep.dev/products/semgrep-agentic-workflows/>
- OpenHands 评测（OSS 自治 agent）— <https://theaiagentindex.com/blog/openhands-review-2026>
- Forgejo/Gitea AI review 开源实现 — <https://github.com/ccsert/opencode-review-gitea>
- AI code review 工具综述 — <https://futureagi.com/blog/best-ai-code-review-tools-2026/>
- Copilot 缺陷预测实证 — <https://ieeexplore.ieee.org/document/11600404>
- TestAgent（多智能体单测生成）— <https://dl.acm.org/doi/10.1145/3803437.3806428>
- Repo-context 竞品分析 — <https://raw.githubusercontent.com/invariance-ai/gps/main/docs/competitive-landscape.md>
- 企业 AI agent 部署 — <https://northflank.com/blog/enterprise-ai-coding-agent-deployment>
- DryRun Security 能力矩阵 — <https://matrix.dryrun.security/index.html>
