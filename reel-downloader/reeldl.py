#!/usr/bin/env python3
"""reeldl - download a public Facebook reel to your computer or phone.

Two ways to use it:

    python3 reeldl.py https://www.facebook.com/reel/123456789
    python3 reeldl.py serve

The first downloads straight to ./downloads. The second starts a small web
server on your machine with a phone-friendly page, so any device on the same
Wi-Fi can paste a link and save the video.

Only download videos you have the right to save. Public reels for personal
offline viewing are the intended use; redistributing someone else's video
generally is not.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

__version__ = "1.0.0"

DEFAULT_PORT = 8080
DEFAULT_OUTPUT = Path("downloads")

FACEBOOK_HOSTS = {
    "facebook.com",
    "m.facebook.com",
    "web.facebook.com",
    "www.facebook.com",
    "fb.watch",
    "fb.me",
    "fb.gg",
}

# A desktop user agent gets the full page markup, which is what the fallback
# extractor needs to find the video URL.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class DownloadError(Exception):
    """Anything that stops us from producing a file, phrased for a human."""


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------


def normalise_url(raw: str) -> str:
    """Validate a pasted link and return it cleaned up.

    Accepts the share links the Facebook apps actually produce, including
    bare `fb.watch/xxxx` with no scheme.
    """
    url = raw.strip().strip("<>\"'")
    if not url:
        raise DownloadError("No link given.")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    if host.startswith("www.") and host != "www.facebook.com":
        host = host[4:]
    if host not in FACEBOOK_HOSTS:
        raise DownloadError(
            f"{host or url!r} is not a Facebook link. Expected something like "
            "facebook.com/reel/... or fb.watch/..."
        )
    return url


def safe_filename(name: str, fallback: str = "facebook-reel") -> str:
    """Strip anything that would upset a filesystem or a Content-Disposition."""
    cleaned = re.sub(r"[^\w\s.-]", "", name, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(". ")
    if not cleaned:
        return fallback
    return cleaned[:120]


# --------------------------------------------------------------------------
# Download engines
# --------------------------------------------------------------------------


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _import_yt_dlp():
    try:
        import yt_dlp  # noqa: PLC0415 - optional dependency, probed at runtime
    except ImportError:
        return None
    return yt_dlp


def download_with_yt_dlp(url: str, outdir: Path, progress=None, audio_only=False) -> Path:
    """Preferred path. yt-dlp keeps up with Facebook's markup changes."""
    yt_dlp = _import_yt_dlp()
    if yt_dlp is None:
        raise DownloadError("yt-dlp is not installed.")

    # Merging separate video+audio streams needs ffmpeg, so without it we ask
    # for a single already-muxed file instead of failing at the merge step.
    if audio_only:
        fmt = "bestaudio/best"
    elif have_ffmpeg():
        fmt = "bestvideo*+bestaudio/best"
    else:
        fmt = "best[ext=mp4]/best[acodec!=none][vcodec!=none]/best"

    produced: list[str] = []

    class Silent:
        """Swallow yt-dlp's own output; we report errors ourselves."""

        def debug(self, msg): pass
        def info(self, msg): pass
        def warning(self, msg): pass
        def error(self, msg): pass

    def hook(status: dict) -> None:
        if status.get("status") == "downloading" and progress:
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            done = status.get("downloaded_bytes") or 0
            pct = int(done * 100 / total) if total else None
            progress(pct, status.get("_speed_str", "").strip())
        elif status.get("status") == "finished":
            produced.append(status["filename"])
            if progress:
                progress(100, "")

    options = {
        "format": fmt,
        "outtmpl": str(outdir / "%(title).80B [%(id)s].%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "logger": Silent(),
        "http_headers": {"User-Agent": USER_AGENT},
        "retries": 3,
    }
    if audio_only:
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(_explain_yt_dlp_error(str(exc))) from exc

    # After post-processing the final name can differ from what the hook saw.
    requested = (info or {}).get("requested_downloads") or []
    final = requested[0].get("filepath") if requested else None
    if final and Path(final).exists():
        return Path(final)
    for candidate in reversed(produced):
        path = Path(candidate)
        if path.exists():
            return path
        # Audio extraction rewrites the extension.
        for sibling in outdir.glob(path.stem + ".*"):
            return sibling
    raise DownloadError("yt-dlp finished but no file was written.")


def _explain_yt_dlp_error(message: str) -> str:
    lowered = message.lower()
    if any(s in lowered for s in ("proxy", "connection", "timed out", "resolve", "network")):
        return (
            "Couldn't reach Facebook. Check that this machine is online and "
            "that no proxy or firewall is blocking facebook.com."
        )
    if "login" in lowered or "private" in lowered or "not available" in lowered:
        return (
            "Facebook did not serve this video to an anonymous request. It is "
            "probably private, friends-only, or region-locked - those can't be "
            "downloaded without being signed in as someone who can see them."
        )
    if "unsupported url" in lowered:
        return "That link doesn't point at a video Facebook will serve."
    # Strip yt-dlp's ANSI prefix for a tidier message.
    return re.sub(r"^ERROR:\s*", "", message.strip().splitlines()[-1])


# Facebook embeds the media URLs in inline JSON on the reel page. These are the
# keys it has used, most preferred first.
_URL_KEYS = (
    "browser_native_hd_url",
    "playable_url_quality_hd",
    "hd_src_no_ratelimit",
    "hd_src",
    "browser_native_sd_url",
    "playable_url",
    "sd_src_no_ratelimit",
    "sd_src",
)


def extract_direct_url(page_html: str) -> str | None:
    """Fallback extractor: pull a media URL out of the reel page's inline JSON."""
    for key in _URL_KEYS:
        match = re.search(rf'"{key}"\s*:\s*"(https:[^"]+)"', page_html)
        if not match:
            continue
        # The JSON is escaped (\/ and \uXXXX), so decode it as a JSON string.
        try:
            return json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:
            continue
    return None


def extract_title(page_html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.DOTALL | re.IGNORECASE)
    if not match:
        return "facebook-reel"
    title = html.unescape(match.group(1)).strip()
    title = re.sub(r"\s*\|\s*Facebook\s*$", "", title, flags=re.IGNORECASE)
    return title or "facebook-reel"


def _open(url: str, timeout: int = 30):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "*/*",
        },
    )
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - scheme is validated


