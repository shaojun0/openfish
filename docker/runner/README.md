# openfish-runner — 智能体运行时沙箱

§9.2 的沙箱镜像与运行方式。它承载「智能体运行时」这一个平面：从队列里领任务、
在 `/work/<task_id>` 里 clone 只读副本、跑 gates、产出 §9.5 的 `result.json`，
仅 fix 模式**且结果门控通过**才推 `agent/*` 分支并开 PR（见 §10）。可以按
`--scale runner=N` 起成一个 N 副本的池子（见 §2.1）。

镜像里**没有**模型权重，**不挂宿主 docker socket**，**不发布宿主端口**。

---

## 1. 构建

构建上下文是 `backend/`（running 镜像 = 后端应用 + git + 最小构建工具）：

```bash
# 在仓库根执行
docker build -t openfish-runner:latest -f docker/runner/Dockerfile backend/
```

`docker/runner/Dockerfile` 与 `backend/Dockerfile` 用同一份 `backend/pyproject.toml`
安装依赖（依赖政策不变：不新增运行时依赖）。

## 2. 运行形态（§13 开放问题 5 的选择）

**当前选择：容器内单进程 headless，runner 容器自己跑队列 worker。**

```
backend (API 容器)  --enqueue-->  agent_tasks 表
                                        │
runner (profile: runner)  python -m services.agent_queue worker --loop
    └─ 每个任务：/work/<task_id>/repo  (git clone --no-checkout + fetch <sha>)
       3 gates → 4 review → 5 search → 6 emit
       fix 模式：commit → git push origin HEAD:refs/heads/agent/<name> → Forgejo 开 PR（I4）
```

不采用「backend 通过 docker socket 为每个任务起一个容器」，原因有三：

1. **不挂宿主 docker socket** 是硬约束，而按任务起容器要么需要 socket，要么需要
   把 Docker API 暴露成服务，二者都扩大攻击面；
2. 队列 worker 本来就要跑在某个进程里（§9.1 的 `python -m services.agent_queue
   worker`），让它跑在 runner 容器内，沙箱边界与 worker 边界重合，没有第二套编排；
3. 任务间的隔离由「每任务一个 `/work/<task_id>`」+ 容器级资源/网络限制共同保证，
   对「一个任务只在一个仓库内」（§0.2）的规模足够。

后续若要更强的隔离，可把本镜像当模板，由平台按任务 `docker run` 一份——届时
`RunnerAdapter` 换成 docker 实现即可，`services/agent_runner.py` 一行不用改。

模型调用由注入的 `review_fn` 承担（`SubprocessRunnerAdapter(review_fn=…)`），
在容器内以 headless 子进程执行 DSH / 模型客户端；它读环境变量里的
`OPENFISH_MODEL_*`（见下），不读任何落盘密钥。

### 2.1 N 个副本（横向扩展的 runner 池）

`runner` **没有** `container_name`，所以可以直接水平扩展：

```bash
docker compose --profile runner --scale runner=N up -d
docker compose --profile runner ps      # 看到 openfish-runner-1 … openfish-runner-N
```

每个副本跑的都是同一条 `services.agent_queue worker --loop`，从**同一张
`agent_tasks` 表**领任务，靠数据库租约互不干扰：

- **领取是原子的**：SQLite 先 `BEGIN IMMEDIATE` 拿写锁再 SELECT；PostgreSQL 用
  `SELECT … FOR UPDATE SKIP LOCKED`，N 个 worker 各拿各的、互不阻塞；
- **租约 + 心跳**：worker 每 15s 续 `lease_until`；副本被杀后租约到期，另一副本回收
  重试（`attempts += 1`），超过 `max_attempts` 落 `dead` 交给人工；
- **worker 身份唯一**：`worker_id()` = `<host>-<pid>-<random>`，副本之间不会互相顶替。

**每任务工作目录不会跨副本撞车。** `AGENT_WORK_ROOT=/work` 是所有副本共享的 bind
mount，`agent_runner.workdir_for(root, task_id, attempt)` 产生
`/work/<task_id>`（首次）或 `/work/<task_id>-attempt<n>`（重试）：`task_id` 是共享
数据库里全局唯一的 `agent_tasks.id`，attempt 后缀则保证「租约到期被另一个副本接管」
时，第二次尝试不会删掉第一次仍在用的 checkout。`prepare()` 在 clone 前只清掉自己
这个目录里的残留 `repo/`；24h `.done` 回收规则不变。租约丢失时旧副本不会再发布
（发布前的 fencing 检查），所以不会出现两个 PR。

