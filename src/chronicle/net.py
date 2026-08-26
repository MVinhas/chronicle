"""HTTP fetching: polite, cached, retrying. Standard library only.

Per-host rate limiting and conditional requests keep the archive build from
hammering anyone's server — a full first run touches ~2,000 pages.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field

from . import paths

log = logging.getLogger("chronicle.net")

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/141.0.0.0 Safari/537.36 Chronicle/1.0 (personal archive reader)")

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


class FetchError(Exception):
    def __init__(self, url: str, status: int | None, message: str):
        super().__init__(f"{status or 'ERR'} {url}: {message}")
        self.url, self.status, self.message = url, status, message


@dataclass
class Response:
    url: str
    status: int
    headers: dict
    body: bytes
    from_cache: bool = False

    def text(self, fallback: str = "utf-8") -> str:
        ctype = self.headers.get("content-type", "")
        enc = None
        if "charset=" in ctype:
            enc = ctype.split("charset=")[-1].split(";")[0].strip().strip('"')
        if not enc:
            head = self.body[:4096].decode("ascii", "replace").lower()
            if 'charset="' in head:
                enc = head.split('charset="')[1].split('"')[0]
            elif "charset=" in head:
                enc = head.split("charset=")[1].split('"')[0].split("'")[0].split(">")[0].strip()
        for candidate in (enc, fallback, "utf-8", "latin-1"):
            if not candidate:
                continue
            try:
                return self.body.decode(candidate, "strict")
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text())


class _HostLimiter:
    """Minimum interval between request *starts* to the same host.

    The sleep happens outside the lock on purpose. Holding it across the sleep
    would serialise every request to a host, which silently cancels out any
    parallelism above — and some archives answer slowly enough (10-15s a page)
    that serial fetching turns a large archive into hours of work.
    """

    def __init__(self, interval: float = 0.6):
        self.interval = interval
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(host, 0.0))
            self._next[host] = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


_limiter = _HostLimiter()
_cancelled = threading.Event()


def set_rate(interval: float) -> None:
    _limiter.interval = max(0.0, interval)


def cancel_all() -> None:
    _cancelled.set()


def reset_cancel() -> None:
    _cancelled.clear()


def is_cancelled() -> bool:
    return _cancelled.is_set()


def _decompress(body: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    try:
        if encoding == "gzip":
            return gzip.decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return body
    return body


_cache_lock = threading.Lock()


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _cache_meta_path(url: str):
    return paths.HTTP_CACHE_DIR / f"{_cache_key(url)}.json"


def _cache_body_path(url: str):
    return paths.HTTP_CACHE_DIR / f"{_cache_key(url)}.bin"


def _read_cache(url: str):
    mp, bp = _cache_meta_path(url), _cache_body_path(url)
    if not (mp.exists() and bp.exists()):
        return None
    try:
        return json.loads(mp.read_text()), bp.read_bytes()
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(url: str, headers: dict, body: bytes) -> None:
    with _cache_lock:
        try:
            paths.HTTP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            meta = {"etag": headers.get("etag"),
                    "last-modified": headers.get("last-modified"),
                    "content-type": headers.get("content-type", ""),
                    "fetched": time.time()}
            _cache_meta_path(url).write_text(json.dumps(meta))
            _cache_body_path(url).write_bytes(body)
        except OSError as exc:
            log.debug("cache write failed for %s: %s", url, exc)


def _ascii_safe(url: str) -> str:
    """Percent-encode a URL so it survives http.client's ASCII-only request line.

    A real page can link to a non-ASCII path (ribbonfarm.com has posts titled
    with Greek letters), and BeautifulSoup/urljoin happily hand that straight
    back as a Unicode `href`. `http.client` does not: it encodes the request
    line as ASCII and raises otherwise. `safe="/%:@"` leaves already-percent-
    encoded and structural characters alone so a normal URL round-trips as-is.
    """
    try:
        url.encode("ascii")
        return url
    except UnicodeEncodeError:
        p = urllib.parse.urlsplit(url)
        path = urllib.parse.quote(p.path, safe="/%")
        query = urllib.parse.quote(p.query, safe="=&%")
        return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, p.fragment))


def fetch(url: str, *, headers: dict | None = None, timeout: float = 45.0,
          retries: int = 3, use_cache: bool = True, max_bytes: int = 25_000_000,
          method: str = "GET", data: bytes | None = None) -> Response:
    """Fetch a URL with retries, rate limiting and conditional caching."""
    if _cancelled.is_set():
        raise FetchError(url, None, "cancelled")

    url = _ascii_safe(url)
    host = urllib.parse.urlparse(url).netloc
    req_headers = dict(DEFAULT_HEADERS)
    if headers:
        req_headers.update(headers)

    cached = _read_cache(url) if use_cache and method == "GET" else None
    if cached:
        meta, body = cached
        if meta.get("etag"):
            req_headers["If-None-Match"] = meta["etag"]
        if meta.get("last-modified"):
            req_headers["If-Modified-Since"] = meta["last-modified"]

    last_exc: Exception | None = None
    for attempt in range(retries):
        if _cancelled.is_set():
            raise FetchError(url, None, "cancelled")
        _limiter.wait(host)
        try:
            req = urllib.request.Request(url, headers=req_headers, method=method,
                                         data=data)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(max_bytes)
                rheaders = {k.lower(): v for k, v in resp.headers.items()}
                body = _decompress(raw, rheaders.get("content-encoding", ""))
                if use_cache and method == "GET" and resp.status == 200:
                    _write_cache(url, rheaders, body)
                return Response(resp.geturl(), resp.status, rheaders, body)
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached:
                meta, body = cached
                return Response(url, 200, {"content-type": meta.get("content-type", "")},
                                body, from_cache=True)
            last_exc = exc
            if exc.code in (400, 401, 403, 404, 410, 451):
                raise FetchError(url, exc.code, exc.reason or "http error") from exc
            if exc.code == 429:
                time.sleep(min(30, 5 * (attempt + 1)))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
        if attempt < retries - 1:
            time.sleep(min(12.0, (2 ** attempt) + random.uniform(0, 0.7)))

    raise FetchError(url, None, str(last_exc) if last_exc else "unknown failure")


def fetch_text(url: str, **kw) -> str:
    return fetch(url, **kw).text()


def fetch_json(url: str, **kw):
    kw.setdefault("headers", {})
    kw["headers"] = {**kw["headers"], "Accept": "application/json"}
    return fetch(url, **kw).json()


def head_ok(url: str, timeout: float = 20.0) -> bool:
    try:
        fetch(url, method="HEAD", timeout=timeout, retries=1, use_cache=False)
        return True
    except FetchError:
        return False


def absolutise(base: str, href: str) -> str:
    try:
        return urllib.parse.urljoin(base, href.strip())
    except ValueError:
        return href


def canonical_url(url: str) -> str:
    """Normalise for de-duplication: drop fragments, tracking params, trailing slash."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url
    scheme = "https" if p.scheme in ("http", "https", "") else p.scheme
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc.endswith(":80") or netloc.endswith(":443"):
        netloc = netloc.rsplit(":", 1)[0]
    query = urllib.parse.parse_qsl(p.query, keep_blank_values=False)
    query = [(k, v) for k, v in query
             if not k.lower().startswith(("utm_", "fbclid", "gclid", "mc_", "ref"))]
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urllib.parse.urlunsplit(
        (scheme, netloc, path, urllib.parse.urlencode(query), ""))
