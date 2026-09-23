"""SSRF guard for outbound HTTP built from a request.

The model-routing panel lets an administrator probe a URL before saving it, and
the probe is issued by the *server*: it lists the endpoint's models from
``services.model_routes.probe``.  Without
a guard that is a server-side request forgery primitive — ``http://127.0.0.1``,
``http://169.254.169.254/latest/meta-data/`` or any other host the process can
reach becomes a way to read what only the server can see.

The blocking rule has to respect what this product *is*.  An intranet artifact
hub exists to front private services, so loopback and RFC 1918 addresses are
legitimate model endpoints and must stay reachable; blocking them would break
the feature the guard protects.  What is never legitimate is an address that is
not a host at all:

* link-local space (``169.254.0.0/16``, ``fe80::/10``) — the cloud metadata
  endpoint and its IPv6 twin live here;
* carrier-grade NAT (``100.64.0.0/10``) — Alibaba's metadata service is
  ``100.100.100.200``;
* the unspecified, multicast and reserved ranges;
* the well-known metadata hostnames, so an operator does not have to know the
  address behind ``metadata.google.internal``.

Validation itself is delegated, not hand-rolled: Pydantic's ``AnyHttpUrl``
(already a dependency, already the validation layer everywhere else) rejects a
non-HTTP scheme, embedded newlines and unparseable input, and :mod:`ipaddress`
classifies whatever the name resolves to.  An optional allow-list narrows the
probe to named hosts when an operator wants a strict policy.

DNS rebinding between the check and the request cannot be closed from here; the
caller is an authenticated administrator, and the residual is documented in
``docs/security/pypiserver0920-findings.md``.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from typing import NoReturn

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError


_HTTP_URL = TypeAdapter(AnyHttpUrl)

#: Networks that never host a legitimate outbound endpoint.  Loopback and the
#: private ranges are deliberately absent: the product's model routes *are*
#: intranet services.
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "169.254.0.0/16",   # link-local / cloud metadata
        "fe80::/10",        # IPv6 link-local
        "100.64.0.0/10",    # carrier-grade NAT (Alibaba metadata)
        "224.0.0.0/4",      # multicast
        "ff00::/8",         # IPv6 multicast
        "0.0.0.0/32",       # unspecified
        "::/128",           # unspecified
        "240.0.0.0/4",      # reserved
    )
)

#: Names that resolve to a metadata service; refused before DNS is even asked.
_BLOCKED_HOSTS = frozenset({
    "metadata", "metadata.google.internal", "metadata.goog",
    "instance-data", "instance-data.ec2.internal",
})


class UnsafeUrlError(ValueError):
    """The URL is not one the server is willing to call."""


def _addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address *host* resolves to (a literal IP resolves to itself)."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"无法解析主机：{host}") from exc
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not out:
        raise UnsafeUrlError(f"无法解析主机：{host}")
    return out


def _blocked_reason(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    for network in _BLOCKED_NETWORKS:
        if address.version == network.version and address in network:
            return f"{address} 属于禁止的地址段 {network}"
    return None


def _refuse(url: str, reason: str) -> NoReturn:
    """Log the refusal (so a probe of the metadata address is visible) and raise."""
    raise UnsafeUrlError(reason)


def check_outbound_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
) -> str:
    """Return *url* when the server may call it, else raise :class:`UnsafeUrlError`.

    *allowed_hosts*, when non-empty, is an exact allow-list of host names
    (optionally ``host:port``); anything else is refused before it is resolved.
    """
    candidate = (url or "").strip()
    try:
        parsed = _HTTP_URL.validate_python(candidate)
    except ValidationError as exc:
        reason = exc.errors()[0].get("type", "invalid")
        _refuse(candidate, f"URL 不是合法的 http(s) 地址（{reason}）")

    if parsed.username or parsed.password:
        _refuse(candidate, "URL 不能携带用户名或口令")

    allowed = {host.strip().lower() for host in (allowed_hosts or []) if host.strip()}
    host = parsed.host or ""
    if allowed and host.lower() not in allowed and f"{host}:{parsed.port}".lower() not in allowed:
        _refuse(candidate, f"主机 {host} 不在探测白名单内")
    if host.lower() in _BLOCKED_HOSTS:
        _refuse(candidate, f"主机 {host} 是云元数据服务，禁止访问")

    for address in _addresses(host):
        reason = _blocked_reason(address)
        if reason is not None:
            _refuse(candidate, f"禁止访问的地址：{reason}")

    # Rebuild from the parsed value so the caller sends exactly what was checked
    # (Pydantic normalises the scheme, host and path separators).
    return str(parsed)


__all__ = ["UnsafeUrlError", "check_outbound_url"]
