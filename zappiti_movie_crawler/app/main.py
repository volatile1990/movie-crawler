from __future__ import annotations

import base64
import csv
import gzip
import io
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import smbclient
from guessit import guessit


VIDEO_EXTENSIONS = {
    ".avi",
    ".bdmv",
    ".divx",
    ".iso",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}

DEFAULT_OPTIONS: dict[str, Any] = {
    "sources": [
        {
            "name": "Zappiti1",
            "mounted_path": "/media/Zappiti1",
            "smb_unc": r"\\192.168.1.91\Volume_9a4a7d5c4a7d365b",
        },
        {
            "name": "Zappiti2",
            "mounted_path": "/media/Zappiti2",
            "smb_unc": r"\\192.168.1.91\Volume_ca7a3d6d7a3d5801",
        },
    ],
    "libraries": ["FILME", "SERIEN", "DEMOS"],
    "smb_username": "guest",
    "smb_password": "guest",
    "smb_require_signing": False,
    "smb_require_secure_negotiate": False,
    "prefer_mounted_paths": True,
    "enable_smb_fallback": True,
    "min_file_size_mb": 10,
    "enable_ffprobe": True,
    "run_on_start": True,
    "run_once": False,
    "scan_interval_minutes": 360,
    "tmdb_api_key": "",
    "tmdb_language": "de-DE",
    "tmdb_region": "DE",
    "enable_imdb_ratings": True,
    "imdb_ratings_dataset_url": "https://datasets.imdbws.com/title.ratings.tsv.gz",
    "imdb_cache_ttl_hours": 24,
    "github_repo": "volatile1990/eclipse-cinema-homepage",
    "github_branch": "main",
    "github_token": "",
    "github_assets_prefix": "assets",
    "github_commit_message": "Update Zappiti media catalog",
    "local_output_dir": "/data/output",
    "request_timeout_seconds": 20,
    "publish_require_all_sources_reachable": True,
    "publish_allow_empty_catalog": False,
}

STOP_REQUESTED = False


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("smbclient").setLevel(logging.WARNING)
    logging.getLogger("smbprotocol").setLevel(logging.WARNING)


