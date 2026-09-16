# Debian 离线更新中继（互联网 ↔ 内网）

当内网无法访问任何 apt 镜像时，可以用「两套 openfish」把更新搬运进去：互联网侧
的 openfish 能看到上游镜像，内网侧的 openfish 只服务本地 `.deb`。两者之间不建
立网络连接，只靠人工拷贝三个文件完成同步。

```
 互联网 openfish                          内网 openfish
 ┌───────────────┐                       ┌───────────────┐
 │ apt 上游镜像   │                       │ DEBIAN_DIR/   │
 │ dists/ pool/  │                       │   *.deb       │
 └──────┬────────┘                       └──────┬────────┘
        │ ① snapshot.txt（文本快照）              │
        └──────────────────────────────────────▶ │ ② 对比本机仓库
                                                │    → plan.txt（待更新清单）
        ┌───────────────────────────────────────┘
        │ ③ plan.txt
        ▼
  按清单下载依赖 → bundle.tar.gz（离线更新包）
        │
        └──────────────────────────────────────▶ ④ 导入 → 本地仓库更新完成
```

四步各自只产出一个文件，服务端不在步骤之间隐藏状态；任何一步中断，重跑即可。

## 一、准备

- **互联网侧**：配置 `DEBIAN_UPSTREAM`（例如 `http://deb.debian.org/debian`），
  并确认 `DEBIAN_SUITES` / `DEBIAN_COMPONENTS` / `DEBIAN_ARCHES` 指向你需要的
  发行版、组件与架构。
- **内网侧**：`DEBIAN_DIR` 中放有本地仓库；apt 客户端使用扁平源：

  ```bash
  echo "deb [trusted=yes] http://<内网openfish>/debian/ ./" \
    | sudo tee /etc/apt/sources.list.d/openfish.list
  ```

- 两侧都需要一个 API 密钥（Web 控制台「API 密钥」页签发），并具备相应权限：

  | 权限点 | 用途 |
  | ------ | ---- |
  | `debian:offline` | 导出快照、生成待更新清单、构建离线包 |
  | `debian:download` | 下载已构建的离线包 |
  | `debian:upload` | 导入离线包（写入本地仓库，默认仅管理员） |

## 二、命令行（推荐）

安装后自带 `cpypiserver-debian-offline`：

```bash
export OPENFISH_URL=http://openfish.intra        # 含路由前缀（如有）
export OPENFISH_API_KEY=cpypi_xxxxxxxx
# 没有 API 密钥时也可以用控制台账号做 HTTP Basic：
# export OPENFISH_USER=dev OPENFISH_PASSWORD=...
```

**① 互联网侧：导出快照**

```bash
cpypiserver-debian-offline snapshot -o snapshot.txt
# 只想同步某几个包时可以用 --only 缩小范围（依赖仍会自动补齐）
```

**② 内网侧：生成待更新清单**

```bash
cpypiserver-debian-offline plan snapshot.txt -o plan.txt
# 仅更新指定包 + 其依赖：
cpypiserver-debian-offline plan snapshot.txt -o plan.txt --only curl,vim
# 其他开关：--allow-downgrade、--verify-hashes、--recommends/--no-recommends
```

**③ 互联网侧：按清单下载依赖并打包**

```bash
cpypiserver-debian-offline bundle -p plan.txt -o bundle.tar.gz
```

**④ 内网侧：导入离线包**

```bash
cpypiserver-debian-offline import bundle.tar.gz
```

`status` 子命令可以随时查看中继配置与已构建的离线包：

```bash
cpypiserver-debian-offline status
```

## 三、Web 控制台

打开 Debian 页面下方的「离线更新中继」卡片，四个步骤各有对应按钮：
导出快照 → 上传快照生成清单 → 上传清单构建离线包 → 上传离线包导入。
导入成功后包清单会自动刷新。

## 四、HTTP 接口

同一套能力也直接暴露为 HTTP 接口，便于脚本编排：

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| `GET`  | `/debian/offline` | 中继配置与已构建离线包 |
| `GET`  | `/debian/offline/snapshot` | 导出快照（text/plain） |
| `POST` | `/debian/offline/plan` | 上传快照 → 待更新清单（text/plain） |
| `POST` | `/debian/offline/bundle` | 上传清单 → 构建离线包（JSON） |
| `GET`  | `/debian/offline/bundles/<文件名>` | 下载离线包（支持 Range 断点续传） |
| `POST` | `/debian/offline/import` | 上传离线包 → 校验并导入（JSON） |

