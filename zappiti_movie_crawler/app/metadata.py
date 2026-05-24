from __future__ import annotations

import json
import logging
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from guessit import guessit

from utils import (
    clean_title,
    coerce_int,
    coerce_number_to_int,
    extract_year,
    is_season_folder,
    json_safe,
)


def parse_file_metadata(item: dict[str, Any]) -> dict[str, Any]:
    kind = item["kind"]
    relative_parts = item["relative_path"].split("/")[:-1]
    file_name = item["file_name"]
    guess_type = "episode" if kind == "tv" else "movie"
    guessed = json_safe(guessit(file_name, {"type": guess_type}))

    title = guessed.get("title") if isinstance(guessed, dict) else None
    if kind == "tv":
        title = title_from_series_path(relative_parts) or title or clean_title(file_name)
    else:
        title = title or title_from_movie_path(relative_parts, file_name) or clean_title(file_name)

    year = None
    if isinstance(guessed, dict):
        year_value = guessed.get("year")
        if isinstance(year_value, int):
            year = year_value
    year = year or extract_year(file_name, *relative_parts)

    parsed = {
        "title": title,
        "year": year,
        "parsed": guessed,
    }
    if isinstance(guessed, dict):
        for source_key, target_key in (
            ("season", "season"),
            ("episode", "episode"),
            ("episode_title", "episode_title"),
            ("screen_size", "screen_size"),
            ("source", "source_quality"),
            ("video_codec", "video_codec"),
            ("audio_codec", "audio_codec"),
        ):
            if source_key in guessed:
                parsed[target_key] = json_safe(guessed[source_key])
    return parsed


def title_from_series_path(relative_parts: list[str]) -> str | None:
    candidates = [part for part in relative_parts[1:] if not is_season_folder(part)]
    if candidates:
        return clean_title(candidates[0])
    return None


def title_from_movie_path(relative_parts: list[str], file_name: str) -> str | None:
    if len(relative_parts) < 2:
        return None
    parent = relative_parts[-1]
    if clean_title(file_name).lower() in {"movie", "film", "video"}:
        return clean_title(parent)
    parent_title = clean_title(parent)
    file_title = clean_title(file_name)
    if parent_title and len(parent_title) > len(file_title):
        return parent_title
    return None


def read_local_nfo(item: dict[str, Any]) -> dict[str, Any] | None:
    local_path = item.get("_local_path")
    if not local_path:
        return None
    video_path = Path(local_path)
    candidates = [
        video_path.with_suffix(".nfo"),
        video_path.parent / "movie.nfo",
        video_path.parent / "tvshow.nfo",
    ]
    for candidate in candidates:
        try:
            if not candidate.exists() or candidate.stat().st_size > 1024 * 1024:
                continue
            raw = candidate.read_text(encoding="utf-8", errors="ignore")
            root = ET.fromstring(raw.strip())
        except (OSError, ET.ParseError) as exc:
            logging.debug("Cannot parse NFO %s: %s", candidate, exc)
            continue
        return xml_metadata(root)
    return None


def xml_text(root: ET.Element, *names: str) -> str | None:
    for name in names:
        element = root.find(name)
        if element is not None and element.text:
            return element.text.strip()
    return None


def xml_int(root: ET.Element, *names: str) -> int | None:
    value = xml_text(root, *names)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def xml_uniqueid(root: ET.Element, uniqueid_type: str) -> str | None:
    for element in root.findall("uniqueid"):
        if (element.get("type") or "").lower() == uniqueid_type and element.text:
            return element.text.strip()
    return None


def xml_imdb_id(root: ET.Element) -> str | None:
    imdb_id = xml_text(root, "imdbid") or xml_uniqueid(root, "imdb")
    if imdb_id:
        return imdb_id
    uniqueid = xml_text(root, "uniqueid")
    match = re.search(r"\btt\d+\b", uniqueid or "")
    return match.group(0) if match else None