def download_fallback(url: str, outdir: Path, progress=None) -> Path:
    """No yt-dlp available: fetch the page, find the media URL, stream it down."""
    try:
        with _open(url) as response:
            page = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise DownloadError(f"Couldn't load the reel page: {exc}") from exc

    media_url = extract_direct_url(page)
    if not media_url:
        raise DownloadError(
            "Couldn't find a video URL on that page. It may be private, or "
            "Facebook may have changed its markup - installing yt-dlp "
            "(pip install yt-dlp) handles that case much better."
        )

    outdir.mkdir(parents=True, exist_ok=True)
    target = unique_path(outdir / f"{safe_filename(extract_title(page))}.mp4")

    try:
        with _open(media_url, timeout=60) as response, open(target, "wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(256 * 1024):
                handle.write(chunk)
                done += len(chunk)
                if progress:
                    progress(int(done * 100 / total) if total else None, "")
    except urllib.error.URLError as exc:
        target.unlink(missing_ok=True)
        raise DownloadError(f"The video download failed: {exc}") from exc

    if progress:
        progress(100, "")
    return target


def unique_path(path: Path) -> Path:
    """Never clobber an existing download."""
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem} ({uuid.uuid4().hex[:8]}){path.suffix}")


def download(url: str, outdir: Path, progress=None, audio_only=False) -> Path:
    """Download `url` into `outdir`, using whichever engine is available."""
    url = normalise_url(url)
    outdir.mkdir(parents=True, exist_ok=True)

    if _import_yt_dlp() is not None:
        return download_with_yt_dlp(url, outdir, progress=progress, audio_only=audio_only)
    if audio_only:
        raise DownloadError("Audio-only needs yt-dlp and ffmpeg installed.")
    return download_fallback(url, outdir, progress=progress)


# --------------------------------------------------------------------------
# Job tracking for the web UI
# --------------------------------------------------------------------------


