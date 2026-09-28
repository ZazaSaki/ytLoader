# Video Downloader

Self-hosted web GUI for [yt-dlp](https://github.com/yt-dlp/yt-dlp). Paste YouTube links
(single videos or whole playlists), pick a quality or audio-only format, and watch them
download with live progress. History is kept across restarts.

## Features

- Paste one or many links at once (one per line)
- Playlists expand into one job per video, saved in a folder named after the playlist
- Formats: best / 1080p / 720p / 480p (MP4), or audio-only MP3 / M4A
- Download queue with concurrency limit, per-item progress, speed and ETA
- Cancel, retry, remove, delete file, clear history
- Open or save finished files straight from the browser
- Download everything (or one playlist) as a single ZIP, streamed with no temp copy

## Run locally

Requires Python 3.12+ and `ffmpeg` on the PATH.

```bash
./run.sh            # creates .venv on first run, serves on http://localhost:8000
```

## Run with Docker

```bash
docker compose up -d --build
```

Files land in `./downloads`, the database in `./data`.

If the build fails on `apt-get` or downloads end with *"Temporary failure in name
resolution"*, Docker's bridge network has no DNS on your host. Build with
`docker compose build --network host` and enable `network_mode: host` in
`docker-compose.yml` (see the comment there).

## Configuration (env vars)

| Variable         | Default      | Meaning                                  |
|------------------|--------------|------------------------------------------|
| `DOWNLOAD_DIR`   | `downloads`  | Where finished files are written         |
| `DATA_DIR`       | `data`       | Where `app.db` (queue + history) lives   |
| `MAX_CONCURRENT` | `2`          | How many downloads run at the same time  |
| `PORT` / `HOST`  | `8000` / `0.0.0.0` | (run.sh only) bind address         |

## Keeping yt-dlp working

YouTube changes often; when downloads start failing, update yt-dlp:

```bash
.venv/bin/pip install -U yt-dlp      # local
docker compose build --no-cache      # docker
```

## API

| Method | Path                            | Purpose                              |
|--------|---------------------------------|--------------------------------------|
| GET    | `/api/formats`                  | Available format keys                |
| GET    | `/api/downloads`                | All jobs (active first)              |
| POST   | `/api/downloads`                | `{ "urls": "...", "format": "720" }` |
| POST   | `/api/downloads/{id}/cancel`    | Cancel active job                    |
| POST   | `/api/downloads/{id}/retry`     | Re-queue a failed/cancelled job      |
| DELETE | `/api/downloads/{id}?delete_file=true` | Remove job (optionally file)  |
| DELETE | `/api/history`                  | Clear finished jobs                  |
| GET    | `/api/archive[?playlist=...]`   | ZIP of all finished files (or one playlist) |
| GET    | `/api/events`                   | Server-Sent Events stream            |
| GET    | `/files/{path}`                 | Serve downloaded files               |
