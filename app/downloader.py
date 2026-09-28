"""Download queue: job model, yt-dlp workers, progress broadcasting."""
import asyncio
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadCancelled, sanitize_filename

from . import db

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "downloads")).resolve()
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))

ACTIVE = ("queued", "downloading", "processing")
FINISHED = ("done", "error", "cancelled")

def _capped(h: int) -> str:
    """Prefer H.264 + AAC (plays everywhere) up to the given height, fall back to anything."""
    return (
        f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio[acodec^=mp4a]"
        f"/bestvideo[height<={h}]+bestaudio/best[height<={h}]"
    )


FORMATS: dict[str, dict] = {
    "best": {"label": "Best video", "format": "bestvideo*+bestaudio/best", "merge_output_format": "mp4"},
    "1080": {"label": "1080p", "format": _capped(1080), "merge_output_format": "mp4"},
    "720":  {"label": "720p",  "format": _capped(720),  "merge_output_format": "mp4"},
    "480":  {"label": "480p",  "format": _capped(480),  "merge_output_format": "mp4"},
    "mp3":  {"label": "Audio (MP3)", "format": "bestaudio/best", "audio": "mp3"},
    "m4a":  {"label": "Audio (M4A)", "format": "bestaudio/best", "audio": "m4a"},
}


def _err_msg(e: BaseException) -> str:
    msg = str(e).splitlines()[-1] if str(e) else e.__class__.__name__
    return msg.replace("ERROR: ", "")


@dataclass
class Job:
    url: str
    format_key: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str | None = None
    thumbnail: str | None = None
    status: str = "queued"
    progress: float = 0.0
    speed: str | None = None
    eta: str | None = None
    filename: str | None = None
    error: str | None = None
    playlist_title: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    # runtime-only
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    _last_emit: float = field(default=0.0, repr=False, compare=False)
    _partials: set = field(default_factory=set, repr=False, compare=False)

    FIELDS = ("id", "url", "title", "thumbnail", "format_key", "status", "progress", "speed",
              "eta", "filename", "error", "playlist_title", "created_at", "finished_at")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.FIELDS}