class Job:
    def __init__(self, url: str, audio_only: bool):
        self.id = uuid.uuid4().hex
        self.url = url
        self.audio_only = audio_only
        self.state = "queued"  # queued | running | done | error
        self.percent: int | None = None
        self.speed = ""
        self.error = ""
        self.path: Path | None = None
        self.created = time.time()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "percent": self.percent,
            "speed": self.speed,
            "error": self.error,
            "filename": self.path.name if self.path else None,
            "size": self.path.stat().st_size if self.path and self.path.exists() else None,
        }


class JobStore:
    """In-memory job registry. Serving files by job id keeps paths off the wire."""

    def __init__(self, outdir: Path, keep: int = 50):
        self.outdir = outdir
        self.keep = keep
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, url: str, audio_only: bool) -> Job:
        job = Job(url, audio_only)
        with self._lock:
            self._jobs[job.id] = job
            self._evict()
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _evict(self) -> None:
        if len(self._jobs) <= self.keep:
            return
        for job in sorted(self._jobs.values(), key=lambda j: j.created)[: -self.keep]:
            self._jobs.pop(job.id, None)

    def _run(self, job: Job) -> None:
        job.state = "running"

        def progress(percent, speed):
            job.percent = percent
            job.speed = speed

        try:
            job.path = download(job.url, self.outdir, progress=progress, audio_only=job.audio_only)
            job.state = "done"
            job.percent = 100
        except DownloadError as exc:
            job.state, job.error = "error", str(exc)
        except Exception as exc:  # noqa: BLE001 - a crashed thread must still report back
            job.state, job.error = "error", f"Unexpected error: {exc}"


# --------------------------------------------------------------------------
# Web UI
# --------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Reel Downloader</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f4f5f8; --card: #fff; --text: #16181d; --muted: #6b7280;
    --line: #e3e5ea; --accent: #1877f2; --accent-text: #fff;
    --error-bg: #fdecec; --error-text: #a01b1b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #101216; --card: #191c22; --text: #e9eaee; --muted: #9aa1ad;
      --line: #2a2e37; --accent: #4c9aff; --accent-text: #06131f;
      --error-bg: #3a1d1d; --error-text: #ffb4b4;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100dvh; background: var(--bg); color: var(--text);
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; justify-content: center; padding: 24px 16px 48px;
  }
  main { width: 100%; max-width: 480px; }
  h1 { font-size: 1.4rem; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: .9rem; margin: 0 0 20px; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 18px; margin-bottom: 16px;
  }
  label { display: block; font-size: .85rem; color: var(--muted); margin-bottom: 6px; }
  input[type=url] {
    width: 100%; padding: 13px 14px; font-size: 16px; border-radius: 10px;
    border: 1px solid var(--line); background: var(--bg); color: var(--text);
  }
  input[type=url]:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  .row { display: flex; align-items: center; gap: 8px; margin: 14px 0; font-size: .9rem; }
  button {
    width: 100%; padding: 14px; font-size: 1rem; font-weight: 600; cursor: pointer;
    border: 0; border-radius: 10px; background: var(--accent); color: var(--accent-text);
  }
  button:disabled { opacity: .55; cursor: default; }
  .hidden { display: none; }
  .bar { height: 8px; border-radius: 99px; background: var(--line); overflow: hidden; }
  .bar > i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .25s; }
  .bar.indeterminate > i { width: 40%; animation: slide 1.1s ease-in-out infinite; }
  @keyframes slide { 0% { margin-left: -40%; } 100% { margin-left: 100%; } }
  .status { font-size: .9rem; color: var(--muted); margin-top: 10px; }
  .error { background: var(--error-bg); color: var(--error-text); border-radius: 10px;
           padding: 12px 14px; font-size: .9rem; }
  a.save {
    display: block; text-align: center; text-decoration: none; padding: 14px;
    border-radius: 10px; background: var(--accent); color: var(--accent-text); font-weight: 600;
  }
  .filename { font-size: .85rem; color: var(--muted); word-break: break-all; margin-top: 10px;
              text-align: center; }
  .tips { font-size: .82rem; color: var(--muted); }
  .tips p { margin: 0 0 8px; }
  .tips b { color: var(--text); font-weight: 600; }
