npm/ —— 本地 npm 目录（脚手架）
===============================

现状
----
npm registry 协议（packument / version manifest / tarball 协商）**尚未实现**。
当前提供的是本地目录的**静态索引**，可以直接浏览和脚本抓取：

    GET /npm/               HTML 索引（模板 static/npm/index.html）
    GET /npm/?format=json   全量索引 JSON
    GET /npm/-/all          全量索引 JSON（npm 旧版约定，见下）
    GET /npm/-/ping         健康检查，返回 {}

为什么是 /-/all
---------------
npm 官方没有 HTML 索引页；它的 registry 是 JSON 优先（每个包一个 packument）。
最接近"静态全量索引"的公开约定是旧版 `GET /-/all`：一个以包名为键、值为
`dist-tags` + `versions` 的 JSON 对象。npm 官方在 2017 年下线了它，改用
`GET /-/v1/search` 与复制流，但私有 registry（Verdaccio、cnpm 等）普遍仍保留，
所以这里按同样的形状输出。`/-/v1/search` 是后续要补的现代替代。

文件约定
--------
* *.tgz                      文件名会被解析成 <name>-<version>.tgz，可直接下载
* catalog.json               {"packages": [{"name": ..., "version": ..., "description": ...}]}
* 以 "." 开头的文件会被忽略

客户端接入（代理实现后生效）
----------------------------
    npm config set registry http://<openfish>/npm/
    npm ping
