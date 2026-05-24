from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from github_publisher import GithubPublisher
from imdb_ratings import ImdbRatingsClient
from metadata import (
    apply_nfo_metadata,
    ffprobe,
    imdb_id_for_item,
    parse_file_metadata,
    read_local_nfo,
)
from runtime import stop_requested
from scanner import SourceScanner, unreachable_libraries
from tmdb_client import TmdbClient
from utils import atomic_write_bytes, normalize_path_part, utc_now


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
        if stop_requested():
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
    prefix = normalize_path_part(str(options.get("github_assets_prefix") or "assets")) or "assets"
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
    if not isinstance(stats, dict):
        blockers.append("catalog stats are not an object")
        stats = {}

    items = catalog.get("items") or []
    sources = catalog.get("sources") or []
    expected_libraries = [str(library) for library in options.get("libraries", [])]

    if stop_requested():
        blockers.append("scan was interrupted")

    if not isinstance(items, list):
        blockers.append("catalog items are not a list")
        items = []
    if not isinstance(sources, list):
        blockers.append("catalog sources are not a list")
        sources = []

    try:
        total = int(stats.get("total") or 0)
    except (TypeError, ValueError):
        total = -1
        blockers.append("catalog stats total is invalid")

    if total != len(items):
        blockers.append("catalog stats total does not match item count")

    if not options.get("publish_allow_empty_catalog", False) and total == 0:
        blockers.append("scan found 0 media files")

    if any(not isinstance(item, dict) for item in items):
        blockers.append("catalog contains non-object items")

    item_ids = [str(item.get("id") or "") for item in items if isinstance(item, dict)]
    if len(item_ids) != len(set(item_ids)):
        blockers.append("catalog contains duplicate item ids")
    if any(not item_id for item_id in item_ids):
        blockers.append("catalog contains items without ids")

    if options.get("publish_require_all_sources_reachable", True):
        expected_sources = [str(source.get("name") or "unnamed") for source in options.get("sources", []) if isinstance(source, dict)]
        reported_sources = {
            str(status.get("name") or "unnamed")
            for status in sources
            if isinstance(status, dict)
        }
        missing_sources = [source for source in expected_sources if source not in reported_sources]
        if missing_sources:
            blockers.append(f"missing source scan results: {', '.join(missing_sources)}")

        offline_sources = [
            str(status.get("name") or "unnamed")
            for status in sources
            if isinstance(status, dict) and not status.get("reachable")
        ]
        if offline_sources:
            blockers.append(f"unreachable sources: {', '.join(offline_sources)}")

        offline_libraries = []
        for status in sources:
            if not isinstance(status, dict):
                blockers.append("catalog contains non-object source status")
                continue
            source_name = str(status.get("name") or "unnamed")
            for library in unreachable_libraries(status, expected_libraries):
                offline_libraries.append(f"{source_name}/{library}")
        if offline_libraries:
            blockers.append(f"unreachable libraries: {', '.join(offline_libraries)}")

    return blockers


def asset_file_blockers(files: dict[str, bytes]) -> list[str]:
    blockers: list[str] = []
    expected_suffixes = {
        "zappiti-catalog.json",
        "zappiti-catalog.min.json",
        "zappiti-catalog-summary.json",
    }
    actual_suffixes = {path.rsplit("/", 1)[-1] for path in files}
    missing = sorted(expected_suffixes - actual_suffixes)
    if missing:
        blockers.append(f"missing asset files: {', '.join(missing)}")

    for path, content in files.items():
        if not path.endswith(".json"):
            continue
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            blockers.append(f"invalid JSON asset {path}: {exc}")
            continue
        if not isinstance(payload, dict):
            blockers.append(f"JSON asset {path} is not an object")
    return blockers


def write_local_outputs(files: dict[str, bytes], output_dir: str) -> None:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()
    for path, content in files.items():
        target = (root / path).resolve()
        if not target.is_relative_to(root_resolved):
            raise RuntimeError(f"Refusing to write outside output directory: {path}")
        atomic_write_bytes(target, content)
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
    asset_blockers = asset_file_blockers(files)
    if asset_blockers:
        elapsed = time.monotonic() - started
        logging.error(
            "Skipping catalog write and GitHub publish after %.1f seconds because generated assets are invalid: %s",
            elapsed,
            "; ".join(asset_blockers),
        )
        return

    try:
        write_local_outputs(files, str(options["local_output_dir"]))
    except (OSError, RuntimeError) as exc:
        logging.error("Cannot write local catalog assets: %s", exc)

    try:
        publisher = GithubPublisher(options)
        result = publisher.publish(files, str(options["github_commit_message"]))
    except (RuntimeError, ValueError) as exc:
        logging.error("Catalog was not published to GitHub: %s", exc)
    else:
        if result.get("published"):
            logging.info("Published catalog to GitHub commit %s.", result["commit_sha"])
        else:
            logging.warning("Catalog was not published: %s.", result.get("reason"))

    elapsed = time.monotonic() - started
    logging.info("Scan finished in %.1f seconds with %s items.", elapsed, catalog["stats"]["total"])
