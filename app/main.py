"""FastAPI entrypoint: REST API, SSE stream, static UI and file serving."""
import asyncio
import json
import re
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .downloader import DOWNLOAD_DIR, FORMATS, manager

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.start()
    yield
    await manager.stop()


app = FastAPI(title="Video Downloader", lifespan=lifespan)


class AddRequest(BaseModel):
    urls: str
    format: str = "best"


_URL_RE = re.compile(r"https?://\S+")


@app.get("/api/formats")
def formats():
    return [{"key": k, "label": v["label"]} for k, v in FORMATS.items()]


@app.get("/api/downloads")
def list_downloads():
    return manager.list_jobs()


@app.post("/api/downloads")
async def add_downloads(req: AddRequest):
    urls = _URL_RE.findall(req.urls)
    if not urls:
        raise HTTPException(400, "No URLs found in input")
    if req.format not in FORMATS:
        raise HTTPException(400, f"Unknown format '{req.format}'")
    # de-duplicate while keeping order
    urls = list(dict.fromkeys(urls))
    return await manager.add(urls, req.format)


@app.delete("/api/downloads/{job_id}")
def delete_download(job_id: str, delete_file: bool = False):
    if not manager.remove(job_id, delete_file=delete_file):
        raise HTTPException(404, "Job not found")
    return {"ok": True}


@app.post("/api/downloads/{job_id}/cancel")
def cancel_download(job_id: str):
    job = manager.cancel(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.to_dict()


@app.post("/api/downloads/{job_id}/retry")
def retry_download(job_id: str):
    job = manager.retry(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.to_dict()


@app.delete("/api/history")
def clear_history():
    return {"removed": manager.clear_history()}


class _ChunkSink:
    """Write-only file object that buffers what ZipFile writes so it can be streamed out."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.pos = 0

    def write(self, b) -> int:
        self.buf += b
        self.pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self.pos

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def _zip_stream(files: list[tuple[Path, str]]):
    """Stream a ZIP of the given (path, arcname) pairs without a temp file.
    Media is already compressed, so entries are stored rather than deflated."""
    sink = _ChunkSink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path, arcname in files:
            info = zipfile.ZipInfo.from_file(path, arcname)  # keeps the file's mtime
            with path.open("rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
                    yield sink.drain()
            yield sink.drain()
    yield sink.drain()


@app.get("/api/archive")
def archive(playlist: str | None = None):
    """Download every finished file (optionally only one playlist's) as a single ZIP."""
    files: dict[Path, str] = {}
    for job in manager.list_jobs():
        if job["status"] != "done" or not job["filename"]:
            continue
        if playlist is not None and job["playlist_title"] != playlist:
            continue
        path = (DOWNLOAD_DIR / job["filename"]).resolve()
        if not path.is_file() or not path.is_relative_to(DOWNLOAD_DIR):
            continue
        # inside a playlist ZIP the playlist folder would just be noise
        files[path] = path.name if playlist is not None else job["filename"]
    if not files:
        raise HTTPException(404, "No finished files to download")
    name = f"{playlist or 'downloads'}.zip"
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return StreamingResponse(
        _zip_stream(sorted(files.items(), key=lambda f: f[1].lower())),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}",
        },
    )


@app.get("/api/events")
async def events():
    q = manager.broadcaster.subscribe()

    async def gen():
        try:
            yield "event: snapshot\ndata: " + json.dumps(manager.list_jobs()) + "\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield "data: " + json.dumps(ev) + "\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            manager.broadcaster.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=DOWNLOAD_DIR), name="files")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
