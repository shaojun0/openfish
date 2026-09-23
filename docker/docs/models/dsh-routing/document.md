# 模型路由（下游 DSH）

模型路由表存在**数据库表 `model_routes`** 里——与 API key、用户、Agent 队列同一个库
（默认 `API_KEYS_FILE` 指向的 SQLite，或 `DATABASE_URL` 指向的 PostgreSQL）。它描述下游
内网 DSH 可以指向的模型端点。本服务器只**登记、展示并检测连通性**，不代理推理流量。
持有 `model:write` 的管理员可以直接在 `/models` 页面维护这张表，无需登录服务器改文件。

全新部署要预置一组路由，用仓库里的种子脚本（幂等，`ON CONFLICT (name) DO NOTHING`）：

```bash
sqlite3 backend/data/cpypiserver.db < backend/config/model_routes.seed.sql
# 或：psql "$DATABASE_URL" -f backend/config/model_routes.seed.sql
```

## 一行就是一条路由

```sql
INSERT INTO model_routes
  (name, provider, kind, base_url, api_key, model, aliases, path, enabled, description)
VALUES
  ('qwen2.5-7b', 'openai', 'chat', 'http://10.0.0.8:8000', '',
   'Qwen/Qwen2.5-7B-Instruct', '["qwen", "fast"]', '/v1/chat/completions', true,
   '内网主力对话模型');
```

字段说明（与列一一对应）：

| 列 | 必填 | 说明 |
| ---- | ---- | ---- |
| `name` | 是 | 路由名称，全局唯一；`probe` 为系统保留字 |
| `description` | 是 | 路由描述，便于他人识别用途 |
| `provider` | 否 | **调用协议**：`openai` / `anthropic`，默认 `openai`。MinerU 这类文档解析服务走 OpenAI 格式，因此用 `provider=openai` + `kind=ocr`，没有单独的协议 |
| `kind` | 否 | **模型功能**：`chat` / `completion` / `embedding` / `rerank` / `ocr` / `asr` / `tts`，留空按协议取默认值（见下） |
| `base_url` | 是 | 形如 `http(s)://主机[:端口]` 的地址。注意：探活走 `{base_url}/models`（Anthropic 走 `{root}/v1/models`），所以这里填 **API 根地址**（网关只提供 `/v1/models` 时写 `https://主机/v1`） |
| `api_key` | 否 | 上游鉴权密钥，**密封后**存储（`enc:v1:` 信封），可以为空。不要在这里手写明文：见下一节 |
| `path` | 否 | 端点路径；留空按格式取默认值：`/v1/chat/completions`、`/v1/messages`。这是下游调用推理的路径，探活不使用它 |
| `model` | 否 | 模型 ID |
| `aliases` | 否 | 供 DSH 使用的别名列表（JSON 数组） |
| `enabled` | 否 | 是否发布给下游，默认 `true` |

## 密钥：落库前密封，主密钥只有 backend 有

`api_key` 在写入前用 Fernet 加密（信封前缀 `enc:v1:`），主密钥是 `MODEL_ROUTE_KEY`。
因此**数据库、备份、副本里都不含可直接使用的上游凭证**，明文只存在于即将发请求的
那个进程里。种子脚本给每条路由留空 `api_key`，真实密钥事后写入：

```bash
python cli.py model-route set-key qwen2.5-7b --key-env QWEN_API_KEY
#   --key-env 从进程环境读值（不进 argv / shell history）；省略则隐藏提示输入
python cli.py model-route list     # 每条路由的鉴权状态（从不打印密钥）
python cli.py model-route seal     # 把仍是明文的旧行一次性重新密封
```

`MODEL_ROUTE_KEY` 留空时**写入直接失败**（fail-closed，绝不降级明文）。下游 DSH 从
`/api/v1/models/resolved` 拿到的是**解密后**的明文，所以它不需要、也不应该持有主密钥。

