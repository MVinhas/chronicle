"""In-process fake HTTP layer for exercising discovery against synthetic sites.

Patches ``chronicle.net.fetch`` so the whole pipeline — detection, discovery,
content fetching — runs against a dict of canned responses, with every request
counted. This is what lets the tests prove architectural properties ("the
second sync makes no per-article requests") instead of testing one website.
"""
from __future__ import annotations

import contextlib
from unittest import mock

from chronicle import net


class FakeNet:
    """A fake origin: url -> (status, body, headers) with request accounting."""

    def __init__(self):
        self.pages: dict[str, tuple[int, bytes, dict]] = {}
        self.redirects: dict[str, str] = {}
        self.requests: list[str] = []
        self.dead = False   # simulate an origin that never answers at all

    # -- building the site --------------------------------------------------

    def add(self, url: str, body: str | bytes, status: int = 200,
            headers: dict | None = None) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.pages[url] = (status, body, headers or {})

    def redirect(self, url: str, target: str) -> None:
        self.redirects[url] = target

    # -- the patched fetcher -------------------------------------------------

    def fetch(self, url: str, **kw):
        self.requests.append(url)
        if self.dead:
            raise net.FetchError(url, None, "unreachable")
        seen = set()
        while url in self.redirects:
            if url in seen:
                raise net.FetchError(url, None, "redirect loop")
            seen.add(url)
            url = self.redirects[url]
        if url not in self.pages and "://" in url and "/" not in url.split("://", 1)[1]:
            url += "/"   # https://host and https://host/ are the same page
        if url not in self.pages:
            raise net.FetchError(url, 404, "not found")
        status, body, headers = self.pages[url]
        if status >= 400:
            raise net.FetchError(url, status, "http error")
        return net.Response(url=url, status=status,
                            headers={k.lower(): v for k, v in headers.items()},
                            body=body)

    def count(self, fragment: str) -> int:
        return sum(1 for u in self.requests if fragment in u)

    @contextlib.contextmanager
    def patched(self):
        with mock.patch.object(net, "fetch", self.fetch):
            yield self


# --------------------------------------------------------------------------
# synthetic content builders
# --------------------------------------------------------------------------

PARA = ("This is a reasonably long paragraph of article prose, written so the "
        "extractor treats it as genuine content rather than navigation chrome. "
        "It talks about things at length, with commas, like an essay would. ")


def post_html(title: str, date_iso: str | None = None, *, paragraphs: int = 3,
              meta_date: bool = True, time_tag: bool = False,
              jsonld_date: str | None = None, og_type: str | None = "article") -> str:
    head = [f"<title>{title} | Fake Blog</title>"]
    if date_iso and meta_date:
        head.append(f'<meta property="article:published_time" content="{date_iso}">')
    if og_type:
        head.append(f'<meta property="og:type" content="{og_type}">')
    if jsonld_date:
        head.append('<script type="application/ld+json">'
                    f'{{"@type": "BlogPosting", "datePublished": "{jsonld_date}"}}'
                    "</script>")
    body = [f"<h1>{title}</h1>"]
    if date_iso and time_tag:
        body.append(f'<time class="entry-date" datetime="{date_iso}">then</time>')
    body += [f"<p>{PARA}</p>" for _ in range(paragraphs)]
    return (f"<html><head>{''.join(head)}</head>"
            f"<body><article>{''.join(body)}</article></body></html>")


def listing_html(title: str, links: list[str], *, next_page: str | None = None) -> str:
    items = "".join(f'<li><a href="{u}">{u.rsplit("/", 2)[-2] or u}</a></li>'
                    for u in links)
    nav = f'<a rel="next" href="{next_page}">Older</a>' if next_page else ""
    return (f"<html><head><title>{title}</title></head><body>"
            f"<h1>{title}</h1><ul>{items}</ul>{nav}</body></html>")


def sitemap_xml(urls: list[str | tuple[str, str]]) -> str:
    entries = []
    for u in urls:
        if isinstance(u, tuple):
            entries.append(f"<url><loc>{u[0]}</loc><lastmod>{u[1]}</lastmod></url>")
        else:
            entries.append(f"<url><loc>{u}</loc></url>")
    return ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/'
            f'schemas/sitemap/0.9">{"".join(entries)}</urlset>')


def sitemap_index_xml(children: list[str]) -> str:
    entries = "".join(f"<sitemap><loc>{c}</loc></sitemap>" for c in children)
    return f'<?xml version="1.0"?><sitemapindex>{entries}</sitemapindex>'


def rss_xml(site: str, items: list[dict], *, next_page: str | None = None) -> str:
    """items: {link, title, date (RFC822), content?}."""
    blobs = []
    for it in items:
        content = (f"<content:encoded><![CDATA[<p>{PARA * 3}</p>]]></content:encoded>"
                   if it.get("content", True) else "")
        date = f"<pubDate>{it['date']}</pubDate>" if it.get("date") else ""
        blobs.append(f"<item><title>{it['title']}</title><link>{it['link']}</link>"
                     f"{date}{content}</item>")
    nxt = (f'<atom:link rel="next" href="{next_page}"/>' if next_page else "")
    return (f'<?xml version="1.0"?><rss version="2.0" '
            f'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
            f'xmlns:atom="http://www.w3.org/2005/Atom">'
            f"<channel><title>Fake</title><link>{site}</link>{nxt}"
            f"{''.join(blobs)}</channel></rss>")
