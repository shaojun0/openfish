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

本目录是插件的**权威副本**（随 openfish 仓库一起版本化）。要求 **DSH ≥ 0.2.0**：
`package.json` 的 `peerDependencies` 声明了这条，DSH 的兼容性检查据此在跑错版本时
明确报错，而不是等到运行时才炸。

两种方式等价，都落到同一份 `dsh.profile.bundles`：

1. CLI（唯一支持的来源是本仓库的就地安装）：

```bash
dsh plugin --profile web add link:<openfish 仓库路径>/integrations/dsh-plugin-enterprise-intranet
```

2. 侧栏 **「插件」页**（0.2.0 新增）里安装：该页能安装、开关、卸载组合包，并展示每个
   组合包的 patch 声明与存活状态。

> `dsh plugin add` 会把声明了 `dsh.bundle.patch` 的包自动选入 profile
> （`dsh-plugin-manager` 的 `reconcile`），所以上面两条路都不必再手工编辑 profile 文件。

> 插件的 `package.json` 标了 `private: true` + `license: UNLICENSED`：本仓库尚未
> 声明开源许可（见根 README 的 License 一节），所以**不要**把它发布到公共 npm。

> 实际部署里 Docker 镜像从 `/home/linaro/dsh/enterprise-intranet/plugin` 构建
> ——那是本目录的**工作副本**，因为 Docker 不能 `COPY` 构建上下文之外的文件。
> 两份用 `integrations/sync-to-deployment.sh` 同步，改动请落在本目录。

装完重启 `dsh web`，F5 刷新：左下角出现「内网」按钮；插件页里本插件的详情页头部出现
**「完全还原（卸载前）」**按钮（客户端半边，见「依赖与客户端半边」一节）。

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

**先说结论：卸载本身清不干净 —— DSH 至今没有插件卸载钩子。** 0.2.0 的
`dsh-plugin-manager` 卸载顺序是「从 `dsh.profile.bundles` 取消选入 → 卸载运行时贡献
（即 dispose 本插件）→ `pnpm remove`」，而唯一的 `plugin-manager/changed` 事件是在
这些步骤**全部完成之后**才发出的 —— 那时插件已经听不到了。`dsh-host-plugin-inventory`
是只读的，`plugins.detail.actions` 是个 UI 槽位而不是钩子。所以插件侧那段「dispose 时
检查自己是否还在 profile 的声明里」的兜底仍然必要（见 `lib/index.js` 的
`pluginStillDeclared()`）。

正确顺序（0.2.0 起两条等价入口）：

1. **先完全还原**：插件页的本插件详情页头部点「完全还原（卸载前）」（0.2.0 新增的
   客户端半边），或在下方面板点同名按钮。要调端点也可以
   （`POST /dsh-intranet/teardown`，或 `POST /mode` 带 `{enabled:false, purge:true}`）
   —— 它和面板其它端点一样要求 `X-DSH-Intranet-Token`（只下发在 index HTML 里的
   per-process CSRF token）；详情页那个按钮从同一份 boot 负载里自己取，走 UI 时
   不必手工抄 token。
2. **再卸载**：插件页里卸载，或：

```bash
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
* key 存在 DSH 凭据服务，落盘在 `$DSH_HOME/.credentials.yaml`：
  `ENTERPRISE_INTRANET_PLATFORM_KEY` 与每条路由的 `ENTERPRISE_INTRANET_KEY_<SLUG>`。
  该文件在 0.2.0 里是 `version: 1` 的带节文档（`refs:` / `records:`）。插件只经
  `ctx.credentials` 读写、从不自己解析它，而旧版扁平格式由 `dsh-credentials-local`
  在启动时原地迁移，所以这个格式变化不影响插件。
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

## 依赖与客户端半边

宿主侧**不 import 任何 `@deepseek-ai/*`**，全部协作通过 `ctx` 上的服务完成：
`webServer` / `settings` / `credentials` / `logger`，以及 0.2.0 起优先使用的
`agentDefaultModel`（默认模型选择的正式写入路径；`settings()` 那套仍是回退路径）。

> 早先这里写的理由是「内部包从插件位置解析不到」。实测不准确：profile 的
> `node_modules` 里有 pnpm 提升出来的 `@deepseek-ai/*` 软链，`require.resolve`
> 在 profile 目录下是成功的。真实约束是**不该**依赖内部包（它们不是给第三方插件的
> 稳定接口），而不是**不能** —— 上面的写法依旧照此执行。

**客户端半边**（`lib/client.js`）：按 0.2.0 的客户端模块契约注册一个 id 等于包名的
惰性工厂（`window.__ModuleLoader__.load`，React 从浏览器模块表取），因此本插件**仍然
是零构建**的 —— 没有打包步骤，也没有 node_modules 运行时依赖。它只贡献一件事：
`plugins.detail.actions` 槽位上的「完全还原（卸载前）」按钮。

宿主侧用 0.2.0 的**结构化 index 注入**（`webserver/index-inject` 事件的 `global` 与
`script-src` 行）取代了原先的 `tapIndex` 字符串改写，把按钮所需的 boot 负载（含 CSRF
token）下发到页面；`tapIndex` 仍是官方保留的逃生通道。

## 许可

本插件随 openfish 仓库一起分发。仓库尚未声明开源许可（见根 README 的 License
一节），因此 `package.json` 标为 `license: UNLICENSED` 且 `private: true`。
要单独授权，先在本仓库补一份 LICENSE，再改这个字段。
