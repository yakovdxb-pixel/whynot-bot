"""
Link previews for reference links (like Telegram's): title + image + site name.

YouTube / TikTok go through their public oEmbed endpoints; everything else is a
plain GET of the page and its og:/twitter: meta tags. The request identifies as
Telegram's preview bot, which many sites (Instagram included) answer with OG tags.

Server-side fetching of user-supplied URLs is an SSRF risk (the worker sits inside
Railway's private network next to Postgres), so every hop is checked: http(s)
only, no *.internal / localhost names, every resolved IP must be public, redirects
are followed manually and re-checked, 5 s total, 1 MB max.
"""
import asyncio
import ipaddress
import json
import socket
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, quote

import httpx

TIMEOUT = 5.0
MAX_BYTES = 1_000_000
MAX_REDIRECTS = 3
UA = "TelegramBot (like TwitterBot)"

_OEMBED = {
    "youtube.com": "https://www.youtube.com/oembed?format=json&url=",
    "youtu.be": "https://www.youtube.com/oembed?format=json&url=",
    "tiktok.com": "https://www.tiktok.com/oembed?url=",
}
_SITE_NAMES = {"instagram.com": "Instagram", "youtube.com": "YouTube", "youtu.be": "YouTube",
               "tiktok.com": "TikTok", "t.me": "Telegram", "pinterest.com": "Pinterest"}


def _base_domain(host: str) -> str:
    host = (host or "").lower().rstrip(".")
    for d in list(_OEMBED) + list(_SITE_NAMES):
        if host == d or host.endswith("." + d):
            return d
    return host


async def _assert_public(url: str):
    """Raise ValueError unless url is http(s) to a host that resolves only to public IPs."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("bad scheme/host")
    host = p.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".internal", ".local", ".localhost")):
        raise ValueError("private host")
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError("non-public address")


async def _get(client: httpx.AsyncClient, url: str):
    """GET with manual, re-validated redirects; returns (final_url, body_bytes, content_type)."""
    for _ in range(MAX_REDIRECTS + 1):
        await _assert_public(url)
        async with client.stream("GET", url) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            r.raise_for_status()
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) >= MAX_BYTES:
                    break
            return str(r.url), bytes(buf[:MAX_BYTES]), r.headers.get("content-type", "")
    raise ValueError("too many redirects")


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self._in_title, self.title = {}, False, ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"].strip()
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data


def _clip(s, n=300):
    s = " ".join(unescape(s or "").split())
    return s[:n] or None


async def fetch_preview(url: str) -> dict:
    """{"title", "image", "site"} — any of them may be None. Never raises."""
    host = urlparse(url).hostname or ""
    dom = _base_domain(host)
    out = {"title": None, "image": None, "site": _SITE_NAMES.get(dom) or (host.removeprefix("www.") or None)}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                                     headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"}) as client:
            async def run():
                if dom in _OEMBED:
                    _, body, _ = await _get(client, _OEMBED[dom] + quote(url, safe=""))
                    d = json.loads(body.decode("utf-8", "replace"))
                    out["title"] = _clip(d.get("title"))
                    out["image"] = d.get("thumbnail_url")
                    out["site"] = d.get("provider_name") or out["site"]
                    return
                final, body, ctype = await _get(client, url)
                if "html" not in ctype.lower():
                    return
                p = _MetaParser()
                p.feed(body.decode("utf-8", "replace"))
                m = p.meta
                out["title"] = _clip(m.get("og:title") or m.get("twitter:title") or p.title)
                img = m.get("og:image") or m.get("og:image:url") or m.get("twitter:image")
                out["image"] = urljoin(final, unescape(img)) if img else None
                out["site"] = _clip(m.get("og:site_name"), 80) or out["site"]
            await asyncio.wait_for(run(), TIMEOUT)
    except Exception:  # noqa: BLE001 — a missing preview is fine, the link still works
        pass
    if out["image"] and not out["image"].startswith("https://"):
        out["image"] = None   # mixed content would be blocked inside the Mini App
    return out
