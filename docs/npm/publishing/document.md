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

## 相关页面

| 页面 | 路径 | 说明 |
| ---- | ---- | ---- |
| npm 目录 | `/npm` | 本地 npm 包与 Node 构建（下拉切换） |
| npm 静态索引 | `/npm/` | registry 协议入口 |
| Node 构建 | `/node-builds/` | `nodejs.org/dist` 镜像 |
