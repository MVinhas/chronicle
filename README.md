<div align="center">
  <img src="data/icons/hicolor/scalable/apps/io.github.mvinhas.Chronicle.svg" width="112" alt="Chronicle">
  <h1>Chronicle</h1>
  <p><strong>Read the blogs you follow in one chronological queue — oldest first.</strong></p>
</div>

---

Chronicle builds a **complete historical archive** of the blogs you follow and
merges every article into a single reading queue ordered by original publication
date, regardless of which site it came from:

```
2004-03   Blog A     An early post
2004-07   Blog C     Something from the same summer
2005-01   Blog B     …
2005-02   Blog A     …
```

It is a way to read a body of writing in the order it was written, rather than
in the order you happened to discover it.

Chronicle ships with **no blogs configured**. You add what you read.

## Why not a feed reader

Feed readers solve a different problem. They tell you what is *new*; Chronicle
reconstructs what *was*. RSS feeds typically expose only the most recent handful
of posts, some sites have no feed at all, some publish unreliable date metadata,
and some sit behind bot protection a feed reader cannot pass.

So Chronicle does not force every site through one mechanism. When you add a
blog it probes the site and picks the route that recovers the most history:

| Route | Used when | Yields |
|---|---|---|
| WordPress REST API | `/wp-json/wp/v2/posts` answers | every post, publisher timestamps, full bodies |
| Ghost Content API | the site is Ghost (the key is read from the page) | every post, exact `published_at` |
| Sitemap crawl | a sitemap exists | every listed page, dates read per page |
| RSS / Atom | nothing better is available | recent posts only — flagged as incomplete |

### Recipes for awkward sites

Generic detection handles most blogs. Some need real work, so Chronicle ships
**recipes** — purpose-built adapters that activate automatically when you add a
matching site, and are otherwise inert:

- **A wiki-style site that revises essays for years.** Feed readers trust the
  modification date and file a 2009 essay under 2026. The recipe reads the
  Dublin Core `dc.date.issued` for the original date and keeps `dcterms.modified`
  separately.
- **A hand-written static site with no date metadata at all.** The only
  authoritative date is the dateline printed at the top of each essay, which
  gives month precision — so month precision is what gets recorded, rather than
  a fabricated day.
- **A WordPress blog behind Cloudflare**, where the REST API, the sitemap and
  even `/feed/` all return 403. The recipe rebuilds the archive from the
  Internet Archive and takes each date from the permalink itself.

**Is a blog you read handled badly?**
[Open an adapter request](https://github.com/MVinhas/chronicle/issues/new?labels=adapter-request)
with the URL and what goes wrong. Adapters are small and self-contained — see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Publication dates

Reading order is the whole point, so dates are treated as evidence rather than
as a single number:

- The **original** publication date is recorded — never the date of discovery.
- Every date stores its **provenance** (`meta:dc.date.issued`, `url:permalink`,
  `text:dateline`, …) and a **confidence**: `exact`, `high`, `medium` or
  `inferred`.
- **Precision** is kept honestly. A month-precision date displays as
  "February 2002", not "1 February 2002".
- Uncertain dates show as *circa* with the reason on hover. Articles whose date
  cannot be determined go to an **Undated** section rather than being dropped
  into the timeline at a guessed position.

A worked example: on a site whose index page is chronological in its tail but
curated at its head, an undated essay is bracketed between its neighbours *only*
when those neighbours are actually in sequence and close together. Otherwise it
stays undated. Chronicle would rather show you a gap than invent a date.

## Reading

The reader is deliberately plain — no social features, no recommendations, no
animation. Typography is the feature: Source Serif 4 at a ~65-character measure
with 1.62 leading. Headings, lists, quotations, code, tables, figures and
captions are preserved, and images are downloaded and cached locally so the
library keeps working offline.

Two palettes, a warm paper light and a low-contrast dark, neither using pure
black or pure white. Chronicle follows the desktop's preference by default;
`Ctrl+T` cycles system → light → dark and the choice is remembered.

Reading position is remembered per article, and the queue resumes where you left
off.

### Keyboard

| | |
|---|---|
| `→` `N` `J` | next article |
| `←` `P` `K` | previous article |
| `Space` / `Shift+Space` | scroll |
| `F` | favourite |
| `R` | mark read / unread |
| `L` / `Esc` | library |
| `Ctrl+F` or `/` | search |
| `Ctrl+T` | switch theme |
| `F5` | update archive |
| `Ctrl+O` | open the original page |

## Install

```sh
flatpak install flathub io.github.mvinhas.Chronicle
```

Or build from source (requires Flatpak and the GNOME 50 runtime):

```sh
git clone https://github.com/MVinhas/chronicle
cd chronicle
./build-flatpak.sh
flatpak run io.github.mvinhas.Chronicle
```

Then open **Blogs**, add a site, and choose **Update all**. The first build of a
long-running blog fetches every article it has ever published, so it takes a
while and is rate-limited to stay polite. Later updates are incremental, and
articles are never duplicated.

### Running from a checkout

```sh
./run-dev.sh        # runs against the GNOME SDK runtime
tools/run-tests.sh  # unit tests, no network, no GUI
```

## Command line

The archive is scriptable, which is what makes scheduled updates and future
export formats possible:

```sh
cli() { flatpak run --command=chronicle-cli io.github.mvinhas.Chronicle "$@"; }

cli add https://example.com  # detect and follow a new blog
cli sources                  # what you follow
cli sync                     # build or update everything
cli sync --source example    # just one
cli stats                    # coverage and date confidence
cli queue --scope unread     # the reading queue
cli export --content         # JSON dump of the library
```

The CLI ships **inside** the Flatpak on purpose. A Flatpak app's data lives in
`~/.var/app/io.github.mvinhas.Chronicle/data`, which is deliberately hidden from
every other sandbox, so a CLI run from outside would quietly build a different,
invisible library.

Only one Chronicle process may hold the library at a time, so close the app
before running a sync from the CLI; it will say so rather than starting a second
writer.

To keep a second, separate library — a scratch one for trying a blog out, say —
set `CHRONICLE_LIBRARY`:

```sh
flatpak run --env=CHRONICLE_LIBRARY=~/scratch-library io.github.mvinhas.Chronicle
```

## Design

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

**Stack.** Python 3 + GTK 4 + libadwaita + WebKitGTK 6, packaged as a Flatpak
against the GNOME 50 runtime. SQLite for storage. The only bundled third-party
code is BeautifulSoup and soupsieve, both pure Python; everything else is the
standard library and the runtime.

Articles are stored as sanitised HTML rather than as scraped text, which is what
keeps images, structure and captions intact — and is the seam a future **EPUB /
e-reader export** builds on. Nothing in the storage model is tied to the desktop
UI.

## Licence

[GPL-3.0-or-later](LICENSE). Bundled Source Serif 4 is licensed under the SIL
Open Font License (see `data/fonts/OFL.txt`).
