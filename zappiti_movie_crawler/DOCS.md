# Zappiti Movie Crawler

This add-on crawls the Zappiti SMB shares, enriches detected video files, writes catalog JSON files, and commits them to the `assets/` folder of `volatile1990/eclipse-cinema-homepage`.

## What It Produces

The add-on writes these files locally to `/data/output/assets/` and publishes the same paths to GitHub:

- `assets/zappiti-catalog.json`: pretty-printed full catalog
- `assets/zappiti-catalog.min.json`: minified full catalog for frontend use
- `assets/zappiti-catalog-summary.json`: source status and counts only

Each catalog item includes the source, library, relative path, title/year parsing, size, modified date, optional `ffprobe` media information, optional NFO metadata, optional TMDB metadata, optional episode metadata, and optional IMDb ratings for movies.

## Source Handling

Configured sources are:

- `Zappiti1` at `/media/Zappiti1`, fallback `\\192.168.1.91\Volume_9a4a7d5c4a7d365b`
- `Zappiti2` at `/media/Zappiti2`, fallback `\\192.168.1.91\Volume_ca7a3d6d7a3d5801`

The add-on first tries the Home Assistant media mount. If your network storage is mounted under `/share` instead of `/media`, change `mounted_path` in the add-on options to `/share/Zappiti1` and `/share/Zappiti2`. If the Zappiti is off or the mount is unavailable, it tries direct SMB with the configured credentials. Offline shares are logged and recorded in the summary instead of failing the whole scan.

Catalog writes and GitHub publishing are skipped when the scan is incomplete. By default, `publish_require_all_sources_reachable` requires every configured source to be reachable and `publish_allow_empty_catalog` prevents replacing the last good catalog with an empty one.

## Required Configuration

Set `github_token` in the add-on configuration. The token needs permission to write contents to `volatile1990/eclipse-cinema-homepage`.

Do not commit the token to this add-on repository. Keep it only in the Home Assistant add-on configuration or in a Home Assistant secret.

## Optional Configuration

Set `tmdb_api_key` to enrich movies and series with TMDB IDs, descriptions, genres, poster URLs, backdrop URLs, ratings, runtime information, and missing TV episode titles. Episode titles are only filled when the filename or local NFO did not already provide one.

`enable_imdb_ratings` downloads and caches IMDb's non-commercial `title.ratings.tsv.gz` dataset, then adds `imdb_rating`, `imdb_votes`, and `imdb_url` to movie catalog items when an IMDb ID is available from TMDB or a local NFO file. `imdb_cache_ttl_hours` controls how long the downloaded rating lookups are reused before refreshing.

`enable_ffprobe` extracts duration, resolution, codecs, and audio stream data for files available through the mounted Home Assistant media paths. Direct SMB fallback scanning still catalogs files, but cannot run `ffprobe` without a mounted filesystem path.

## Recommended Defaults

The default scan interval is 360 minutes. This avoids frequent wakeups while still keeping the homepage reasonably current.

`min_file_size_mb` defaults to 10 MB to skip tiny samples and metadata files. Lower it if your DEMOS folder contains very small clips.
