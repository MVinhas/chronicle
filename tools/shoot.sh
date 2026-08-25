#!/bin/sh
# Visual review harness: run Chronicle, have it render its own window to PNG, exit.
#   tools/shoot.sh <out.png> [page] [delay-seconds]
# page: reader | library | sources
# Must run on the HOST (flatpak-spawn --host) — the app needs a display.
set -e
here=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-$here/.scratch/shot.png}
page=${2:-reader}
delay=${3:-5}

app_data="$HOME/.var/app/io.github.mvinhas.Chronicle"
mkdir -p "$app_data/data" "$app_data/cache" "$app_data/config" \
         "$(dirname "$out")" "$here/.scratch"
rm -f "$out"

timeout 90 flatpak run \
  --filesystem="$here" --filesystem="$app_data" \
  --share=network --share=ipc --socket=wayland --socket=fallback-x11 \
  --socket=session-bus --device=dri \
  --env=PYTHONPATH="$here/vendor:$here/src" \
  --env=XDG_DATA_HOME="$app_data/data" \
  --env=XDG_CACHE_HOME="$app_data/cache" \
  --env=XDG_CONFIG_HOME="$app_data/config" \
  --env=CHRONICLE_DEV=1 \
  --env=CHRONICLE_SHOT="$out" \
  --env=CHRONICLE_SHOT_PAGE="$page" \
  --env=CHRONICLE_SHOT_DELAY="$delay" \
  --command=python3 org.gnome.Sdk//50 -m chronicle \
  >"$here/.scratch/app.log" 2>&1 || true

if [ -f "$out" ]; then
  echo "screenshot: $out ($(stat -c%s "$out") bytes)"
else
  echo "NO SCREENSHOT — app log:" >&2
fi
tail -20 "$here/.scratch/app.log"
