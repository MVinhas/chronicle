"""paulgraham.com — 230+ essays in hand-written 1997-era HTML.

There is no publication metadata anywhere on the site. Every essay instead
opens with a dateline ("February 2002") as its first line of body text, which
is the only authoritative date available. A feed reader that trusts HTTP
Last-Modified gets these badly wrong, which is exactly the failure the user hit.

Three essays predate the dateline convention and carry no date at all. Rather
than invent one, we bracket them between their neighbours in articles.html
(which is chronological in its tail) and mark them 'inferred', so the reader
can see the date is an estimate.

Body text lives inside <font face="verdana"> with <br><br> as paragraph
separators, and quotations are yellow <table bgcolor="#ffffdd"> boxes.
"""
from __future__ import annotations

import re

from .. import dates, htmlutil, net
from .base import Content, Context, Source, Stub, assess, probe_all

INDEX = "https://paulgraham.com/articles.html"
BASE = "https://paulgraham.com/"

# Site furniture and non-essay pages linked from the same index.
DENY = {
    "index.html", "articles.html", "books.html", "arc.html", "bel.html",
    "lisp.html", "antispam.html", "kedrosky.html", "faq.html", "raq.html",
    "quo.html", "rss.html", "bio.html", "sfp.html", "lib.html", "ilink.html",
    "arcchallenge.html", "arc0.html", "arc1.html", "bookshelf.html",
    "goodart.html", "carl.html", "rootsoflisp.html", "onlisp.html",
}

_SLUG_RE = re.compile(r'href="([a-z0-9][a-z0-9_.-]*\.html)"', re.I)
_MONTH_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{4})\b", re.I)
_DATELINE_LIMIT = 14   # lines of body text in which a dateline may appear