</style>
</head>
<body>
<main>
  <h1>Reel Downloader</h1>
  <p class="sub">Paste a public Facebook reel link and save the video.</p>

  <form class="card" id="form">
    <label for="url">Reel link</label>
    <input id="url" name="url" type="url" inputmode="url" autocomplete="off"
           autocapitalize="off" spellcheck="false"
           placeholder="https://www.facebook.com/reel/...">
    <label class="row"><input type="checkbox" id="audio"> Audio only (MP3)</label>
    <button type="submit" id="go">Download</button>
  </form>

  <div class="card hidden" id="progress">
    <div class="bar indeterminate"><i></i></div>
    <div class="status" id="status">Starting…</div>
  </div>

  <div class="card hidden" id="result">
    <a class="save" id="save" href="#" download>Save to device</a>
    <div class="filename" id="filename"></div>
  </div>

  <div class="card hidden" id="failure"><div class="error" id="errtext"></div></div>

  <div class="card tips">
    <p><b>Getting the link:</b> in the Facebook app tap <b>Share &rarr; Copy link</b> on the reel,
       then paste it above.</p>
    <p><b>On iPhone:</b> after tapping Save, choose <b>Download</b>; the file lands in
       Files &rarr; Downloads. Open it there and share it to Photos to keep it in your camera roll.</p>
    <p><b>On Android:</b> the file goes straight to your Downloads folder.</p>
    <p>Private, friends-only and age-restricted reels can't be fetched anonymously and will fail.</p>
  </div>
</main>
<script>
const form = document.getElementById('form');
const urlInput = document.getElementById('url');
const audio = document.getElementById('audio');
const go = document.getElementById('go');
const progress = document.getElementById('progress');
const bar = progress.querySelector('.bar');
const fill = progress.querySelector('.bar > i');
const status = document.getElementById('status');
const result = document.getElementById('result');
const save = document.getElementById('save');
const filename = document.getElementById('filename');
const failure = document.getElementById('failure');
const errtext = document.getElementById('errtext');

function show(el, visible) { el.classList.toggle('hidden', !visible); }

function reset() {
  show(progress, false); show(result, false); show(failure, false);
  bar.classList.add('indeterminate'); fill.style.width = '0';
  status.textContent = 'Starting…';
}

function fail(message) {
  show(progress, false);
  errtext.textContent = message;
  show(failure, true);
  go.disabled = false;
  go.textContent = 'Download';
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const url = urlInput.value.trim();
  if (!url) return;
  reset();
  go.disabled = true;
  go.textContent = 'Working…';
  show(progress, true);
  try {
    const response = await fetch('api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, audio_only: audio.checked }),
    });
    const data = await response.json();
    if (!response.ok) return fail(data.error || 'Request failed.');
    poll(data.id);
  } catch (err) {
    fail('Could not reach the downloader: ' + err.message);
  }
});

async function poll(id) {
  try {
    const response = await fetch('api/status/' + id);
    const job = await response.json();
    if (job.state === 'error') return fail(job.error || 'Download failed.');
    if (job.state === 'done') return finish(id, job);
    if (typeof job.percent === 'number') {
      bar.classList.remove('indeterminate');
      fill.style.width = job.percent + '%';
      status.textContent = job.percent + '%' + (job.speed ? ' · ' + job.speed : '');
    } else {
      status.textContent = job.state === 'running' ? 'Downloading…' : 'Queued…';
    }
    setTimeout(() => poll(id), 700);
  } catch (err) {
    fail('Lost contact with the downloader: ' + err.message);
  }
}

