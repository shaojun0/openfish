docker-images/ —— Docker 离线制品目录与 Registry v2 代理
========================================================

现状
----
两条路都可以走：

1. **在线拉取**：配置 `DOCKER_UPSTREAM`（如 https://registry-1.docker.io 或内网
   registry）后，`docker pull` 可直接指向本服务。清单与 blob 按需回源并缓存，
   blob 以 digest 寻址、永久有效。见 `routes/docker.py` 里的 `/docker/v2/*`。
2. **离线导入**：把 `docker save` 的镜像 tar 放进本目录，下载后
   `docker load -i <文件>.tar` 导入。

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
    GET /docker/v2/<name>/manifests/<ref>    清单（需要 docker:read 权限）
    GET /docker/v2/<name>/blobs/<digest>     层/配置实体（需要 docker:download 权限）
    GET /docker/files/<文件名>    下载制品（需要 docker:download 权限）

`/v2/_catalog` 是 registry 协议里唯一的枚举端点，等价于 npm 的 `/-/all`。
清单是元数据、blob 是实体字节，所以权限点分开：只授 docker:read 的角色能
浏览目录与清单，但拉不动镜像层。
