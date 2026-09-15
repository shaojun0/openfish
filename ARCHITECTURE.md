# openfish 架构解构

本文把仓库按**构建单元**而不是按文件类型拆开：每一节对应一个可以独立构建、
独立运行、独立部署的单元，末尾给出迁移对照表与验证证据。

```
openfish/
├── backend/          ① Flask 后端         → openfish-backend 镜像
├── frontend/         ② Vue 3 SPA          → openfish-frontend 镜像
├── docker/           ③ 编排、边缘网关与制品库 → 4 容器拓扑
│   ├── tools/ npm/ node-builds/ docker-images/ debian/ docs/   ④ 制品库（操作员数据）
│   ├── docker-compose.yml  .env.example
│   └── nginx/nginx.conf
├── integrations/     ⑤ 下游客户端代码（不进任何镜像）
└── README.md  本文件
```

---

## ① backend/ — Flask 后端

一个自包含的 Python 包：自带 `Dockerfile`、`pyproject.toml`、`uv.lock`、`.venv/`
和全部验证门禁。构建上下文就是本目录，因此镜像里**不存在**任何前端或制品库
内容。

### 请求处理链路

```
app.py                     创建 Flask 应用，禁用内置 static handler
  └─ extensions/           插件注册表（拓扑序初始化）
       ├─ error_handlers   401 → OAuth 跳转 / Basic 挑战
       ├─ database         SQLAlchemy + SQLite（WAL）
       ├─ cache            flask-caching
       ├─ index_ext        watchdog 维护内存包索引
       └─ stats_refresh    后台统计刷新线程
  └─ routes/__init__.py    注册蓝图 + 绑定每个蓝图的鉴权策略
  └─ services/authz.py     播种权限点 / 内置角色 / 冷启动超管
```

### 分层

| 层 | 目录 | 职责 |
| --- | --- | --- |
| 入口 | `app.py` `cli.py` `errors.py` `schemas.py` | 应用装配、管理 CLI、错误类型、请求/响应模型（`/openapi.json` 的唯一来源） |
| 配置 | `config/` | pydantic-settings 模型（server / storage / auth / security / hub）+ `paths.py` 路径锚定 |
| 鉴权 | `auth/` | 装饰器、守卫、权限点目录、API key、OAuth2 |
| 路由 | `routes/` | 每个生态一个蓝图；机器面协议与 `/api/v1` JSON 同址 |
| 服务 | `services/` | 授权服务、目录聚合、Markdown 渲染（markdown-it-py 封装）、上游穿透代理与缓存、各生态 registry 适配器，以及三个共享原语：`format.py`（字节/时间格式化）、`fileio.py`（原子写 + JSON）、`digest.py`（SHA-256 缓存） |
| 索引 | `index/` | 包 / CPython 构建 / Node 构建的发现与索引 |
| 模型 | `models/` | SQLAlchemy 表：users / roles / permissions / user_roles / role_permissions / api_keys / stats |
| 描述 | `openapi/` | OpenAPI 3.1 元数据注册表、spec 生成、渲染器 |
| 模板 | `static/<生态>/*.html` | **机器面** Jinja 模板（`pip`/`uv`/`nvm` 直接解析，不跑 JS），由 Flask 自带模板加载器（`template_folder="static"`）渲染，**不对外公开** |
| 门禁 | `scripts/check_*.py` | 12 个离线回归门禁 |

### 代码约定（熵减规则）

后端遵循最初提交确立的风格，并把"每件事只有一种做法"作为硬约束。新增代码照抄
最近的同类模块即可，不需要另立一套：