function finish(id, job) {
  show(progress, false);
  save.href = 'file/' + id;
  filename.textContent = job.filename + (job.size ? ' · ' + (job.size / 1048576).toFixed(1) + ' MB' : '');
  show(result, true);
  go.disabled = false;
  go.textContent = 'Download another';
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = f"reeldl/{__version__}"
    jobs: JobStore  # set on the class before the server starts

    def log_message(self, fmt, *args):  # quieter than the default access log
        if self.path.startswith("/api/status"):
            return
        sys.stderr.write(f"  {self.command} {self.path}\n")

    # -- responses ---------------------------------------------------------

    def _send(self, status, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload: dict):
        self._send(status, json.dumps(payload).encode(), "application/json")

    # -- routes ------------------------------------------------------------

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(HTTPStatus.OK, PAGE.encode(), "text/html; charset=utf-8")
        if path.startswith("/api/status/"):
            return self._status(path.rsplit("/", 1)[-1])
        if path.startswith("/file/"):
            return self._file(path.rsplit("/", 1)[-1])
        return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    do_HEAD = do_GET

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path != "/api/download":
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        length = int(self.headers.get("Content-Length") or 0)
        if length > 8192:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "Request too large."})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "Malformed request."})

        try:
            url = normalise_url(str(payload.get("url", "")))
        except DownloadError as exc:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        job = self.jobs.start(url, bool(payload.get("audio_only")))
        return self._json(HTTPStatus.ACCEPTED, {"id": job.id})

    def _status(self, job_id: str):
        job = self.jobs.get(job_id)
        if job is None:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown job."})
        return self._json(HTTPStatus.OK, job.as_dict())

    def _file(self, job_id: str):
        job = self.jobs.get(job_id)
        if job is None or job.state != "done" or not job.path or not job.path.exists():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "File not ready."})

        path = job.path
        size = path.stat().st_size
        name = path.name
        mime = "audio/mpeg" if path.suffix.lower() == ".mp3" else "video/mp4"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        # Both forms, so browsers that ignore filename* still get a sane name.
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{safe_filename(name)}"; '
            f"filename*=UTF-8''{quote(name)}",
        )
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as handle:
            shutil.copyfileobj(handle, self.wfile, 256 * 1024)


def lan_address() -> str | None:
    """Best guess at this machine's address on the local network."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 53))  # TEST-NET-1: routable-looking, never answers
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def serve(host: str, port: int, outdir: Path) -> int:
    Handler.jobs = JobStore(outdir)
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"Can't listen on {host}:{port} - {exc}", file=sys.stderr)
        print("Try a different port:  python3 reeldl.py serve --port 8090", file=sys.stderr)
        return 1

    print(f"reeldl {__version__} - saving to {outdir.resolve()}")
    print(f"\n  This computer:  http://localhost:{port}")
    if host == "0.0.0.0" and (ip := lan_address()):  # noqa: S104 - LAN access is the point
        print(f"  Your phone:     http://{ip}:{port}   (same Wi-Fi)")
    if _import_yt_dlp() is None:
        print("\n  Note: yt-dlp isn't installed, using the built-in extractor.")
        print("        pip install yt-dlp  gives far better results.")
    print("\nPress Ctrl+C to stop.\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cli_download(url: str, outdir: Path, audio_only: bool) -> int:
    last = [-1]

    def progress(percent, speed):
        if percent is None or percent == last[0]:
            return
        last[0] = percent
        bar = "#" * (percent // 4) + "." * (25 - percent // 4)
        sys.stdout.write(f"\r  [{bar}] {percent:3d}%  {speed}   ")
        sys.stdout.flush()

    try:
        path = download(url, outdir, progress=progress, audio_only=audio_only)
    except DownloadError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130

    size = path.stat().st_size / 1048576
    print(f"\r  Saved: {path}  ({size:.1f} MB){' ' * 20}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reeldl",
        description="Download a public Facebook reel to your computer or phone.",
        epilog="Examples:\n"
        "  python3 reeldl.py https://www.facebook.com/reel/123456789\n"
        "  python3 reeldl.py serve --port 8080",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", nargs="?", help="reel link, or 'serve' to start the web UI")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"where to save files (default: {DEFAULT_OUTPUT}/)")
    parser.add_argument("--audio-only", action="store_true",
                        help="extract MP3 audio instead of video (needs yt-dlp + ffmpeg)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port for serve mode (default: {DEFAULT_PORT})")
    parser.add_argument("--local-only", action="store_true",
                        help="serve mode: bind to localhost so phones can't reach it")
    parser.add_argument("--version", action="version", version=f"reeldl {__version__}")
    args = parser.parse_args(argv)

    outdir = args.output.expanduser()

    if args.url == "serve":
        host = "127.0.0.1" if args.local_only else "0.0.0.0"  # noqa: S104
        outdir.mkdir(parents=True, exist_ok=True)
        return serve(host, args.port, outdir)
    if not args.url:
        parser.print_help()
        return 2
    return cli_download(args.url, outdir, args.audio_only)


if __name__ == "__main__":
    sys.exit(main())
