# 离线包导入的加固说明

本文说明 `backend/services/debian_offline.py` 里那个**解包循环**为什么是静态扫描的
误报，以及沿着这条线真正该修的是什么。结论分三档，沿用
[`pypiserver0920-findings.md`](pypiserver0920-findings.md) 的写法：

| 项 | 结论 |
| --- | --- |
| 旧实现「来自不可信文件的写入 / 路径遍历」 | **误报** —— 污点链在语义上早已切断 |
| 相邻的「解压无上限」 | **真实缺陷**（低危，DoS），本次修复 |
| 换第三方库以免被报 | **不做** —— 见 §4，代价是把误报换成一条更贵的账 |

## 1. 旧实现为什么是误报

被指向的代码（改动前 `_extract_bundle`）：

```python
with extracted, open(target, "wb") as handle:
    shutil.copyfileobj(extracted, handle, CHUNK)
```

它是「tar 成员 → 文件写入」的污点形状，但四个前提都不成立：

1. **没有走 `extractall()` / `extract()`**。归档穿越的经典形态是它们；这里是
   `extractfile(member)` 自己拷字节。
2. **只接受普通文件**：`member.isfile()` 不成立就 `raise`。软链、硬链、设备节点、
   FIFO 一个都进不来，所以「先落一个指向 `/` 的软链、再往链里写」在结构上不可能。