| 约定 | 说明 |
| --- | --- |
| **先找库，再自己写** | 有成熟依赖就不要手写。已落地的替换：Markdown 渲染由约 400 行手写解析器改为 `markdown-it-py`（`html=False` + 收窄的 `link_validator`，安全性是结构性的）；机器面模板由 `services/templates.py` 的 lru 文件缓存改为 Flask/Jinja 自带加载器（`render_template("python/simple_index.html", …)`）。协议代理（npm / Docker Registry v2 / apt）没有对应库，属于业务逻辑，保留自研。 |
| **一个关注点只有一份实现** | 字节格式化、ISO 时间、原子写 + JSON、SHA-256 缓存分别只有 `services/format.py`、`services/fileio.py`、`services/digest.py` 三处实现；此前它们各有 2–3 份不同写法的副本。 |
| **从定义处导入** | `from services.docs import read`，不要经 `services/__init__.py` 之类的门面转一手；`auth/`、`services/` 的包初始化文件只保留 docstring（`index/` 另留唯一的 `register_all`）。 |
| **模块头** | 每个模块以 `from __future__ import annotations` 开头，其后先标准库、再三方、再本仓库，各段内部按字母序。**唯一例外**是 `routes/pypi.py`：PEP 563 会把 `query: FormatQuery` 变成字符串，而 flask-pydantic 正是读这个注解并交给 `issubclass`，文件头注明了原因。 |
| **类型写法** | 一律 `X \| None`，不使用 `typing.Optional`；公共函数写全签名。 |
| **日志** | 模块级 `logger = logging.getLogger("cpypiserver.<域>")`，不使用 `log` / `_log`。 |
| **依赖** | `pyproject.toml` 里只留真正被 import 的包；`cryptography` / `pyjwt` 已因无人使用而删除。`scripts/check_lint.py`（pyflakes）把这套约定变成可执行门禁：未定义名、死导入一律失败。 |
| **契约稳定** | `/openapi.json` 的 `components.schemas` 顺序是确定的（`_build_schemas` 对引用集合排序），因此契约变更在 diff 里只显示真正改动的行。 |
| **分区注释** | 长模块用 `# ── 标题 ────…` 分段，与既有模块保持一致的视觉节奏。 |
| **模板归属** | 机器面 Jinja 模板写在 `static/<生态>/`，用 `render_template("<生态>/<文件>.html", …)` 渲染；浏览器页面属于 `frontend/` 的 Vue SPA，唯一例外是设备授权页（`routes/device.py` 内的自包含字符串模板）。 |

### 蓝图与 URL 归属

`routes/__init__.py` 是唯一的注册点，注释里写明了每个前缀的归属与鉴权策略。

| 前缀 | 蓝图 | 面向 |
| --- | --- | --- |
| `/simple/` `/packages/<f>` `POST /` | pypi | pip / uv / twine |
| `/python-builds/` | python_build | uv |
| `/node-builds/` | node_build | nvm / fnm / node-gyp |
| `/tools/` `/npm/` `/docker/` `/debian/` | hub / npm / docker / debian | 制品中心协议 |
| `/docs/` `/documentation/<生态>` | docs | Markdown 文档（后者是 SPA 页面） |
| `/api/v1/*` | session / api_keys / admin / access / hub / npm / docker / debian / docs / device | SPA 与 API-key 客户端 |
| `/auth/*` `/device` | auth_routes / device | 登录与设备授权 |
| `/openapi.json` `/docs` `/llms.txt` `/.well-known/` | discovery | 匿名契约发布 |
| `/certs/ca_chain.pem` | certs | 私网 CA（匿名，只此一文件） |
| `/` `/static/dist/*` `/<path>` | spa | 浏览器壳与 bundle（`app:read`） |

鉴权总纲（代码注释原话）：**anonymous 只有文档，别无其它**。

### 路径锚定（本次重组新引入）

`config/paths.py` 定义三个根，所有路径默认值由 `__file__` 解析，与启动时的
工作目录无关：

| 根 | 含义 | 谁用它 |
| --- | --- | --- |
| `BACKEND_ROOT` = `backend/` | 代码、模板、本机状态 | `packages/` `data/` `certs/` `config/model_routes.json` `static/<生态>/` |
| `PROJECT_ROOT` = 仓库根 | 部署包与两个构建单元 | `docker/` `backend/` `frontend/` `integrations/` |
| `CATALOGS_ROOT` = `docker/` | 操作员投放的制品库 | `tools/` `npm/` `node-builds/` `docker-images/` `debian/` `docs/` `python-build-standalone/` |

环境变量永远优先；Docker Compose 把它们全部覆盖成 `/app/…` 绝对路径。

---

