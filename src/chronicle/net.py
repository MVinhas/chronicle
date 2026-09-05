"""HTTP fetching: polite, cached, retrying. Standard library only.

Per-host rate limiting and conditional requests keep the archive build from
hammering anyone's server — a full first run touches ~2,000 pages.
"""
from __future__ import annotations

import codecs
import gzip
import hashlib
import json
import logging
import random
import re
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


# --------------------------------------------------------------------------
# Bytes to text
#
# Getting this wrong is invisible and permanent: it corrupts what gets archived,
# not just what gets shown. The specific failure that motivates the care here is
# paulgraham.com, which serves "Content-Type: text/html" with no charset at all
# and windows-1252 bytes. Decoded as ISO-8859-1 -- the obvious fallback, and the
# one a strict reading of the RFCs asks for -- byte 0x97 becomes U+0097, a C1
# control character that renders as *nothing*. Every em dash in the essay
# silently disappears.
#
# So we do what browsers do instead of what the RFCs say, per the WHATWG
# Encoding Standard.
# --------------------------------------------------------------------------

# Labels that must not be honoured literally. ISO-8859-1 is the one that
# matters: the bytes publishers actually send in 0x80-0x9F are curly quotes,
# dashes and ellipses, never control codes, so every browser decodes a page
# that declares latin-1 as windows-1252. The rest of the table follows the
# same standard's index, covering the other labels seen in the wild.
_LABEL_ALIASES = {
    "ascii": "windows-1252", "us-ascii": "windows-1252",
    "iso-8859-1": "windows-1252", "iso8859-1": "windows-1252",
    "iso_8859-1": "windows-1252", "iso88591": "windows-1252",
    "latin1": "windows-1252", "latin-1": "windows-1252",
    "l1": "windows-1252", "cp819": "windows-1252", "cp1252": "windows-1252",
    "iso-8859-9": "windows-1254", "iso8859-9": "windows-1254",
    "latin5": "windows-1254",
    "iso-8859-11": "windows-874", "tis-620": "windows-874",
    "gb2312": "gbk", "gb_2312": "gbk", "euc-cn": "gbk", "chinese": "gbk",
    "ks_c_5601-1987": "euc-kr", "korean": "euc-kr",
    "shift-jis": "shift_jis", "sjis": "shift_jis", "x-sjis": "shift_jis",
    "utf8": "utf-8", "unicode-1-1-utf-8": "utf-8",
}

# The printable characters windows-1252 puts where ISO-8859-1 has C1 controls.
# Text that reaches us already decoded the wrong way -- by an older Chronicle,
# or by the publisher's own toolchain -- is repaired with this, because a C1
# control in prose is never anything but a mis-decoded dash or quote.
_C1_REPAIR = {}
for _b in range(0x80, 0xA0):
    try:
        _C1_REPAIR[_b] = ord(bytes([_b]).decode("cp1252"))
    except UnicodeDecodeError:
        pass                      # 0x81, 0x8D, 0x8F, 0x90, 0x9D: undefined
del _b

_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+?charset\s*=\s*["']?\s*([A-Za-z0-9_:.+-]+)""", re.I)

# BOM sniffing, which outranks every declaration. Only UTF-8 and UTF-16 are
# sniffed -- that is all the HTML standard sniffs, and UTF-32 does not exist
# on the web.
_BOMS = ((codecs.BOM_UTF8, "utf-8-sig"),
         (codecs.BOM_UTF16_LE, "utf-16"),
         (codecs.BOM_UTF16_BE, "utf-16"))


# Built from the repair table itself, not from the 0x80-0x9F range, so the
# question it answers is exactly "would repair_c1 change this?". The five bytes
# windows-1252 leaves undefined are not in it: text carrying only those is
# beyond repair, and a caller told otherwise would rewrite a row to itself.
_C1_RE = re.compile("[%s]" % "".join(chr(c) for c in sorted(_C1_REPAIR)))


def has_c1(text: str | None) -> bool:
    """Whether repair_c1 would change this text.

    Separated from the repair because the two costs are nothing alike: the
    search is a C-level scan, the translate allocates a new string a character
    at a time. Almost no page needs repairing, and the callers that ask this --
    every fetch during a sync, every article during the schema-5 migration --
    are deciding what to touch across an entire library.
    """
    return bool(text) and _C1_RE.search(text) is not None


