#!/bin/sh
# Run Chronicle from a source checkout inside the GNOME SDK runtime.
# The SDK supplies GTK 4, libadwaita and WebKitGTK 6; the repo supplies the rest.
set -e
here=$(cd "$(dirname "$0")" && pwd)
app_data="$HOME/.var/app/io.github.mvinhas.Chronicle"
mkdir -p "$app_data/data" "$app_data/cache" "$app_data/config"

exec flatpak run \
  --filesystem="$here" --filesystem="$app_data" \
  --share=network --socket=wayland --socket=fallback-x11 --device=dri \
  --env=PYTHONPATH="$here/vendor:$here/src" \
  --env=XDG_DATA_HOME="$app_data/data" \
  --env=XDG_CACHE_HOME="$app_data/cache" \
  --env=XDG_CONFIG_HOME="$app_data/config" \
  --env=GDK_BACKEND="${GDK_BACKEND:-wayland,x11}" \
  --command=python3 org.gnome.Sdk//50 -m chronicle "$@"