## ② frontend/ — Vue 3 SPA

自包含的 npm 工程，构建产物在 `frontend/dist/`。

| 目录 | 内容 |
| --- | --- |
| `src/api/` | axios 实例；401 → `/auth/login` 拦截器 |
| `src/router/` | 路由表 + 会话/permission 前置守卫 |
| `src/stores/` | Pinia：session、app |
| `src/views/` | Home / Packages / Npm / Tools / Models / Docker / Debian / Docs / ApiKeys / Admin / Access / NotFound |
| `src/components/` | 布局、表格分页器、Markdown 编辑器与附件管理、构建目录视图 |
| `src/locales/` | zh-CN / en-US |
| `src/composables/` | `usePagination`（前端分页/排序） |
| `nginx.conf` | 静态托管：`/static/dist/` 前缀重写、带哈希资源 `immutable` 缓存、history fallback、`/healthz` |
| `Dockerfile` | `node:24-slim` 构建 → `nginx:1.27-alpine` 运行，Node 不进生产镜像 |
| `scripts/smoke-render.ts` | jsdom 渲染全部路由的冒烟测试 |

`vite.config.ts` 的 `outDir` 是 `dist`（不再写进后端目录），`base` 仍是
`/static/dist/` —— 因此同一份产物既能被前端容器托管，也能通过
`FRONTEND_DIST_DIR` 交回 Flask 托管。

---

## ③ docker/ — 编排与边缘网关

`docker/` 只放编排，不含应用代码。

### 四容器拓扑

| 服务 | 镜像 | 宿主端口 | 角色 |
| --- | --- | --- | --- |
| `nginx` | `nginx:1.27-alpine` | `20416 → 80` | **唯一对外入口**，按路径分流 |
| `backend` | `openfish-backend:latest` | 不发布（`expose 8080`） | Flask + gunicorn，API 与各生态协议 |
| `frontend` | `openfish-frontend:latest` | 不发布（`expose 80`） | SPA 静态托管 |
| `db` | `postgres:16-alpine` | 不发布（`expose 5432`） | `profile: db`，**预留，应用尚未接入** |

另有 `backend-debug`（`profile: debug`，空闲 bash 容器，端口 20417）。

### 边缘路由表

| 路径 | 上游 | 说明 |
| --- | --- | --- |
| `GET /` | frontend | SPA 壳 |
| `POST /` | backend | twine / pip 上传（`limit_except POST`） |
| `/static/dist/` | frontend | 带哈希 bundle |
| `= /packages` `= /tools` `= /npm` `= /docker` `= /debian` | frontend | SPA 页面；**必须精确匹配**，否则 nginx 会把 `/tools` 301 到 `/tools/` 而落到后端 401 |
| `/packages/` `/tools/` `/npm/` `/docker/` `/debian/` | backend | 同名机器面目录 |
| `/simple/` `/legacy/` `/python-builds/` `/node-builds/` | backend | 包管理器协议 |
| `/api/` `/health` `/openapi.json` `/llms.txt` `/.well-known/` | backend | JSON 与发现面 |
| `/docs` `/docs/` `/certs/` `/device` `/device/approve` | backend | 契约文档、CA、设备授权 |
| `= /auth` `/auth/` | backend | OAuth2 回调与登录（`= /auth` 必须精确，否则被 301 成 `/auth/`） |
| 其它（`/admin` `/models` `/documentation/<生态>` …） | frontend | SPA history 深链 |

`client_max_body_size 100m` 与后端 `MAX_CONTENT_LENGTH` 对齐，否则上传会被
nginx 提前拒绝。

### 安全取舍

SPA 壳与 bundle 现在由前端容器静态托管，因此**匿名可下载**（bundle 不含任何
registry 数据）；所有数据端点、`/docs/*`、`/api/v1` 仍各自鉴权，匿名浏览器
加载后被 401 拦截器送到 `/auth/login`。若要恢复"壳本身也要求 `app:read`"的
旧行为，`docker/nginx/nginx.conf` 的注释里写了三步改法（把 `= /` 与
`/static/dist/` 指回 backend，并给后端挂载 dist + 设置 `FRONTEND_DIST_DIR`）。