def repair_c1(text: str) -> str:
    """Turn stray C1 control characters back into the punctuation they were."""
    return text.translate(_C1_REPAIR) if has_c1(text) else text


def _codec(label: str | None) -> str | None:
    """Normalise a charset label, or None if Python cannot decode it."""
    key = (label or "").strip().strip('"\'').lower()
    key = _LABEL_ALIASES.get(key, key)
    if not key:
        return None
    try:
        codecs.lookup(key)
    except LookupError:
        return None
    return key


def _declared_charset(ctype: str, body: bytes) -> str | None:
    """The encoding the page claims, from the HTTP header or its own <meta>."""
    if "charset=" in ctype.lower():
        enc = _codec(ctype.lower().split("charset=")[-1].split(";")[0])
        if enc:
            return enc
    m = _META_CHARSET_RE.search(body[:4096])
    if m:
        return _codec(m.group(1).decode("ascii", "replace"))
    return None


def _is_utf8(body: bytes) -> bool:
    """Whether the body is UTF-8, tolerating a truncated final sequence.

    _decompress hands back partial bodies on purpose, and a body cut mid
    sequence is still a UTF-8 body -- rejecting it here would send the whole
    page down the single-byte path and mojibake all of it. An incremental
    decoder draws that line exactly: with final=False it holds back an
    incomplete trailing sequence and raises only on bytes that could not begin
    one, so a lone 0x97 at the end is still rejected.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(body, False)
    except UnicodeDecodeError:
        return False
    return True


def decode_body(body: bytes, ctype: str = "", fallback: str = "utf-8") -> str:
    """Decode a response body the way a browser would."""
    for bom, enc in _BOMS:
        if body.startswith(bom):
            return repair_c1(body.decode(enc, "replace"))

    declared = _declared_charset(ctype, body)
    wide = bool(declared) and declared.startswith(("utf-16", "utf-32"))

    # A declared single-byte encoding is not evidence of anything: those codecs
    # accept any byte sequence at all, so honouring one over a body that is
    # plainly UTF-8 is how a page that mislabels itself latin-1 turns every em
    # dash into "â€”". Valid UTF-8 wins. (Not for UTF-16, whose ASCII text
    # is full of NULs and so passes a UTF-8 check while meaning nothing.)
    if not wide and _is_utf8(body):
        return repair_c1(body.decode("utf-8", "replace"))

    for candidate in (declared, fallback, "utf-8", "windows-1252"):
        if not candidate:
            continue
        try:
            return repair_c1(body.decode(candidate, "strict"))
        except (LookupError, UnicodeDecodeError):
            continue
    # windows-1252, not latin-1: its five undefined bytes become a visible
    # replacement character rather than an invisible control.
    return repair_c1(body.decode("windows-1252", "replace"))


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
        return decode_body(self.body, self.headers.get("content-type", ""),
                           fallback)

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
        self.base = interval
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
    _limiter.base = _limiter.interval = max(0.0, interval)


def set_rate_scale(factor: float) -> None:
    """Scale the polite interval, keeping the configured rate as the baseline.

    Building a blog's archive for the first time is hundreds of pages of one
    site, and this spacing is the entire cost of it -- worth going faster for
    that one job, and back to the gentler rate for routine updates, which
    fetch a handful of pages and are in no hurry.

    Scaling rather than assigning matters: the tests set the rate to zero, and
    a sync that assigned its own would make the whole suite wait in real time.
    """
    _limiter.interval = max(0.0, _limiter.base * max(0.0, factor))


def cancel_all() -> None:
    _cancelled.set()


def reset_cancel() -> None:
    _cancelled.clear()


def is_cancelled() -> bool:
    return _cancelled.is_set()


def _decompress(body: bytes, encoding: str) -> bytes:
    """Decompress a response body, tolerating truncation.

    A body cut short by `max_bytes` is a truncated stream; decompressobj
    yields what it can instead of raising, which is exactly right for
    existence probes that only need the head of a large compressed file.
    (gzip.decompress raises EOFError there, which used to escape uncaught.)
    """
    encoding = (encoding or "").lower()
    try:
        if encoding == "gzip":
            return zlib.decompressobj(wbits=16 + zlib.MAX_WBITS).decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompressobj().decompress(body)
            except zlib.error:
                return zlib.decompressobj(wbits=-zlib.MAX_WBITS).decompress(body)
    except (OSError, EOFError, zlib.error):
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
