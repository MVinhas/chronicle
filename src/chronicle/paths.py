"""Filesystem locations. XDG-compliant, Flatpak-safe."""
from __future__ import annotations

import os
from pathlib import Path

APP_ID = "io.github.mvinhas.Chronicle"


def _xdg(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or Path.home() / default)


DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "chronicle"
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / "chronicle"
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "chronicle"

DB_PATH = DATA_DIR / "library.db"
IMAGE_DIR = DATA_DIR / "images"
HTTP_CACHE_DIR = CACHE_DIR / "http"
LOG_PATH = CACHE_DIR / "chronicle.log"


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, CONFIG_DIR, IMAGE_DIR, HTTP_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def image_path(digest: str, ext: str) -> Path:
    """Content-addressed image path: images/ab/abcdef...png"""
    sub = IMAGE_DIR / digest[:2]
    sub.mkdir(parents=True, exist_ok=True)
    return sub / f"{digest}{ext}"


def image_relpath(digest: str, ext: str) -> str:
    return f"{digest[:2]}/{digest}{ext}"
