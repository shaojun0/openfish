# 模型路由（下游 DSH）

`MODELS_FILE`（默认 `config/model_routes.json`）描述下游内网 DSH
可以指向的模型端点。本服务器只**发布这张表**，不代理推理流量。

## 文件格式

```json
{
  "version": 1,
  "routes": [
    {
      "name": "qwen2.5-7b",
      "provider": "openai-compatible",
      "base_url": "http://10.0.0.8:8000/v1",
      "model": "Qwen/Qwen2.5-7B-Instruct",
      "aliases": ["qwen", "fast"],
      "enabled": true
    }
  ]
}
```

## 下游使用

```bash
export DSH_MODEL_BASE_URL=http://10.0.0.8:8000/v1
```

`GET /api/v1/models` 返回解析后的路由表（含 `exists` / `error` 字段，
文件缺失不会报错）。浏览需要 `model:read` 权限。

## 相关页面

| 页面 | 路径 | 说明 |
| ---- | ---- | ---- |
| 模型路由 | `/models` | 路由表与可粘贴的配置片段 |
| 路由表 JSON | `/api/v1/models` | 机器可读的路由表 |
