#!/bin/sh
here=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$here/vendor:$here/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m unittest discover -s "$here/tests" -v "$@"
