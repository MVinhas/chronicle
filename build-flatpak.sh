#!/bin/sh
# Build and install Chronicle as a Flatpak from this checkout.
#
# The canonical manifest in build-aux/ is the one Flathub uses: it pins a git
# tag, which Flathub requires and which is what makes released builds
# reproducible. For a local build we want the working tree instead, so this
# script derives a throwaway manifest with a `dir` source.
set -e
here=$(cd "$(dirname "$0")" && pwd)
appid=io.github.mvinhas.Chronicle
manifest="$here/build-aux/$appid.json"
builddir="$here/.flatpak-build"
devmanifest="$here/build-aux/.dev-manifest.json"

echo "==> Ensuring the GNOME 50 runtime is present"
flatpak install -y --noninteractive flathub \
    org.gnome.Platform//50 org.gnome.Sdk//50 2>/dev/null || true

if command -v flatpak-builder >/dev/null 2>&1; then
    BUILDER="flatpak-builder"
else
    echo "==> flatpak-builder not found; using org.flatpak.Builder"
    flatpak install -y --noninteractive flathub org.flatpak.Builder 2>/dev/null || true
    BUILDER="flatpak run org.flatpak.Builder"
fi

echo "==> Deriving a local manifest that builds this working tree"
python3 - "$manifest" "$devmanifest" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
m = json.load(open(src))
m["modules"][0]["sources"] = [{
    "type": "dir",
    "path": "..",
    "skip": [".flatpak-build", ".flatpak-builder", ".scratch", ".git",
             "tests", "__pycache__", "devlib"],
}]
json.dump(m, open(dst, "w"), indent=4)
PY

echo "==> Building"
rm -rf "$builddir"
# --user installs into ~/.local/share/flatpak; no root needed on Silverblue.
$BUILDER --force-clean --user --install "$builddir" "$devmanifest"
rm -f "$devmanifest"

echo
echo "Installed. Run it with:"
echo "    flatpak run $appid"
