"""HTTP for URLs a person pasted: refuse anything that is not the public web.

The manual add (jobfeed/manual_add.py) fetches whatever URL is typed into the
web app, from the M1, which sits on a home network and a tailnet. Without a
guard that is a server-side request forgery: `http://192.168.1.1/`,
`http://127.0.0.1:8080/` (the web app itself) or a cloud metadata address
would be fetched and their text stored as a "posting".

`GuardedSession` is a requests.Session, so the existing enrichers take it
unchanged, and it checks every request it makes, including each redirect hop
and the enrichers' own API calls: http(s) only, the host resolved and every
address it resolves to global (no private, loopback, link-local, reserved,
multicast or IPv4-mapped forms of those), a capped timeout, and the body read
in chunks up to `MAX_BYTES`. A refusal raises `Refused`, a RequestException,
so enrichers treat it as a failed fetch and return "".

Residual: the address is checked when the request is made, and urllib3
resolves the name again to connect, so a DNS answer that changes between the
two (rebinding) is not covered. The exposure is a single owner-only form.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import requests

TIMEOUT = 15
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5


class Refused(requests.RequestException):
    """The URL is not one we will fetch; the message says why."""


def _bad_address(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (not ip.is_global or ip.is_multicast or ip.is_private or ip.is_loopback
            or ip.is_link_local or ip.is_reserved or ip.is_unspecified)


def check_url(url: str) -> None:
    """Raise Refused unless `url` is http(s) on a host that resolves only to
    public addresses."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https"):
        raise Refused("only http and https links can be added")
    host = parts.hostname
    if not host:
        raise Refused("the link has no host")
    if parts.username or parts.password:
        raise Refused("links with credentials in them are refused")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise Refused("the link has an invalid port")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        raise Refused(f"{host} does not resolve")
    if not infos:
        raise Refused(f"{host} does not resolve")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            raise Refused(f"{host} resolves to an unreadable address")
        if _bad_address(ip):
            raise Refused(f"{host} resolves to a private or local address ({ip})")


class GuardedSession(requests.Session):
    """A Session whose every request passes `check_url`, hop by hop."""

    def __init__(self, timeout: int = TIMEOUT, max_bytes: int = MAX_BYTES):
        super().__init__()
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.refusal = ""  # the last refusal, for the person who pasted the link

    def request(self, method, url, **kwargs):
        timeout = kwargs.pop("timeout", None) or self.timeout
        if isinstance(timeout, (int, float)):
            timeout = min(timeout, self.timeout)
        kwargs["timeout"] = timeout
        follow = kwargs.pop("allow_redirects", True)
        kwargs["allow_redirects"] = False
        kwargs["stream"] = True
        for _ in range(MAX_REDIRECTS + 1):
            try:
                check_url(url)
            except Refused as exc:
                self.refusal = str(exc)
                raise
            response = super().request(method, url, **kwargs)
            self._read_capped(response)
            if not (follow and response.is_redirect and response.headers.get("location")):
                return response
            url = urljoin(response.url, response.headers["location"])
            if response.status_code in (301, 302, 303) and method.upper() != "HEAD":
                method = "GET"
                for key in ("data", "json", "files"):
                    kwargs.pop(key, None)
        raise requests.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")

    def _read_capped(self, response: requests.Response) -> None:
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            response.close()
            self.refusal = "the page is larger than the size cap"
            raise Refused(self.refusal)
        chunks, total = [], 0
        for chunk in response.iter_content(65536):
            total += len(chunk)
            if total > self.max_bytes:
                response.close()
                self.refusal = "the page is larger than the size cap"
                raise Refused(self.refusal)
            chunks.append(chunk)
        response._content = b"".join(chunks)
        response._content_consumed = True
