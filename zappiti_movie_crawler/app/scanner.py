from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import smbclient

from constants import VIDEO_EXTENSIONS
from runtime import stop_requested
from utils import iso_from_timestamp, stable_id


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


def unreachable_libraries(status: dict[str, Any], expected_libraries: list[str] | None = None) -> list[str]:
    libraries = status.get("libraries", [])
    reported = {
        str(library.get("name") or "unnamed"): bool(library.get("reachable"))
        for library in libraries
        if isinstance(library, dict)
    }
    if expected_libraries is not None:
        return [library for library in expected_libraries if not reported.get(library)]
    return [name for name, reachable in reported.items() if not reachable]


def source_status_complete(status: dict[str, Any], expected_libraries: list[str]) -> bool:
    return bool(status.get("reachable")) and not unreachable_libraries(status, expected_libraries)


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
        self.name = str(source.get("name") or "unnamed")
        self.libraries = [str(library) for library in options["libraries"]]
        self.min_size_bytes = int(options["min_file_size_mb"]) * 1024 * 1024

    def scan(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        items: list[dict[str, Any]] = []
        status: dict[str, Any] = {}
        if self.options.get("prefer_mounted_paths", True):
            items, status = self.scan_mounted_path()
            if source_status_complete(status, self.libraries) and items:
                return items, status
            if status["reachable"] and not self.options.get("enable_smb_fallback", True):
                return items, status
            missing_libraries = unreachable_libraries(status, self.libraries)
            if status["reachable"] and missing_libraries:
                logging.warning(
                    "Mounted path %s for %s is missing libraries %s; trying SMB fallback.",
                    status["path"],
                    self.name,
                    ", ".join(missing_libraries),
                )
            elif status["reachable"]:
                logging.warning(
                    "Mounted path %s for %s yielded 0 files; trying SMB fallback.",
                    status["path"],
                    self.name,
                )

        if self.options.get("enable_smb_fallback", True):
            fallback_items, fallback_status = self.scan_smb()
            if status.get("reachable") and not fallback_items:
                logging.warning(
                    "SMB fallback for %s yielded 0 files; keeping mounted-path scan result.",
                    self.name,
                )
                return items, status
            return fallback_items, fallback_status

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

            try:
                for path in library_path.rglob("*"):
                    if stop_requested():
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
                library_status["reachable"] = True
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
            if stop_requested():
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
            try:
                stat = entry.stat()
            except Exception as exc:  # noqa: BLE001
                logging.warning("Cannot stat SMB file %s: %s", child, exc)
                continue
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
