tools/ —— 内网工具目录（脚手架）
================================

目录结构
--------
    tools/
      catalog.json          可选的展示覆盖层（分类名、工具说明、标签）
      <分类>/               一级子目录即一个分类
        <文件>              文件本身就是一个可下载工具

新增一个工具
------------
    1. 把文件放进某个分类目录，例如 tools/ops/backup.sh
    2. （可选）在 catalog.json 的 tools 里补上说明与标签
    3. 刷新 /tools 页面即可看到，并可直接点击下载

约定
----
* 以 "." 开头的文件、catalog.json 以及 README/LICENSE/CHANGELOG 不会被列出。
* 文件名即下载名；SHA256 会在列表中展示（超过 64MB 的文件跳过计算）。
* 下载地址形如 /tools/<分类>/<文件名>，需要 tool:download 权限。
* 真实部署时该目录通常以只读方式挂载进容器，改文件不需要重建镜像。

静态索引
--------
/tools/ 是服务端渲染的静态索引（模板 static/tools/index.html，仅服务端读取，不经 HTTP 暴露）：
    GET /tools/                   HTML 索引，浏览器/脚本都能读
    GET /tools/?format=json       与 /api/v1/tools 相同的 JSON
两者都需要 tool:read 权限。控制台里的「静态索引」按钮也指向这里。
