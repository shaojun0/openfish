# dsh-plugin-enterprise-intranet

DSH（DeepSeek Harness）的**企业内网模式**插件。把 DSH 接到企业制品 / 模型平台
（openfish，平台地址在启用时填写，没有内置默认值）。

## 它解决什么

| 问题 | 做法 |
|---|---|
| 企业平台的 **api-key 是必选项**，没有就跑不起来 | 没有 key 时**不会**启用企业内网模式；面板把用户送到平台的设备授权页登录，登录成功后平台签发的 key **由插件自动收下**，无需复制粘贴 |
| 模型路由表里哪条是默认模型 | 用 key 读 `GET /api/v1/models/resolved`，为每条启用的**对话 / 补全**路由（`kind` 为 `chat` / `completion`）注册一个 `llm-pi-ai` provider，并把 `agent-default-model` 指向 `aliases` 含 `default` 的那条；向量化 / OCR / 语音等路由只在面板里展示 |
| 内网包源要一个个手配 | 自动写 pip / npm / apt / docker / nvm 的镜像配置 |
| git 仓库的 push 凭据 | 生成 git credential helper（`/usr/local/bin/openfish-git-credential`，`700`）+ `/etc/gitconfig`：clone/push 时用平台 key 向平台换一张**短期 Forgejo 票**（Forgejo 只认自己的 token，平台 key 本身推不上去） |
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
        platformUrl: https://registry.example.com:9443
        autoMirrors: true
        defaultAlias: default
        # 证书校验默认开启。自签部署请用 caFile 钉住 CA：
        # caFile: /etc/openfish/ca_chain.pem
        # 只有明确接受中间人风险时才关掉校验：
        # verifyTls: false