### 2.2 为什么没有 Redis / Celery

DEVELOPMENT.md §3.1 明确不引入 Celery/Redis。队列状态必须和业务数据在**同一个数据库**，
否则会出现「Redis 说 queued、数据库里仓库已经删了」这种两个真相。现有
`services/agent_queue.py` 已经具备 broker 才会提供的能力——原子领取、15s 心跳、过期
回收、指数退避、死信——并用 `FOR UPDATE SKIP LOCKED` / `BEGIN IMMEDIATE` 做到多 worker
安全。再加一个 broker 只会多一份需要持久化、监控、对账的状态，收益为零。

### 2.3 SQLite 写竞争：N > 2 建议 PostgreSQL

SQLite 的 `BEGIN IMMEDIATE` 会把「领取」串行化，副本一多，领取与心跳开始互相等锁
（`busy_timeout=5s`）。**N ≤ 2 时 SQLite 够用；N > 2 请切 PostgreSQL**（`docker
compose --profile db`）：

```bash
# docker/.env
DATABASE_URL=postgresql+psycopg://openfish:****@db:5432/openfish
```

```bash
docker compose --profile db --profile runner --scale runner=6 up -d
```

`backend` 与**所有** `runner` 副本必须指向同一个 `DATABASE_URL`，否则副本之间互相
看不见任务。

## 3. 资源限制

| 限制 | 值 | 理由 |
| --- | --- | --- |
| CPU | `--cpus 2` | 一次只跑少量任务，构建/测试门禁是短时突发 |
| 内存 | `--memory 2g` | 防止某个 checkout 的构建把宿主拖垮 |
| 进程数 | `--pids-limit 512` | fork 炸弹兜底（npm/编译会起不少进程，给足余量） |
| 根文件系统 | 只读 + `/tmp` tmpfs | runner 只应写 `/work` 和 `/app/data` |
| capabilities | `cap_drop: ALL` | 不需要任何 capability |
| 提权 | `no-new-privileges:true` | setuid 兜底 |

## 4. 网络

- **无外网**：runner 只接入内网网络，不接任何带公网出口的网络。
- 允许的目标只有两类：**模型端点**与**内网镜像**（PyPI/npm/apt 镜像、Forgejo）。
- 若模型端点在另一张内网网络上，把它加进 runner 的 `networks` 即可；
  **不要**为了取模型而给 runner 打开公网出口——§8.4 的机制兜底正是靠这一条。
- Forgejo 的 clone/push 也走内网；runner 不发布任何宿主端口。

严格模式下可新建 `internal: true` 的网络：

```yaml
networks:
  openfish-runner-internal:
    internal: true
```

## 5. 挂载与 24h 回收

| 容器路径 | 宿主来源 | 模式 | 说明 |
| --- | --- | --- | --- |
| `/work` | `docker/agent-work/` | rw | 每任务一个子目录；任务结束写 `.done`，**保留 24h** 再回收 |
| `/app/data` | `docker/data/` | rw | 与 backend 共用同一 SQLite / 缓存目录（同一数据库） |
| `/app/config/model_routes.json` | `docker/config/model_routes.json` | ro | 只读解析模型路由；runner 不改路由表 |

24h 规则由 `services/agent_runner.py` 实现：

- 任务结束时在 `/work/<task_id>/.done` 写一个时间戳；
- `sweep_workdirs(now, root)` 是**纯函数**，只返回到期的目录（`now - mtime >
  86400`），不删除；删除由 `remove_workdirs()` 执行，便于测试与运维审阅；
- **没有 `.done` 的任务永不回收**（可能仍在运行，或需要人工排障）。

建议在宿主 cron 里定时调用同一条规则（也可由 worker 每轮顺带 sweep）：

```bash
docker compose --profile runner exec -T runner \
  python -c "from services.agent_runner import AgentRunner; print(AgentRunner().sweep())"
```

## 6. 模型凭据：只进环境变量，任务结束即失效

- runner 进程用现有入口 `services.model_routes.resolve(MODELS_FILE)` 取到含
  `api_key` 的路由（就是 `/api/v1/models/resolved` 背后的同一份能力）。