---

## ④ 制品库目录（docker/ 下）

刻意收进 Compose 目录 `docker/`：它们是**操作员数据**而非代码，Compose 以
bind mount 注入容器，丢文件即生效、无需重建镜像；收在 `docker/` 下则是为了让
仓库根只保留构建单元。

**所有 bind mount 源都是 `docker/` 内的路径**（`./share` `./npm` `./data` …），
`docker-compose.yml` 里既没有 `../backend/...` 也没有宿主机绝对路径。每个源都是
**可插拔**的：它要么是真实目录，要么是指向数据盘的符号链接。`docker/prepare-mounts.sh`
负责创建（`./prepare-mounts.sh /media/…/openfish-mirror` 走数据盘布局），以后
换存储只需 `ln -sfn` 重指一个链接，compose 一个字都不用改。原先的 `*_SRC` 环境
变量已删除——符号链接是唯一的重定向机制，宿主机绝对路径因此无法再回流到
compose 文件里。随仓库提交的样例目录移到了 `docker/examples/`，由该脚本播种进
空的挂载目录。

| 目录 | 内容 | 主要环境变量 |
| --- | --- | --- |
| `tools/<分类>/<文件>` + `catalog.json` | 可下载工具 | `TOOLS_DIR` |
| `npm/` | 本地 npm tarball / `catalog.json` | `NPM_DIR` |
| `node-builds/` | `nodejs.org/dist` 布局的 Node 镜像 | `NODE_BUILDS_DIR` |
| `docker-images/` | `docker save` tar + compose/Dockerfile 片段 | `DOCKER_DIR` |
| `debian/` | 本地 `.deb` + apt 片段 | `DEBIAN_DIR` |
| `docs/<生态>/<文档>/document.md` | 各生态 Markdown 文档 | `DOCS_DIR` |

---

## ⑤ integrations/

下游客户端代码。因为 `backend/` 与 `frontend/` 是两个独立构建上下文，
`integrations/` 天然落在两者之外，**不可能**进入镜像。目前只有
`dsh-plugin-enterprise-intranet`（DSH 企业内网插件）。

---

## 迁移对照表

| 迁移前 | 迁移后 |
| --- | --- |
| `app.py` `cli.py` `errors.py` `schemas.py` | `backend/` 同名 |
| `config/` `auth/` `extensions/` `index/` `models/` `openapi/` `routes/` `services/` | `backend/` 同名 |
| `scripts/` | `backend/scripts/` |
| `static/<生态>/`（Jinja 模板） | `backend/static/<生态>/` |
| `static/dist/`（SPA 产物） | `frontend/dist/`（`vite outDir` 改为 `dist`） |
| `.env.example` | `backend/.env.example` |
| `Dockerfile`（含 node 构建阶段） | `backend/Dockerfile`（纯 Python）+ `frontend/Dockerfile`（node → nginx） |
| `.dockerignore`（根） | `backend/.dockerignore` + `frontend/.dockerignore`（根的那份已删除：没有根构建上下文） |
| `data/` `packages/` `.venv/` `cpypiserver.egg-info/` | `backend/` 同名 |
| `docker/docker-compose.yml`（2 服务） | 4 服务（backend / frontend / nginx / db）+ debug profile |
| `docker/packages/` `docker/python_build_standalone/` | 删除（空目录，无人引用） |
| 镜像内置但无配置的 nginx / supervisor | 删除；反向代理改为独立的 `nginx` 容器 |
| 容器名 `cpypiserver-std` `cpypiserver-debug` | `openfish-backend` `openfish-frontend` `openfish-nginx` `openfish-db` `openfish-backend-debug` |
| `python app.py`（容器 CMD） | `gunicorn --workers 1 --threads 8`（单进程以保持 RBAC 缓存语义） |

---

## 构建 / 运行 / 验证

