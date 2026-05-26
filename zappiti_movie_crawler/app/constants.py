from __future__ import annotations

from typing import Any


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
    "min_files_per_library": [],
    "max_item_drop_percent": 20,
    "skip_github_push_when_unchanged": True,
}

RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
