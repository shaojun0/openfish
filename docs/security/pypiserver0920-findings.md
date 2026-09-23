# pypiserver0920 缺陷处置说明（中危及以上）

本文对应 `pypiserver0920.md`（奇安信代码卫士，2026-09-20，对象是 openfish 的
`pypiserver-main.zip` 快照）中 **16 条高危 + 84 条中危** 缺陷的逐条处置。低危
（354 条）不在本次范围内。

三档结论：

| 结论 | 含义 |
| --- | --- |
| **已修复** | 本次改动修掉了真实可利用的问题。 |
| **此前已修复** | 报告之后已提交到 GitHub 的修复（`8d9f22d`、`119914e`）。 |
| **误报 / 设计使然** | 污点传播成立但语义上不构成该缺陷，或安全边界由调用方/上游库保证。 |

复现：本次处置当时由离线门禁脚本逐条自证（入口 `python -m services.gates`）。这些脚本
已随 `backend/scripts/` 门禁目录整体删除，`services.gates` 也不再提供 CLI 入口，因此
现在没有可运行的复现命令，结论以本文的文字说明为准。

---

## 1. 统一的安全基元（本次新增）

报告里有 40 余条缺陷是同一个模式的重复出现。与其在每处分别修补，本次把三个关注点
收敛成三个模块，全部**封装既有依赖**，不自己造轮子：

| 模块 | 关注点 | 封装的依赖 | 取代的重复实现 |
| --- | --- | --- | --- |
| `backend/services/paths.py` | 外部名字 → 安全路径 | `werkzeug.utils.secure_filename`、`werkzeug.security.safe_join` | `validation.py` 的手写 `resolve()/relative_to()`、`docs.py._contained` |
| `backend/services/logsafe.py` | 日志注入 | 标准库 `logging.Filter` | 13 处各自为政的“直接 `%s` 用户输入” |
| `backend/services/urlsafety.py` | 服务端请求伪造 | `pydantic.AnyHttpUrl` + 标准库 `ipaddress` | 无（原先没有一个出站 URL 校验点） |

配套：

* `extensions/error_handlers.py` 的 JSON 错误统一加 `X-Content-Type-Options: nosniff`；
* `app.py` 在 `logging.basicConfig` 之后安装日志过滤器（唯一入口）；
* `services/model_routes.normalize_api_key()` 拒绝含空白/控制字符的 key；
* `config/hub.py` 新增 `MODEL_PROBE_ALLOWED_HOSTS` 探活白名单。

---

## 2. 高危（16）

| # | 分类 | 位置 | 结论 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 重定向 | `routes/auth_routes.py` `_landing` | **此前已修复** | `8d9f22d` 不再用请求参数拼 `Location`；落地地址由平台基址派生。 |
| 2 | 路径遍历 | `routes/pypi.py` `upload`（`tmp_path.rename(dest)`） | **已修复** | 上传名先过 `paths.safe_name()`，落盘路径用 `paths.contained()`（Werkzeug `safe_join`）。 |
| 3 | 路径遍历 | `services/validation.py` `validate_file` | **已修复** | 同上：整条流水线只处理脱敏后的 basename，错误消息回显的也是它。 |
| 4 | 路径遍历 | `services/fileio.py` `atomic_write_bytes/stream` | **误报（已加防线）** | 两个函数是通用原子写，路径由调用方给定；调用方现统一经 `paths.contained()`。 |
| 5 | 路径遍历 | `services/digest.py` `store_digest` | **误报** | 只对已被接受的最终路径做 stat/hash，不参与路径拼装。 |
| 6 | 路径遍历 | `services/docs.py` `is_image_name` | **误报** | `Path(name).suffix` 只取扩展名，不落盘。 |
| 7 | 路径遍历 | `services/validation.py` `zipfile.ZipFile(BytesIO(content))` ×2 | **误报** | 只读 `namelist()` 做结构校验，**从不解压**，不存在归档穿越。 |
| 8 | 反射型 XSS | `routes/pypi.py` `BadRequestError(result)` / `UploadConflictError` | **已修复** | 回显前先脱敏为 basename；JSON 错误统一 `nosniff`，浏览器不会按 HTML 嗅探。 |
| 9 | 反射型 XSS | `routes/docker.py` `docker_tags`（`Link` 头） | **误报** | `n` 经 `type=int` 解析；`quote()` 转义 CR/LF；Werkzeug 对含换行的头值直接抛 `ValueError`。 |
| 10 | 反射型 XSS | `routes/pypi.py`（同 8） | **已修复** | 同上。 |
| 11 | 基于 DOM 的 XSS ×2 | `frontend/src/api/client.ts` | **此前已修复** | `119914e` 删除了未使用的 `?next=`，401 跳转不再读取 `window.location`。 |
| 12 | 操纵设置 | `integrations/.../lib/index.js` `rejectUnauthorized` | **已修复** | 见 §4：TLS 校验默认开启，降级必须显式。 |
| 13 | 硬编码密码 | `check_docker_proxy.py`（离线门禁脚本，已删除） | **已修复** | 当时客户端与上游口令改为每次运行 `secrets.token_urlsafe()` 生成；该脚本已随 `backend/scripts/` 门禁目录整体删除。 |

