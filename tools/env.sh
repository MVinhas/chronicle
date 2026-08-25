# Point the source-checkout CLI at a THROWAWAY dev library.
#
# The real library lives in the Flatpak app's data directory
# (~/.var/app/io.github.mvinhas.Chronicle/data), which Flatpak hides from every other
# sandbox. To touch the real archive, use the CLI that ships inside the app:
#
#     flatpak run --command=chronicle-cli io.github.mvinhas.Chronicle sync
#
export CHRONICLE_DEV_HOME="${CHRONICLE_DEV_HOME:-$HOME/Workspace/chronicle/.scratch/devlib}"
mkdir -p "$CHRONICLE_DEV_HOME/data" "$CHRONICLE_DEV_HOME/cache" "$CHRONICLE_DEV_HOME/config"
export XDG_DATA_HOME="$CHRONICLE_DEV_HOME/data"
export XDG_CACHE_HOME="$CHRONICLE_DEV_HOME/cache"
export XDG_CONFIG_HOME="$CHRONICLE_DEV_HOME/config"
