# integrations/

面向**本平台下游客户端**的集成代码。这些不是服务端的一部分，不会进入
`cpypiserver` 镜像（见 `.dockerignore` 里的 `integrations/`），但它们与平台
的接口是强耦合的，所以和平台放在同一个仓库里一起版本化。

## `dsh-plugin-enterprise-intranet/`

DSH（DeepSeek Harness）的**企业内网模式**插件。它让 DSH：

1. **用 api-key 接入平台**（api-key 是必选项）。没有 key 时插件不启用模式，
   而是把用户送到平台的设备授权页登录，**登录成功后平台签发的 key 由插件自动
   收下**，不需要复制粘贴。
2. **自动接入模型路由的默认模型**：读 `GET /api/v1/models/resolved`，为每条启用
   的路由注册一个 `llm-ai` provider，并把 `agent-default-model` 指向 `aliases`
   含 `default` 的那条。
3. **切换内网包源**：pip / npm / apt / docker / nvm 的配置一次写好。
4. **接上工具与文档目录**：面板里列出 `/api/v1/tools`、`/api/v1/docs`。

### 它依赖平台的哪些接口

| 平台能力 | 端点 | 权限 |
| --- | --- | --- |
| 设备授权（登录后自动发 key） | `POST /api/v1/device/code`、`POST /api/v1/device/token`、`GET /device`、`POST /device/approve` | 前两个匿名；批准需 `key:create` |
| 模型路由（含上游 key） | `GET /api/v1/models/resolved` | `model:resolve` |
| 模型路由（掩码，控制台用） | `GET /api/v1/models` | `model:read` |
| 工具 / 文档目录 | `GET /api/v1/tools`、`GET /api/v1/docs` | `tool:read` / `doc:read` |
| 身份探测 | `GET /api/v1/session` | 匿名（返回 `authenticated:false`） |

这些接口由 `routes/device.py`、`services/device_auth.py` 和
`services/model_routes.py::resolve` 提供，回归门槛是
`scripts/check_device_flow.py`。

### 安装

```bash
# 从本仓库就地安装（推荐，随仓库一起更新）
dsh plugin --profile web add link:<本仓库路径>/integrations/dsh-plugin-enterprise-intranet
dsh web
```

装完刷新浏览器，界面左下角出现「内网」按钮。

### 与部署工作副本的关系

`/home/linaro/dsh/enterprise-intranet/` 那份 DSH 部署目录里有一份**工作副本**
（`plugin/`），Docker 镜像从它构建。**本目录是纳入版本管理的权威副本**；
两边内容必须一致，用 `sync-to-deployment.sh` 同步，避免悄悄漂移：

```bash
# 本目录（权威） -> 部署目录（构建用）
./integrations/sync-to-deployment.sh

# 反向：部署目录 -> 本目录（若你在部署侧直接改了代码）
./integrations/sync-to-deployment.sh --from-deployment
```

同步后重新构建镜像并重启容器（部署目录里的 `deploy/run-dsh.sh --rebuild`）。

## 许可

这些集成代码随 openfish 仓库一起分发。仓库尚未声明开源许可（见根 README 的
License 一节），所以插件的 `package.json` 标为 `license: UNLICENSED` 且
`private: true` —— **不要**把它发布到公共 npm。要单独授权，先在本仓库补一份
LICENSE，再改那个字段。
