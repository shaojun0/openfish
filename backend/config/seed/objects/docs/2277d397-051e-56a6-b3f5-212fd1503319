# Debian / apt 生态使用指南

本服务器提供两种 apt 使用形态：**扁平本地仓库** 与 **`dists/`/`pool/` 穿透镜像**。

## 扁平本地仓库

`DEBIAN_DIR` 下的 `.deb` 会被渲染成 `/debian/Packages`，对应软件源：

```bash
echo "deb [trusted=yes] http://<server>/debian/ ./" \
  | sudo tee /etc/apt/sources.list.d/openfish.list
sudo apt update
sudo apt install openfish-hello
```

## 标准镜像源（需配置上游）

设置 `DEBIAN_UPSTREAM` 后，`dists/` 与 `pool/` 会被代理，元数据按
`DEBIAN_METADATA_TTL` 缓存，`.deb` 以流式转发（支持 `Range` 断点续传）：

```bash
echo "deb http://<server>/debian bookworm main" \
  | sudo tee /etc/apt/sources.list.d/openfish.list
```

## 相关页面

| 页面 | 路径 | 说明 |
| ---- | ---- | ---- |
| Debian 目录 | `/debian` | 本地 `.deb` 与 apt 配置片段 |
| Debian 静态索引 | `/debian/` | 扁平 `Packages` 与镜像代理入口 |
