from __future__ import annotations

import csv
import gzip
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from constants import RETRYABLE_HTTP_STATUSES
from metadata import normalize_imdb_id
from runtime import sleep_before_retry, stop_requested
from utils import atomic_write_json, utc_now


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
            atomic_write_json(self.cache_path, self.cache)
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
        headers = {"User-Agent": "zappiti-movie-crawler/0.1"}
        logging.info("Downloading IMDb ratings dataset for %s title IDs.", len(imdb_ids))
        for attempt in range(3):
            if stop_requested():
                return None
            remaining = set(imdb_ids)
            found: dict[str, dict[str, Any]] = {}
            try:
                with requests.get(self.dataset_url, headers=headers, stream=True, timeout=self.timeout) as response:
                    if response.status_code in RETRYABLE_HTTP_STATUSES:
                        if attempt < 2:
                            logging.warning("IMDb ratings dataset returned HTTP %s, retrying.", response.status_code)
                            sleep_before_retry(attempt, response.headers.get("Retry-After"))
                            continue
                        logging.warning("IMDb ratings dataset returned HTTP %s.", response.status_code)
                        return None
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
                return found
            except (OSError, EOFError, ValueError, csv.Error, requests.RequestException, gzip.BadGzipFile) as exc:
                if attempt < 2:
                    logging.warning("Cannot load IMDb ratings dataset, retrying: %s", exc)
                    sleep_before_retry(attempt)
                    continue
                logging.warning("Cannot load IMDb ratings dataset: %s", exc)
                return None
        return None
