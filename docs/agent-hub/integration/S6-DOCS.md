# S6-DOCS — 主智能体需要并入主文档的精确替换文本

> 本文件由 S6 切片实现者写。按 §11 的共享文件纪律，S6 **没有改**
> `docs/agent-hub/DEVELOPMENT.md` 与根 `README.md`；下面是可直接替换的精确文本。
> `backend/routes/repos.py` 是 S6 的交付物（已改），列在第 ③ 节仅供对齐与复核。
> 锚点都用内容而不是行号（行号会漂移）。

> ⚠ 现状更新：下方提到的 `backend/scripts/` 门禁目录已整体删除（连同 Makefile 的
> `make gates` / `contract-gate` 目标与 CI 的 backend job），因此本文里的门禁命令与
> 「新增门禁」清单已失效，仅作为当时的交付记录保留。校验套件现在按 `.agent/checks/`
> → policy `checks:` → manifest 自动发现解析。

---

## ① `docs/agent-hub/DEVELOPMENT.md` §13 开放问题第 1 条

**替换 §13 列表的第 1 条**（从 `1. **（最高优先，已确认未闭合）push 的凭据链。**`
到 `避免假装闭环。` 整段）为：

```markdown
1. **（已闭合，S6）push 的凭据链。** `git-receive-pack` 的 Basic 认证由 **Forgejo**
   承担，而旧 `/git-credential` 铸造的是 **openfish API key**；Forgejo 不认识它，
   因此 `clone`（匿名只读）成立、**`push` 必 401**。S6 采用**身份兑换**闭合：
   `GET /api/v1/repos/<slug>/git-credential` 保持 `repo:push` 作为第一道门，然后用平台
   的 `FORGEJO_ADMIN_TOKEN` 为用户懒创建一个 Forgejo 账号（用户名
   `of-<user_id>-<sha256(external_id)[:10]>`，**一经上线不可改**）并铸一张
   **Forgejo access token** 返回；token 用 Fernet（`GIT_IDENTITY_KEY`）加密落库、
   按用户隔离、过期轮换，撤销接到 `cli.py disable-user`。`kind=upstream` 返回
   `read:repository` 只读票（不再 409），`kind=workspace` 返回 `write:repository`。
   缺 `GIT_IDENTITY_KEY` 时该端点返回 **503**，不降级为明文。部署变量、admin token
   的最小 scope（`write:admin`）与等价 CLI 路径见
   `docs/agent-hub/integration/S6.md`。分支保护仍独立成立（I4），用户票不含任何
   admin scope。
```

其余第 2–9 条编号不变（保持 §13 既有序号）。

---

## ② 根 `README.md` 权限表中的 `repo:push` 行

**替换**包含 `| \`repo:push\` | \`GET /api/v1/repos/<slug>/git-credential\`` 的
**整行**（当前紧跟在 `repo:write` 行之后）为：

```markdown
| `repo:push` | `GET /api/v1/repos/<slug>/git-credential` — brokers a **Forgejo** credential for the git plane: openfish is the identity authority, but `git-receive-pack` is authenticated by Forgejo, so the endpoint mints a per-user Forgejo account plus a short-lived access token (`FORGEJO_ADMIN_TOKEN`, sealed with `GIT_IDENTITY_KEY`) instead of handing back an API key. `kind=upstream` mirrors receive a `read:repository` ticket, `workspace` a `write:repository` one; an unset key answers `503`. Branch protection is independent: an agent may only ever push `agent/*` (invariant I4) |
```

> 若权限表在同一 PR 中还有其它改动，按内容锚定替换，不要按行号。

---

## ③ `backend/routes/repos.py` 的端点说明

**这一条不需要主智能体再改**：S6 交付物 #3 已经改写了
`git_credential` 的 `@api_operation(summary=..., description=...)`，把旧的
“Known limitation — the push half is not closed yet” 段落换成了真实行为
（兑换机制、`read_only`、503 配置错误、S6.md 指针）。

为便于评审对齐，规范文本就是该函数上方的 decorator；要点：

```text
summary = "Mint a git credential for this repository"

description 的要点（逐条与实现对应）：
  1. 调用方认证不变：require_permission(REPO_PUSH)，平台凭据是第一道门；
  2. 平台把用户映射到专属 Forgejo 账号（git_identities，懒创建、幂等），
     登录名 of-<user_id>-<sha256(external_id)[:10]>；
  3. 用 FORGEJO_ADMIN_TOKEN 铸一张短期 Forgejo access token（未过期复用，
     过期先撤销旧的再铸新的）；
  4. 响应 {username, password} 可直接用于 HTTP Basic；
  5. kind=upstream 允许并返回 read:repository 只读票（read_only: true），
     kind=workspace 返回 write:repository；
  6. 缺 GIT_IDENTITY_KEY 返回 503，不回退明文；
  7. 分支保护独立成立：只允许推 agent/*（I4）。
```

同一函数的响应 schema `_GIT_CREDENTIAL_SCHEMA` 已补 `read_only` / `kind`
（`required` 现为 `slug` / `clone_url` / `username` / `password` / `read_only`），
`check_openapi.py` 已绿。

---

## ④ 可选：Forgejo 部署 README 的 scope 建议

若主智能体愿意顺手统一口径，`docker/forgejo/README.md` 第 4 节的
`--scopes all` 可加一句最小权限建议：

```markdown
> 平台 token 只需要 **`write:admin`**（建用户 / 替用户铸票 / 删用户都属于
> `/admin/*`）。`all` 也能工作但过宽，仅在没有细粒度 scope 的旧版本上使用。
> 最终用户拿到的 git 票由平台铸造，只带 `read:repository` / `write:repository`，
> **不含任何 admin scope**。详见 `docs/agent-hub/integration/S6.md` §②。
```
