node-builds/ —— Node.js 预编译镜像（nodejs.org/dist 布局）
============================================================

用途
----
把 nvm / fnm / node-gyp 的镜像指向本服务，即可在内网安装 Node.js 运行时：

    export NVM_NODEJS_ORG_MIRROR=http://<openfish>/node-builds
    nvm install 20

    export FNM_NODE_DIST_MIRROR=http://<openfish>/node-builds
    fnm install 20

对应的服务端端点在 routes/node_build.py；完整说明见根目录 README.md 的
「Prebuilt interpreter mirrors」一节。

目录约定（与 nodejs.org/dist 一致）
-----------------------------------
    node-builds/
      index.json                 可选：官方 index.json，用于补齐 lts/date/npm 等字段
      index.tab                  可选：官方 index.tab（同上，制表符形式）
      v20.11.0/
        node-v20.11.0-linux-x64.tar.xz
        node-v20.11.0-darwin-arm64.tar.gz
        node-v20.11.0-win-x64.zip
        SHASUMS256.txt           可选：存在则原样返回，缺失则按实际文件生成
      v22.14.0/
        ...

规则
----
* 一级子目录名必须是版本号（vX.Y.Z，可带 rc/beta/nightly 后缀），否则整个
  目录被忽略；
* 文件名必须是 node-v<版本>-<平台>[-<架构>].<扩展名>（tar.gz / tar.xz /
  tar.bz2 / zip / 7z / msi / pkg），否则不会被索引；node-v<版本>-headers.tar.gz
  这类没有架构的文件同样支持；
* 服务端只列出磁盘上真实存在的文件，所以客户端不会被指向一个 404；
* 只有 *文件* 是事实来源：index.json 只是补充元数据，不会让磁盘上不存在的
  版本出现在版本列表里。

同步示例
--------
    # 用仓库自带的 helper（默认同步 linux-x64 / linux-arm64）：
    tools/net/node-builds-mirror.sh 20.11.0 22.14.0

    # 或者直接 rsync nodejs.org 的发布树：
    rsync -av --include='*/' \
          --include='node-v*-linux-x64.tar.xz' \
          --include='SHASUMS256.txt' --exclude='*' \
          rsync://rsync.nodejs.org/nodejs-release/ ./node-builds/

服务端端点
----------
    GET /node-builds/                        版本列表（HTML；?format=json 为 JSON）
    GET /node-builds/index.json              nvm / fnm 读取的版本清单
    GET /node-builds/index.tab               同一清单的制表符形式
    GET /node-builds/<tag>/                  某个版本（tag 可为 latest、latest-v20.x）
    GET /node-builds/<tag>/SHASUMS256.txt    校验和
    GET /node-builds/<tag>/<文件名>          下载
    GET /node-builds/<tag>/<文件名>/sha256   单个产物的 SHA256
    GET /node-builds/health                  镜像状态（无需认证）
    GET /api/v1/node-builds                  SPA 目录页使用的 JSON