- `services/agent_runner.build_model_env()` 把它翻译成子进程环境变量：
  `OPENFISH_MODEL_{PROVIDER,BASE_URL,PATH,MODEL,API_KEY}`，并按 provider 额外给
  `OPENAI_API_KEY` / `OPENAI_BASE_URL`（或 `ANTHROPIC_*`）。
- `AgentRunner.run()` 开始时安装、`finally` 里清空；**不写进 `/work`，不写明文进日志**。
  日志走 `mask_env()` / `SecretRedactingFilter`，产物走 `mask_secrets()` 清洗。
- 模型 key **不**放进 compose 的 `environment:`，避免落进 `docker inspect`。

## 7. 精确的 compose 片段（写入 `docker/docker-compose.yml`，不直接改共享文件）

在 `services:` 下新增（`profiles: [runner]`，默认 `up` 不会启动它）：

```yaml
  # ── 智能体运行时沙箱（profile: runner）────────────────────────────
  runner:
    build:
      context: ../backend
      dockerfile: ../docker/runner/Dockerfile
    image: openfish-runner:latest
    # 不要写 container_name：固定名字会让 --scale runner=N 失败。
    profiles:
      - runner
    restart: unless-stopped
    command: ["python", "-m", "services.agent_queue", "worker", "--loop"]
    cpus: 2.0
    mem_limit: 2g
    pids_limit: 512
    read_only: true
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    tmpfs:
      - /tmp:size=512m
    environment:
      # 只挂 runner 专用的 env 锚点（compose 文件里的 x-runner-env）：
      # 队列数据库 + 模型路由 + FORGEJO_RUNNER_TOKEN + AGENT_* 旋钮。
      # **不要**在这里写 `<<: *backend-env`：那会把 SECRET_KEY /
      # FORGEJO_ADMIN_TOKEN / GIT_IDENTITY_KEY 交给一个会执行仓库自带
      # check_*.py 的容器。
      <<: *runner-env
    volumes:
      - ./agent-work:/work
      - ./data:/app/data
      - ./config/model_routes.json:/app/config/model_routes.json:ro
    networks:
      - openfish
```

启动：

```bash
docker compose --profile runner up -d runner          # 单副本
docker compose --profile runner --scale runner=N up -d  # N 副本（§2.1）
```

> 注意：`runner` 与 `backend` 必须指向**同一个数据库**（同一个
> `DATABASE_URL` 或同一个 `API_KEYS_FILE`），否则 API 入队的任务 runner 看不见。

## 8. `docker/prepare-mounts.sh` 需要加的挂载点

在「Runtime state」一节（`link "$DOCKER_DIR/data" …` 之前）加 `agent-work`：

```bash
# 智能体工作目录（§9.2）：每任务一个子目录，任务结束保留 24h 再回收。
mkdir -p "$DOCKER_DIR/agent-work"
link "$DOCKER_DIR/agent-work" "$PROJECT_DIR/backend/data/agent-work"
```

说明：本地开发（`cd backend && python app.py`）下 `AGENT_WORK_ROOT` 默认 `/work`，
可设成 `backend/data/agent-work`；`prepare-mounts.sh` 把它建在数据盘上时同样用
`link`（与 `data -> ../backend/data` 同一手法），换存储只需重指符号链接。

## 9. 安全清单（构建后自查）

- [ ] `docker inspect openfish-runner` 里没有 `/var/run/docker.sock` 挂载；
- [ ] 镜像里没有模型权重、没有 `.env`、没有 `model_routes.json` 的密钥副本
      （路由表是运行时只读挂载）；
- [ ] runner 不发布宿主端口（`docker compose --profile runner port runner` 为空）；
- [ ] **runner 的环境里没有** `SECRET_KEY` / `FORGEJO_ADMIN_TOKEN` /
      `GIT_IDENTITY_KEY` / `OAUTH2_CLIENT_SECRET` / 上游口令
      （`docker inspect` 里逐个确认；`scripts/check_git_boundary.py` 会离线断言）；
- [ ] `FORGEJO_RUNNER_TOKEN` 是一枚**单独签发、可单独吊销、非 admin** 的 token；
- [ ] runner 所在网络无公网出口（`internal: true` 或防火墙等价物）——
      **待补**：当前 compose 仍是普通 bridge，见 §7 上方的说明；
