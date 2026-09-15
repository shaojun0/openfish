# 模型路由（下游 DSH）

`MODELS_FILE`（默认 `config/model_routes.json`）描述下游内网 DSH
可以指向的模型端点。本服务器只**登记、展示并检测连通性**，不代理推理流量。
持有 `model:write` 的管理员可以直接在 `/models` 页面维护这张表，无需登录服务器改文件。

## 文件格式

```json
{
  "version": 1,
  "routes": [
    {
      "name": "qwen2.5-7b",
      "provider": "openai",
      "base_url": "http://10.0.0.8:8000",
      "api_key": "",
      "model": "Qwen/Qwen2.5-7B-Instruct",
      "aliases": ["qwen", "fast"],
      "path": "/v1/chat/completions",
      "enabled": true,
      "description": "内网主力对话模型"
    }
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
| ---- | ---- | ---- |
| `name` | 是 | 路由名称，全局唯一；`probe` 为系统保留字 |
| `description` | 是 | 路由描述，便于他人识别用途 |
| `provider` | 否 | 调用格式：`openai` / `mineru` / `anthropic`，默认 `openai` |
| `base_url` | 是 | 形如 `http(s)://主机[:端口]` 的地址 |
| `api_key` | 否 | 下游鉴权用密钥，可以为空 |
| `path` | 否 | 端点路径；留空按格式取默认值：`/v1/chat/completions`、`/file_parse`、`/v1/messages` |
| `model` | 否 | 模型 ID |
| `aliases` | 否 | 供 DSH 使用的别名列表 |
| `enabled` | 否 | 是否发布给下游，默认 `true` |

## 在线维护与连通性检测

页面顶部“新增路由”会在表格第一行弹出编辑行，填写后保存即写入
`MODELS_FILE`（原子写入，不会出现半截文件），随后服务器会立即对该
URL 发起一次 `GET` 探测——只判断“是否通畅”，不会发送推理请求。
检测结果按状态着色：`通畅`（2xx/3xx）、`需鉴权`（401/403）、
`方法受限`（405）、`路径不存在`（404）、`服务异常`（5xx）、`不通`。
每行可“重新检测”，也可点“检测全部”。

检测结果保存在 `MODEL_HEALTH_FILE`（默认 `data/model_health.json`），
按路由名记录，不写进路由表本身，保证下游读取的文档始终是纯路由数据。

> 注意：接口**从不返回** `api_key` 原文，只返回是否已设置与末四位提示；
> 编辑时留空表示保持原密钥，勾选“清除密钥”才会删除。

## 下游使用

```bash
export DSH_MODEL_BASE_URL=http://10.0.0.8:8000/v1
```

`GET /api/v1/models` 返回解析后的路由表（含 `exists` / `error` / `providers`
字段，文件缺失不会报错）。浏览需要 `model:read` 权限，增删改与检测需要
`model:write` 权限（默认仅内置管理员角色持有）。

## 相关页面

| 页面 | 路径 | 说明 |
| ---- | ---- | ---- |
| 模型路由 | `/models` | 路由表、在线增删改与连通性检测、可粘贴的配置片段 |
| 路由表 JSON | `/api/v1/models` | 机器可读的路由表（管理员可 POST/PUT/DELETE） |
