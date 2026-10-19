# openfish · Docker 部署手册

这份文档只回答一件事：**compose 这层怎么配、怎么换库、怎么排障**。
拓扑图、快速开始、边缘 nginx 的路由表在根 [`README.md`](../README.md#quick-start-docker)，
每个环境变量的含义在 [`docker/.env.example`](.env.example) 的中文注释里——三份不重复。

---

## 1. 快速开始

```bash
cd docker
mkdir -p /data/share/packages /data/share/python-build-standalone   # compose 里的两个绝对挂载源
./bootstrap-env.sh                         # 生成 .env + forgejo.env 的全部密钥（幂等，可重跑）
docker compose up -d --build               # docker/ 下缺失的 bind 源由 Docker 自动创建
docker compose --profile db up -d          # + PostgreSQL（见 §6）
docker compose --profile runner up -d      # + 智能体运行时（同一镜像的第二平面）
docker compose --profile debug up -d       # + 只起 bash 的调试容器（20417）
```

`bootstrap-env.sh` 只**补空值**，已有的 `.env` 永不覆盖、也不重置（没有 `--force`；要重来
就自己删掉文件）。`--dry-run` 只打印将要做什么，`--host <域名或 IP>` 指定对外地址
（默认取本机出口 IP，`OPENFISH_PUBLIC_HOST` 也可以）。手工路径仍然是
`cp .env.example .env` 再自己填 `SECRET_KEY` —— 它为空时 compose 直接中止。
Forgejo 的管理员 / runner 令牌只能在实例起来之后签发，脚本会在结尾把这两步打印出来（见 §3）。

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

- **`PACKAGES_DIR` / `PYTHON_BUILDS_DIR` 的宿主路径** —— 固定为 compose 里的
  `/data/share/packages` 与 `/data/share/python-build-standalone`（分别挂到容器
  `/app/share/python`、`/app/share/python-build-standalone`）。这两个目录不走
  `docker/share` 符号链接，换盘请改 `docker-compose.yml` 的 `x-backend-volumes`，见 §4。
- **`AUTH_ENABLED`** —— 本拓扑永远开着认证（框架默认 `true`）。关闭它意味着边缘 nginx
  后面的整个管理面匿名可写；要关请改 `docker-compose.yml`，并清楚这是安全边界的改动。
- **`ROUTE_PREFIX`** —— 不是隐藏旋钮，写在 `.env` 里即可：它只被 **nginx 边缘容器**
  读取（模板渲染成 `set $openfish_external_prefix`），用来剥掉外部前缀并传
  `X-Forwarded-Prefix`；backend 不读它。典型值是 `/pypi`，配一级代理的
  `location /pypi { proxy_pass http://<openfish>:20416; }`。见 §2 末尾。
- **`FRONTEND_DIST_DIR`** —— 只在 Flask 自己托管 SPA 时有意义；这里 SPA 由 `frontend`
  容器供应，`backend/routes/spa.py` 这条路径永远走不到。

### 外部路径前缀（`ROUTE_PREFIX`）

一级代理按路径分流、把整个应用挂在 `/pypi` 这种前缀下时，**不用改 nginx，也不用在一级
代理上加任何头**——一级代理原样转发即可：

```nginx
location /pypi {
    proxy_pass http://<openfish-host>:20416;
}
```

在 `docker/.env` 里设：

```dotenv
ROUTE_PREFIX=/pypi
```

`docker compose build frontend && docker compose up -d --force-recreate frontend nginx` 之后：

* 边缘 nginx 的模板把它渲染成 `set $openfish_external_prefix`：请求进来先剥掉 `/pypi`
  再做 location 匹配（根路径的 location 全部复用），并给 backend 传
  `X-Forwarded-Prefix: /pypi`；
* backend 的 `ProxyFix(x_prefix=1)` 把它变成 `SCRIPT_NAME`，`services/urlprefix.py`
  统一据此生成 URL：PEP 691 文件链接、`/simple/` 与静态索引的链接、308 跳转、`Link`
  头、npm `dist.tarball`、`/api/v1/*` catalog 的 `download_url`、device
  `verification_uri` 都会带 `/pypi`；
* 同一个值作为 build arg `VITE_BASE_PATH` 打进 SPA：vite 的资源 base 变成
  `/pypi/static/dist/`，vue-router 的 history base 变成 `/pypi`，axios 的 baseURL
  变成 `/pypi/api/v1`，界面里的机器面链接（`/simple/`、`/python-builds/` 等）
  也由 `src/base.ts` 统一加上前缀；
* 留空（默认）时行为与之前完全一致。

`ROUTE_PREFIX` **只给 nginx 与 frontend 构建**，不要配到 backend：backend 若也挂前缀，
就会和 nginx 的剥离叠加成 `/pypi/pypi`。改这个值要**同时重建 frontend 镜像**
（SPA 的 base 是构建期固化的）和重建 nginx（模板渲染），backend 不用重建。

还有一类不是"部署配置"而是**进程间消息**的变量，由代码自己设置、不该出现在 `.env` 里：
`OPENFISH_ROLE`（compose 设）、`OPENFISH_GIT_TOKEN` / `OPENFISH_GIT_HOST`、
`OPENFISH_MODEL_*`、`OPENFISH_TASK_KIND`、`GUARDDOG_TOP_PACKAGES_CACHE_LOCATION`。

### 前端构建参数

SPA 是构建期固化的，没有运行时环境变量；只有三个 build args（compose `frontend.build.args`）：

```bash
NPM_REGISTRY=https://registry.npmmirror.com   # 内网 npm 源；留空用默认
BUILD_ID=$(git describe --always)             # 侧边栏显示的构建标识；留空 = 构建时刻
# 外部路径前缀；compose 从 ROUTE_PREFIX 转发，一般不用手工传
VITE_BASE_PATH=/pypi
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

**`packages` 与 `python-build-standalone` 是两个例外**：它们的宿主机来源直接写在
compose 里，不经过符号链接：

```yaml
# docker/docker-compose.yml 的 x-backend-volumes
- /data/share/packages:/app/share/python                                 # → PACKAGES_DIR
- /data/share/python-build-standalone:/app/share/python-build-standalone # → PYTHON_BUILDS_DIR
```

换盘 = 改对应那一行（启动前先 `mkdir -p` 新目录），再 `docker compose up -d`。容器内
`PACKAGES_DIR` / `PYTHON_BUILDS_DIR` 默认 `/app/share/python` 与
`/app/share/python-build-standalone`，与 `docker/.env.example` 一致；要改容器内路径，
同时改 `.env` 的同名变量和对应挂载的目标侧。

**其余** bind 源都是 `docker/` 下的相对路径，换盘 = 重指同名符号链接，compose 一个字不改：

```bash
ln -sfn /srv/openfish-mirror/npm docker/npm
```

**链接目标必须在容器里也解析得到。** `docker/` 下作为 bind 源的链接由 Docker 在宿主机上解析，
写绝对路径没问题；但**被挂载目录内部**的链接是在容器里解析的，宿主机的绝对路径在容器里根本不存在，
于是它就是一个断链：

```bash
docker/share/legacy-releases/20241016 -> /mnt/big-disk/releases   # ✗ 容器里没有 /mnt/big-disk
docker/share/legacy-releases/20241016 -> ../releases              # ✓ 相对链接，挂载后仍然成立
```

相对链接的目标**只是还没建出来**时，后端启动会自己补上（目标必须和链接在同一文件系统上，
即仍在挂载内）——这跟 `makedirs` 补一个缺失目录是一回事。绝对链接不会：它指向的东西在容器里
不存在，替它建目录只会把包装进容器可写层，所以后端报出变量名、链接和目标后拒绝启动，见 §5。

| 目录 | 内容 | 进数据库吗 |
| --- | --- | --- |
| `/data/share/packages`（compose 挂载，容器内是 `PACKAGES_DIR`） | Python 包字节 | 否——文件名即身份，索引由 `PACKAGES_DIR` 扫描得到 |
| `/data/share/python-build-standalone`（compose 挂载，容器内是 `PYTHON_BUILDS_DIR`） | CPython 构建字节 | 否——目录名是 release tag，索引扫描得到 |
| `share/` `npm/` `node-builds/` `docker-images/` `debian/` | 制品字节 | npm / docker / debian 的 overlay 行在 `catalog_entries` 表里 |
| `tools/` `docs/` | 对象字节（`objects/<uuid4>`） | **条目在数据库里**，目录只是存放处与 import/export 的桥 |
| `data/` | SQLite 库、回源缓存、模型路由表所在库——本部署**自己的一份** | 是 |
| `forgejo/` | Forgejo 的 `/data`（bare 仓库 + 实例配置） | 独立 |
| `agent-work/` | runner 的 `/work` 任务沙箱 | 否 |

因此：**换库会丢 `tools`/`docs` 的条目，但字节还在**——迁移时按 §6 的 import 步骤补回。

### `docker/data` 与 `backend/data` 是两份

除 packages 外，compose 的每一个 bind 源都在 `docker/` 下，`docker/data` 也不例外，所以：

| 你怎么起 | 数据库在哪 |
| --- | --- |
| `cd docker && docker compose up -d` | `docker/data/cpypiserver.db`（容器内 `/app/data`） |
| `cd backend && python app.py`、宿主 `cli.py` | `backend/data/cpypiserver.db` |

两条路互不依赖：跑过容器不会在 `backend/` 里留下 root 属主的目录，本地开发也不会
因为容器跑过就写不进去（老版本的 compose 挂的是 `../backend/data`，正好会反过来卡住
本地开发）。代价是同一个仓库里可能有两份库——**你在哪起服务，就动哪一份**。

老部署（数据原本就在 `backend/data`）升级上来时，先把容器停掉（SQLite 是 WAL，
别热拷贝），再按需要二选一：

```bash
cd docker && docker compose stop backend
```

```bash
# A. 继续共用同一个文件（升级前的行为）
ln -sfn ../backend/data data
```

```bash
# B. 拆成独立的一份（docker/data 现在是符号链接时先摘掉它；
#    rm 删的是链接本身，不是它指向的 ../backend/data）
[ -L data ] && rm data
cp -a ../backend/data data
```

改完 `docker compose up -d` 即可。`bootstrap-env.sh` 只检查链接能不能在容器里解析，
不替运维搬家；只有 `data` 已经是**真实目录**时才不用管符号链接那一步。

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
- `docker compose up` 失败并报 `error while creating mount source path '/data/share/...'` →
  宿主 packages / python-build-standalone 目录不存在，或它是断链。compose 的挂载源由
  Docker 在**宿主机**解析，先 `sudo mkdir -p /data/share/packages
  /data/share/python-build-standalone`（或改 compose 里对应那一行）。注意
  `docker compose config --quiet` 只校验语法与插值，不校验 mount 源，这类错误只有 `up` 才暴露。
- 后端反复重启，日志里 `RuntimeError: cannot use PACKAGES_DIR=...`（或 `PYTHON_BUILDS_DIR=...`）
  → 容器内那个路径不是目录。先确认 compose 的挂载目标与 `.env` 的容器内路径一致：

  ```bash
  docker compose config | grep -A2 'PACKAGES_DIR'   # 或直接看 x-backend-volumes
  ```

  `docker/share` 内部的链接是**容器里**解析的，宿主绝对目标会断链，要改成相对路径；
  相对链接的目标只是缺失时后端会自己建出来。绝对链接、跨文件系统、路径被普通文件占住
  这三种都会报错拒绝启动。`./bootstrap-env.sh` 在 `up` 之前也会把这类链接列出来。
- webhook 401 → `FORGEJO_WEBHOOK_SECRET` 与 forgejo 容器里的值不一致。
- `/api/v1/repos/<slug>/git-credential` 503 → `GIT_IDENTITY_KEY` 为空。
- 上传 `413` → 请求体超过边缘 nginx 的 `client_max_body_size`（100m，与
  `MAX_CONTENT_LENGTH` 配套）。
- 面板没有文档/工具 → 空库首启靠 `SEED_CATALOGS`（默认开）；手动补：

  ```bash
  docker compose exec backend python cli.py catalogs seed --force
  docker compose exec backend python cli.py docs import --from /app/docs
  ```

- `docker compose up -d` 报 `dependency failed to start: container openfish-backend is
  unhealthy`，并且 `docker compose ps` 里 nginx 一直是 `Created`（没起来）→ **不是 nginx
  坏了**，是 backend 冷启动超过了健康检查宽限，而 nginx 的 `depends_on` 要求 backend
  `healthy`。gunicorn 要先 `import app`，其间会扫描三个索引目录（`PACKAGES_DIR` 只列
  文件名、不读内容），再初始化数据库/鉴权，然后才 bind 端口。`PACKAGES_DIR` 有十万级
  以上 whl、或元数据在 NFS/CIFS/冷缓存上时，这段可能远超默认的 60s：

  ```bash
  docker inspect --format '{{json .State.Health}}' openfish-backend | python3 -m json.tool
  docker compose logs --tail=20 backend                       # 看到 gunicorn "Booting worker" 才算 bind
  docker compose exec backend sh -c 'time ls /app/share/python | wc -l'   # 冷缓存下量一下目录成本
  ```

  应急：先只起 backend，等它 `healthy` 再补其余服务：

  ```bash
  docker compose up -d backend
  until [ "$(docker inspect --format '{{.State.Health.Status}}' openfish-backend)" = healthy ]; do sleep 10; done
  docker compose up -d
  ```

  长期：在 `docker/.env` 把 `BACKEND_HEALTHCHECK_START_PERIOD` 调大（1.5T 级目录先设
  `10m`，再按实测第一次启动的耗时收窄），见 `.env.example`。不要把 nginx 的依赖改成
  `service_started` 绕过——那只是把 502 提前。另一个同类症状是内存而不是时间：后端
  反复 `Restarting` 且 `docker inspect --format '{{.State.OOMKilled}}' openfish-backend`
  为 `true`，说明索引条目（每个文件一条，全在内存里）把配额吃满了，要加内存或缩小
  单个 `PACKAGES_DIR`，调宽限没用。

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
`docker compose up -d`。SQLite 文件一直在 `docker/data/cpypiserver.db`（§4），没有被改动过。

**不迁移的东西（不要期待）：** 统计数据与历史、待办队列、agent 任务记录、
`repo_runners` 配置、以及文档的**修订历史**（`docs export` 只写当前版本）。
切换是"换一个库"，不是"搬一个库"；上面的 export/import 只覆盖目录条目，
身份类数据是重建。