def xml_metadata(root: ET.Element) -> dict[str, Any]:
    genres = [element.text.strip() for element in root.findall("genre") if element.text]
    year = extract_year(xml_text(root, "year", "premiered", "releasedate"))
    metadata = {
        "metadata_type": root.tag.lower(),
        "provider": "nfo",
        "title": xml_text(root, "title"),
        "show_title": xml_text(root, "showtitle"),
        "original_title": xml_text(root, "originaltitle"),
        "year": year,
        "overview": xml_text(root, "plot", "outline"),
        "imdb_id": xml_imdb_id(root),
        "tmdb_id": xml_text(root, "tmdbid") or xml_uniqueid(root, "tmdb"),
        "season": xml_int(root, "season"),
        "episode": xml_int(root, "episode"),
        "runtime_minutes": xml_text(root, "runtime"),
        "genres": genres,
    }
    return {key: value for key, value in metadata.items() if value not in (None, "", [])}


def apply_nfo_metadata(item: dict[str, Any], metadata: dict[str, Any]) -> None:
    item["local_metadata"] = metadata
    metadata_type = str(metadata.get("metadata_type") or "")

    if item.get("kind") == "tv":
        if metadata.get("show_title"):
            item["title"] = metadata["show_title"]
        elif metadata_type != "episodedetails" and metadata.get("title"):
            item["title"] = metadata["title"]

        if metadata_type == "episodedetails" and metadata.get("title") and not item.get("episode_title"):
            item["episode_title"] = metadata["title"]
        if metadata.get("season") is not None and item.get("season") in (None, "", []):
            item["season"] = metadata["season"]
        if metadata.get("episode") is not None and item.get("episode") in (None, "", []):
            item["episode"] = metadata["episode"]
        return

    item["title"] = metadata.get("title") or item.get("title")
    item["year"] = metadata.get("year") or item.get("year")


def ffprobe(path: str, timeout_seconds: int) -> dict[str, Any] | None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logging.debug("ffprobe failed for %s: %s", path, exc)
        return None
    if completed.returncode != 0:
        logging.debug("ffprobe returned %s for %s: %s", completed.returncode, path, completed.stderr.strip())
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    media_info: dict[str, Any] = {}
    fmt = payload.get("format") or {}
    duration = coerce_number_to_int(fmt.get("duration"))
    bit_rate = coerce_number_to_int(fmt.get("bit_rate"))
    if duration is not None:
        media_info["duration_seconds"] = duration
    if bit_rate is not None:
        media_info["bit_rate"] = bit_rate

    video_streams = [stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"]
    if video_streams:
        stream = video_streams[0]
        media_info["video"] = {
            "codec": stream.get("codec_name"),
            "width": coerce_int(stream.get("width")),
            "height": coerce_int(stream.get("height")),
            "profile": stream.get("profile"),
            "pix_fmt": stream.get("pix_fmt"),
        }
    if audio_streams:
        media_info["audio"] = [
            {
                "codec": stream.get("codec_name"),
                "channels": coerce_int(stream.get("channels")),
                "language": (stream.get("tags") or {}).get("language"),
            }
            for stream in audio_streams[:8]
        ]
    return json_safe(media_info)


def normalize_title_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def tmdb_image_url(path: str | None, size: str) -> str | None:
    if not path:
        return None
    return f"https://image.tmdb.org/t/p/{size}{path}"


def normalize_imdb_id(value: Any) -> str | None:
    if not value:
        return None
    match = re.search(r"\btt\d+\b", str(value))
    return match.group(0) if match else None


def imdb_id_for_item(item: dict[str, Any]) -> str | None:
    for container in (item, item.get("metadata"), item.get("local_metadata")):
        if isinstance(container, dict):
            imdb_id = normalize_imdb_id(container.get("imdb_id"))
            if imdb_id:
                return imdb_id
    return None
