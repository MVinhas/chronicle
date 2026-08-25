#!/bin/sh
# Build and install Chronicle as a Flatpak.
#
# Fedora Silverblue has no flatpak-builder layered by default, so fall back to
# the Flathub-packaged builder (org.flatpak.Builder) when the host lacks one.
set -e
here=$(cd "$(dirname "$0")" && pwd)
manifest="$here/build-aux/io.github.mvinhas.Chronicle.json"
builddir="$here/.flatpak-build"

echo "==> Ensuring the GNOME 49 runtime is present"
flatpak install -y --noninteractive flathub \
    org.gnome.Platform//49 org.gnome.Sdk//49 2>/dev/null || true

if command -v flatpak-builder >/dev/null 2>&1; then
    BUILDER="flatpak-builder"
else
    echo "==> flatpak-builder not found; using org.flatpak.Builder"
    flatpak install -y --noninteractive flathub org.flatpak.Builder 2>/dev/null || true
    BUILDER="flatpak run org.flatpak.Builder"
fi

echo "==> Building"
rm -rf "$builddir"
# --user installs into ~/.local/share/flatpak; no root needed on Silverblue.
# Deps are installed above; --install-deps-from would look for a *user* remote.
$BUILDER --force-clean --user --install "$builddir" "$manifest"

echo
echo "Installed. Run it with:"
echo "    flatpak run io.github.mvinhas.Chronicle"
