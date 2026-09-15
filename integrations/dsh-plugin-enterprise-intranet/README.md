# dsh-plugin-enterprise-intranet

DSH（DeepSeek Harness）的**企业内网模式**插件。把 DSH 接到企业制品 / 模型平台
（openfish，默认 `https://47.97.243.86:9443`）。

## 它解决什么

| 问题 | 做法 |
|---|---|
| 企业平台的 **api-key 是必选项**，没有就跑不起来 | 没有 key 时**不会**启用企业内网模式；面板把用户送到平台的设备授权页登录，登录成功后平台签发的 key **由插件自动收下**，无需复制粘贴 |
| 模型路由表里哪条是默认模型 | 用 key 读 `GET /api/v1/models/resolved`，为每条启用的路由注册一个 `llm-pi-ai` provider，并把 `agent-default-model` 指向 `aliases` 含 `default` 的那条 |
| 内网包源要一个个手配 | 自动写 pip / npm / apt / docker / nvm 的镜像配置 |
| 工具与文档入口 | 面板列出平台的 `/api/v1/tools`、`/api/v1/docs` 与各生态页面 |

## 安装

本目录是插件的**权威副本**（随 openfish 仓库一起版本化）。就地安装：

```bash
# 从本仓库就地安装（唯一支持的安装方式）
dsh plugin --profile web add link:<openfish 仓库路径>/integrations/dsh-plugin-enterprise-intranet
```

> 插件的 `package.json` 标了 `private: true` + `license: UNLICENSED`：本仓库尚未
> 声明开源许可（见根 README 的 License 一节），所以**不要**把它发布到公共 npm。

> 实际部署里 Docker 镜像从 `/home/linaro/dsh/enterprise-intranet/plugin` 构建
> ——那是本目录的**工作副本**，因为 Docker 不能 `COPY` 构建上下文之外的文件。
> 两份用 `integrations/sync-to-deployment.sh` 同步，改动请落在本目录。

装完重启 `dsh web`，F5 刷新，左下角出现「内网」按钮。

## 配置

运行状态在 `$DSH_HOME/enterprise-intranet.json`，也可以在 `cordis.patch.yml`
的 `config:` 里预置：

```yaml
- insert:
    - id: enterprise-intranet
      name: dsh-plugin-enterprise-intranet
      config:
        platformUrl: https://47.97.243.86:9443
        autoMirrors: true
        defaultAlias: default
        verifyTls: false      # 平台自签证书；装了 CA 后可设 caFile 强校验
```

| 键 | 默认 | 说明 |
|---|---|---|
| `platformUrl` | `https://47.97.243.86:9443` | 企业平台地址 |
| `apiKey` | `''` | 一般不填，让用户在面板里领 |
| `autoMirrors` | `true` | 自动写包源配置 |
| `defaultAlias` | `default` | 认哪个别名是默认模型 |
| `verifyTls` / `caFile` | `false` / `''` | 平台证书校验 |
| `requestTimeoutMs` | `20000` | 平台请求超时 |

## 宿主侧 HTTP 端点

全部前缀 `/dsh-intranet`，且都要求 `X-DSH-Intranet-Token`（只在 index HTML 里
下发的 per-process CSRF token）：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/state.json` | 状态快照（不含 key 明文） |
| POST | `/login/start` | 发起设备授权 |
| GET | `/login/poll?login_id=` | 轮询；批准后自动落 key 并应用模式 |
| POST | `/login/cancel` | 取消 |
| POST | `/key` | 手工提交一枚 key（校验后落库） |
| POST | `/mode` | `{enabled:true|false}` 启用/停用企业内网模式 |
| POST | `/apply` | 重新拉取路由并应用 |
| GET | `/mirrors.json` | 包源配置预览 |
| POST | `/mirrors/apply` | 重写包源配置文件 |
| GET | `/routes.json` | 原始模型路由 |
| GET | `/catalog.json` | 工具 / 文档目录与入口链接 |
| POST | `/config` | 改 `platformUrl` / `autoMirrors` / `verifyTls` / `defaultAlias` |

## 安全

* 从不把 api-key 的值返回给浏览器；面板只显示前缀与来源。
* key 存在 DSH 凭据服务（`$DSH_HOME/.credentials.yaml`）：
  `ENTERPRISE_INTRANET_PLATFORM_KEY` 与每条路由的 `ENTERPRISE_INTRANET_KEY_<SLUG>`。
* 平台自签证书用 `node:https` 的 `rejectUnauthorized` / `ca` 处理，
  不会去动 `NODE_TLS_REJECT_UNAUTHORIZED`。

## 依赖

不 import 任何 `@deepseek-ai/*`：插件装在 profile 的 node_modules 下，而 DSH 内部包
嵌在 `@deepseek-ai/dsh/node_modules` 里，从插件位置解析不到。全部协作通过 `ctx`
上的 `webServer` / `settings` / `credentials` 服务完成。

## 许可

本插件随 openfish 仓库一起分发。仓库尚未声明开源许可（见根 README 的 License
一节），因此 `package.json` 标为 `license: UNLICENSED` 且 `private: true`。
要单独授权，先在本仓库补一份 LICENSE，再改这个字段。