```bash
# 直接用 curl 走一遍
curl -fsS -H "Authorization: Bearer $OPENFISH_API_KEY" \
  "$OPENFISH_URL/debian/offline/snapshot" -o snapshot.txt
curl -fsS -H "Authorization: Bearer $OPENFISH_API_KEY" \
  -F snapshot=@snapshot.txt "$OPENFISH_URL/debian/offline/plan" -o plan.txt
curl -fsS -H "Authorization: Bearer $OPENFISH_API_KEY" \
  -F plan=@plan.txt "$OPENFISH_URL/debian/offline/bundle" -o bundle.json
curl -fsS -H "Authorization: Bearer $OPENFISH_API_KEY" \
  -F bundle=@bundle.tar.gz "$OPENFISH_URL/debian/offline/import"
```

## 五、三个文件的格式

三者都是「魔数 + 注释 + 表头 + 制表符表格 + 自校验」的纯文本（离线包是
`.tar.gz`，内部也带一份同样的清单），可以用 `grep`、`diff`、Excel 直接看。

- **快照** `openfish-debian-snapshot-*.txt`：每行一个包，列为
  `name version arch suite component filename size sha256 depends pre_depends provides recommends essential priority section description`。
- **待更新清单** `openfish-debian-plan-*.txt`：列为
  `action name version arch filename size sha256 source`。
  `action` 为 `install` / `upgrade` / `reinstall` / `downgrade`；
  `source` 为 `missing` / `outdated` / `sha-mismatch` / `dependency` / `downgrade`。
  表头里 `snapshot_sha256`、`unresolved_*` 分别记录来源快照摘要与无法解析的依赖。
- **离线更新包** `openfish-debian-bundle-*.tar.gz`：内含

  ```
  openfish-debian-bundle.txt   校验清单（每行的 size/sha256 都会被重新验证）
  Packages                     针对包内文件生成的扁平 apt 索引
  pool/<apt Filename>          原始 .deb，保持 apt 的 pool 布局
  ```

## 六、语义与边界

- **待更新 = 快照里有、而本机 `DEBIAN_DIR` 没有或版本更旧**。不写 `--only`
  时是全量同步；内网仓库越接近一个完整镜像，清单就越小。
- **依赖闭包**：从 `Depends` / `Pre-Depends` 展开，`Recommends` 默认不跟随
  （`DEBIAN_OFFLINE_RECOMMENDS=true` 可开启）。版本约束按 Debian 规则比较
  （`~` 早于一切，含 epoch）；虚拟包通过 `Provides` 解析。无法满足的依赖会写进
  清单表头的 `unresolved_*`，不会静默丢弃。
- **版本比较**采用 dpkg 语义；本地版本更新的包默认跳过（不降级），需要时用
  `--allow-downgrade`。
- **同版本内容漂移**默认不检测（哈希整个仓库代价高）；需要时用
  `--verify-hashes`，漂移的包会以 `reinstall` 出现。
- **架构**：`Architecture: all` 的包对所有架构生效；其他架构只匹配自身。
- **离线包是「请求 → 权威解析」**：互联网侧会用本机元数据重新解析清单里的
  包，再按大小与 SHA256 校验后打包；上游已下架或校验失败的包会出现在返回的
  `skipped_packages` 中，而不是悄悄进包。
- **导入是原子的**：先整体解包校验，全部通过才写入 `DEBIAN_DIR`；任一文件失败
  则一个字节都不写。重复导入同一离线包时，摘要一致的包记为跳过。
- 导入后 `.deb` 会同时落在 `pool/...`（镜像布局）和仓库根目录（硬链接，供扁平
  `/debian/Packages` 索引使用），因此两种 apt 接入方式都能立即看到新包。

## 七、相关配置

| 变量 | 默认值 | 说明 |
| ---- | ------ | ---- |
| `DEBIAN_SUITES` | `bookworm bookworm-updates bookworm-security` | 快照枚举的 suite |
| `DEBIAN_COMPONENTS` | `main` | 快照枚举的组件 |
| `DEBIAN_ARCHES` | `amd64` | 快照枚举的架构 |
| `DEBIAN_OFFLINE_DIR` | `<backend>/data/offline/debian` | 构建出的离线包存放目录 |
| `DEBIAN_OFFLINE_MAX_MB` | `4096` | 单个离线包（及一次下载总量）上限，0 表示不限 |
| `DEBIAN_OFFLINE_RECOMMENDS` | `false` | 是否把 `Recommends` 纳入依赖闭包 |

## 八、常见问题

- **导入报 `413`**：上传体超过 `MAX_CONTENT_LENGTH`（默认 100 MB）。调大该
  配置，或把清单拆成多个 `--only` 批次分别打包。
- **离线包里很多包被跳过**：上游镜像已经删掉了这些版本。用更新的快照重新生成
  清单即可；被跳过的条目会在返回结果里列出原因。
- **清单表头出现 `snapshot_integrity mismatch`**：快照文件被截断或手工改过。
  协议仍会继续，但建议用 `sha256` 头部核对后重新导出。
- **内网导入后 apt 仍看不到新包**：确认客户端源指向的是 `/debian/` 扁平仓库，
  并执行了一次 `sudo apt update`。