class PaulGrahamSource(Source):
    plugin_id = "paulgraham"
    display_name = "Paul Graham"
    discover_concurrency = 6
    # Site-wide promotional insert, not part of any essay.
    _PROMO_RE = re.compile(
        r"want to start a startup\?|get funded by\s*y\s*combinator", re.I)
    # A dateline *opens* its line. Anything may follow -- the site records
    # revisions in several shapes: "rev. April 2007", "(rev. May 2002)",
    # "corrected June 2006", or a bare list of later dates.
    _DATELINE_START = re.compile(
        r"^(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{4})\b", re.I)
    # Every image on the site is a title GIF, a spacer or navigation chrome.
    image_blocklist = Source.image_blocklist + (
        "turbifycdn.com", "yimg.com", "ycombinator.com/arc/",
    )

    def discover(self, ctx: Context):
        ctx.say("Reading paulgraham.com essay index…")
        html = net.fetch_text(INDEX)

        slugs: list[str] = []
        for m in _SLUG_RE.finditer(html):
            s = m.group(1).lower()
            if s in DENY or s in slugs:
                continue
            slugs.append(s)
        # The dateline lives in the essay's own body, so there is nothing to
        # enumerate by but the essays themselves -- 229 requests, minutes of
        # them. An essay already archived with its body has nothing further to
        # give, so only the ones never resolved are read.
        todo = [(i, slug) for i, slug in enumerate(slugs)
                if not ctx.no_direct(net.canonical_url(BASE + slug))]
        complete = len(todo) == len(slugs)
        ctx.say(f"paulgraham.com: {len(slugs)} essays listed, "
                f"{len(todo)} to read")
        ctx.result_note = f"index {len(slugs)}, read {len(todo)}"

        # The index order matters for bracketing the undated essays, so
        # collect in order and only then yield.
        def read(item):
            i, slug = item
            url = BASE + slug
            try:
                resp = net.fetch(url)
            except net.FetchError:
                return None
            raw = resp.text()
            title, date = self._title_and_date(raw, slug)
            return Stub(guid=net.canonical_url(url), url=url, title=title,
                        date=date, author="Paul Graham", source_order=i,
                        raw_html=raw, base_url=resp.url, content_source="direct")

        found = [s for s in probe_all(ctx, todo, read,
                                      workers=self.discover_concurrency,
                                      label="paulgraham.com: reading")
                 if s is not None]

        # Bracketing reads an undated essay's date off its neighbours in the
        # index, so it is only meaningful when every neighbour is present. On
        # a partial pass the essays either side may simply not have been read,
        # and a window measured against the wrong pair would invent a date --
        # exactly what this source exists to avoid. Undated is the honest
        # answer until the next full scan.
        if complete:
            self._bracket_undated(found)
        yield from found

    # -- metadata ----------------------------------------------------------

    @staticmethod
    def _body_lines(raw: str) -> list[str]:
        text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
        text = re.sub(r"<[^>]+>", "\n", text)
        import html as _h
        text = _h.unescape(text)
        return [l.strip() for l in text.split("\n") if l.strip()]

    def _title_and_date(self, raw: str, slug: str) -> tuple[str, dates.PubDate]:
        lines = self._body_lines(raw)
        # The document title is authoritative; the first body line repeats it
        # as an artefact of the site's image-replacement markup.
        m = re.search(r"<title>(.*?)</title>", raw, re.S | re.I)
        title = htmlutil.clean_title(m.group(1) if m else slug)
        if title.lower() in ("essays", "", "untitled") and lines:
            title = lines[0].replace("-->", "").strip()

        return title.replace("-->", "").strip() or slug, self._dateline(lines)

    @classmethod
    def _dateline(cls, lines: list[str]) -> dates.PubDate:
        """Find the essay's dateline.

        The dateline opens its own line. Requiring that, rather than matching a
        date anywhere in the opening lines, is what separates it from two other
        things that look alike:

        * a title naming the period it describes -- "Snapshot: Viaweb, June
          1998" was written in January 2012, and the loose rule dated it
          fourteen years early;
        * a date mentioned in prose -- "a talk I gave in April 2001" is not a
          publication date, and an essay with no dateline should stay undated
          rather than borrow one from its first paragraph.

        Only the leading date is taken; any revision dates that follow are
        deliberately ignored, since the original publication date is what
        orders the reading queue.
        """
        for line in lines[:_DATELINE_LIMIT]:
            m = cls._DATELINE_START.match(line.strip())
            if m:
                return dates.parse_freeform(
                    f"{m.group(1)} {m.group(2)}",
                    confidence="medium", source="text:dateline")
        return dates.UNKNOWN

    # Beyond this, neighbouring essays say nothing useful about a date.
    _MAX_BRACKET_YEARS = 4

    @classmethod
    def _bracket_undated(cls, stubs: list[Stub]) -> None:
        """Estimate dates for the few essays with no dateline.

        articles.html is chronological through most of its tail, so an undated
        essay usually sits between two dated neighbours. But the head of the
        list is a curated selection and a couple of entries are appended out of
        order, so the neighbours are not always in sequence. We infer a date
        only where the surrounding order actually is monotonic and the window
        is narrow; otherwise the essay keeps no date at all and the reader
        shows it as undated rather than placing it somewhere invented.
        """
        from datetime import datetime

        for i, stub in enumerate(stubs):
            if stub.date.known:
                continue
            newer = next((s.date.iso for s in reversed(stubs[:i]) if s.date.known), None)
            older = next((s.date.iso for s in stubs[i + 1:] if s.date.known), None)
            if not (older and newer):
                continue
            a, b = datetime.fromisoformat(older), datetime.fromisoformat(newer)
            if a > b:
                continue                       # neighbours out of sequence
            if (b - a).days > cls._MAX_BRACKET_YEARS * 366:
                continue                       # window too wide to mean anything
            est = dates.interpolate(older, newer, "index:bracketed-neighbours")
            stubs[i] = Stub(**{**stub.__dict__, "date": est})

    # -- content -----------------------------------------------------------

    def fetch_content(self, ctx: Context, url: str, stub_html=None, base_url=None,
                      extra: dict | None = None) -> Content:
        raw = stub_html
        if raw is None:
            resp = net.fetch(url)
            raw, base_url = resp.text(), resp.url
        return self._extract(raw, base_url or url)

    def _extract(self, raw: str, base_url: str) -> Content:
        soup = htmlutil.parse(raw)

        # Navigation lives in an imagemap; drop it and the site chrome.
        for tag in soup.find_all(["map", "script", "style", "noscript"]):
            tag.decompose()

        blocks = [f for f in soup.find_all("font")
                  if (f.get("face") or "").lower().startswith("verdana")]
        if not blocks:
            node = htmlutil.extract_main(soup, [])
            return self.clean(str(node), base_url, source="direct")

        holder = htmlutil.parse("<div></div>")
        root = holder.div
        for f in blocks:
            for child in list(f.contents):
                root.append(child.extract())

        self._strip_promo(root)
        self._strip_dateline_node(root)
        self._quotes_to_blockquotes(root)
        self._unwrap_layout_tables(root)
        self._drop_empty_divs(root)
        html = htmlutil.sanitise(root, base_url)
        html = self._unwrap_divs(html)
        html = self._paragraphise(html)
        html = self.drop_decorative_images(html)
        html = self._strip_furniture(html)

        return Content(html, status=assess(html), source="direct")

    @classmethod
    def _strip_promo(cls, root) -> None:
        """Remove the YC banner while the original table structure survives."""
        for node in list(root.find_all(["b", "font", "td", "p", "div"])):
            if node.attrs is None or getattr(node, "decomposed", False):
                continue
            text = node.get_text(" ", strip=True)
            if not text or len(text) > 160 or not cls._PROMO_RE.search(text):
                continue
            target = node
            for anc in node.parents:
                if getattr(anc, "name", None) not in ("table", "tr", "td", "tbody"):
                    break
                if len(anc.get_text(" ", strip=True)) <= 160:
                    target = anc
            if target.parent is not None:
                target.decompose()

    @classmethod
    def _strip_dateline_node(cls, root) -> None:
        """Remove the dateline text node; the reader shows the date itself."""
        from bs4 import NavigableString
        for node in list(root.descendants):
            if not isinstance(node, NavigableString):
                continue
            text = str(node).strip()
            if not text:
                continue
            if cls._DATELINE_START.match(text):
                node.extract()
            return  # only ever the first piece of body text

    @staticmethod
    def _drop_empty_divs(root) -> None:
        for d in list(root.find_all(["div", "font", "center"])):
            if d.attrs is None or getattr(d, "decomposed", False):
                continue
            d.unwrap()

    @staticmethod
    def _quotes_to_blockquotes(root) -> None:
        """The pale-yellow boxes are pull-quotes, not tabular data.

        The colour sits on the inner <td>, so find those and promote the
        outermost table wrapping them.
        """
        tinted = [n for n in root.find_all(["td", "tr", "table"])
                  if str(n.get("bgcolor", "")).lower().lstrip("#") in
                  ("ffffdd", "fff8dc", "ffffe0")]
        seen = set()
        for node in tinted:
            outer = node
            for anc in node.parents:
                if getattr(anc, "name", None) == "table":
                    outer = anc
            if id(outer) in seen or getattr(outer, "name", None) != "table":
                continue
            seen.add(id(outer))
            outer.name = "blockquote"
            for inner in list(outer.find_all(["table", "tbody", "thead", "tr", "td", "th"])):
                inner.unwrap()
            for k in list(outer.attrs):
                del outer[k]

    @staticmethod
    def _unwrap_layout_tables(root) -> None:
        """1990s layout tables carry no meaning; flatten what is left."""
        for t in list(root.find_all(["table", "tbody", "thead", "tr", "td", "th"])):
            if t.find(["th"]) and t.name == "table":
                continue  # a genuine data table
            t.unwrap()

    @staticmethod
    def _unwrap_divs(html: str) -> str:
        """PG's markup has no semantic <div>; drop the layout wrappers."""
        soup = htmlutil.parse(html)
        for d in list(soup.find_all(["div", "span"])):
            if d.attrs is not None and not getattr(d, "decomposed", False):
                d.unwrap()
        return soup.decode()

    @staticmethod
    def _paragraphise(html: str) -> str:
        """<br><br> is PG's paragraph break; convert to real <p> elements."""
        parts = re.split(r"(?:\s*<br\s*/?>\s*){2,}", html, flags=re.I)
        out = []
        for part in parts:
            part = re.sub(r"^(?:\s*<br\s*/?>\s*)+|(?:\s*<br\s*/?>\s*)+$", "", part,
                          flags=re.I).strip()
            if not part:
                continue
            if re.match(r"^\s*<(blockquote|ul|ol|pre|table|h[1-6]|figure)", part, re.I):
                out.append(part)
            else:
                out.append(f"<p>{part}</p>")
        joined = "\n".join(out)
        return re.sub(r"<p>\s*</p>", "", joined)

    @classmethod
    def _strip_furniture(cls, html: str) -> str:
        """Drop the YC banner and the duplicated dateline from the body."""
        soup = htmlutil.parse(html)

        for node in list(soup.find_all(["p", "div", "blockquote", "table"])):
            if getattr(node, "decomposed", False) or node.attrs is None:
                continue
            text = node.get_text(" ", strip=True)
            if text and len(text) < 200 and cls._PROMO_RE.search(text):
                node.decompose()

        # The dateline is the first short standalone block of body text.
        checked = 0
        for node in list(soup.find_all(["p", "div"])):
            if getattr(node, "decomposed", False) or node.attrs is None:
                continue
            text = node.get_text(" ", strip=True)
            if not text:
                continue
            checked += 1
            if checked > 4:
                break
            if len(text) > 80:
                break
            if re.fullmatch(
                    r"(?:January|February|March|April|May|June|July|August|"
                    r"September|October|November|December)\s+\d{4}"
                    r"(?:\s*,?\s*rev\.?\s*.*)?", text.strip(), re.I):
                node.decompose()
                break
        return soup.decode()
