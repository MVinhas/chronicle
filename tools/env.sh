# Point the source-checkout CLI at a THROWAWAY dev library.
#
# The real library lives in the Flatpak app's data directory, which Flatpak
# hides from every other sandbox. To touch the real archive, use the CLI that
# ships inside the app:
#
#     flatpak run --command=chronicle-cli io.github.mvinhas.Chronicle sync
#
# To run the app itself against a different library, set CHRONICLE_LIBRARY --
# Flatpak sets XDG_DATA_HOME for the app and ignores --env=XDG_DATA_HOME, so
# that is the only override that works inside the sandbox:
#
#     flatpak run --env=CHRONICLE_LIBRARY=/path/to/lib io.github.mvinhas.Chronicle
#
_chronicle_root=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)
export CHRONICLE_LIBRARY="${CHRONICLE_LIBRARY:-$_chronicle_root/.scratch/devlib}"
mkdir -p "$CHRONICLE_LIBRARY"
