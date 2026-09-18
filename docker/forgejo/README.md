# openfish · Forgejo（git 协议面）

决策 D1（`docs/agent-hub/DEVELOPMENT.md` §2.2）把**字节与协议**交给 Forgejo：
bare repo、`clone/push`、GitHub 原生 migration（带 issue / label / milestone / PR）、
webhook。openfish 只负责治理与智能，**不直连 git 对象做写操作**。

这个目录是 Forgejo 容器的部署说明与配置模板。它**不包含**可提交的密钥：
真实值放 `docker/forgejo/forgejo.env`（被 `.gitignore` 忽略，权限 `600`），
backend 容器通过 compose 注入同名变量。

```
docker/forgejo/
├── README.md                 ← 本文件
├── forgejo.env.example       ← 非机密默认值模板；cp 成 forgejo.env 后填密钥
└── forgejo/                  ← bind mount 的数据目录（prepare-mounts.sh 创建）
    ├── forgejo.db            ← SQLite（默认）
    ├── repositories/         ← bare repo 与 LFS
    ├── log/
    └── ...
```

> 数据目录由 `docker/prepare-mounts.sh` 创建（见 S1.md 的挂载片段）。
> `docker/forgejo/` 本身在仓库里只保留 `README.md`、`forgejo.env.example`，
> 运行态数据一律不进版本库。

---

## 1. 镜像 tag

| 选择 | tag | 说明 |
|---|---|---|
| **推荐（LTS）** | `codeberg.org/forgejo/forgejo:15` | v15.0 是当前 LTS，EOL 2027-07-15；内网私有化部署只跟 LTS |
| 次选（最新稳定） | `codeberg.org/forgejo/forgejo:16` | v16.0，新特性多但支持窗口只有约 3 个月（EOL 2026-10-29） |

规格 §2.2 写的是 `:9`，那是文档起草时的版本；**v9 已于 2025-01-16 EOL**，
继续用会拿不到安全补丁。镜像 tag 在 `docker/forgejo/forgejo.env` 的
`FORGEJO_IMAGE` 里指定一次，compose 片段引用 `${FORGEJO_IMAGE}`。

镜像来源：`codeberg.org/forgejo/forgejo`（官方 registry）。内网若无外网，
先把镜像 `docker save` 成 tar 放进 `docker/docker-images/`，用现有离线导入流程
`docker load` 后把 `FORGEJO_IMAGE` 指向本地 tag。

**升级前**：`docker exec -u git openfish-forgejo forgejo dump` 之外，`docker/forgejo/`
整目录做一次冷备即可（单机 SQLite + bare repo，没有外部状态）。

---

## 2. 不发布宿主端口

Forgejo **不映射任何 host port**：只 `expose: 3000`，与 backend 同在一个
compose 网络里。外部只能通过边缘 nginx 的两个前缀到达它：

| 前缀 | 用途 | 认证 |
|---|---|---|
| `/git/` | clone / fetch / push（HTTP smart protocol）、Forgejo Web UI | Forgejo 自己的账号 / token；push 另受分支保护约束 |
| `/forgejo-api/` | openfish 后端调用的 REST API | 仅内网；**不应对公网开放**（见下） |

`/forgejo-api/` 是给 backend 用的（migration 触发、进度轮询、issue 分页）。
backend 走的是 compose 内网 `http://forgejo:3000/api/v1/...`，**根本不需要经过
nginx**；这里的 location 是为了排障与将来「内网运维直连」的方便。若部署面向
更广的网络，把 `/forgejo-api/` 的 location 整段删掉即可——删除它不影响任何功能。

---

## 3. 配置模板

```bash
cp docker/forgejo/forgejo.env.example docker/forgejo/forgejo.env
chmod 600 docker/forgejo/forgejo.env
# 填三个应用级密钥
python -c "import secrets; print(secrets.token_urlsafe(48))"   # SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"   # INTERNAL_TOKEN
python -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET
python -c "import secrets; print(secrets.token_hex(32))"       # FORGEJO_WEBHOOK_SECRET
```

`ROOT_URL` 必须以 `/git/` 结尾，Forgejo 用它生成 clone URL 与 webhook 回调地址；
写成 `https://<host>:20416/git` 会导致 `/git//…` 与错误的 webhook 地址。

---

## 4. 生成 admin token（migration 与 API 都要它）

容器第一次起来会自动完成安装（`INSTALL_LOCK` 由环境变量推导）。然后：

