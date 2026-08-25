# Contributing to Chronicle

## Writing an adapter

Most blogs need no code: generic detection tries a WordPress REST API, a Ghost
Content API, a sitemap crawl and finally a feed. An adapter ("recipe") is worth
writing when a site's *full history* cannot be recovered any other way, or when
its publication dates need site-specific knowledge to get right.

An adapter is one module in `src/chronicle/sources/`. It answers two questions:
how to enumerate everything the site has ever published, and how to turn one of
those URLs into clean reader HTML.

```python
from .. import dates, htmlutil, net
from .base import Content, Context, Source, Stub


class ExampleSource(Source):
    plugin_id = "example"
    display_name = "Example"
    content_selectors = ["#article-body", "article"]   # tried before scoring

    def discover(self, ctx: Context):
        ctx.say("Reading the example.com index…")
        for order, url in enumerate(self._all_urls(ctx)):
            ctx.check()                    # honour cancellation
            yield Stub(
                guid=net.canonical_url(url),
                url=url,
                title="…",
                date=dates.parse_iso(raw, confidence="exact",
                                     source="meta:article:published_time"),
                source_order=order,
            )
```

Then register it in `sources/__init__.py`: add the class to `REGISTRY`, and add
an entry to `RECIPES` so it activates automatically when someone adds a matching
host.

`fetch_content()` only needs overriding when the shared extractor is not enough —
if the site's markup is conventional, the base class handles extraction and
sanitisation for you.

### Rules for dates

This is the part that matters most, because dates decide reading order.

- Record the **original** publication date. Never a modification date, and never
  the date of discovery.
- Always set `confidence`: `exact` (publisher metadata or API field), `high`
  (unambiguous machine-readable signal such as a date in the permalink),
  `medium` (parsed from a conventional position in the page) or `inferred`
  (derived from ordering — genuinely uncertain).
- Always set `precision` honestly. If a site only tells you the month, use
  `PRECISION_MONTH`; do not pick a day.
- Set `source` to a short provenance string. It is shown to the reader.
- If you cannot determine a date, **leave it unknown**. An article with no date
  lands in the Undated section, which is correct. Guessing puts it in the wrong
  place in someone's reading queue, silently.

`db.upsert_article()` only ever overwrites a date with one of strictly higher
confidence, so a later, better signal wins and a worse one cannot regress it.

## Development

The app is Python 3 + GTK 4 + libadwaita + WebKitGTK 6, run against the GNOME
SDK runtime.

```sh
flatpak install flathub org.gnome.Platform//50 org.gnome.Sdk//50
./run-dev.sh          # run from the checkout
tools/run-tests.sh    # unit tests: no network, no GUI
./build-flatpak.sh    # build and install the Flatpak
```

For backend work without a GUI:

```sh
export PYTHONPATH="$PWD/vendor:$PWD/src"
. tools/env.sh                     # a throwaway dev library
tools/chronicle-cli add https://example.com
tools/chronicle-cli sync --source example
```

### Verifying UI changes

There is no usable compositor screenshot on modern GNOME, so the app renders
itself through its own GSK renderer:

```sh
tools/shoot.sh /tmp/shot.png reader 7     # reader | library | sources
```

Any change to the interface, the reader stylesheet or article rendering should
be checked with a screenshot before it is called done.

## Tests

`tests/test_core.py` covers the parts that decide reading order and reading
quality: date parsing, URL canonicalisation, the sanitiser, queue ordering and
de-duplication. It needs no network. New adapters should come with a test for
their date extraction — a small fixture of the site's real markup is enough.

## Style

Match the surrounding code. Comments explain *why* something is done, especially
where a site's behaviour forced the decision; the code already says what it does.