3. **每个成员名都过 `_safe_member_path()`**：空名、`/`、`\` 开头、含 `\` 或 NUL
   直接拒；再解析后判定是否仍在暂存目录内。
4. **落点是刚 `mkdtemp` 的空目录**，没有预先埋好的软链，也就没有 TOCTOU 窗口；
   写出来的文件还要按清单的 size/sha256 全部校验通过才 `os.replace` 进仓库。

也就是说：`open()` 的 `target` 在写之前已经被证明是暂存目录内的普通路径，报警只是
规则不认识 `_safe_member_path` 是消毒函数。**唯一会把它变成真问题的改法**是删掉
`isfile()` 判断或那个路径检查。

## 2. 顺着这条线真正该修的

同一段代码里有两个真问题，规则没报：

* **解压没有任何上限**：`DEBIAN_OFFLINE_MAX_MB` 只管出包侧（`build_bundle`），而
  内网侧——也就是不信任外部产物的那一侧——没有体积或成员数上限。1 KB 的 gzip
  炸弹可以把 hub 的盘写满。
* **清单在成员全部写完之后才读**（旧实现 1541-1542），因此任何基于清单的校验都
  发生在字节已经落盘之后。

## 3. 本次改动

| 位置 | 改动 |
| --- | --- |
| `_bundle_members()`（新增） | 解包前逐成员判定：只接受目录与普通文件，其余**拒绝整个包**（不静默跳过）；成员名必须落在暂存目录内；成员数封顶 `MAX_BUNDLE_MEMBERS = 20_000`；解压后总大小封顶 `DEBIAN_OFFLINE_MAX_MB`（`0` 关闭，与出包侧同一语义） |
| `_extract_bundle()` | 改为 `tarfile.extractall(staging, members=<白名单>, filter="data")`——写出的就是刚刚证明过的那些成员，再由标准库的 `data` 过滤器复核路径、剥掉 setuid/setgid 与组/其他写位 |
| `_safe_member_path()` | 收敛到 `services.paths.contained_resolved()`（新增）：`safe_join` 是词法检查，看不见 `pool/main/x` 本身就是指向外部的软链；仓库侧正是这种「已被别人填充过的存活目录」 |
| `MAX_BUNDLE_MEMBERS` | 新常量（`__all__` 已导出），是压缩炸弹「成员数」那一半的兜底 |

体积上限刻意**复用出包侧那个数字**：互联网侧本来就不允许打包超过
`DEBIAN_OFFLINE_MAX_MB` 的载荷，所以合法离线包永远不会被它拒绝，而会解压到超过它
的包必然是炸弹。两个方向都自洽。

## 4. 为什么不引入第三方依赖（tarsafe / exarch）

「换成开源依赖就不会误报」在机制上是对的——sink 出了自己的源码，只扫一方代码的
工具就不再报——但账不止一笔，而且这里有实测证据。

**tarsafe**（`TarSafe` 是 `TarFile` 子类，用法就是 `extractall()`）：

* 它的 `extractall` 覆盖签名里**没有 `filter` 参数**，实测传 `filter="data"` 会
  `TypeError`。于是 3.12/3.13 上只有它自己的检查，拿不到 PEP 706 过滤器。
* Bandit B202 的判定是「该文件 `import tarfile` + 调用名含 `extractall` + 关键字里
  没有 `filter="data"` → HIGH」。`debian_offline.py` 本来就 `import tarfile`（要用
  `tarfile.TarError`），所以这一条**必然命中且无法用 filter 消掉**——为消掉一条误报
  换来一条更难辩的告警。
* 保证比旧实现**更弱**：`_is_device()` 只挡 `ischr()/isblk()`，FIFO 不挡（标准库有
  `isfifo -> makefifo` 分支，Linux 上会真的建出 FIFO）；只拒「目标逃出 root」的链接，
  树内软链会被**创建**（`os.replace` 搬的是链接本体，相对目标随后按新目录重新解析）；
  `set_attrs=True` + `chmod()` 会把归档里的 mode（含 setuid）原样落地；整体相当于
  两趟读取。它只有 140 行，逻辑基本就是 `commonpath` 包含性判断 + `chr/blk` 拒绝。

**同类依赖的历史**：本仓库上传链的安全解压是 `guarddog.utils.archives.safe_extract`
（TAR 分支底层就是 tarsafe）。它在 2026 年 1 月连吃两枚公告——CVE-2026-22870（ZIP
压缩炸弹，缺体积/数量校验）与 CVE-2026-22871（路径遍历 → 任意文件覆盖/RCE，`zip.extract`
的 `path` 参数用法错误）——直到 guarddog 2.7.1 才修。把这段逻辑交给依赖，换来的正是
这种必须持续跟的账。

**exarch**（Rust/PyO3，`SecurityConfig` 默认拒绝软链/硬链/绝对路径，并带
`max_file_size`/`max_total_size`/`max_file_count`/`max_compression_ratio`）是功能上最
对症的候选：它有你今天缺的那套配额。但它是 0.x / Beta、单人维护、2025-12 首发，且在
一个安全关键路径上引入原生扩展，需要按仓库「每条依赖都要论证」的标准单独决策。**如果
将来要的是「配额 + 一个被维护的实现」，它是第一顺位；本次要的是「边界清楚、不加依赖」，
所以没上。**

**为什么是 `filter="data"`**：它是 `extractall` 唯一不被 B202 命中的形态，策略又比
tarsafe 强（`data` 过滤器会拒绝 FIFO/设备/绝对链接/越界链接，并把 mode 收敛为
`mode & 0o755`、必要时清 exec、再补 `0o600`）。代价见 §5。

## 5. 前置条件：Python 版本

`filter="data"` 需要 Python 3.12+（本项目下限），**且** 3.12.11 / 3.13.4+ ——
`CVE-2025-4517`（filter 绕过，CRITICAL，可越界写任意文件）与同族的
CVE-2025-4330 / 4138 / 12718 / 4435 都在这些补丁版本里才修好。也就是说：3.12 的早期
补丁版**有**这个 filter，但**没有**它的修复。

镜像基线是 `python:3.12-slim`（浮动标签），构建时自然满足；如果哪天把它钉到某个具体
补丁号，**不要低于 3.12.11**。解包策略本身不受影响：白名单是第一道，`filter` 是第二道。

## 6. 加固应满足的断言

这些断言原先由离线门禁脚本覆盖；`backend/scripts/` 门禁目录已整体删除（连同
`make gates-backend`），因此下表改为**手工 / 自建检查的验收清单**：用一个**合法离线包**
重打包出各种恶意形状，payload 逐字节不变，所以清单的自校验一定通过——被拒只能是
解包策略在说话：

| 断言 | 说明 |
| --- | --- |
| `..` 成员被拒 | 且仓库一个字节没变 |
| 绝对路径成员被拒 | 同上 |
| 软链 / 硬链 / FIFO / 设备节点成员被拒 | 四种类型逐一 |
| 超过 `MAX_BUNDLE_MEMBERS` 被拒 | 检查时临时调低常量，不构造两万个成员 |
| 解压后超过 `DEBIAN_OFFLINE_MAX_MB` 被拒 | 检查时把上限设在「本包 + 1 MiB」，再加一个 4 MiB 成员 |
| 每次拒绝都没留下暂存目录 | 原子性 |
| 带 setuid 成员的包仍能正常导入 | 不是一刀切拒绝 |
| setuid/setgid 位到不了仓库 | 在 Linux 上另需断言组/其他写位也被剥掉 |

另一条要验的边界：`services.paths.contained()`（词法检查）会放行指向外部的软链组件，
而 `contained_resolved()` 拒绝它——这正是新增这个变体的理由。

## 7. 残余限制（不做的部分）

* **phase 1/2 的 `_safe_member_path` 保留**：那里的输入是包内清单文本，同样「来自
  不可信文件」，同类规则仍可能指向 `os.replace`/`compute_sha256` 那两行。那一条与 §1
  同源、同样应判误报——换解压方式动不了它。
* `filter="data"` 自身在 2025 年有 5 枚 CVE（见 §5）；我们依赖的是 ≥3.12.11，而不是
  「filter 一定安全」。白名单那一道不依赖它。
* **不做来源认证**：离线包没有签名，威胁模型是「人工携带、可能被篡改」，不是「网络
  传输」。要防的是畸形归档，不是伪造清单——后者靠 `manifest_sha256` 与带外核对。
* 体积按 `member.size` 计。GNU sparse 成员的 `size` 是逻辑大小
  （`tarfile._proc_sparse` 里 `self.size = origsize`），也正是写入的字节数。
