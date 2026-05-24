from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_id(*parts: str) -> str:
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def iso_from_timestamp(timestamp: float | int | None) -> str | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).replace(microsecond=0).isoformat()
    except (OSError, ValueError, TypeError):
        return None


def normalize_path_part(value: str) -> str:
    parts = [
        part
        for part in value.replace("\\", "/").split("/")
        if part and part not in {".", ".."}
    ]
    return "/".join(parts)


def atomic_write_bytes(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, target)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            logging.debug("Cannot remove temporary file %s.", temporary)


def atomic_write_json(target: Path, payload: Any) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    atomic_write_bytes(target, content)


def clean_title(raw: str) -> str:
    stem = Path(raw).stem
    stem = re.sub(r"[\._]+", " ", stem)
    stem = re.sub(r"\b(480p|576p|720p|1080p|2160p|4k|uhd|hdr|dv|bluray|blu-ray|web-dl|webrip|hdtv|x264|x265|h264|h265|hevc|remux|proper|repack)\b", " ", stem, flags=re.I)
    stem = re.sub(r"[\[\(]?(19|20)\d{2}[\]\)]?", " ", stem)
    stem = re.sub(r"\s+", " ", stem)
    return stem.strip(" -._")


def extract_year(*values: str | None) -> int | None:
    for value in values:
        if not value:
            continue
        match = re.search(r"\b(19|20)\d{2}\b", value)
        if match:
            return int(match.group(0))
    return None


def is_season_folder(value: str) -> bool:
    return bool(re.search(r"\b(season|staffel|saison)\s*\d+\b|\bs\d{1,2}\b", value, flags=re.I))


def coerce_number_to_int(value: Any) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def coerce_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        return int(match.group(0)) if match else None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            number = coerce_int(item)
            if number is not None:
                return number
    return None


def coerce_int_list(value: Any) -> list[int]:
    raw_values = value if isinstance(value, (list, tuple, set)) else [value]
    numbers: list[int] = []
    for raw_value in raw_values:
        number = coerce_int(raw_value)
        if number is not None and number not in numbers:
            numbers.append(number)
    return numbers