class Broadcaster:
    """Fan-out of job updates to SSE subscribers."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subs):
            q.put_nowait(event)


class Manager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.broadcaster = Broadcaster()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._workers: list[asyncio.Task] = []

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db.init()
        for row in db.all_jobs():
            job = Job(**row)
            self.jobs[job.id] = job
            if job.status in ACTIVE:
                job.status = "queued"
                job.progress = 0.0
                job.speed = job.eta = None
                self.queue.put_nowait(job.id)
        for _ in range(MAX_CONCURRENT):
            self._workers.append(asyncio.create_task(self._worker()))

    async def stop(self) -> None:
        for t in self._workers:
            t.cancel()

    # ---- public API ------------------------------------------------------

    def list_jobs(self) -> list[dict]:
        active = [j for j in self.jobs.values() if j.status in ACTIVE]
        done = [j for j in self.jobs.values() if j.status in FINISHED]
        active.sort(key=lambda j: j.created_at)
        done.sort(key=lambda j: j.finished_at or j.created_at, reverse=True)
        return [j.to_dict() for j in active + done]

    async def add(self, urls: list[str], format_key: str) -> list[dict]:
        if format_key not in FORMATS:
            raise ValueError(f"Unknown format: {format_key}")
        created: list[Job] = []
        for url in urls:
            try:
                entries = await asyncio.to_thread(self._expand, url)
            except Exception as e:  # unreachable URL, private video, etc.
                entries = [{"url": url, "error": _err_msg(e)}]
            for entry in entries:
                job = Job(
                    url=entry["url"],
                    format_key=format_key,
                    title=entry.get("title"),
                    thumbnail=entry.get("thumbnail"),
                    playlist_title=entry.get("playlist_title"),
                )
                if entry.get("error"):
                    job.status = "error"
                    job.error = entry["error"]
                    job.finished_at = time.time()
                self.jobs[job.id] = job
                self._save(job)
                if job.status == "queued":
                    self.queue.put_nowait(job.id)
                created.append(job)
        return [j.to_dict() for j in created]

    def cancel(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if not job:
            return None
        if job.status == "queued":
            self._finish(job, "cancelled")
        elif job.status == "downloading":
            # "processing" (ffmpeg) is left to finish: the download is complete and
            # aborting mid-conversion would only leave broken files behind.
            job.cancel_event.set()
        return job

    def remove(self, job_id: str, delete_file: bool = False) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        if job.status in ACTIVE:
            self.cancel(job_id)
        if delete_file and job.filename:
            path = DOWNLOAD_DIR / job.filename
            if path.is_file():
                path.unlink()
        self.jobs.pop(job_id, None)
        db.delete(job_id)
        self.broadcaster.publish({"type": "removed", "id": job_id})
        return True

    def retry(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if not job or job.status in ACTIVE:
            return job
        job.status = "queued"
        job.progress = 0.0
        job.error = job.speed = job.eta = None
        job.finished_at = None
        job.cancel_event = threading.Event()
        self._save(job)
        self.queue.put_nowait(job.id)
        return job

    def clear_history(self) -> list[str]:
        ids = db.delete_finished()
        for i in ids:
            self.jobs.pop(i, None)
            self.broadcaster.publish({"type": "removed", "id": i})
        return ids

    # ---- internals -------------------------------------------------------

    @staticmethod
    def _expand(url: str) -> list[dict]:
        """Resolve a URL into one entry per video (expands playlists)."""
        opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info is None:
            raise RuntimeError("No video found at that URL")
        if info.get("_type") == "playlist":
            playlist_title = info.get("title")
            out = []
            for e in info.get("entries") or []:
                if not e:
                    continue
                thumbs = e.get("thumbnails") or []
                out.append({
                    "url": e.get("url") or e.get("webpage_url") or url,
                    "title": e.get("title"),
                    "thumbnail": e.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None),
                    "playlist_title": playlist_title,
                })
            if not out:
                raise RuntimeError("Playlist is empty or unavailable")
            return out
        return [{
            "url": info.get("webpage_url") or url,
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
        }]

    def _save(self, job: Job, force: bool = True) -> None:
        """Persist and broadcast. Progress-only updates are throttled."""
        now = time.time()
        if not force and now - job._last_emit < 0.25:
            return
        job._last_emit = now
        db.upsert(job.to_dict())
        self.broadcaster.publish({"type": "job", "job": job.to_dict()})

    def _save_threadsafe(self, job: Job, force: bool = True) -> None:
        self.loop.call_soon_threadsafe(self._save, job, force)

    def _finish(self, job: Job, status: str, error: str | None = None) -> None:
        job.status = status
        job.error = error
        job.speed = job.eta = None
        job.finished_at = time.time()
        if status == "done":
            job.progress = 100.0
        self._save(job)

    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            job = self.jobs.get(job_id)
            if not job or job.status != "queued":
                continue
            job.status = "downloading"
            self._save(job)
            try:
                filename = await asyncio.to_thread(self._download, job)
                job.filename = filename
                self._finish(job, "done")
            except DownloadCancelled:
                self._cleanup_partial(job)
                self._finish(job, "cancelled")
            except Exception as e:
                self._finish(job, "error", _err_msg(e))

    def _ydl_opts(self, job: Job) -> dict:
        fmt = FORMATS[job.format_key]
        out_dir = DOWNLOAD_DIR
        if job.playlist_title:
            out_dir = out_dir / sanitize_filename(job.playlist_title)
        opts: dict = {
            "format": fmt["format"],
            "outtmpl": str(out_dir / "%(title)s [%(id)s].%(ext)s"),
            "noplaylist": True,
            "overwrites": True,  # re-requesting a video (e.g. other quality) replaces the old file
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [lambda d: self._on_progress(job, d)],
            "postprocessor_hooks": [lambda d: self._on_postprocess(job, d)],
            "postprocessors": [],
        }
        if "merge_output_format" in fmt:
            opts["merge_output_format"] = fmt["merge_output_format"]
        if "audio" in fmt:
            opts["postprocessors"].append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt["audio"],
                "preferredquality": "0",
            })
        opts["postprocessors"].append({"key": "FFmpegMetadata"})
        return opts

    def _download(self, job: Job) -> str:
        """Runs in a worker thread. Returns the final file path relative to DOWNLOAD_DIR."""
        with yt_dlp.YoutubeDL(self._ydl_opts(job)) as ydl:
            info = ydl.extract_info(job.url, download=True)
        if info and not job.title:
            job.title = info.get("title")
        if info and not job.thumbnail:
            job.thumbnail = info.get("thumbnail")
        path = None
        for rd in (info or {}).get("requested_downloads") or []:
            path = rd.get("filepath") or rd.get("filename")
            if path:
                break
        if not path:
            raise RuntimeError("Download finished but output file was not reported")
        return os.path.relpath(path, DOWNLOAD_DIR)

    def _on_progress(self, job: Job, d: dict) -> None:
        if d.get("filename"):
            job._partials.add(d["filename"])
        if job.cancel_event.is_set():
            raise DownloadCancelled()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                job.progress = round(done * 100 / total, 1)
            job.speed = d.get("_speed_str", "").strip() or None
            job.eta = d.get("_eta_str", "").strip() or None
            job.status = "downloading"
            self._save_threadsafe(job, force=False)
        elif d.get("status") == "finished":
            job.progress = 100.0
            job.speed = job.eta = None
            self._save_threadsafe(job)

    def _on_postprocess(self, job: Job, d: dict) -> None:
        if d.get("status") == "started" and job.status != "processing":
            job.status = "processing"
            self._save_threadsafe(job)

    @staticmethod
    def _cleanup_partial(job: Job) -> None:
        """Remove every intermediate file yt-dlp reported for a cancelled job."""
        for name in job._partials:
            base = Path(name)
            for p in (base, base.with_name(base.name + ".part"), base.with_name(base.name + ".ytdl")):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


manager = Manager()
