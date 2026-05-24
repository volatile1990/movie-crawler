from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from constants import DEFAULT_OPTIONS


def option_int(options: dict[str, Any], key: str, minimum: int) -> int:
    default = int(DEFAULT_OPTIONS[key])
    try:
        value = int(options.get(key, default))
    except (TypeError, ValueError):
        logging.warning("Invalid integer option %s=%r; using default %s.", key, options.get(key), default)
        value = default
    return max(minimum, value)


def load_options() -> dict[str, Any]:
    options = dict(DEFAULT_OPTIONS)
    options_path = Path(os.environ.get("CRAWLER_OPTIONS_PATH", "/data/options.json"))
    if options_path.exists():
        try:
            with options_path.open("r", encoding="utf-8-sig") as handle:
                loaded = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Cannot read options file %s; using defaults: %s", options_path, exc)
        else:
            if isinstance(loaded, dict):
                options.update(loaded)
            else:
                logging.warning("Options file %s does not contain an object; using defaults.", options_path)
    else:
        logging.warning("Options file %s not found; using defaults.", options_path)

    if not isinstance(options.get("sources"), list) or not all(
        isinstance(source, dict) for source in options.get("sources", [])
    ):
        logging.warning("Invalid sources option; using defaults.")
        options["sources"] = DEFAULT_OPTIONS["sources"]
    if not isinstance(options.get("libraries"), list):
        logging.warning("Invalid libraries option; using defaults.")
        options["libraries"] = DEFAULT_OPTIONS["libraries"]

    options["scan_interval_minutes"] = option_int(options, "scan_interval_minutes", 1)
    options["min_file_size_mb"] = option_int(options, "min_file_size_mb", 0)
    options["request_timeout_seconds"] = option_int(options, "request_timeout_seconds", 5)
    options["imdb_cache_ttl_hours"] = option_int(options, "imdb_cache_ttl_hours", 1)
    return options