```bash
# ── 后端（含依赖）──────────────────────────────────────────────
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
python cli.py create-admin <账号>
python app.py                       # http://127.0.0.1:9090

# ── 前端 ───────────────────────────────────────────────────────
cd frontend
npm install
npm run dev                         # http://127.0.0.1:5173，代理到后端
npm run build                       # → frontend/dist
npm run smoke                       # jsdom 全路由冒烟

# ── 门禁（11 个，均可从任意目录运行）───────────────────────────
backend/.venv/bin/python backend/scripts/check_openapi.py
#   check_openapi / check_auth_guards / check_auth_disabled / check_rbac /
#   check_markdown / check_permission_catalog / check_permission_labels /
#   check_device_flow / check_npm_proxy / check_docker_proxy / check_debian_proxy /
#   check_lint（pyflakes：未定义名 / 死导入）
#   另有 check_contract.py，需要对着活服务跑（--base-url + --api-key）

# ── 容器（需要 Docker Compose v2；仓库自带的 docker-compose 1.25 解析不了）──
cd docker
cp .env.example .env                # 必填 SECRET_KEY
docker compose up -d --build
docker compose build backend        # 只重建后端
docker compose build frontend       # 只重建前端

# 单独构建某个镜像（构建上下文就是组件目录）
docker build -t openfish-backend  backend/
docker build -t openfish-frontend frontend/
```

---

## 验证证据

* **12 个离线门禁全部通过**（从仓库根运行，与迁移前基线一致；`check_openapi`
  现在还会执行完整的 OpenAPI 3.1 结构校验，新增的 `check_lint` 用 pyflakes
  兜住"改名后漏改调用点"这类只有单条路由才炸的静默错误）。
* **两个镜像构建成功**：`backend/Dockerfile`（pip 依赖 + gunicorn）、
  `frontend/Dockerfile`（`npm ci` + vite build → nginx）。
* **两个 nginx 配置 `nginx -t` 通过**，包括 `limit_except POST` 的根路由写法和
  精确匹配修正。
* **真实三容器端到端路由验证**（两个镜像 + `nginx:1.27-alpine` 跑在同一
  bridge 网络，用网络别名模拟 Compose 的服务名）：SPA 页面与深链 200、
  `/health` `/api/v1/session` `/openapi.json` `/llms.txt` `/docs`
  `/.well-known/api-catalog` 200、同名前缀的机器面目录匿名 401 / 携带 Basic
  凭据 200、`POST /` 命中后端、`/auth` 回调命中后端、SPA 资源 200 且带
  `Cache-Control: public, immutable`。
* **后端镜像内可跑门禁**：容器内执行 `scripts/check_auth_guards.py` 通过。
* **前端冒烟**：`npm run smoke` 全路由渲染无 Vue 警告。

---

## 已知取舍与后续

1. **`db` 服务尚未接线。** 授权、API key、统计仍走 `backend/data/cpypiserver.db`
   （SQLite）。启用 `--profile db` 不改变应用行为。要真正切换，需要引入
   按引擎可选的连接层并迁移 `models/` 与 `authz` 的会话管理。
2. **SPA 壳匿名可取。** 见上文"安全取舍"，恢复旧行为的改法已写在边缘配置注释里。
3. **gunicorn 单 worker 是刻意的。** 每个 worker 各自缓存 RBAC 授权集，多 worker
   会把"改角色立即生效"变成最多 30s 的最终一致；要提高并发优先加 `--threads`。
4. **镜像以 root 运行。** 与 bind mount 的操作员目录权限兼容性最好；若要非
   root，需要同时调整这些挂载点的属主。
5. **Compose v2 是硬要求。** 本机只装了 docker-compose 1.25，无法解析
   `profiles:` 与 `${VAR:-default}`；本文的 compose 仅做 YAML 解析与路径存在性
   校验，未用 compose 实际启动过（改用等价的 `docker run` 三容器验证）。
6. **`integrations/` 的部署工作副本**仍需用 `integrations/sync-to-deployment.sh`
   与 `/home/linaro/dsh/enterprise-intranet/plugin/` 对齐。
7. **凭据卫生。** 本仓库克隆的 `.git/config` 里，`origin` URL 内嵌了一个明文
   GitHub PAT。URL 已被多次复制传播，应视为已泄露：请立刻在 GitHub 吊销并改用
   credential helper / SSH，同时检查该 PAT 的授权范围与审计日志。
