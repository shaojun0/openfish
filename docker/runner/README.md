# openfish-runner — 智能体运行时沙箱

§9.2 的沙箱镜像与运行方式。它承载「智能体运行时」这一个平面：从队列里领任务、
在 `/work/<task_id>` 里 clone 只读副本、跑 gates、产出 §9.5 的 `result.json`，
仅 fix 模式才推 `agent/*` 分支并开 PR。

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
       fix 模式：git push origin HEAD:agent/<name>  +  Forgejo 开 PR（I4）
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
    container_name: openfish-runner
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
      AGENT_WORK_ROOT: /work
      AGENT_WORK_RETENTION_SECONDS: "86400"
      AGENT_GATE_TIMEOUT: "120"
      AGENT_MAX_FINDINGS: "50"
      DATABASE_URL: ${DATABASE_URL:-}
      API_KEYS_FILE: /app/data/cpypiserver.db
      MODELS_FILE: /app/config/model_routes.json
    volumes:
      - ./agent-work:/work
      - ./data:/app/data
      - ./config/model_routes.json:/app/config/model_routes.json:ro
    networks:
      - openfish
```

启动：

```bash
docker compose --profile runner up -d runner
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
- [ ] runner 所在网络无公网出口（`internal: true` 或防火墙等价物）；
- [ ] `read_only` 根文件系统下，唯一可写路径是 `/work`、`/app/data` 与 `/tmp`。