> `GET /api/v1/models` 的 `api_key_source` 说明每条路由的鉴权状态：
> `stored` 正常；`plaintext` 是密封机制上线前写入的旧行，需要跑 `model-route seal`；
> `unreadable` 表示解不开信封（`MODEL_ROUTE_KEY` 缺失或不对，或该行损坏）；
> `none` 表示未配置。探活会带上解密出的密钥，因此已配置的路由不会每次都报“需鉴权”。

## 分类：协议 × 功能

一条路由用两个**互相正交**的维度描述：`provider` 说“怎么调用”，`kind`
说“这个端点能干什么”。一条路由就是一个端点，所以它只有一个 `kind`；
同一台服务器既提供对话又提供向量化时，登记成两条路由。

| `kind` | 含义 | 典型路径 |
| ------ | ---- | -------- |
| `chat` | 对话 | `/v1/chat/completions` |
| `completion` | 文本补全（续写） | `/v1/completions` |
| `embedding` | 向量化 | `/v1/embeddings` |
| `rerank` | 重排序 | `/v1/rerank` |
| `ocr` | 文档解析 / OCR | `/v1/chat/completions`（MinerU 等 OpenAI 兼容端点） |
| `asr` | 语音转文字 | `/v1/audio/transcriptions` |
| `tts` | 文字转语音 | `/v1/audio/speech` |

`kind` 留空时按协议取默认值，因此**旧行不需要一次性补齐**：

| `provider` | 默认 `kind` |
| ---------- | ----------- |
| `openai` | `chat` |
| `anthropic` | `chat` |

下游 DSH 的企业内网插件只把 `kind` 为 `chat` / `completion` 的路由注册成
对话（LLM）provider，并从中选出 `aliases` 含 `default` 的一条作为默认模型；
`embedding` / `rerank` / `ocr` / `asr` / `tts` 只在本页登记与展示，供向量库、
文档解析等其它消费者使用。**不要把向量化或 OCR 端点标成 `chat`**——那会让它
出现在 DSH 的模型下拉框里，调用时必然失败。

`path` 不会因为 `kind` 自动改变（只有 `provider` 决定默认路径）：登记
`embedding` 路由时请显式写 `/v1/embeddings`。

## 在线维护与连通性检测

页面顶部“新增路由”会在表格第一行弹出编辑行，填写后保存即写入
`model_routes` 表，随后服务器会立即用 OpenAI 客户端向该端点**列出模型**
（`GET {base_url}/models`；Anthropic 为 `{root}/v1/models`）——这是最轻的一次调用，
既判断“是否通畅”，也顺带验证密钥，而且不会发送推理请求。
检测结果按状态着色：`通畅`（2xx/3xx）、`需鉴权`（401/403）、
`方法受限`（405）、`路径不存在`（404）、`服务异常`（5xx）、`不通`。
每行可“重新检测”，也可点“检测全部”。

检测结果存在**该路由自己那一行**上，不会出现在下游读取的字段里，保证发布出去的路由
数据始终是纯路由信息；因此改个名字检测结果跟着走，删掉路由检测结果一起消失。

> 注意：接口**从不返回** `api_key` 原文，只返回是否已设置与末四位提示；
> 编辑时留空表示保持原密钥，勾选“清除密钥”才会删除。

## 下游使用

```bash
export DSH_MODEL_BASE_URL=http://10.0.0.8:8000/v1
```

`GET /api/v1/models` 返回解析后的路由表（含 `exists` / `error` / `providers` /
`kinds` 字段；表为空不会报错）。浏览需要 `model:read` 权限，增删改与检测需要
`model:write` 权限（默认仅内置管理员角色持有）。下游插件要拿含真实密钥的机器视图，
用 `GET /api/v1/models/resolved`（`model:resolve` 权限）。

## 相关页面

| 页面 | 路径 | 说明 |
| ---- | ---- | ---- |
| 模型路由 | `/models` | 路由表、在线增删改与连通性检测、可粘贴的配置片段 |
| 路由表（掩码） | `/api/v1/models` | 机器可读的路由表，不含密钥（管理员可 POST/PUT/DELETE） |
| 路由表（含密钥） | `/api/v1/models/resolved` | 下游 DSH 插件读取的机器视图 |
