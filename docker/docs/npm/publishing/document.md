# npm 生态使用指南

本服务器同时提供 **npm registry 协议** 与 `nodejs.org/dist` 形态的 Node.js 预编译镜像。

## 指向本服务器

```bash
npm config set registry http://<server>/npm/
npm install lodash
```

本地已缓存的 tarball 会被优先使用；未命中的包由 `NPM_UPSTREAM`
回源并缓存，因此首次安装后内网即可离线复用。

## Node.js 预编译镜像

`nvm`、`fnm`、`node-gyp` 都可以直接使用：

```bash
export NODEJS_ORG_MIRROR=http://<server>/node-builds/
nvm install 20
```

## 本地 npm 目录

把 `*.tgz` 放进 `NPM_DIR`，或在 `NPM_DIR/catalog.json` 中登记条目，
刷新页面即可看到。该目录的浏览需要 `npm:read` 权限。

## 发布包（npm publish）

`PUT /npm/<包名>` 就是 `npm publish` 写入的接口，需要 `npm:publish` 权限
（已认证用户默认持有，可在 `/access` 单独撤销）。认证用 `.npmrc` 里的
Basic 口令或 API key：

```bash
npm config set registry http://<server>/npm/
# 方式一：用户名 + 口令（Basic）
npm config set //<server>/npm/:_auth "$(printf 'admin:%s' "$PASSWORD" | base64 -w0)"
# 方式二：API key（Bearer，在 Web 控制台「API 密钥」页签发）
npm config set //<server>/npm/:_authToken "<api-key>"

cd your-package
npm publish              # 发布 latest
npm publish --tag next   # 发布到 next 标签，之后 npm install pkg@next 可解析
```

服务端会逐项校验后才落盘：包名与 URL 必须一致、tarball 必须是
可解析的 `.tgz` 且其中的 `package/package.json` 的 `name`/`version`
与所发布的版本一致、文档里声明的 `shasum`/`integrity` 必须与字节实际
哈希一致。通过后 tarball 以 npm 的扁平命名 `<包名>-<版本>.tgz`
（`@scope/name` 去掉 scope）原子写入 `NPM_DIR`，dist-tag 记在
`NPM_DIR/publish.json`。

已存在的版本会被拒绝（`409`，已发布版本不可变）；极少数情况下需要在
服务端放开覆盖，可把 `backend/.env`（或 compose 的 `.env`）中的
`OVERWRITE=true` 打开——这与 twine 上传共用同一个开关。

> 该接口是**写**接口，只影响 npm 目录；Docker、Debian、Python 的上传
> 能力各自独立，权限点也分开，便于按需收口。

## 相关页面

| 页面 | 路径 | 说明 |
| ---- | ---- | ---- |
| npm 目录 | `/npm` | 本地 npm 包与 Node 构建（下拉切换） |
| npm 静态索引 | `/npm/` | registry 协议入口 |
| Node 构建 | `/node-builds/` | `nodejs.org/dist` 镜像 |
