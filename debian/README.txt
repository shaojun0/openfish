debian/ —— Debian 包目录（脚手架）
==================================

现状
----
没有 apt 代理：这里提供本地 `.deb` 与配置片段。扁平仓库的索引就是
`/debian/Packages`，`apt` 可以按 sources.list 直接读取。

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