- [ ] `read_only` 根文件系统下，唯一可写路径是 `/work`、`/app/data` 与 `/tmp`。

## 10. 结果门控的 PR（`pr_policy`，fix 模式）

fix 模式**不再无条件**推分支开 PR。「根据实际情况提交 PR」由 `pr_policy` 决定：

| `pr_policy` | 行为 |
| --- | --- |
| `on_green`（默认） | commit 后**在改好的 checkout 上重跑 gates**；全绿才 `push` + 开 PR。有失败 gate 则任务标 `failed`，错误里带上具体 gate 名，**不 push、不开 PR、不留半成品分支** |
| `always` | 不论 gates 结果都 push + 开 PR（已文档化的逃生开关） |
| `never` | 只产出 finding，不 commit / push / 开 PR（纯报告模式） |

- 优先级：`AgentRunner(pr_policy=…)` > 环境变量 `AGENT_PR_POLICY` >
  仓库 `.agent/review-policy.yml` 的 `defaults.pr_policy` > 默认 `on_green`。
- **空提交不开 PR**：`commit()` 发现 `HEAD` 没有移动（没有改动）即把任务标 `failed`，
  不会推一个零 diff 的分支。
- **push 触发的 review 升级为 fix+PR** 需要显式 `defaults.auto_fix: true`（默认
  false，见 AGENTS.md §6.4）：只有「存在失败 gate」时才升级，且升级后仍走 `on_green`
  门控。`review` 任务绝不写 `main`/保护分支（I4）。

## 11. review 命令（`AGENT_REVIEW_COMMAND`）

runner 用 `SubprocessRunnerAdapter(review_fn=…)` 调一个 headless 命令：

```bash
AGENT_REVIEW_COMMAND="/app/tools/review --policy .agent/review-policy.yml"
AGENT_REVIEW_TIMEOUT=900        # 可选，默认 900s
```

- 命令在 `/work/<task_id>/repo` 下执行，**必须**往 stdout 打印 findings JSON 数组
  （或 `{"findings": [...]}`）；平台再按 §9.5 校验，脏产出让任务 `failed`。
- **角色信号 `OPENFISH_TASK_KIND`**：平台给命令注入 `review` / `fix` / `checks`。
  `review` 必须只读（不许改工作树），`fix` 必须真的改文件（否则 `commit()` 会以
  「fix 模式没有产生可推送的提交」把任务标 `failed`），`checks`（curator）只能改
  `.agent/checks/**` 与测试文件。命令必须按角色行事，否则路径守卫会让任务失败。
  同时注入的还有 `OPENFISH_WORKDIR` / `OPENFISH_REPO_DIR` / `OPENFISH_COMMIT_SHA`。
- **未配置即失败**：`AGENT_REVIEW_COMMAND` 为空时 review 步骤直接抛错，任务标
  `failed`，**绝不会**被当作 `done` 静默退休（缺陷 A1）。
- 模型凭据只经环境变量（`OPENFISH_MODEL_*`）传给子进程，**不进 argv、不落盘、不进
  日志**；命令自身不要往参数里塞 key。注意：review 命令是**唯一**拿到模型 key 的
  子进程——平台密钥（`SECRET_KEY` / `FORGEJO_ADMIN_TOKEN` / `GIT_IDENTITY_KEY`）
  被 `services/sandbox_env.py` 的白名单挡在外面，仓库自带的 gate 也拿不到模型 key。
- PR 由 `ForgejoClient.create_pull_request()` 打开，git push 由
  `credential.helper` + `OPENFISH_GIT_TOKEN` 认证；两者都用**同一枚**
  `FORGEJO_RUNNER_TOKEN`（`agent_worker.runner_token()`），它**不是**平台的
  `FORGEJO_ADMIN_TOKEN`。token 不进 argv、不进 clone URL、不落 `.git/config`。
  未设置时 fix 任务在 push/PR 处**明确失败**，不会回落到 admin 权限。
- 发布前有**租约 fencing**：`AgentRunner` 在 `push` 与 `open_pr` 之前各调用一次
  `publish_guard`（worker 注入的 DB 活性检查）。租约被回收后，旧副本拒绝发布，
  而不是再推一个分支/再开一个 PR。

