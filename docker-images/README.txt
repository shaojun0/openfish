docker-images/ —— Docker 离线制品目录（脚手架）
===============================================

现状
----
没有 registry 代理：`docker pull` 还不能指向本服务。这里放的是离线制品，
下载后 `docker load -i <文件>.tar` 导入。

文件约定
--------
* `<name>-<tag>.tar`、`<name>-<tag>.tar.gz`、`<name>-<tag>.tgz`
      `docker save` 出来的镜像，文件名会被解析成 名称 + 标签
* `*.yml`、`*.yaml`                        compose 片段，作为普通附件下载
* `Dockerfile*`                            Dockerfile 片段
* `catalog.json`                           展示覆盖层与"仅元数据"登记项
* 以 "." 开头、以及 README/LICENSE/CHANGELOG 不会被列出

静态索引
--------
    GET /docker/                  HTML 索引（模板 static/docker/index.html）
    GET /docker/?format=json      与 /api/v1/docker 相同的 JSON
    GET /docker/v2/_catalog       仓库名列表，OCI distribution spec 形状
    GET /docker/files/<文件名>    下载制品（需要 docker:download 权限）

`/v2/_catalog` 是 registry 协议里唯一的枚举端点，等价于 npm 的 `/-/all`。
