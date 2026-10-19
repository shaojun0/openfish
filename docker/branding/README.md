# docker/branding —— 自定义品牌 logo（可选）

把图片放进本目录，前端容器会直接提供，**不需要重建镜像**。`docker-compose.yml`
把本目录挂到 frontend 容器的 `/usr/share/nginx/html/branding/`（只读），frontend 的
nginx 按文件返回（`frontend/nginx.conf` 的 `location /branding/`）。

## 文件命名

SPA（`frontend/src/components/AppSidebar.vue`）按主题、按下面的顺序探测，第一个能
加载的就用于侧边栏品牌位：

| 优先级 | 深色主题（默认） | 浅色主题 |
| --- | --- | --- |
| 1 | `logo-dark.svg` | `logo-light.svg` |
| 2 | `logo-dark.png` | `logo-light.png` |
| 3 | `logo.svg` | `logo.svg` |
| 4 | `logo.png` | `logo.png` |

全都没有（或加载失败）时回退到内置图标 + 品牌名 `SERVER_NAME`（见 `docker/.env`）。
只提供一个通用版本时，放 `logo.svg` / `logo.png` 即可，但**深色底上的可读性要自己
保证**——默认主题是深色。

## 尺寸与配色

- 侧边栏品牌位高 56px、内容宽 180px；logo 按 `height: 30px; max-width: 100%` 展示，
  建议宽高比 **5.5:1 ~ 7:1**，PNG 用 2×（例如 800×130 或 900×150）。
- 用**透明背景**；深色版用浅色文字、提亮品牌色；浅色版用深色文字。
- 侧边栏折叠（64px）时会回退到内置图标，避免宽版 logo 缩得看不清。

## 生效方式

```bash
# 第一次：挂载目录（compose 会读取本目录；之后替换文件不需要再执行）
docker compose up -d frontend

# 之后：直接覆盖文件，刷新页面
cp ~/agent-ecosystem-logo-dark.png  docker/branding/logo-dark.png
cp ~/agent-ecosystem-logo-light.png docker/branding/logo-light.png
```

`/branding/` 带 `Cache-Control: no-cache`，正常刷新就能看到新文件；如果浏览器还缓存
旧图，Ctrl/Cmd+Shift+R 强制刷新。

## 说明

- 本目录只跟踪这个 README；logo 图片属于部署方资产，按需放入，不要提交进仓库。
- 品牌名（浏览器标签页、`/health` 的 `server` 字段、回退文字）走 `docker/.env` 的
  `SERVER_NAME`，只重启 backend 即可，不参与前端构建。
