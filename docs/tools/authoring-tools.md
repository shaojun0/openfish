# 工具目录维护指南

“工具”生态用于分发内网脚本与小工具，落地方式与其它生态一致：
**文件系统即目录**，把文件放进去就完成发布。

## 目录结构

```
tools/
  catalog.json          # 可选：覆盖显示名、描述、标签
  dev/                  # 一个分类（目录名即分类 key）
    fmt.sh
  ops/
    check-health.sh
```

分类下可以有子目录，文件会递归列出。

## 覆盖元数据

```json
{
  "categories": {
    "ops": { "name": "运维脚本", "description": "日常巡检与备份", "icon": "Tools" }
  },
  "tools": {
    "ops/check-health.sh": { "name": "健康检查", "tags": ["ops"] }
  }
}
```

## 权限

| 操作 | 权限点 |
| ---- | ------ |
| 浏览分类与文件清单 | `tool:read` |
| 下载文件 | `tool:download` |

## 相关页面

| 页面 | 路径 | 说明 |
| ---- | ---- | ---- |
| 工具目录 | `/tools` | 分类浏览与下载 |
| 工具静态索引 | `/tools/` | 无 JavaScript 的机器接口 |
