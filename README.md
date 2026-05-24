# movie-crawler

Home Assistant add-on repository for the Eclipse Cinema Zappiti media crawler.

## Add-on

- Add-on folder: `zappiti_movie_crawler`
- Crawls Home Assistant media mounts `Zappiti1` and `Zappiti2`
- Falls back to direct SMB with `guest` / `guest`
- Scans `FILME`, `SERIEN`, and `DEMOS`
- Writes catalog JSON files to `assets/` in `volatile1990/eclipse-cinema-homepage`

Install this repository as a custom Home Assistant add-on repository, then configure `github_token` in the add-on options before the first run. The token is intentionally not stored in this git repository.