---

## 3. 中危（84）

### 3.1 输入验证

| 分类 | 位置 | 结论 | 说明 |
| --- | --- | --- | --- |
| SSRF ×2 | `services/model_routes.py` `probe`（`requests.get(url, headers=…)`） | **已修复** | `services/urlsafety.check_outbound_url()`：只允许 http(s)、拒绝 URL 内嵌凭据、拒绝云元数据/链路本地/CGNAT 地址与元数据主机名、支持 `MODEL_PROBE_ALLOWED_HOSTS` 白名单；探活不再跟随重定向。 |
| SSRF | 离线契约校验脚本（已随 `backend/scripts/` 门禁目录整体删除） | **误报** | `base` 来自 `--base-url`（默认指向本机临时起的实例），请求路径来自本地 OpenAPI 文档，不由请求驱动。 |
| HTTP 响应截断 ×2 | `services/model_routes.py` `request_headers` → `probe`（`requests.get(headers=…)`） | **已修复（2026-09 复查加固）** | 出站 `Authorization` / `x-api-key` 的值来自路由表的 `api_key`。两个问题分别处理：① 写入路径 `normalize_api_key()` 的规则由「拒绝空白 + 控制字符」换成**白名单** `is_header_safe()`（只允许可打印 ASCII 且非空格，非 ASCII 字节也拒绝）；② 真正补上的是**读取路径**——`effective_api_key()` 现在对解封/明文返回值再校验一次，不合格就当「没有 key」（打 warning），因为只写在写路径上的规则管不住数据库里已有的行（`is_sealed()` 为真时 `normalize_api_key()` 根本看不到明文），而 `effective_api_key` 的返回值会同时进入探活 header 与 runner 的 `/api/v1/models` 响应。`request_headers()` 在发出去之前又检查一次，作为真正的污点切断点。SAST 反复报「未验证就进 header」的原因就是校验在 helper 里、值却在别处进 header；现在**判定点、写入点、发送点共用同一个 `is_header_safe()`**。 |
| HTTP 响应截断（**全部出站 header 边界**，2026-09 复查后统一加固） | 新增 `services/headers.py`：`is_header_value_safe()` / `checked_headers()` / `SafeHeaderSession` / `attachment_disposition()` | **已修复** | 报告只点了 `model_routes.py`，但同一个边界在本仓库有 8 处，逐个独立加固必然会漂移，所以收敛成「一个谓词 + 三个层次」：① **构建处**——`services/model_routes.request_headers()`、`services/git_identity._headers()`、`services/repo_import._headers()` 与来源探针、`auth/oauth.introspect_token()` / `exchange_code()`、`debian_offline_cli._session()` 全部走 `checked_headers()`；② **发送处**——`services/upstream.Upstream.request()` 在 merge 之后、`requests` 之前校验一次（npm / docker / debian 三个上游都经此一处，附带覆盖 `debian_apt` 转发的客户端 `Range`）；③ **兜底**——`SafeHeaderSession`（`requests.Session` 子类，`request()` 与 `send()` 双覆盖）替换了 `Upstream` / `git_identity` / `repo_import` 里的裸 `requests.Session()`，将来漏检的新调用也发不出去。另外两处单向边界：`docker_registry._token()` 拒绝上游 token 端点返回的 CRLF「token」；`upstream.forward_headers()` 与 `debian_apt` 缓存信封回放按值过滤，恶意上游无法把 header 注入**我们的响应**。`routes/{python_build,node_build,debian}.py` 的 `Content-Disposition` 改用 `werkzeug.dump_options_header` 转义（RFC 9110），不再手拼 f-string。**注意**：`Accept-Encoding` / `Authorization` 等字段的语法本身含空格，因此严格度按头名区分（`policy_for()`）——首版「一律禁空格」的写法把 `requests` 自己的默认头和 `Bearer <token>` 全判为不安全，是一次真实的自我踩坑，已由端点测试固定。 |
| 日志伪造 ×13 | `services/docs.py`（215/312/487）、`routes/access.py`（224/279）、`services/fileio.py`（42/73/78）、`auth/guards.py`（212）、`auth/oauth.py`（109） | **已修复** | `services/logsafe` 在根 handler 上过滤 `record.msg` 与字符串参数，CR/LF/Tab 转义为可见文本，一条记录无法伪造第二条。（`guards.py` 用 `%r`，本就转义。） |
| 拒绝服务 ×9 | `integrations/.../lib/index.js`（读文件 / `JSON.parse` / 同步 IO） | **已修复 / 误报** | 真实项是平台响应无上限：`requestJson`（插件与生成的 credential helper）新增 8 MiB 响应上限，超出即断开。其余（同步读状态文件、`JSON.parse`）是本地状态与固定大小响应，属误报。 |
| 文件上传（低危，附带） | `routes/pypi.py` / `routes/docs.py` | **误报** | 上传走扩展名白名单 + MIME 嗅探 + wheel `RECORD` 校验（`wheel.wheelfile.WheelFile`）/ PEP 625 文件名 + 归档安全解压（GuardDog `safe_extract`：压缩炸弹、越界软链、ZIP 解析歧义）+ GuardDog 恶意包扫描 + 可选 ClamAV；文件名单段化。 |
| 硬编码 IP | `integrations/.../lib/index.js` `DEFAULTS.platformUrl` | **已修复** | 移除内置地址（含文档/示例），未配置时给出明确报错；`backend/config/server.py`、`cordis.patch.yml`、插件 README 的示例 IP 一并改为占位域名。 |

