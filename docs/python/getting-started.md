# Python 生态使用指南

本页介绍如何把本服务器当作内网 Python 包索引使用。文档为 Markdown，
**只有管理员可以上传 `.md` 文件来修改内容**，其他用户仅可查看和下载。

## 安装包

```bash
# pip
pip install -i http://<server>/simple/ --trusted-host <server> <package>

# uv（推荐，速度更快）
export UV_INDEX_URL=http://<server>/simple/
uv pip install <package>
```

需要 `package:read` 权限，凭据可用 API key（用户名固定为 `__token__`）。

## 上传包

```bash
twine upload --repository-url http://<server>/simple/ dist/*
```

上传需要 `package:write` 权限。

## 预编译解释器

`python-build-standalone` 镜像让离线机器也能安装 CPython：

```bash
export UV_PYTHON_INSTALL_MIRROR=http://<server>/python-builds/
uv python install 3.12
```

## 相关页面

| 页面 | 路径 | 说明 |
| ---- | ---- | ---- |
| 包管理 | `/packages` | 浏览已上传的包 |
| Python 索引 | `/simple/` | PEP 503 / PEP 691 机器接口 |
| Python 构建 | `/python-builds/` | 预编译 CPython 列表 |