def request_stop(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    logging.info("Received signal %s, stopping after current step.", signum)


def load_options() -> dict[str, Any]:
    options = dict(DEFAULT_OPTIONS)
    options_path = Path(os.environ.get("CRAWLER_OPTIONS_PATH", "/data/options.json"))
    if options_path.exists():
        with options_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        options.update(loaded)
    else:
        logging.warning("Options file %s not found; using defaults.", options_path)

    options["scan_interval_minutes"] = max(1, int(options["scan_interval_minutes"]))
    options["min_file_size_mb"] = max(0, int(options["min_file_size_mb"]))
    options["request_timeout_seconds"] = max(5, int(options["request_timeout_seconds"]))
    options["imdb_cache_ttl_hours"] = max(1, int(options["imdb_cache_ttl_hours"]))
    return options


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
    return value.replace("\\", "/").strip("/")


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


def kind_for_library(library: str) -> str:
    normalized = library.upper()
    if normalized == "FILME":
        return "movie"
    if normalized == "SERIEN":
        return "tv"
    if normalized == "DEMOS":
        return "demo"
    return "video"


def should_skip_path(parts: list[str]) -> bool:
    skip_names = {"@eadir", "$recycle.bin", "recycler", "system volume information"}
    return any(part.startswith(".") or part.lower() in skip_names for part in parts)


def directory_preview(path: Path, limit: int = 12) -> str:
    try:
        names = sorted(child.name for child in path.iterdir())
    except OSError as exc:
        return f"cannot list entries: {exc}"
    if not names:
        return "empty"
    preview = ", ".join(names[:limit])
    if len(names) > limit:
        preview = f"{preview}, ... (+{len(names) - limit} more)"
    return preview


def build_item(
    source_name: str,
    library: str,
    relative_parts: list[str],
    file_name: str,
    size_bytes: int | None,
    modified_at: str | None,
    local_path: str | None = None,
    smb_path: str | None = None,
) -> dict[str, Any]:
    relative_path = "/".join([library, *relative_parts, file_name])
    item = {
        "id": stable_id(source_name, relative_path),
        "source": source_name,
        "library": library,
        "kind": kind_for_library(library),
        "relative_path": relative_path,
        "display_path": f"{source_name}/{relative_path}",
        "file_name": file_name,
        "extension": Path(file_name).suffix.lower(),
        "size_bytes": size_bytes,
        "modified_at": modified_at,
    }
    if local_path:
        item["_local_path"] = local_path
    if smb_path:
        item["_smb_path"] = smb_path
    return item


class SourceScanner:
    def __init__(self, source: dict[str, Any], options: dict[str, Any]) -> None:
        self.source = source
        self.options = options
        self.name = source.get("name") or "unnamed"
        self.libraries = [str(library) for library in options["libraries"]]
        self.min_size_bytes = int(options["min_file_size_mb"]) * 1024 * 1024

    def scan(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self.options.get("prefer_mounted_paths", True):
            items, status = self.scan_mounted_path()
            if status["reachable"] and items:
                return items, status
            if status["reachable"] and not self.options.get("enable_smb_fallback", True):
                return items, status
            if status["reachable"]:
                logging.warning(
                    "Mounted path %s for %s yielded 0 files; trying SMB fallback.",
                    status["path"],
                    self.name,
                )

        if self.options.get("enable_smb_fallback", True):
            return self.scan_smb()

        items, status = self.scan_mounted_path()
        return items, status

    def scan_mounted_path(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        root = Path(str(self.source.get("mounted_path") or ""))
        status = {
            "name": self.name,
            "mode": "mounted_path",
            "path": str(root),
            "reachable": False,
            "libraries": [],
            "message": "",
        }
        if not root.is_dir():
            status["message"] = "Mounted path is not available."
            logging.warning("Mounted path %s for %s is not available.", root, self.name)
            return [], status

        items: list[dict[str, Any]] = []
        status["reachable"] = True
        logging.info("Mounted path %s for %s is available. Top-level entries: %s", root, self.name, directory_preview(root))
        for library in self.libraries:
            library_path = root / library
            library_status = {
                "name": library,
                "reachable": False,
                "files": 0,
                "visited_files": 0,
                "skipped_extension": 0,
                "skipped_size": 0,
            }
            if not library_path.is_dir():
                library_status["message"] = "Library folder is not available."
                status["libraries"].append(library_status)
                logging.warning("Library folder %s for %s is not available.", library_path, self.name)
                continue

            library_status["reachable"] = True
            try:
                for path in library_path.rglob("*"):
                    if STOP_REQUESTED:
                        break
                    if not path.is_file():
                        continue
                    library_status["visited_files"] += 1
                    if path.suffix.lower() not in VIDEO_EXTENSIONS:
                        library_status["skipped_extension"] += 1
                        continue
                    relative_parts = list(path.relative_to(library_path).parts[:-1])
                    if should_skip_path([library, *relative_parts, path.name]):
                        continue
                    try:
                        stat = path.stat()
                    except OSError as exc:
                        logging.warning("Cannot stat %s: %s", path, exc)
                        continue
                    if stat.st_size < self.min_size_bytes:
                        library_status["skipped_size"] += 1
                        continue
                    items.append(
                        build_item(
                            self.name,
                            library,
                            relative_parts,
                            path.name,
                            stat.st_size,
                            iso_from_timestamp(stat.st_mtime),
                            local_path=str(path),
                        )
                    )
                    library_status["files"] += 1
            except OSError as exc:
                library_status["message"] = str(exc)
                logging.warning("Cannot scan %s: %s", library_path, exc)
            status["libraries"].append(library_status)
            logging.info(
                "Library %s/%s scanned: %s files accepted, %s file entries seen, %s skipped by extension, %s skipped below minimum size.",
                self.name,
                library,
                library_status["files"],
                library_status["visited_files"],
                library_status["skipped_extension"],
                library_status["skipped_size"],
            )

        status["files"] = len(items)
        return items, status

    def scan_smb(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        unc = str(self.source.get("smb_unc") or "")
        status = {
            "name": self.name,
            "mode": "smb",
            "path": unc,
            "reachable": False,
            "libraries": [],
            "message": "",
        }
        parsed = parse_unc(unc)
        if not parsed:
            status["message"] = "SMB UNC path is invalid."
            return [], status

        server, _share, _subpath = parsed
        try:
            smbclient.reset_connection_cache()
            smbclient.ClientConfig(
                require_secure_negotiate=bool(self.options.get("smb_require_secure_negotiate", False))
            )
            smbclient.register_session(
                server,
                username=str(self.options.get("smb_username") or ""),
                password=str(self.options.get("smb_password") or ""),
                require_signing=bool(self.options.get("smb_require_signing", False)),
            )
        except Exception as exc:  # noqa: BLE001
            status["message"] = f"SMB login failed: {exc}"
            logging.warning("SMB login failed for %s: %s", self.name, exc)
            return [], status

        items: list[dict[str, Any]] = []
        status["reachable"] = True
        for library in self.libraries:
            library_root = join_unc(unc, library)
            library_status = {
                "name": library,
                "reachable": False,
                "files": 0,
                "visited_files": 0,
                "skipped_extension": 0,
                "skipped_size": 0,
            }
            try:
                for item in self.walk_smb_library(library, library_root, [], library_status):
                    items.append(item)
                    library_status["files"] += 1
                library_status["reachable"] = True
            except Exception as exc:  # noqa: BLE001
                library_status["message"] = str(exc)
                logging.warning("Cannot scan SMB folder %s: %s", library_root, exc)
            status["libraries"].append(library_status)
            if library_status["reachable"]:
                logging.info(
                    "SMB library %s/%s scanned: %s files accepted, %s file entries seen, %s skipped by extension, %s skipped below minimum size.",
                    self.name,
                    library,
                    library_status["files"],
                    library_status["visited_files"],
                    library_status["skipped_extension"],
                    library_status["skipped_size"],
                )

        status["files"] = len(items)
        return items, status

    def walk_smb_library(
        self,
        library: str,
        directory: str,
        relative_parts: list[str],
        counters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for entry in smbclient.scandir(directory):
            if STOP_REQUESTED:
                break
            name = entry.name
            if should_skip_path([library, *relative_parts, name]):
                continue
            child = join_unc(directory, name)
            if entry.is_dir():
                items.extend(self.walk_smb_library(library, child, [*relative_parts, name], counters))
                continue
            counters["visited_files"] += 1
            if not entry.is_file() or Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
                counters["skipped_extension"] += 1
                continue
            stat = entry.stat()
            size = getattr(stat, "st_size", None)
            if size is not None and int(size) < self.min_size_bytes:
                counters["skipped_size"] += 1
                continue
            modified = iso_from_timestamp(getattr(stat, "st_mtime", None))
            items.append(build_item(self.name, library, relative_parts, name, size, modified, smb_path=child))
        return items


def parse_unc(value: str) -> tuple[str, str, str] | None:
    normalized = value.replace("/", "\\").rstrip("\\")
    match = re.match(r"^\\\\([^\\]+)\\([^\\]+)(?:\\(.*))?$", normalized)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3) or ""


def join_unc(base: str, *parts: str) -> str:
    normalized = base.replace("/", "\\").rstrip("\\")
    suffix = "\\".join(part.strip("\\/") for part in parts if part)
    return f"{normalized}\\{suffix}" if suffix else normalized


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
        if not candidate.exists() or candidate.stat().st_size > 1024 * 1024:
            continue
        try:
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

    media_info: dict[str, Any] = {}
    fmt = payload.get("format") or {}
    if fmt.get("duration"):
        media_info["duration_seconds"] = int(float(fmt["duration"]))
    if fmt.get("bit_rate"):
        media_info["bit_rate"] = int(fmt["bit_rate"])

    video_streams = [stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"]
    if video_streams:
        stream = video_streams[0]
        media_info["video"] = {
            "codec": stream.get("codec_name"),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "profile": stream.get("profile"),
            "pix_fmt": stream.get("pix_fmt"),
        }
    if audio_streams:
        media_info["audio"] = [
            {
                "codec": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "language": (stream.get("tags") or {}).get("language"),
            }
            for stream in audio_streams[:8]
        ]
    return json_safe(media_info)


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


class TmdbClient:
    def __init__(self, options: dict[str, Any]) -> None:
        self.api_key = str(options.get("tmdb_api_key") or "").strip()
        self.language = str(options.get("tmdb_language") or "de-DE")
        self.region = str(options.get("tmdb_region") or "DE")
        self.timeout = int(options["request_timeout_seconds"])
        self.cache_path = Path("/data/cache/tmdb_cache.json")
        self.cache = self.load_cache()

    def load_cache(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            with self.cache_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}

    def save_cache(self) -> None:
        if not self.api_key:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open("w", encoding="utf-8") as handle:
                json.dump(self.cache, handle, ensure_ascii=False, indent=2, sort_keys=True)
        except OSError as exc:
            logging.warning("Cannot write TMDB cache: %s", exc)

    def enrich(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if not self.api_key:
            return None
        if item["kind"] not in {"movie", "tv"}:
            return None
        title = item.get("title")
        if not title:
            return None
        year = item.get("year")
        cache_key = f"{item['kind']}|{title.lower()}|{year or ''}|{self.language}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        metadata = self.search(item["kind"], str(title), year)
        self.cache[cache_key] = metadata
        return metadata

    def enrich_episodes(self, item: dict[str, Any], series_metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not self.api_key or item.get("kind") != "tv" or not series_metadata:
            return []

        series_tmdb_id = coerce_int(series_metadata.get("tmdb_id"))
        season = coerce_int(item.get("season"))
        episodes = coerce_int_list(item.get("episode"))
        if not series_tmdb_id or season is None or not episodes:
            return []

        results: list[dict[str, Any]] = []
        for episode in episodes:
            cache_key = f"episode|{series_tmdb_id}|{season}|{episode}|{self.language}"
            if cache_key not in self.cache:
                self.cache[cache_key] = self.episode_details(series_tmdb_id, season, episode)
            metadata = self.cache.get(cache_key)
            if metadata:
                results.append(metadata)
        return results

    def search(self, kind: str, title: str, year: int | None) -> dict[str, Any] | None:
        endpoint = "movie" if kind == "movie" else "tv"
        params: dict[str, Any] = {
            "api_key": self.api_key,
            "query": title,
            "language": self.language,
            "include_adult": "false",
        }
        if self.region:
            params["region"] = self.region
        if year:
            params["primary_release_year" if kind == "movie" else "first_air_date_year"] = year

        result = self.tmdb_get(f"/search/{endpoint}", params)
        if not result or not result.get("results"):
            if year:
                params.pop("primary_release_year" if kind == "movie" else "first_air_date_year", None)
                result = self.tmdb_get(f"/search/{endpoint}", params)
        if not result or not result.get("results"):
            return None

        best = self.choose_best_result(kind, title, year, result["results"])
        if not best:
            return None

        details = self.tmdb_get(
            f"/{endpoint}/{best['id']}",
            {
                "api_key": self.api_key,
                "language": self.language,
                "append_to_response": "external_ids",
            },
        )
        return self.normalize(kind, details or best)

    def episode_details(self, series_tmdb_id: int, season: int, episode: int) -> dict[str, Any] | None:
        details = self.tmdb_get(
            f"/tv/{series_tmdb_id}/season/{season}/episode/{episode}",
            {
                "api_key": self.api_key,
                "language": self.language,
                "append_to_response": "external_ids",
            },
        )
        return self.normalize_episode(details) if details else None

    def tmdb_get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        url = f"https://api.themoviedb.org/3{path}"
        try:
            response = requests.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            logging.warning("TMDB request failed: %s", exc)
            return None
        if response.status_code == 429:
            time.sleep(2)
            return self.tmdb_get(path, params)
        if response.status_code >= 400:
            logging.warning("TMDB returned HTTP %s for %s", response.status_code, path)
            return None
        return response.json()

    @staticmethod
    def choose_best_result(kind: str, title: str, year: int | None, results: list[dict[str, Any]]) -> dict[str, Any] | None:
        normalized_title = normalize_title_for_match(title)

        def score(result: dict[str, Any]) -> tuple[int, float]:
            result_title = result.get("title" if kind == "movie" else "name") or ""
            result_year = extract_year(result.get("release_date" if kind == "movie" else "first_air_date"))
            title_score = 2 if normalize_title_for_match(result_title) == normalized_title else 0
            year_score = 1 if year and result_year == year else 0
            return title_score + year_score, float(result.get("popularity") or 0)

        return sorted(results, key=score, reverse=True)[0] if results else None

    @staticmethod
    def normalize(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == "movie":
            date = payload.get("release_date")
            title = payload.get("title")
            original_title = payload.get("original_title")
        else:
            date = payload.get("first_air_date")
            title = payload.get("name")
            original_title = payload.get("original_name")

        external_ids = payload.get("external_ids") or {}
        metadata = {
            "provider": "tmdb",
            "tmdb_id": payload.get("id"),
            "imdb_id": external_ids.get("imdb_id"),
            "title": title,
            "original_title": original_title,
            "overview": payload.get("overview"),
            "release_date": date,
            "year": extract_year(date),
            "genres": [genre.get("name") for genre in payload.get("genres", []) if genre.get("name")],
            "runtime_minutes": payload.get("runtime") or payload.get("episode_run_time"),
            "vote_average": payload.get("vote_average"),
            "vote_count": payload.get("vote_count"),
            "poster_url": tmdb_image_url(payload.get("poster_path"), "w500"),
            "backdrop_url": tmdb_image_url(payload.get("backdrop_path"), "w1280"),
            "homepage": payload.get("homepage"),
        }
        if kind == "tv":
            metadata["number_of_seasons"] = payload.get("number_of_seasons")
            metadata["number_of_episodes"] = payload.get("number_of_episodes")
        return {key: value for key, value in metadata.items() if value not in (None, "", [])}

    @staticmethod
    def normalize_episode(payload: dict[str, Any]) -> dict[str, Any]:
        external_ids = payload.get("external_ids") or {}
        metadata = {
            "provider": "tmdb",
            "tmdb_id": payload.get("id"),
            "imdb_id": external_ids.get("imdb_id"),
            "title": payload.get("name"),
            "overview": payload.get("overview"),
            "air_date": payload.get("air_date"),
            "year": extract_year(payload.get("air_date")),
            "season": payload.get("season_number"),
            "episode": payload.get("episode_number"),
            "runtime_minutes": payload.get("runtime"),
            "vote_average": payload.get("vote_average"),
            "vote_count": payload.get("vote_count"),
            "still_url": tmdb_image_url(payload.get("still_path"), "w300"),
        }
        return {key: value for key, value in metadata.items() if value not in (None, "", [])}


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


class ImdbRatingsClient:
    def __init__(self, options: dict[str, Any]) -> None:
        self.enabled = bool(options.get("enable_imdb_ratings", True))
        self.dataset_url = str(options.get("imdb_ratings_dataset_url") or "").strip()
        self.timeout = int(options["request_timeout_seconds"])
        self.ttl = timedelta(hours=int(options["imdb_cache_ttl_hours"]))
        self.cache_path = Path("/data/cache/imdb_ratings_cache.json")
        self.cache = self.load_cache()

    def load_cache(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {"ratings": {}, "missing": {}}
        try:
            with self.cache_path.open("r", encoding="utf-8") as handle:
                cache = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"ratings": {}, "missing": {}}
        if not isinstance(cache, dict):
            return {"ratings": {}, "missing": {}}
        cache.setdefault("ratings", {})
        cache.setdefault("missing", {})
        return cache

    def save_cache(self) -> None:
        if not self.enabled:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open("w", encoding="utf-8") as handle:
                json.dump(self.cache, handle, ensure_ascii=False, indent=2, sort_keys=True)
        except OSError as exc:
            logging.warning("Cannot write IMDb ratings cache: %s", exc)

    def lookup_many(self, imdb_ids: set[str]) -> dict[str, dict[str, Any]]:
        if not self.enabled or not self.dataset_url or not imdb_ids:
            return {}

        normalized_ids = {imdb_id for imdb_id in (normalize_imdb_id(value) for value in imdb_ids) if imdb_id}
        if not normalized_ids:
            return {}

        ratings = self.cached_ratings()
        missing = self.cached_missing()
        fresh = self.cache_is_fresh()
        result = {imdb_id: ratings[imdb_id] for imdb_id in normalized_ids if fresh and imdb_id in ratings}
        needed = sorted(
            imdb_id
            for imdb_id in normalized_ids
            if not fresh or (imdb_id not in ratings and imdb_id not in missing)
        )
        if not needed:
            return result

        downloaded = self.download_ratings(set(needed))
        if downloaded is None:
            return {imdb_id: ratings[imdb_id] for imdb_id in normalized_ids if imdb_id in ratings}

        checked_at = utc_now()
        for imdb_id in needed:
            if imdb_id in downloaded:
                ratings[imdb_id] = downloaded[imdb_id]
                missing.pop(imdb_id, None)
            else:
                ratings.pop(imdb_id, None)
                missing[imdb_id] = checked_at

        self.cache["dataset_url"] = self.dataset_url
        self.cache["fetched_at"] = checked_at
        return {imdb_id: ratings[imdb_id] for imdb_id in normalized_ids if imdb_id in ratings}

    def cached_ratings(self) -> dict[str, dict[str, Any]]:
        ratings = self.cache.get("ratings")
        if not isinstance(ratings, dict):
            ratings = {}
            self.cache["ratings"] = ratings
        return ratings

    def cached_missing(self) -> dict[str, str]:
        missing = self.cache.get("missing")
        if not isinstance(missing, dict):
            missing = {}
            self.cache["missing"] = missing
        return missing

    def cache_is_fresh(self) -> bool:
        if self.cache.get("dataset_url") != self.dataset_url:
            return False
        fetched_at = self.cache.get("fetched_at")
        if not isinstance(fetched_at, str):
            return False
        try:
            timestamp = datetime.fromisoformat(fetched_at)
        except ValueError:
            return False
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp + self.ttl > datetime.now(timezone.utc)

    def download_ratings(self, imdb_ids: set[str]) -> dict[str, dict[str, Any]] | None:
        remaining = set(imdb_ids)
        found: dict[str, dict[str, Any]] = {}
        headers = {"User-Agent": "zappiti-movie-crawler/0.1"}
        logging.info("Downloading IMDb ratings dataset for %s title IDs.", len(remaining))
        try:
            with requests.get(self.dataset_url, headers=headers, stream=True, timeout=self.timeout) as response:
                if response.status_code >= 400:
                    logging.warning("IMDb ratings dataset returned HTTP %s.", response.status_code)
                    return None
                response.raw.decode_content = False
                with gzip.GzipFile(fileobj=response.raw) as gzip_file:
                    with io.TextIOWrapper(gzip_file, encoding="utf-8", newline="") as text_file:
                        reader = csv.DictReader(text_file, delimiter="\t")
                        for row in reader:
                            imdb_id = row.get("tconst")
                            if imdb_id not in remaining:
                                continue
                            found[imdb_id] = {
                                "provider": "imdb",
                                "rating": float(row["averageRating"]),
                                "votes": int(row["numVotes"]),
                            }
                            remaining.remove(imdb_id)
                            if not remaining:
                                break
        except (OSError, EOFError, ValueError, csv.Error, requests.RequestException, gzip.BadGzipFile) as exc:
            logging.warning("Cannot load IMDb ratings dataset: %s", exc)
            return None
        return found


class GithubPublisher:
    def __init__(self, options: dict[str, Any]) -> None:
        self.token = str(options.get("github_token") or "").strip()
        self.owner, self.repo = parse_github_repo(str(options["github_repo"]))
        self.branch = str(options.get("github_branch") or "").strip()
        self.timeout = int(options["request_timeout_seconds"])
        self.api_base = f"https://api.github.com/repos/{self.owner}/{self.repo}"

    def publish(self, files: dict[str, bytes], message: str) -> dict[str, Any]:
        if not self.token:
            return {"published": False, "reason": "github_token is empty"}

        branch = self.branch or self.default_branch()
        ref = self.request("GET", f"{self.api_base}/git/ref/heads/{branch}")
        head_sha = ref["object"]["sha"]
        commit = self.request("GET", f"{self.api_base}/git/commits/{head_sha}")
        base_tree_sha = commit["tree"]["sha"]

        tree_entries = []
        for path, content in sorted(files.items()):
            blob = self.request(
                "POST",
                f"{self.api_base}/git/blobs",
                {
                    "content": base64.b64encode(content).decode("ascii"),
                    "encoding": "base64",
                },
            )
            tree_entries.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )

        tree = self.request(
            "POST",
            f"{self.api_base}/git/trees",
            {"base_tree": base_tree_sha, "tree": tree_entries},
        )
        new_commit = self.request(
            "POST",
            f"{self.api_base}/git/commits",
            {"message": message, "tree": tree["sha"], "parents": [head_sha]},
        )
        self.request(
            "PATCH",
            f"{self.api_base}/git/refs/heads/{branch}",
            {"sha": new_commit["sha"], "force": False},
        )
        return {"published": True, "commit_sha": new_commit["sha"], "branch": branch}

    def default_branch(self) -> str:
        repo = self.request("GET", self.api_base)
        return repo.get("default_branch") or "main"

    def request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = requests.request(method, url, json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"GitHub request failed: {exc}") from exc
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub returned HTTP {response.status_code}: {response.text[:500]}")
        if not response.content:
            return {}
        return response.json()


def parse_github_repo(value: str) -> tuple[str, str]:
    repo = value.strip()
    if repo.startswith("http://") or repo.startswith("https://"):
        path = urlparse(repo).path.strip("/")
    else:
        path = repo
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        raise ValueError(f"Invalid github_repo value: {value}")
    return parts[-2], parts[-1]


def apply_episode_metadata(item: dict[str, Any], episodes: list[dict[str, Any]]) -> None:
    if not episodes:
        return

    item["episode_metadata"] = episodes[0] if len(episodes) == 1 else episodes
    if item.get("episode_title"):
        return

    titles = [str(episode["title"]) for episode in episodes if episode.get("title")]
    if not titles:
        return
    item["episode_title"] = " / ".join(titles)
    if len(titles) > 1:
        item["episode_titles"] = titles


def apply_imdb_rating(item: dict[str, Any], imdb_id: str, rating: dict[str, Any]) -> None:
    item["imdb_id"] = imdb_id
    item["imdb_rating"] = rating["rating"]
    item["imdb_votes"] = rating["votes"]
    item["imdb_url"] = f"https://www.imdb.com/title/{imdb_id}/"

    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        if not metadata.get("imdb_id"):
            metadata["imdb_id"] = imdb_id
        metadata["imdb_rating"] = rating["rating"]
        metadata["imdb_votes"] = rating["votes"]


def apply_imdb_ratings(items: list[dict[str, Any]], imdb: ImdbRatingsClient) -> None:
    movie_imdb_ids: set[str] = set()
    for item in items:
        if item.get("kind") != "movie":
            continue
        imdb_id = imdb_id_for_item(item)
        if imdb_id:
            movie_imdb_ids.add(imdb_id)

    ratings = imdb.lookup_many(movie_imdb_ids)
    if not ratings:
        return

    for item in items:
        if item.get("kind") != "movie":
            continue
        imdb_id = imdb_id_for_item(item)
        if imdb_id and imdb_id in ratings:
            apply_imdb_rating(item, imdb_id, ratings[imdb_id])


def enrich_items(items: list[dict[str, Any]], options: dict[str, Any]) -> list[dict[str, Any]]:
    tmdb = TmdbClient(options)
    imdb = ImdbRatingsClient(options)
    enriched: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        if STOP_REQUESTED:
            break
        item.update(parse_file_metadata(item))
        nfo = read_local_nfo(item)
        if nfo:
            apply_nfo_metadata(item, nfo)

        if options.get("enable_ffprobe") and item.get("_local_path"):
            media_info = ffprobe(str(item["_local_path"]), int(options["request_timeout_seconds"]))
            if media_info:
                item["media_info"] = media_info

        tmdb_metadata = tmdb.enrich(item)
        if tmdb_metadata:
            item["metadata"] = tmdb_metadata

        if item.get("kind") == "tv" and not item.get("episode_title"):
            apply_episode_metadata(item, tmdb.enrich_episodes(item, item.get("metadata")))

        if index % 100 == 0:
            logging.info("Enriched %s/%s media files.", index, len(items))
        enriched.append(item)

    tmdb.save_cache()
    apply_imdb_ratings(enriched, imdb)
    imdb.save_cache()
    return enriched


def crawl(options: dict[str, Any]) -> dict[str, Any]:
    statuses: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for source in options["sources"]:
        scanner = SourceScanner(source, options)
        logging.info("Scanning source %s.", scanner.name)
        source_items, status = scanner.scan()
        statuses.append(status)
        items.extend(source_items)
        logging.info("Source %s yielded %s files.", scanner.name, len(source_items))

    items = enrich_items(items, options)
    public_items = [strip_private_fields(item) for item in items]
    public_items.sort(key=lambda item: (item.get("kind") or "", str(item.get("title") or "").lower(), item["relative_path"]))
    stats = build_stats(public_items, statuses)

    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "sources": statuses,
        "stats": stats,
        "items": public_items,
    }


def strip_private_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def build_stats(items: list[dict[str, Any]], statuses: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    by_library: dict[str, int] = {}
    for item in items:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        by_library[item["library"]] = by_library.get(item["library"], 0) + 1
    return {
        "total": len(items),
        "by_kind": by_kind,
        "by_library": by_library,
        "reachable_sources": sum(1 for status in statuses if status.get("reachable")),
        "offline_sources": sum(1 for status in statuses if not status.get("reachable")),
    }


def make_asset_files(catalog: dict[str, Any], options: dict[str, Any]) -> dict[str, bytes]:
    prefix = normalize_path_part(str(options.get("github_assets_prefix") or "assets"))
    summary = {
        "schema_version": catalog["schema_version"],
        "generated_at": catalog["generated_at"],
        "stats": catalog["stats"],
        "sources": catalog["sources"],
    }
    return {
        f"{prefix}/zappiti-catalog.json": json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        f"{prefix}/zappiti-catalog.min.json": json.dumps(catalog, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        f"{prefix}/zappiti-catalog-summary.json": json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
    }


def catalog_publish_blockers(catalog: dict[str, Any], options: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    stats = catalog.get("stats") or {}

    if STOP_REQUESTED:
        blockers.append("scan was interrupted")

    if not options.get("publish_allow_empty_catalog", False) and int(stats.get("total") or 0) == 0:
        blockers.append("scan found 0 media files")

    if options.get("publish_require_all_sources_reachable", True):
        offline_sources = [
            str(status.get("name") or "unnamed")
            for status in catalog.get("sources", [])
            if not status.get("reachable")
        ]
        if offline_sources:
            blockers.append(f"unreachable sources: {', '.join(offline_sources)}")

    return blockers


def write_local_outputs(files: dict[str, bytes], output_dir: str) -> None:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    logging.info("Wrote catalog assets to %s.", root)


def run_once(options: dict[str, Any]) -> None:
    started = time.monotonic()
    catalog = crawl(options)
    blockers = catalog_publish_blockers(catalog, options)
    if blockers:
        elapsed = time.monotonic() - started
        logging.error(
            "Skipping catalog write and GitHub publish after %.1f seconds because scan is incomplete: %s",
            elapsed,
            "; ".join(blockers),
        )
        return

    files = make_asset_files(catalog, options)
    write_local_outputs(files, str(options["local_output_dir"]))

    publisher = GithubPublisher(options)
    result = publisher.publish(files, str(options["github_commit_message"]))
    if result.get("published"):
        logging.info("Published catalog to GitHub commit %s.", result["commit_sha"])
    else:
        logging.warning("Catalog was not published: %s.", result.get("reason"))

    elapsed = time.monotonic() - started
    logging.info("Scan finished in %.1f seconds with %s items.", elapsed, catalog["stats"]["total"])


def sleep_interruptibly(seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while not STOP_REQUESTED and time.monotonic() < deadline:
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def main() -> int:
    configure_logging()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    options = load_options()
    if options.get("run_on_start", True):
        run_once(options)

    if options.get("run_once", False):
        return 0

    while not STOP_REQUESTED:
        interval_seconds = int(options["scan_interval_minutes"]) * 60
        logging.info("Next scan in %s minutes.", options["scan_interval_minutes"])
        sleep_interruptibly(interval_seconds)
        if STOP_REQUESTED:
            break
        options = load_options()
        run_once(options)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        logging.exception("Crawler failed: %s", exc)
        raise SystemExit(1) from exc
