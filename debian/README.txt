debian/ —— Debian 包目录与 apt 镜像代理
========================================

现状
----
两条路都可以走：

1. **本地扁平仓库**：这里放 `.deb` 与配置片段。索引就是 `/debian/Packages`，
   由实际存在的 `.deb` 生成，`apt` 可按 sources.list 直接读取。
2. **镜像代理**：配置 `DEBIAN_UPSTREAM`（如 http://deb.debian.org/debian）后，
   `/debian/dists/*` 与 `/debian/pool/*` 会从上游按需回源——元数据带 TTL 缓存，
   `.deb` 流式转发。此时可以直接写 `deb http://<本服务>/debian bookworm main`。

文件约定
--------
* `<包名>_<版本>_<架构>.deb`   文件名会被解析成 包名 / 版本 / 架构
* `*.list`、`*.sources`、`*.example`、`*.gpg`、`*.key`   配置片段，作为附件下载
* `catalog.json`               展示覆盖层与"仅元数据"登记项
* 以 "." 开头、以及 README/LICENSE/CHANGELOG 不会被列出

静态索引
--------
    GET /debian/                  HTML 索引（模板 static/debian/index.html）
    GET /debian/?format=json      与 /api/v1/debian 相同的 JSON
    GET /debian/Packages          扁平静态索引，由本地 .deb 生成
    GET /debian/files/<文件名>    下载 .deb（需要 debian:download 权限）

客户端接入
----------
    sudo apt install ./<包名>.deb
    # 或作为扁平源： /etc/apt/sources.list.d/openfish.list
    deb [trusted=yes] http://<openfish>/debian/ ./
