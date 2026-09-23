#!/usr/bin/env python
"""Client CLI for the Debian offline relay — the four air-gap steps.

The relay itself lives on the server (``services/debian_offline.py`` and the
``/debian/offline/*`` routes); this is the operator-facing driver for it, so the
whole exchange can run from two shells without a browser and without a shared
filesystem:

.. code-block:: bash

    # ── on the internet deployment ─────────────────────────────────────
    cpypiserver-debian-offline snapshot -o snapshot.txt
    #    ... carry snapshot.txt to the intranet ...
    cpypiserver-debian-offline bundle -p plan.txt -o bundle.tar.gz
    #    ... carry bundle.tar.gz back to the intranet ...

    # ── on the intranet deployment ─────────────────────────────────────
    cpypiserver-debian-offline plan snapshot.txt -o plan.txt
    cpypiserver-debian-offline import bundle.tar.gz

Connection settings come from ``--url``/``--api-key`` (or ``--user`` for HTTP
Basic) or, preferably, the ``OPENFISH_URL``, ``OPENFISH_API_KEY``,
``OPENFISH_USER`` and ``OPENFISH_PASSWORD`` environment variables.  The URL is
the server's base including any route prefix (``http://openfish.intra/openfish``);
an API key is sent as ``Authorization: Bearer``.

``-o -`` writes a text artifact to stdout, which is what makes
``... snapshot -o - | ... plan -`` usable in a pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from services.headers import checked_headers

#: Default read timeout.  Snapshot/plan builds can walk a lot of apt metadata,
#: so the connect timeout stays short while the read timeout is generous.
DEFAULT_TIMEOUT = 300.0


class CliError(Exception):
    """A failure worth a one-line message and a non-zero exit."""


# ── HTTP plumbing ────────────────────────────────────────────────────

def _session(args: argparse.Namespace) -> requests.Session:
    """The HTTP session every relay step uses.

    The API key comes from ``--api-key`` (or the environment) and is about to
    become an ``Authorization`` header, so the mapping goes through the shared
    header allowlist before the session is used — this is the boundary, and a
    key that is not one printable line is refused here rather than sent.
    """
    if not args.url:
        raise CliError("缺少服务地址：请用 --url 或设置 OPENFISH_URL")
    session = requests.Session()
    headers = {"User-Agent": "cpypiserver-debian-offline/1"}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"
    elif args.user:
        # The server accepts HTTP Basic alongside API keys (that is how pip, uv
        # and twine authenticate); a deployment that has no key handy can use
        # the human username/password instead.  ``session.auth`` makes requests
        # build a base64 ``Basic`` value, whose alphabet cannot carry a CR/LF.
        user, _, inline_password = args.user.partition(":")
        password = args.password or inline_password
        if not password:
            raise CliError("--user 需要 user:pass 形式，或另外提供 --password")
        session.auth = (user, password)
    # One check for both branches: the key is the only value that can be unsafe,
    # and it is in ``headers`` in exactly one of them.
    session.headers.update(checked_headers(headers, context="debian offline client"))
    session.verify = not args.insecure
    return session


def _endpoint(args: argparse.Namespace, path: str) -> str:
    return args.url.rstrip("/") + path


def _error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()[:300] or "无响应体"
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            if payload.get(key):
                return str(payload[key])
    return json.dumps(payload, ensure_ascii=False)[:300]


def _raise_for_status(response: requests.Response) -> None:
    if response.status_code < 400:
        return
    raise CliError(f"HTTP {response.status_code}: {_error_message(response)}")


def _request(args, session, method: str, path: str, **kwargs) -> requests.Response:
    try:
        response = session.request(
            method, _endpoint(args, path), timeout=args.timeout, **kwargs
        )
    except requests.RequestException as exc:
        raise CliError(f"{method} {path} 失败：{exc}") from exc
    _raise_for_status(response)
    return response


# ── Artifact I/O ─────────────────────────────────────────────────────

def _disposition_filename(response: requests.Response, fallback: str) -> str:
    header = response.headers.get("Content-Disposition", "")
    marker = "filename="
    if marker in header:
        name = header.split(marker, 1)[1].strip().strip('"')
        if name:
            return Path(name).name
    return fallback


def _write(path: str | None, data: bytes, default_name: str) -> str:
    if path == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return "-"
    target = Path(path) if path else Path(default_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return str(target)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _note(args: argparse.Namespace, message: str) -> None:
    if not args.quiet:
        print(message, file=sys.stderr)


# ── Commands ─────────────────────────────────────────────────────────

def cmd_status(args, session) -> int:
    response = _request(args, session, "GET", "/debian/offline")
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


def cmd_snapshot(args, session) -> int:
    params: dict[str, str] = {}
    for key in ("suites", "components", "arches"):
        value = getattr(args, key)
        if value:
            params[key] = value
    if args.fresh:
        params["fresh"] = "1"
    response = _request(
        args, session, "GET", "/debian/offline/snapshot", params=params
    )
    data = response.content
    name = _disposition_filename(response, "openfish-debian-snapshot.txt")
    out = _write(args.output, data, name)
    digest = response.headers.get("X-Openfish-Sha256") or _digest(data)
    if out != "-":
        _note(args, f"快照已写入 {out}（{len(data)} 字节，sha256={digest}）")
    return 0


def cmd_plan(args, session) -> int:
    snapshot = Path(args.snapshot).read_bytes()
    data: dict[str, str] = {}
    if args.only:
        data["only"] = args.only
    if args.allow_downgrade:
        data["allow_downgrade"] = "1"
    if args.verify_hashes:
        data["verify_hashes"] = "1"
    if args.recommends is not None:
        data["recommends"] = "1" if args.recommends else "0"
    response = _request(
        args, session, "POST", "/debian/offline/plan",
        files={"snapshot": (Path(args.snapshot).name, snapshot, "text/plain")},
        data=data,
    )
    body = response.content
    name = _disposition_filename(response, "openfish-debian-plan.txt")
    out = _write(args.output, body, name)
    digest = response.headers.get("X-Openfish-Sha256") or _digest(body)
    if out != "-":
        _note(args, f"待更新清单已写入 {out}（{len(body)} 字节，sha256={digest}）")
    return 0


def cmd_bundle(args, session) -> int:
    plan = Path(args.plan).read_bytes()
    response = _request(
        args, session, "POST", "/debian/offline/bundle",
        files={"plan": (Path(args.plan).name, plan, "text/plain")},
    )
    payload: dict[str, Any] = response.json()
    download_url = payload.get("download_url")
    if not download_url:
        raise CliError("服务端未返回 download_url，无法取回离线包")
    parsed = urlparse(args.url)
    absolute = urljoin(f"{parsed.scheme}://{parsed.netloc}/", download_url)
    target = Path(args.output) if args.output else Path(payload.get("filename") or "bundle.tar.gz")
    target.parent.mkdir(parents=True, exist_ok=True)
    _note(
        args,
        f"离线包已构建：{payload.get('packages')} 个包，"
        f"跳过 {payload.get('skipped', 0)} 个，{payload.get('size_human')}",
    )
    written = _download(args, session, absolute, target)
    _note(args, f"离线包已写入 {written}（sha256={payload.get('sha256')}）")
    if payload.get("skipped_packages"):
        _note(args, "跳过的包：")
        for item in payload["skipped_packages"]:
            _note(args, f"  - {item.get('name')} {item.get('version')}: {item.get('reason')}")
    return 0


def _download(args, session, url: str, target: Path) -> str:
    try:
        with session.get(url, stream=True, timeout=args.timeout) as response:
            _raise_for_status(response)
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with open(target, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if not args.quiet and total and sys.stderr.isatty():
                        print(
                            f"\r  {done * 100 // total:3d}%  "
                            f"{done >> 20}/{total >> 20} MiB",
                            end="", file=sys.stderr, flush=True,
                        )
            if not args.quiet and total and sys.stderr.isatty():
                print(file=sys.stderr)
    except requests.RequestException as exc:
        raise CliError(f"下载离线包失败：{exc}") from exc
    return str(target)


def cmd_import(args, session) -> int:
    bundle = Path(args.bundle)
    with open(bundle, "rb") as handle:
        response = _request(
            args, session, "POST", "/debian/offline/import",
            files={"bundle": (bundle.name, handle, "application/gzip")},
        )
    report = response.json()
    print(
        f"导入完成：新增 {report.get('imported')} 个，跳过 {report.get('skipped')} 个，"
        f"失败 {report.get('failed')} 个，共 {report.get('total_size_human')}"
    )
    for item in report.get("failed_packages", []):
        print(f"  ✗ {item.get('filename')}: {item.get('reason')}", file=sys.stderr)
    return 1 if report.get("failed") else 0


# ── Argument parsing ─────────────────────────────────────────────────

def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cpypiserver-debian-offline",
        description="Debian 离线更新中继：快照 → 待更新清单 → 离线包 → 导入",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  OPENFISH_URL=http://openfish.intra OPENFISH_API_KEY=cpypi_… \\\n"
            "    cpypiserver-debian-offline snapshot -o snapshot.txt\n"
            "  cpypiserver-debian-offline plan snapshot.txt -o plan.txt --only curl,vim\n"
            "  cpypiserver-debian-offline bundle -p plan.txt -o bundle.tar.gz\n"
            "  cpypiserver-debian-offline import bundle.tar.gz\n"
        ),
    )
    parser.add_argument("--url", default=_env("OPENFISH_URL"),
                        help="openfish 地址（含路由前缀），默认取 OPENFISH_URL")
    parser.add_argument("--api-key", default=_env("OPENFISH_API_KEY"),
                        help="API 密钥，默认取 OPENFISH_API_KEY")
    parser.add_argument("--user", default=_env("OPENFISH_USER"),
                        help="HTTP Basic 用户名，`user:pass` 或配合 --password，"
                             "默认取 OPENFISH_USER")
    parser.add_argument("--password", default=_env("OPENFISH_PASSWORD"),
                        help="HTTP Basic 密码，默认取 OPENFISH_PASSWORD")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"读写超时秒数（默认 {DEFAULT_TIMEOUT:g}）")
    parser.add_argument("--insecure", action="store_true",
                        help="跳过 TLS 证书校验（自签名内网证书时使用）")
    parser.add_argument("--quiet", action="store_true", help="只输出结果文件内容")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="查看中继配置与已构建的离线包")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("snapshot", help="导出互联网侧软件包快照（互联网主机上运行）")
    p.add_argument("-o", "--output", default=None, help="输出文件；`-` 表示标准输出")
    p.add_argument("--suites", default=None, help="覆盖要枚举的 suite（空格或逗号分隔）")
    p.add_argument("--components", default=None, help="覆盖要枚举的 component")
    p.add_argument("--arches", default=None, help="覆盖要枚举的架构")
    p.add_argument("--fresh", action="store_true", help="忽略 apt 元数据缓存 TTL")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("plan", help="对比快照并生成待更新清单（内网主机上运行）")
    p.add_argument("snapshot", help="互联网侧导出的快照文本文件")
    p.add_argument("-o", "--output", default=None, help="输出文件；`-` 表示标准输出")
    p.add_argument("--only", default=None,
                   help="只把这些包作为直接更新目标（空格或逗号分隔），依赖仍会自动补齐")
    p.add_argument("--allow-downgrade", action="store_true", help="允许回退到快照中的较低版本")
    p.add_argument("--verify-hashes", action="store_true", help="对同版本本地包重新计算 sha256")
    recommends = p.add_mutually_exclusive_group()
    recommends.add_argument("--recommends", dest="recommends", action="store_true",
                            default=None, help="把 Recommends 纳入依赖闭包")
    recommends.add_argument("--no-recommends", dest="recommends", action="store_false",
                            help="不把 Recommends 纳入依赖闭包（默认）")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("bundle", help="按待更新清单下载并打出离线包（互联网主机上运行）")
    p.add_argument("-p", "--plan", required=True, help="内网侧生成的待更新清单")
    p.add_argument("-o", "--output", default=None, help="离线包输出路径")
    p.set_defaults(func=cmd_bundle)

    p = sub.add_parser("import", help="校验并导入离线包（内网主机上运行）")
    p.add_argument("bundle", help="互联网侧打好的 .tar.gz 离线包")
    p.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        session = _session(args)
        return args.func(args, session)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: 找不到文件 {exc.filename}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