```bash
# 1. 建一个平台专用的管理员账号（只给平台用，不要复用人工账号）
docker exec -u git openfish-forgejo \
  forgejo admin user create \
  --username openfish-admin --password "<强口令>" \
  --email openfish-admin@example.invalid --admin --must-change-password=false

# 2. 签发一枚 scoped token，交给 backend 用
docker exec -u git openfish-forgejo \
  forgejo admin user generate-access-token \
  --username openfish-admin --token-name openfish-platform --scopes all --raw
```

把第 2 步的输出同时写进两处：

* `docker/forgejo/forgejo.env` 的 `FORGEJO_ADMIN_TOKEN=…`
* `docker/.env` 的 `FORGEJO_ADMIN_TOKEN=…`（compose 注入 backend 容器）

token **只出现在这两份 600 权限的文件里**，不进仓库、不进日志、不进任何 API
响应。backend 侧只通过 `Authorization: token <…>` 头发送。

---

## 5. 配 webhook（push / issues / pull_request → openfish）

在 Forgejo 里给**平台仓库的 owner** 建一个组织级 webhook（或逐仓配置）：

| 字段 | 值 |
|---|---|
| Target URL | `https://<host>:20416/api/v1/repos/webhook` |
| HTTP Method | `POST` |
| Content Type | `application/json` |
| Secret | 与 `FORGEJO_WEBHOOK_SECRET` **完全一致** |
| Trigger On | Push、Issues、Pull Request |

openfish 校验 `X-Forgejo-Signature`（`HMAC-SHA256(secret, 原始请求体)` 的十六进制，
`hmac.compare_digest` 常数时间比较），不通过一律 `401`。

---

## 6. compose 片段

见 `docs/agent-hub/integration/S1.md` 第 ② 节——那里给出**可直接插入**
`docker/docker-compose.yml` 的精确 YAML（含 `nginx` 服务的 `depends_on` 增量）。

## 7. nginx 片段

同样在 `docs/agent-hub/integration/S1.md` 第 ② 节，给出插入
`docker/nginx/nginx.conf` 的精确 `location` 片段（`/git/` 与 `/forgejo-api/`，
以及 `upstream openfish_forgejo`）。

---

## 8. 验收（集成后由主智能体执行）

```bash
# 容器在跑、nginx 已重载
curl -fsS http://127.0.0.1:20416/git/ -o /dev/null && echo "web ui ok"
git clone http://127.0.0.1:20416/git/openfish/<repo>.git /tmp/probe && echo "G2 ok"
```

G2（`git clone http://<host>/git/<owner>/<repo>.git` 成功）是 §0.1 的硬指标；
`/git/` 没通就是这一段没接好，与 openfish 代码无关。

---

## 9. 分支保护（**红线**：agent 永不自动合并）

openfish 的代码层已经**拿不到**合并/批准能力（`services/agent_surface.py` 的
能力白名单 + `scripts/check_agent_checks.py` 的静态审计），但真正保证
「只有人能放行」的是 Forgejo 分支保护本身——即使 openfish 有 bug 也翻不过去。

对每个被 review 的仓库，在 Forgejo Web UI 配置（Settings → Branches →
`main`/默认分支）：

| 设置 | 值 | 为什么 |
| --- | --- | --- |
| Enable Branch Protection | ✅ | 默认分支只接受 PR |
| Require Pull Request | ✅ | 禁止直接 push |
| Required Approvals | ≥ 1 | 合并前必须有人批准 |
| Enable Merge Whitelist / Restrict merge | 仅人类账号（**不含 bot**） | agent 账号即使拿到 token 也不能点合并 |
| Block on Rejected Reviews | ✅ | 驳回后必须重审 |
| Dismiss stale approvals / Require approval after push | ✅ | agent 追加提交后旧批准失效 |
| Enable Status Check（可选） | `gates` | 与 `make gates` 同一入口 |

账号侧要求：

- Forgejo 的 **service/bot 账号不是 org owner**，无权改分支保护规则；
- bot 只被授予仓库 `write`（push `agent/*` + 开 PR），**不授予** `admin`；
- 人类 reviewer 是唯一的合并者：合并动作只发生在 Forgejo UI 上。

> **这一节是部署步骤，不是代码。** 离线 gate 能断言 openfish 没有合并 API
> 路径、代理白名单里没有合并方法；但「默认分支真的开了保护」只有在活的
> Forgejo 上验证——见 `docs/agent-hub/DESIGN-ai-checks.md` §红线 的诚实清单。