```

| 键 | 默认 | 说明 |
|---|---|---|
| `platformUrl` | `''`（必填） | 企业平台地址；不配置时启用模式会给出明确报错 |
| `apiKey` | `''` | 一般不填，让用户在面板里领 |
| `autoMirrors` | `true` | 自动写包源配置 |
| `autoGitCredential` | `true` | 自动写 git credential helper 与 `/etc/gitconfig`（同 `autoMirrors` 风格；关掉会删除 helper） |
| `defaultAlias` | `default` | 认哪个别名是默认模型 |
| `verifyTls` / `caFile` | `true` / `''` | 平台证书校验（git 侧同步写进 `/etc/gitconfig`）；默认校验，`caFile` 用于自签 CA，`verifyTls: false` 需显式选择 |
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
| POST | `/mode` | `{enabled:true|false}` 启用/停用企业内网模式；再带 `purge:true` 等价于 `/teardown` |
| POST | `/teardown` | **完全还原**：停用 + 注销 provider + 删凭据 + 删 git 助手 + 删本插件生成的包源配置 + 删状态文件。卸载前用，见下节 |
| POST | `/apply` | 重新拉取路由并应用 |
| GET | `/mirrors.json` | 包源配置预览 |
| POST | `/mirrors/apply` | 重写包源配置文件 |
| GET | `/routes.json` | 原始模型路由 |
| GET | `/catalog.json` | 工具 / 文档目录与入口链接 |
| POST | `/config` | 改 `platformUrl` / `autoMirrors` / `autoGitCredential` / `verifyTls` / `defaultAlias` |

## 停用与卸载（不残留）

**先说结论：`dsh plugin remove` 本身清不干净。** 它只是 `pnpm remove` 加重算
profile 的 bundle 层列表（`@deepseek-ai/dsh/lib/plugin-*.js`），DSH 没有插件卸载
钩子，也不会回收插件写过的设置、凭据或文件。所以卸载的正确顺序是：

```bash
# 1. 在 DSH 面板点「完全还原（卸载前）」。
#    要调端点也可以（POST /dsh-intranet/teardown，或 POST /mode 带
#    {enabled:false, purge:true}），但它和面板其它端点一样要求
#    X-DSH-Intranet-Token —— 那是只下发在 index HTML 里的 per-process CSRF
#    token，脚本里得先从页面 HTML 里取出来。
# 2. 确认面板回到「未配置 api-key」后，再移除插件：
dsh plugin --profile web remove dsh-plugin-enterprise-intranet
```

「完全还原」（`POST /teardown`、或 `POST /mode {enabled:false, purge:true}`）
依次做五件事：

1. 注销本插件注册进 `llm-pi-ai` 的 provider，并把 `agent-default-model` 还原成
   启用前的基线；
2. 删除凭据服务里的 `ENTERPRISE_INTRANET_PLATFORM_KEY` 与各条
   `ENTERPRISE_INTRANET_KEY_<SLUG>`（即平台 api-key 不再留在 `.credentials.yaml`）；
3. 删除 `/usr/local/bin/openfish-git-credential` 与 `/etc/gitconfig`；
4. 删除本插件生成的包源文件：`/etc/pip.conf`、`/usr/local/etc/npmrc`、
   `/etc/apt/sources.list.d/enterprise-intranet.list`、`/etc/docker/daemon.json`、
   `/etc/profile.d/enterprise-intranet.sh` —— **只删内容与写入时完全一致的**，
   你改过的文件会保留并在结果里列出来。清单里没有、但内容指向平台的旧版本残留
   （`mirrors_unmanaged`）只报告、不删除，因为那也可能是你自己写的内网源配置；
5. 删除状态文件 `$DSH_HOME/enterprise-intranet.json`。

与「停用」的区别：**停用是非破坏性的**（保留 key、包源配置与状态文件，方便再次
启用），**完全还原是卸载路径**。只停用就卸载，会在磁盘上留下平台 api-key 和一堆
指向内网的包源配置。

另外两条自动化：

* 插件被 **dispose**（热卸载 / 重载）时，如果它在任何 profile 的 bundle 列表与
  patch 文件里都已经不存在了，插件会自动做一次完全还原；如果仍被某个 profile
  声明（正常重启也会 dispose），则**什么都不动** —— 重启不该变成卸载。
* 手动兜底清单（DSH 之外）：删 `/usr/local/bin/openfish-git-credential`、
  `/etc/gitconfig`、`/etc/profile.d/enterprise-intranet.sh`，以及上面第 2、4、5 步
  列出的文件；`grep -rn enterprise-intranet /etc` 可以自查。

本地验证这条删文件的路径（在临时目录里跑，不碰宿主机 `/etc`，不需要 DSH）：

```bash
cd integrations/dsh-plugin-enterprise-intranet && npm test   # = node test/teardown.test.mjs
```

## 安全

* 从不把 api-key 的值返回给浏览器；面板只显示前缀与来源。
* key 存在 DSH 凭据服务（`$DSH_HOME/.credentials.yaml`）：
  `ENTERPRISE_INTRANET_PLATFORM_KEY` 与每条路由的 `ENTERPRISE_INTRANET_KEY_<SLUG>`。
* **git credential helper 里带着平台 key**，所以插件把脚本写成 `700`
  （`/usr/local/bin/openfish-git-credential`，普通用户不可读），
  `/etc/gitconfig` 只写 `helper` 指针与 `useHttpPath = true`。停用企业内网模式、
  或把 `autoGitCredential` 设为 `false` 时，插件会删除这个脚本。
* helper 交给 git 的**不是平台 key**，而是平台按需兑换的**短期 Forgejo 票**
  （默认 7 天，按用户隔离；上游只读仓拿到的是只读 scope）。票由 git 在内存里
  用完即弃，插件不落盘。
* 平台不可达 / 兑换失败时 helper **静默退出**（stdout 为空、exit 0），只在
  stderr 留一行原因——不会把 git 卡死。
* 平台证书**默认校验**（`node:https`，`verifyTls: true`）；自签部署用 `caFile`
  钉住 CA，`verifyTls: false` 需显式选择才会关闭，且不会去动
  `NODE_TLS_REJECT_UNAUTHORIZED`；git 侧对应写 `sslCAInfo` / `sslVerify`。

## 依赖

不 import 任何 `@deepseek-ai/*`：插件装在 profile 的 node_modules 下，而 DSH 内部包
嵌在 `@deepseek-ai/dsh/node_modules` 里，从插件位置解析不到。全部协作通过 `ctx`
上的 `webServer` / `settings` / `credentials` 服务完成。

## 许可

本插件随 openfish 仓库一起分发。仓库尚未声明开源许可（见根 README 的 License
一节），因此 `package.json` 标为 `license: UNLICENSED` 且 `private: true`。
要单独授权，先在本仓库补一份 LICENSE，再改这个字段。