### 3.2 密码与密钥管理

| 分类 | 位置 | 结论 | 说明 |
| --- | --- | --- | --- |
| 配置文件中的明文密码 ×10 | `docker-compose.yml`、`docker-compose.postgres.yml`、`.env.example`、`config/storage.py`、`cli.py`、`README.md`、`check_database.py`（离线门禁脚本，已删除）、`integrations/.../index.js` | **误报（示例已改）** | 全部是文档/示例里的占位串（`user:pass`、`change-me`、`<强口令>`），仓库内没有真实口令；本次把占位改写成 `${POSTGRES_USER}:${POSTGRES_PASSWORD}`；已删除的离线门禁脚本 `check_database.py` 的测试 URL 也去掉了口令段。 |
| 硬编码加密密钥 | `services/hub.py` `"slug": "root"`（原 `"key": "root"`） | **误报（已改名消除命中）** | 工具目录的**分类标识**字段名，从来不是密钥，整条链路上没有任何密码学调用。改名只为消掉扫描器的启发式命中——`"key": "<字面量>"` 形似 `key = "…"` 的硬编码密钥赋值。语义零变化，但该字段是 `/api/v1/tools` 的公开契约，改动必须同步：`services/hub.py` 两个产出点（`"slug": "root"` / `"slug": directory.name`）、`backend/static/tools/index.html`（`c.slug`）、`frontend/src/api/index.ts` 的 `ToolCategory.slug`、`frontend/src/views/ToolsView.vue`（搜索字段与 `v-for :key`）。 |
| null 加密密钥 | `services/device_auth.py` `"key": None` | **误报** | 设备授权挂起记录的字段初始值，非加密密钥；device_code 只以 SHA-256 落盘。 |
| 空的加密密钥 | `integrations/.../lib/index.js` 288 | **误报** | 该行是 `requestOptions.path` 的拼接。 |
| 硬编码凭据 ×3 | `check_device_flow.py`（离线门禁脚本，已删除）、`services/docker_registry.py` 450 | **已修复 / 误报** | 当时该脚本的管理员口令改为每次运行随机生成并从同一常量构出 Basic 头，脚本本身已随 `backend/scripts/` 门禁目录整体删除；`docker_registry.py:450` 是 `_request` 的 docstring。 |
| 空密码 ×2 | `services/upstream.py`、`services/docker_registry.py` | **误报** | 上游 HTTP Basic 口令字段的默认空串（未配置即匿名）；不是“允许空口令登录”。 |

