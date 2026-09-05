# Contributing to Chronicle

## Writing an adapter

Most blogs need no code: detection first looks for an API (WordPress REST,
Ghost Content), and otherwise the generic engine gathers evidence from *every*
route the site offers — its feed (with pagination), its sitemaps (found via
robots.txt or convention), and its own archive pages — merges the results into
one candidate pool per canonical URL, classifies out the non-articles, and only
then fetches what still needs fetching (see `sources/discovery.py`). An adapter
("recipe") is worth writing only when a site's *full history* cannot be
recovered any other way, or when its publication dates need site-specific
knowledge to get right. Before writing one, ask whether the generic engine
could be taught the general pattern instead.

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

### Rules for routine updates

`discover()` is called for both jobs the app offers, and the difference is
`ctx.newest_only`. Ignoring it is not a neutral choice: three adapters did, and
"fetch new posts" quietly re-derived each site's entire history — gwern.net
fetching all 669 of its pages to read one date from each, eight minutes of it.
The button has to cost seconds, so pick whichever of these the site supports:

- **Ask the server.** `ctx.since()` gives a cutoff, or `None` for a full scan.
  An API that filters by date turns the whole update into one request —
  WordPress takes `after=`, Ghost takes `filter=published_at:>…`.
- **Stop early.** Read a newest-first route and give up as soon as
  `ctx.predates_archive(stub.date)` is true.
- **Enumerate by identity.** For a site with no dates to enumerate by at all,
  skip candidates where `ctx.no_direct(guid)` is true — already archived, with
  a body — or that are in `ctx.rejected`.

And call `ctx.reject(guid)` for any page you fetched and found is not an
article. Without that verdict every later sync pays a request to reach the same
conclusion.

Correctness never depends on the flag. A full scan must still find everything,
and nothing may be *dropped* on these tests — they only decide what is worth
asking for.

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

`db.upsert_article()` treats the source's latest reading as authoritative in
either direction — a corrected adapter may honestly become *less* certain —
with one hard rule: an unknown date never overwrites a known one, so a failed
fetch cannot erase good data.

## Command line

The archive is scriptable, which is what makes scheduled updates and export
formats possible. The CLI ships **inside** the Flatpak on purpose: a Flatpak
app's data lives in `~/.var/app/io.github.mvinhas.Chronicle/data`, hidden from
every other sandbox, so a CLI run from outside would quietly build a different,
invisible library.

```sh
cli() { flatpak run --command=chronicle-cli io.github.mvinhas.Chronicle "$@"; }

cli add https://example.com  # detect and follow a new blog
cli sources                  # what you follow
cli rename example "Example" # change a blog's display name
cli sync                     # build or update everything
cli sync --source example    # just one
cli stats                    # coverage and date confidence
cli queue --scope unread     # the reading queue
cli export --content         # JSON dump of the library
```

Only one Chronicle process may hold the library at a time, so close the app
before running a sync; it will say so rather than starting a second writer.

To keep a second, separate library, set `CHRONICLE_LIBRARY`:

```sh
flatpak run --env=CHRONICLE_LIBRARY=~/scratch-library io.github.mvinhas.Chronicle
```

## How it fits together

```
src/chronicle/
  db.py          SQLite schema, the chronological queue, reading state
  dates.py       date parsing with precision + confidence + provenance
  net.py         polite HTTP: rate limiting, retries, conditional caching
  htmlutil.py    content extraction (readability-style) and sanitisation
  images.py      content-addressed image cache
  sync.py        archive building, progress, cancellation
  sources/       one module per ingestion strategy
  ui/            GTK4 + libadwaita; the reader is a WebKitGTK surface
```

Python 3 + GTK 4 + libadwaita + WebKitGTK 6, packaged as a Flatpak against the
GNOME 50 runtime. SQLite for storage. The only bundled third-party code is
BeautifulSoup and soupsieve, both pure Python.

Articles are stored as sanitised HTML rather than scraped text, which is what
keeps images, structure and captions intact — and is the seam an EPUB export
would build on.

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
de-duplication. `tests/test_discovery.py` exercises the generic discovery
engine against synthetic sites (`tests/fakesite.py` fakes the network with
request counting), proving properties like "overlapping routes merge to one
article" and "a re-sync makes no per-article requests" rather than testing any
one real website. Neither needs the network. New adapters should come with a
test for their date extraction — a small fixture of the site's real markup is
enough.

## Style

Match the surrounding code. Comments explain *why* something is done, especially
where a site's behaviour forced the decision; the code already says what it does.
