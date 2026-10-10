# openfish · Docker 部署手册

这份文档只回答一件事：**compose 这层怎么配、怎么换库、怎么排障**。
拓扑图、快速开始、边缘 nginx 的路由表在根 [`README.md`](../README.md#quick-start-docker)，
每个环境变量的含义在 [`docker/.env.example`](.env.example) 的中文注释里——三份不重复。

---

## 1. 快速开始

```bash
cd docker
cp .env.example .env                       # 必填 SECRET_KEY，否则 compose 直接中止
docker compose up -d --build               # 缺失的 bind 源由 Docker 自动创建
docker compose --profile db up -d          # + PostgreSQL（见 §6）
docker compose --profile runner up -d      # + 智能体运行时（同一镜像的第二平面）
docker compose --profile debug up -d       # + 只起 bash 的调试容器（20417）
```

进入应用只有边缘 nginx 一个入口：`http://127.0.0.1:20416/`。

常用操作：

```bash
docker compose ps
docker compose logs -f backend
docker compose build backend               # backend 与 runner 是同一个镜像
docker compose build frontend
docker exec -it openfish-backend-debug bash
```

## 2. 环境变量契约

**`docker/.env` 是唯一入口**：compose 读它做变量插值，再把值写进容器的 `environment:`。
两个镜像都把 `.env`（及 `.env.*`，仅保留 `.env.example`）排除在构建上下文外，
容器里根本没有 `.env` 文件，所以**只改文件不改 compose 是不生效的**。

变量分三类：

| 类别 | 在哪 | 说明 |
| --- | --- | --- |
| 运维旋钮 | `docker/.env.example`（每个变量一条注释） | compose 转发给 backend / runner / frontend 构建；**写在这里就一定生效** |
| compose 写死的容器内取值 | `docker-compose.yml` 的 `x-backend-env` / `x-runner-env` | `HOST=0.0.0.0`、`PORT=8080`、`DEBUG=false`、`API_KEYS_FILE=/app/data/cpypiserver.db`、`TOOLS_DIR` 等目录、`MAX_CONTENT_LENGTH`… 完整清单见 `.env.example` 末尾 |
| 刻意不暴露 | 必须改 compose / nginx 才能动 | 见下 |

### 「不在 .env 里的旋钮」

- **`AUTH_ENABLED`** —— 本拓扑永远开着认证（框架默认 `true`）。关闭它意味着边缘 nginx
  后面的整个管理面匿名可写；要关请改 `docker-compose.yml`，并清楚这是安全边界的改动。
- **`ROUTE_PREFIX`** —— `docker/nginx/nginx.conf` 里的 `/api`、`/simple`、`/npm`、`/tools`
  等全是写死的 `location`，改了前缀服务立即不可达。真要改就得连 nginx 一起改。
- **`FRONTEND_DIST_DIR`** —— 只在 Flask 自己托管 SPA 时有意义；这里 SPA 由 `frontend`
  容器供应，`backend/routes/spa.py` 这条路径永远走不到。

还有一类不是"部署配置"而是**进程间消息**的变量，由代码自己设置、不该出现在 `.env` 里：
`OPENFISH_ROLE`（compose 设）、`OPENFISH_GIT_TOKEN` / `OPENFISH_GIT_HOST`、
`OPENFISH_MODEL_*`、`OPENFISH_TASK_KIND`、`GUARDDOG_TOP_PACKAGES_CACHE_LOCATION`。

### 前端构建参数

SPA 是构建期固化的，没有运行时环境变量；只有两个 build args（compose `frontend.build.args`）：

```bash
NPM_REGISTRY=https://registry.npmmirror.com   # 内网 npm 源；留空用默认
BUILD_ID=$(git describe --always)             # 侧边栏显示的构建标识；留空 = 构建时刻
docker compose build frontend
```

## 3. 密钥清单与平面隔离

写进 `docker/.env` 的密钥（都会转发，且都不进镜像、不进日志）：

| 变量 | 只给谁 | 作用 / 缺失时行为 |
| --- | --- | --- |
| `SECRET_KEY` | backend | 会话签名；compose 用 `${SECRET_KEY:?…}`，缺失直接中止启动 |
| `AUTH_ASSERT` | backend | Basic Auth 口令校验；留空 = 不校验口令 |
| `OAUTH2_CLIENT_SECRET` | backend | OAuth2 客户端密钥 |
| `GIT_IDENTITY_KEY` | **仅 backend** | 加密 `git_identities` 表的 Forgejo token；缺失 → `/git-credential` 503 |
| `RUNNER_CREDENTIAL_KEY` | backend + runner | 密封 `repo_runners.credential_ciphertext`；两侧须同值，缺失 → fail-closed |
| `MODEL_ROUTE_KEY` | **仅 backend** | 密封 `model_routes.api_key`；缺失 → 保存路由密钥被拒 |
| `FORGEJO_ADMIN_TOKEN` | **仅 backend** | 调 Forgejo `/admin/*` 建用户/铸票；不要给 runner |
| `FORGEJO_WEBHOOK_SECRET` | backend | 校验 webhook 签名；与 `docker/forgejo/forgejo.env` 里的同名值必须一致 |
| `FORGEJO_RUNNER_TOKEN` | **仅 runner** | 非 admin、可单独吊销的 clone/push + 开 PR 令牌；留空 → fix 任务在 push/PR 处明确失败 |
| `IMPORT_SOURCE_TOKEN` | backend | 从外部 GitHub/Gitee/GitLab 导入的 clone 凭据；留空 = 只导公开仓库 |
| `NPM_UPSTREAM_TOKEN`、`DOCKER_UPSTREAM_PASSWORD` | backend | 回源上游的鉴权 |

`x-runner-env` **刻意不继承** backend 的环境：runner 会执行被审仓库自带的
`check_*.py` 与 headless review 命令，所以 `SECRET_KEY` / `FORGEJO_ADMIN_TOKEN` /
`GIT_IDENTITY_KEY` 都不进 runner；`services/sandbox_env.py` 在子进程前再收窄一次。

**Forgejo 有两个 env 文件，别写错地方**：`docker/forgejo/forgejo.env`
（由 `env_file:` 只注入 forgejo 容器，compose 不从它插值）与 `docker/.env`
（平台凭据的来源）。把 `FORGEJO_ADMIN_TOKEN` 写进前者，backend 会拿到空值。

## 4. 存储布局

每个 bind 源都是 `docker/` 下的相对路径，换盘 = 重指同名符号链接，compose 一个字不改：

```bash
ln -sfn /srv/openfish-mirror/npm docker/npm
```

| 目录 | 内容 | 进数据库吗 |
| --- | --- | --- |
| `share/` `npm/` `node-builds/` `docker-images/` `debian/` | 制品字节 | npm / docker / debian 的 overlay 行在 `catalog_entries` 表里 |
| `tools/` `docs/` | 对象字节（`objects/<uuid4>`） | **条目在数据库里**，目录只是存放处与 import/export 的桥 |
| `data/`（= `backend/data`） | SQLite 库、回源缓存、模型路由表所在库 | 是 |
| `forgejo/` | Forgejo 的 `/data`（bare 仓库 + 实例配置） | 独立 |
| `agent-work/` | runner 的 `/work` 任务沙箱 | 否 |

因此：**换库会丢 `tools`/`docs` 的条目，但字节还在**——迁移时按 §6 的 import 步骤补回。

## 5. 配置自检

```bash
cd docker
docker compose config --quiet                                          # 语法 + 插值（SECRET_KEY 必须先填）
docker compose --profile db --profile debug --profile runner config --quiet
```

上面两条命令只做校验、不打印解析结果，避免把密钥刷到终端。运行期排障：

```bash
docker compose ps                    # healthcheck 状态
curl -fsS http://127.0.0.1:20416/health
docker compose logs --tail=100 backend forgejo
```

常见症状：
- `error while interpolating services.backend.environment.SECRET_KEY` → 忘了 `cp .env.example .env` 或没填。
- webhook 401 → `FORGEJO_WEBHOOK_SECRET` 与 forgejo 容器里的值不一致。
- `/api/v1/repos/<slug>/git-credential` 503 → `GIT_IDENTITY_KEY` 为空。
- 上传 `413` → 请求体超过边缘 nginx 的 `client_max_body_size`（100m，与
  `MAX_CONTENT_LENGTH` 配套）。
- 面板没有文档/工具 → 空库首启靠 `SEED_CATALOGS`（默认开）；手动补：

  ```bash
  docker compose exec backend python cli.py catalogs seed --force
  docker compose exec backend python cli.py docs import --from /app/docs
  ```

## 6. PostgreSQL：切换与数据迁移

`db` 服务在 `profile: db` 下，**只起容器不会改变后端行为**；真正的开关是 `DATABASE_URL`。

**第 0 步（还在 SQLite 上时）：把数据库里的目录条目导出到磁盘**，否则换库后就只剩字节：

```bash
docker compose exec backend python cli.py docs export   --to /app/data/migration/docs
docker compose exec backend python cli.py tools export  --to /app/data/migration/tools
for ns in npm docker-images debian; do
  docker compose exec backend python cli.py catalogs export --namespace "$ns" \
    --to "/app/data/migration/${ns}-catalog.json"
done
```

（`/app/data/migration/` 落在数据卷里，两个库都能读到。）

**第 1 步：** 在 `docker/.env` 里填口令并指向 db 服务：

```bash
POSTGRES_DB=openfish
POSTGRES_USER=openfish
POSTGRES_PASSWORD=<强口令>
DATABASE_URL=postgresql+psycopg://openfish:<强口令>@db:5432/openfish
```

**第 2 步：** 带健康检查依赖启动（`--profile db` 是必须的）：

```bash
docker compose --profile db -f docker-compose.yml -f docker-compose.postgres.yml up -d --build
```

首次连接会自动建表（`extensions/database.py` 的 `init_engine` → `create_all`，可重复执行）。
空库 + `SEED_CATALOGS` 默认开会重新写入随镜像发布的默认文档/工具。

**第 3 步：重建身份**（账号、角色、API key 都在库里，且 API key 只存哈希，导不出来）：

```bash
docker compose exec backend python cli.py create-admin <账号>
docker compose exec backend python cli.py list-roles                  # 看有哪些内置角色
docker compose exec backend python cli.py grant <账号> <角色>
docker compose exec backend python cli.py list-users
```

API key 需要**重新签发**（控制台 → API Keys）；用户在 Forgejo 的 git 凭据
（`git_identities` 表，用 `GIT_IDENTITY_KEY` 加密）也要重新绑定。模型路由表同样在新库里
是空的：用 `cli.py model-route list/set-key` 重建，或先套用
`backend/config/model_routes.seed.sql`（幂等，`ON CONFLICT (name) DO NOTHING`）。

**第 4 步：把第 0 步的目录条目导回去**：

```bash
docker compose exec backend python cli.py docs import   --from /app/data/migration/docs
docker compose exec backend python cli.py tools import  --from /app/data/migration/tools
docker compose exec backend python cli.py catalogs import --namespace npm \
  --from /app/data/migration/npm-catalog.json
# docker-images / debian 同理
```

**回滚：** 从 `docker/.env` 删掉 `DATABASE_URL`（保留 POSTGRES_* 无妨），
`docker compose up -d`。SQLite 文件一直在 `backend/data/cpypiserver.db`，没有被改动过。

**不迁移的东西（不要期待）：** 统计数据与历史、待办队列、agent 任务记录、
`repo_runners` 配置、以及文档的**修订历史**（`docs export` 只写当前版本）。
切换是"换一个库"，不是"搬一个库"；上面的 export/import 只覆盖目录条目，
身份类数据是重建。
