"""Where the app keeps its files. Override with the UZRAILBOT_HOME environment variable."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME


def app_dir() -> Path:
    custom = os.environ.get("UZRAILBOT_HOME")
    if custom:
        base = Path(custom)
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def data_dir() -> Path:
    d = app_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return data_dir() / "config.json"


def state_path() -> Path:
    return data_dir() / "state.json"


def log_path() -> Path:
    d = app_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "uzrailbot.log"


def lock_path() -> Path:
    return data_dir() / "running.lock"


def tickets_dir() -> Path:
    d = app_dir() / "tickets"
    d.mkdir(parents=True, exist_ok=True)
    return d
