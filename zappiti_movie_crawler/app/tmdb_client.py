from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from constants import RETRYABLE_HTTP_STATUSES
from metadata import normalize_title_for_match, tmdb_image_url
from runtime import sleep_before_retry, stop_requested
from utils import atomic_write_json, coerce_int, coerce_int_list, extract_year


class TmdbClient:
    def __init__(self, options: dict[str, Any]) -> None:
        self.api_key = str(options.get("tmdb_api_key") or "").strip()
        self.language = str(options.get("tmdb_language") or "de-DE")
        self.region = str(options.get("tmdb_region") or "DE")
        self.timeout = int(options["request_timeout_seconds"])
        self.cache_path = Path("/data/cache/tmdb_cache.json")
        self.cache = self.load_cache()
        self.request_failed = False

    def load_cache(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            with self.cache_path.open("r", encoding="utf-8") as handle:
                cache = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return cache if isinstance(cache, dict) else {}

    def save_cache(self) -> None:
        if not self.api_key:
            return
        try:
            atomic_write_json(self.cache_path, self.cache)
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

        self.request_failed = False
        metadata = self.search(item["kind"], str(title), year)
        if not self.request_failed:
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
                self.request_failed = False
                metadata = self.episode_details(series_tmdb_id, season, episode)
                if not self.request_failed:
                    self.cache[cache_key] = metadata
            else:
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
        if result is None and self.request_failed:
            return None
        if not result or not result.get("results"):
            if year:
                params.pop("primary_release_year" if kind == "movie" else "first_air_date_year", None)
                result = self.tmdb_get(f"/search/{endpoint}", params)
                if result is None and self.request_failed:
                    return None
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
        for attempt in range(3):
            if stop_requested():
                self.request_failed = True
                return None
            try:
                response = requests.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                self.request_failed = True
                if attempt < 2:
                    logging.warning("TMDB request failed, retrying: %s", exc)
                    sleep_before_retry(attempt)
                    continue
                logging.warning("TMDB request failed: %s", exc)
                return None

            if response.status_code in RETRYABLE_HTTP_STATUSES:
                self.request_failed = True
                if attempt < 2:
                    logging.warning("TMDB returned HTTP %s for %s, retrying.", response.status_code, path)
                    sleep_before_retry(attempt, response.headers.get("Retry-After"))
                    continue
                logging.warning("TMDB returned HTTP %s for %s.", response.status_code, path)
                return None

            if response.status_code >= 400:
                self.request_failed = True
                logging.warning("TMDB returned HTTP %s for %s", response.status_code, path)
                return None

            try:
                payload = response.json()
            except ValueError as exc:
                self.request_failed = True
                logging.warning("TMDB returned invalid JSON for %s: %s", path, exc)
                return None
            if not isinstance(payload, dict):
                self.request_failed = True
                return None
            self.request_failed = False
            return payload
        return None

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
