# Vendored dependencies

Chronicle bundles only pure-Python libraries, so the Flatpak needs no build
step beyond copying files. Everything else it uses comes from the standard
library or the GNOME runtime.

| Package | Why |
|---|---|
| `beautifulsoup4` | HTML parsing for content extraction and sanitisation |
| `soupsieve` | CSS selector support used by BeautifulSoup |
| `typing_extensions` | required by `beautifulsoup4` at import time |

Refresh with:

```sh
pip install --target vendor --upgrade beautifulsoup4 soupsieve
rm -rf vendor/*.dist-info vendor/__pycache__
```
