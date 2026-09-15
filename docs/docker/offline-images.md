# Docker 生态使用指南

本服务器实现了 Docker Registry v2 的拉取协议，并托管 `docker save` 离线镜像。

## 配置客户端

内网机器把本服务器当作 registry 使用：

```bash
docker pull <server>/library/nginx:1.25
```

若配置了 `DOCKER_UPSTREAM`，未命中的镜像会回源拉取并缓存 manifest 与 blob；
留空则只服务本地已有内容。

## 离线镜像

`docker save` 导出的 tar 放在 `DOCKER_DIR`，即可在无外网的机器上导入：

```bash
curl -O http://<server>/docker/files/nginx-1.25.3.tar
docker load -i nginx-1.25.3.tar
```

## 相关页面

| 页面 | 路径 | 说明 |
| ---- | ---- | ---- |
| Docker 目录 | `/docker` | 镜像 tar 与 compose/Dockerfile 清单 |
| Docker 静态索引 | `/docker/` | Registry v2 协议入口 |
| 镜像目录 | `/docker/v2/_catalog` | OCI `_catalog` 仓库列表 |