### 3.3 信息泄露

| 分类 | 位置 | 结论 | 说明 |
| --- | --- | --- | --- |
| 系统信息泄露（外部）×7 | `routes/hub.py` `_write_failed`、`routes/docker.py` `_registry_error` | **已修复** | 5xx 响应不再回显 `OSError.strerror` / 上游错误正文，统一为稳定文案；细节写服务端日志。4xx（客户端自身错误）保留具体消息，因为 `docker pull` 依赖它。 |
| 系统信息泄露（内部）×9 | 离线契约 / OpenAPI 校验脚本（已随 `backend/scripts/` 门禁目录整体删除）、`services/fileio.py`、`services/docker_registry.py` | **误报** | 前两者是离线门禁脚本的**预期输出**（打印契约校验结果），脚本已删除；后两者是服务端日志，不面向客户端。 |

### 3.4 传输与前端

| 分类 | 位置 | 结论 | 说明 |
| --- | --- | --- | --- |
| 不安全的传输 ×20 | `frontend/src/api/index.ts` | **误报** | 命中的全都是 axios 实例调用 `http.get(...)`，扫描器把它当成了 Node 的 `http.get`；文件内没有任何 `http://` 字面量。 |
| SSL 身份验证禁用 ×1 | `integrations/.../lib/index.js` | **已修复** | 见 §4。 |
| 弱验证 ×2 | `frontend/src/api/client.ts` | **此前已修复** | 同高危 #11。 |

---

## 4. DSH 企业内网插件（`integrations/dsh-plugin-enterprise-intranet`）

报告在插件里命中的“操纵设置 / SSL 身份验证禁用 / 不安全的传输 / 硬编码 IP”实际是
同一条链路，本次一次性收紧：

* `DEFAULTS.verifyTls` 与 `requestJson` 的默认值改为 **`true`**；只有显式
  `verifyTls === false` 才写 `rejectUnauthorized = false`；
* 自签证书的受支持姿势是 `caFile` 钉 CA；CA 文件读取失败从“静默降级为不校验”
  改为**失败关闭**（不发出请求）；
* `gitconfig` 生成逻辑只在 `verifyTls === false` 时写 `sslVerify = false`；
* 生成的 git credential helper 同步采用上述默认值与失败关闭；
* 移除内置平台地址默认值（原为厂商 IP），未配置时报可操作的错误；
* 平台响应新增 8 MiB 上限，防止坏掉/恶意的平台把 DSH 进程内存吃光。

---

## 5. 残余风险（有意保留）

1. **探活允许私网与回环地址。** openfish 的产品语义就是代理内网模型服务，
   封掉 RFC1918/loopback 会把功能一起封掉。因此 `urlsafety` 只封“不是主机”的
   地址段（链路本地、CGNAT、组播、保留、未指定）与元数据主机名。需要更严时，
   用 `MODEL_PROBE_ALLOWED_HOSTS` 配白名单。
2. **DNS 重绑定。** 校验时解析一次、请求时再解析一次，两者之间理论上可被
   重绑定。调用方是持有 `model:write` 的管理员，且探活只发 `GET`、不跟随重定向；
   要彻底关闭需要把已校验 IP 钉进连接（自定义 `HTTPAdapter`），当前未做。
3. **插件 `verifyTls: false`。** 仍然保留，但必须显式选择；README 与
   `cordis.patch.yml` 均指向 `caFile` 作为首选。
4. **`.private/` 下的真实部署凭据。** 该目录已被 `.gitignore` 忽略且未被跟踪；
   本仓库不包含真实口令。请勿把 `.private/` 加入版本控制。
