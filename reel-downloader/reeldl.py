#!/usr/bin/env python3
"""reeldl - download a public Facebook reel to your computer or phone.

Run it with no arguments to open the app in your browser:

    python3 reeldl.py

Or use it straight from the command line:

    python3 reeldl.py https://www.facebook.com/reel/123456789

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
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

__version__ = "2.0.0"

DEFAULT_PORT = 8080
DEFAULT_OUTPUT = Path("downloads")
MAX_THUMB_BYTES = 8 * 1024 * 1024

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


def human_size(num_bytes: float) -> str:
    """Byte count as something readable - 940 KB, not 0.9 MB."""
    if num_bytes < 1024:
        return f"{int(num_bytes)} B"
    if num_bytes < 1048576:
        return f"{num_bytes / 1024:.0f} KB"
    if num_bytes < 1073741824:
        return f"{num_bytes / 1048576:.1f} MB"
    return f"{num_bytes / 1073741824:.2f} GB"


def unique_path(path: Path) -> Path:
    """Never clobber an existing download."""
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem} ({uuid.uuid4().hex[:8]}){path.suffix}")


# --------------------------------------------------------------------------
# Engine probing
# --------------------------------------------------------------------------


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _import_yt_dlp():
    try:
        import yt_dlp  # noqa: PLC0415 - optional dependency, probed at runtime
    except ImportError:
        return None
    return yt_dlp


class _Silent:
    """Swallow yt-dlp's own console output; we report errors ourselves."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _base_options() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "logger": _Silent(),
        "http_headers": {"User-Agent": USER_AGENT},
        "retries": 3,
    }


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
    # Strip yt-dlp's log prefix for a tidier message.
    return re.sub(r"^ERROR:\s*", "", message.strip().splitlines()[-1])


# --------------------------------------------------------------------------
# Fallback extraction (no yt-dlp installed)
# --------------------------------------------------------------------------

# Facebook embeds media URLs in inline JSON on the reel page. These are the
# keys it has used, most preferred first within each quality tier.
_HD_KEYS = ("browser_native_hd_url", "playable_url_quality_hd", "hd_src_no_ratelimit", "hd_src")
_SD_KEYS = ("browser_native_sd_url", "playable_url", "sd_src_no_ratelimit", "sd_src")
_URL_KEYS = _HD_KEYS + _SD_KEYS


def _find_key(page_html: str, keys) -> str | None:
    for key in keys:
        match = re.search(rf'"{key}"\s*:\s*"(https:[^"]+)"', page_html)
        if not match:
            continue
        # The JSON is escaped (\/ and \uXXXX), so decode it as a JSON string.
        try:
            return json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:
            continue
    return None


def extract_direct_url(page_html: str) -> str | None:
    """Pull the best available media URL out of the reel page's inline JSON."""
    return _find_key(page_html, _URL_KEYS)


def extract_title(page_html: str) -> str:
    match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page_html, re.IGNORECASE
    )
    if not match:
        match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.DOTALL | re.IGNORECASE)
    if not match:
        return "facebook-reel"
    title = html.unescape(match.group(1)).strip()
    title = re.sub(r"\s*\|\s*Facebook\s*$", "", title, flags=re.IGNORECASE)
    return title or "facebook-reel"


def extract_thumbnail(page_html: str) -> str | None:
    match = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page_html, re.IGNORECASE
    )
    return html.unescape(match.group(1)) if match else None


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


def _fetch_page(url: str) -> str:
    try:
        with _open(url) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise DownloadError(f"Couldn't load the reel page: {exc}") from exc


# --------------------------------------------------------------------------
# Metadata: what the app shows before you commit to a download
# --------------------------------------------------------------------------


def _format_label(fmt: dict) -> tuple[str, str]:
    height = fmt.get("height")
    label = f"{height}p" if height else (fmt.get("format_note") or "Video")
    bits = []
    if fmt.get("ext"):
        bits.append(fmt["ext"].upper())
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        bits.append(human_size(size))
    elif fmt.get("tbr"):
        bits.append(f"{int(fmt['tbr'])} kbps")
    return label, " · ".join(bits)


def _video_formats(info: dict) -> list[dict]:
    """Pick one entry per resolution, best first, for the quality dropdown."""
    by_height: dict[int, dict] = {}
    for fmt in info.get("formats") or []:
        if not fmt.get("url") or fmt.get("vcodec") == "none":
            continue
        height = fmt.get("height") or 0
        best = by_height.get(height)
        current = fmt.get("filesize") or fmt.get("filesize_approx") or fmt.get("tbr") or 0
        if best is None:
            by_height[height] = fmt
        else:
            previous = best.get("filesize") or best.get("filesize_approx") or best.get("tbr") or 0
            if current > previous:
                by_height[height] = fmt

    out = []
    for height in sorted(by_height, reverse=True):
        fmt = by_height[height]
        label, note = _format_label(fmt)
        out.append({"id": fmt["format_id"], "label": label, "note": note})
    return out


def fetch_info(url: str) -> dict:
    """Look up title, thumbnail and available qualities without downloading."""
    url = normalise_url(url)
    yt_dlp = _import_yt_dlp()

    if yt_dlp is not None:
        try:
            with yt_dlp.YoutubeDL(_base_options()) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise DownloadError(_explain_yt_dlp_error(str(exc))) from exc
        if info and info.get("entries"):
            info = info["entries"][0]
        if not info:
            raise DownloadError("Facebook returned nothing for that link.")

        formats = [{"id": "best", "label": "Best available", "note": "recommended"}]
        formats += _video_formats(info)
        if have_ffmpeg():
            formats.append({"id": "audio", "label": "Audio only", "note": "MP3"})

        return {
            "url": url,
            "title": info.get("title") or "Facebook reel",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "formats": formats,
            "engine": "yt-dlp",
            "direct": {},
        }

    # Fallback: read the page ourselves.
    page = _fetch_page(url)
    hd, sd = _find_key(page, _HD_KEYS), _find_key(page, _SD_KEYS)
    if not (hd or sd):
        raise DownloadError(
            "Couldn't find a video on that page. It may be private, or Facebook "
            "may have changed its markup - installing yt-dlp (pip install yt-dlp) "
            "handles that case much better."
        )

    formats = []
    if hd:
        formats.append({"id": "hd", "label": "HD", "note": "higher quality"})
    if sd:
        formats.append({"id": "sd", "label": "SD", "note": "smaller file"})

    return {
        "url": url,
        "title": extract_title(page),
        "uploader": "",
        "duration": None,
        "thumbnail": extract_thumbnail(page),
        "formats": formats,
        "engine": "builtin",
        "direct": {"hd": hd, "sd": sd},
    }


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------


def _format_selector(format_id: str, audio_only: bool) -> str:
    """Merging separate streams needs ffmpeg, so without it ask for one file."""
    if audio_only:
        return "bestaudio/best"
    if format_id and format_id not in ("best", "hd", "sd"):
        return f"{format_id}+bestaudio/{format_id}" if have_ffmpeg() else format_id
    if have_ffmpeg():
        return "bestvideo*+bestaudio/best"
    return "best[ext=mp4]/best[acodec!=none][vcodec!=none]/best"


def download_with_yt_dlp(url, outdir, progress=None, format_id="best", audio_only=False) -> Path:
    """Preferred path. yt-dlp keeps up with Facebook's markup changes."""
    yt_dlp = _import_yt_dlp()
    if yt_dlp is None:
        raise DownloadError("yt-dlp is not installed.")

    produced: list[str] = []

    def hook(status: dict) -> None:
        if status.get("status") == "downloading" and progress:
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            done = status.get("downloaded_bytes") or 0
            progress(
                int(done * 100 / total) if total else None,
                (status.get("_speed_str") or "").strip(),
                (status.get("_eta_str") or "").strip(),
            )
        elif status.get("status") == "finished":
            produced.append(status["filename"])
            if progress:
                progress(100, "", "")

    options = _base_options() | {
        "format": _format_selector(format_id, audio_only),
        "outtmpl": str(outdir / "%(title).80B [%(id)s].%(ext)s"),
        "restrictfilenames": True,
        "progress_hooks": [hook],
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


def download_fallback(url, outdir, progress=None, format_id="best", info=None) -> Path:
    """No yt-dlp: use the media URL found on the reel page and stream it down."""
    direct = (info or {}).get("direct") or {}
    media_url = direct.get(format_id) or direct.get("hd") or direct.get("sd")
    title = (info or {}).get("title")

    if not media_url:
        page = _fetch_page(url)
        media_url = extract_direct_url(page)
        title = title or extract_title(page)
        if not media_url:
            raise DownloadError(
                "Couldn't find a video URL on that page. It may be private, or "
                "Facebook may have changed its markup - installing yt-dlp "
                "(pip install yt-dlp) handles that case much better."
            )

    outdir.mkdir(parents=True, exist_ok=True)
    target = unique_path(outdir / f"{safe_filename(title or 'facebook-reel')}.mp4")

    try:
        with _open(media_url, timeout=60) as response, open(target, "wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(256 * 1024):
                handle.write(chunk)
                done += len(chunk)
                if progress:
                    progress(int(done * 100 / total) if total else None, "", "")
    except urllib.error.URLError as exc:
        target.unlink(missing_ok=True)
        raise DownloadError(f"The video download failed: {exc}") from exc

    if progress:
        progress(100, "", "")
    return target


def download(url, outdir, progress=None, format_id="best", audio_only=False, info=None) -> Path:
    """Download `url` into `outdir`, using whichever engine is available."""
    url = normalise_url(url)
    outdir.mkdir(parents=True, exist_ok=True)

    if _import_yt_dlp() is not None:
        return download_with_yt_dlp(
            url, outdir, progress=progress, format_id=format_id, audio_only=audio_only
        )
    if audio_only:
        raise DownloadError("Audio-only needs yt-dlp and ffmpeg installed.")
    return download_fallback(url, outdir, progress=progress, format_id=format_id, info=info)


# --------------------------------------------------------------------------
# Job + metadata tracking for the web UI
# --------------------------------------------------------------------------


class Job:
    def __init__(self, url: str, title: str, format_id: str, audio_only: bool):
        self.id = uuid.uuid4().hex
        self.url = url
        self.title = title
        self.format_id = format_id
        self.audio_only = audio_only
        self.state = "queued"  # queued | running | done | error
        self.percent: int | None = None
        self.speed = ""
        self.eta = ""
        self.error = ""
        self.path: Path | None = None
        self.created = time.time()

    def as_dict(self) -> dict:
        exists = bool(self.path and self.path.exists())
        return {
            "id": self.id,
            "state": self.state,
            "percent": self.percent,
            "speed": self.speed,
            "eta": self.eta,
            "error": self.error,
            "title": self.title,
            "filename": self.path.name if self.path else None,
            "size": self.path.stat().st_size if exists else None,
            "is_audio": bool(self.path and self.path.suffix.lower() == ".mp3"),
        }


class JobStore:
    """In-memory registry. Serving files by job id keeps paths off the wire."""

    def __init__(self, outdir: Path, keep: int = 50):
        self.outdir = outdir
        self.keep = keep
        self._jobs: dict[str, Job] = {}
        self._info: dict[str, dict] = {}
        self._lock = threading.Lock()

    # -- metadata ----------------------------------------------------------

    def remember_info(self, info: dict) -> str:
        info_id = uuid.uuid4().hex
        with self._lock:
            self._info[info_id] = info
            if len(self._info) > self.keep:
                for key in list(self._info)[: -self.keep]:
                    self._info.pop(key, None)
        return info_id

    def info(self, info_id: str) -> dict | None:
        with self._lock:
            return self._info.get(info_id)

    # -- jobs --------------------------------------------------------------

    def start(self, url, title, format_id, audio_only, info=None) -> Job:
        job = Job(url, title, format_id, audio_only)
        with self._lock:
            self._jobs[job.id] = job
            if len(self._jobs) > self.keep:
                stale = sorted(self._jobs.values(), key=lambda j: j.created)[: -self.keep]
                for old in stale:
                    self._jobs.pop(old.id, None)
        threading.Thread(target=self._run, args=(job, info), daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def history(self) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        done = [j for j in jobs if j.state == "done" and j.path and j.path.exists()]
        return [j.as_dict() for j in sorted(done, key=lambda j: j.created, reverse=True)]

    def _run(self, job: Job, info: dict | None) -> None:
        job.state = "running"

        def progress(percent, speed, eta):
            job.percent, job.speed, job.eta = percent, speed, eta

        try:
            job.path = download(
                job.url,
                self.outdir,
                progress=progress,
                format_id=job.format_id,
                audio_only=job.audio_only,
                info=info,
            )
            job.state = "done"
            job.percent = 100
        except DownloadError as exc:
            job.state, job.error = "error", str(exc)
        except Exception as exc:  # noqa: BLE001 - a crashed thread must still report back
            job.state, job.error = "error", f"Unexpected error: {exc}"


# --------------------------------------------------------------------------
# Web UI
# --------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1877f2">
<title>Reel Downloader</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f2f4f8; --card: #fff; --text: #14161a; --muted: #6b7280;
    --line: #e2e5ea; --accent: #1877f2; --accent-hover: #1465d8; --accent-text: #fff;
    --field: #f7f8fa; --shadow: 0 1px 2px rgba(16,20,30,.06), 0 8px 24px rgba(16,20,30,.07);
    --ok-bg: #e7f6ec; --ok-text: #10693a;
    --err-bg: #fdecec; --err-text: #a01b1b; --err-line: #f3c9c9;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0e1014; --card: #171a20; --text: #e9eaee; --muted: #98a0ac;
      --line: #272b34; --accent: #4c9aff; --accent-hover: #6aabff; --accent-text: #06131f;
      --field: #101318; --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.35);
      --ok-bg: #12331f; --ok-text: #7fe0a3;
      --err-bg: #351a1a; --err-text: #ffb4b4; --err-line: #5a2b2b;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100dvh; background: var(--bg); color: var(--text);
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    display: flex; justify-content: center;
    padding: 28px 16px calc(48px + env(safe-area-inset-bottom));
  }
  main { width: 100%; max-width: 460px; }

  header { display: flex; align-items: center; gap: 11px; margin-bottom: 22px; }
  .mark {
    width: 38px; height: 38px; border-radius: 11px; flex: none; display: grid; place-items: center;
    background: var(--accent); color: var(--accent-text);
  }
  .mark svg { width: 20px; height: 20px; }
  h1 { font-size: 1.12rem; margin: 0; letter-spacing: -.01em; }
  .engine { font-size: .78rem; color: var(--muted); display: flex; align-items: center; gap: 5px; }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: #22a75b; flex: none; }
  .dot.warn { background: #e0a01a; }

  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 16px;
    padding: 18px; margin-bottom: 14px; box-shadow: var(--shadow);
  }
  .card.flush { padding: 0; overflow: hidden; }
  label.field { display: block; font-size: .8rem; font-weight: 600; color: var(--muted);
                margin-bottom: 7px; letter-spacing: .01em; }

  .input-wrap { display: flex; gap: 8px; }
  input[type=url] {
    flex: 1; min-width: 0; padding: 13px 14px; font-size: 16px; border-radius: 11px;
    border: 1px solid var(--line); background: var(--field); color: var(--text);
    transition: border-color .15s;
  }
  input[type=url]::placeholder { color: var(--muted); opacity: .7; }
  input[type=url]:focus { outline: none; border-color: var(--accent); }
  .paste {
    flex: none; padding: 0 14px; font-size: .85rem; font-weight: 600; cursor: pointer;
    border-radius: 11px; border: 1px solid var(--line); background: var(--field); color: var(--text);
  }
  .paste:active { background: var(--line); }

  button.primary {
    width: 100%; padding: 14px; font-size: 1rem; font-weight: 600; cursor: pointer;
    border: 0; border-radius: 11px; background: var(--accent); color: var(--accent-text);
    margin-top: 12px; transition: background .15s;
  }
  button.primary:hover:not(:disabled) { background: var(--accent-hover); }
  button.primary:disabled { opacity: .5; cursor: default; }
  button.ghost {
    width: 100%; padding: 11px; font-size: .88rem; font-weight: 600; cursor: pointer;
    border: 1px solid var(--line); border-radius: 11px; background: transparent;
    color: var(--muted); margin-top: 9px;
  }
  button.ghost:hover { color: var(--text); }

  .hidden { display: none !important; }

  /* preview */
  .preview { display: flex; gap: 13px; padding: 16px; }
  .thumb {
    width: 78px; height: 100px; border-radius: 10px; object-fit: cover; flex: none;
    background: var(--field); border: 1px solid var(--line);
  }
  .thumb.placeholder { display: grid; place-items: center; color: var(--muted); }
  .meta { min-width: 0; flex: 1; align-self: center; }
  .meta h2 {
    font-size: .95rem; margin: 0 0 5px; line-height: 1.35; letter-spacing: -.01em;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  }
  .meta .sub { font-size: .8rem; color: var(--muted); }
  .picker { border-top: 1px solid var(--line); padding: 16px; }
  select {
    width: 100%; padding: 12px 13px; font-size: 15px; border-radius: 11px;
    border: 1px solid var(--line); background: var(--field); color: var(--text);
    appearance: none; cursor: pointer;
    background-image: linear-gradient(45deg, transparent 50%, currentColor 50%),
                      linear-gradient(135deg, currentColor 50%, transparent 50%);
    background-position: calc(100% - 19px) 51%, calc(100% - 14px) 51%;
    background-size: 5px 5px, 5px 5px; background-repeat: no-repeat;
  }
  select:focus { outline: none; border-color: var(--accent); }

  /* progress */
  .bar { height: 7px; border-radius: 99px; background: var(--line); overflow: hidden; }
  .bar > i { display: block; height: 100%; width: 0; border-radius: 99px;
             background: var(--accent); transition: width .3s ease; }
  .bar.indeterminate > i { width: 35%; animation: slide 1.15s ease-in-out infinite; }
  @keyframes slide { 0% { margin-left: -35%; } 100% { margin-left: 100%; } }
  .prog-head { display: flex; justify-content: space-between; align-items: baseline;
               margin-bottom: 10px; font-size: .88rem; }
  .prog-head b { font-variant-numeric: tabular-nums; font-size: 1.05rem; }
  .prog-sub { font-size: .8rem; color: var(--muted); margin-top: 9px;
              display: flex; justify-content: space-between; }

  /* result */
  video { width: 100%; display: block; background: #000; max-height: 340px; }
  .done-row { display: flex; align-items: center; gap: 7px; font-size: .8rem;
              color: var(--ok-text); background: var(--ok-bg); padding: 9px 16px; }
  .result-body { padding: 16px; }
  a.save {
    display: block; text-align: center; text-decoration: none; padding: 14px;
    border-radius: 11px; background: var(--accent); color: var(--accent-text);
    font-weight: 600; font-size: 1rem;
  }
  a.save:hover { background: var(--accent-hover); }
  .filename { font-size: .8rem; color: var(--muted); word-break: break-word;
              text-align: center; margin-top: 11px; }

  .error { background: var(--err-bg); color: var(--err-text); border: 1px solid var(--err-line);
           border-radius: 12px; padding: 13px 15px; font-size: .88rem; }

  /* history */
  details.history summary {
    cursor: pointer; font-size: .85rem; font-weight: 600; color: var(--muted);
    list-style: none; display: flex; justify-content: space-between; align-items: center;
  }
  details.history summary::-webkit-details-marker { display: none; }
  details.history[open] summary { margin-bottom: 12px; }
  .hist-item { display: flex; align-items: center; gap: 10px; padding: 9px 0;
               border-top: 1px solid var(--line); font-size: .85rem; }
  .hist-item:first-of-type { border-top: 0; }
  .hist-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
               white-space: nowrap; }
  .hist-item a { color: var(--accent); text-decoration: none; font-weight: 600;
                 font-size: .82rem; flex: none; }

  .tips { font-size: .82rem; color: var(--muted); }
  .tips p { margin: 0 0 9px; }
  .tips p:last-child { margin: 0; }
  .tips b { color: var(--text); font-weight: 600; }
</style>
</head>
<body>
<main>
  <header>
    <div class="mark" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
           stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 3v12"/><path d="m7 12 5 5 5-5"/><path d="M4 21h16"/>
      </svg>
    </div>
    <div>
      <h1>Reel Downloader</h1>
      <div class="engine"><span class="dot" id="dot"></span><span id="engine">Ready</span></div>
    </div>
  </header>

  <!-- step 1: the link -->
  <form class="card" id="form">
    <label class="field" for="url">Facebook reel link</label>
    <div class="input-wrap">
      <input id="url" type="url" inputmode="url" autocomplete="off" autocapitalize="off"
             autocorrect="off" spellcheck="false" placeholder="facebook.com/reel/…">
      <button type="button" class="paste hidden" id="pasteBtn">Paste</button>
    </div>
    <button type="submit" class="primary" id="go">Continue</button>
  </form>

  <!-- step 2: preview + quality -->
  <div class="card flush hidden" id="previewCard">
    <div class="preview">
      <img class="thumb hidden" id="thumb" alt="">
      <div class="thumb placeholder" id="thumbFallback" aria-hidden="true">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2"><polygon points="6 3 20 12 6 21 6 3"/></svg>
      </div>
      <div class="meta">
        <h2 id="title"></h2>
        <div class="sub" id="submeta"></div>
      </div>
    </div>
    <div class="picker">
      <label class="field" for="quality">Quality</label>
      <select id="quality"></select>
      <button type="button" class="primary" id="downloadBtn">Download</button>
      <button type="button" class="ghost" id="backBtn">Use a different link</button>
    </div>
  </div>

  <!-- step 3: progress -->
  <div class="card hidden" id="progressCard">
    <div class="prog-head"><span id="progLabel">Downloading</span><b id="pct">—</b></div>
    <div class="bar indeterminate"><i></i></div>
    <div class="prog-sub"><span id="speed"></span><span id="eta"></span></div>
  </div>

  <!-- step 4: the file -->
  <div class="card flush hidden" id="resultCard">
    <video id="player" controls playsinline preload="metadata"></video>
    <div class="done-row">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="20 6 9 17 4 12"/></svg>
      <span id="doneText">Saved to your computer</span>
    </div>
    <div class="result-body">
      <a class="save" id="save" href="#" download>Save to this device</a>
      <div class="filename" id="filename"></div>
      <button type="button" class="ghost" id="againBtn">Download another</button>
    </div>
  </div>

  <div class="card hidden" id="failureCard"><div class="error" id="errtext"></div></div>

  <details class="card history hidden" id="historyCard">
    <summary><span>This session's downloads</span><span id="histCount"></span></summary>
    <div id="histList"></div>
  </details>

  <div class="card tips">
    <p><b>Getting a link:</b> in the Facebook app tap <b>Share → Copy link</b> on the reel,
       then paste it above.</p>
    <p><b>On a phone:</b> tap <b>Save to this device</b>. On iPhone the file lands in
       Files → Downloads — open it there and share it to Photos for your camera roll.
       On Android it goes to Downloads.</p>
    <p>Private, friends-only and age-restricted reels can't be fetched anonymously.</p>
  </div>
</main>

<script>
const $ = (id) => document.getElementById(id);
const form = $('form'), urlInput = $('url'), go = $('go'), pasteBtn = $('pasteBtn');
const previewCard = $('previewCard'), thumb = $('thumb'), thumbFallback = $('thumbFallback');
const titleEl = $('title'), submeta = $('submeta'), quality = $('quality');
const downloadBtn = $('downloadBtn'), backBtn = $('backBtn');
const progressCard = $('progressCard'), bar = progressCard.querySelector('.bar');
const fill = progressCard.querySelector('.bar > i');
const progLabel = $('progLabel'), pct = $('pct'), speed = $('speed'), eta = $('eta');
const resultCard = $('resultCard'), player = $('player'), save = $('save');
const filename = $('filename'), doneText = $('doneText'), againBtn = $('againBtn');
const failureCard = $('failureCard'), errtext = $('errtext');
const historyCard = $('historyCard'), histList = $('histList'), histCount = $('histCount');

let currentInfo = null;

const show = (el, on) => el.classList.toggle('hidden', !on);
function mb(bytes) {
  if (!bytes) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(0) + ' KB';
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
  return (bytes / 1073741824).toFixed(2) + ' GB';
}

function clockTime(seconds) {
  if (!seconds && seconds !== 0) return '';
  const m = Math.floor(seconds / 60), s = Math.round(seconds % 60);
  return m + ':' + String(s).padStart(2, '0');
}

function hideAll() {
  [previewCard, progressCard, resultCard, failureCard].forEach((el) => show(el, false));
}

function fail(message) {
  show(progressCard, false);
  errtext.textContent = message;
  show(failureCard, true);
  go.disabled = false;
  go.textContent = 'Continue';
  downloadBtn.disabled = false;
  downloadBtn.textContent = 'Download';
}

async function api(path, options) {
  const response = await fetch(path, options);
  let data = {};
  try { data = await response.json(); } catch (e) { /* non-JSON error page */ }
  if (!response.ok) throw new Error(data.error || ('Request failed (' + response.status + ')'));
  return data;
}

/* ---- engine badge ---- */
api('api/engine').then((info) => {
  $('engine').textContent = info.engine === 'yt-dlp'
    ? 'yt-dlp ready' + (info.ffmpeg ? ' · ffmpeg' : '')
    : 'basic mode — pip install yt-dlp for best results';
  $('dot').classList.toggle('warn', info.engine !== 'yt-dlp');
}).catch(() => {});

/* ---- clipboard ---- */
if (navigator.clipboard && navigator.clipboard.readText) {
  show(pasteBtn, true);
  pasteBtn.addEventListener('click', async () => {
    try {
      urlInput.value = (await navigator.clipboard.readText()).trim();
      urlInput.focus();
      if (urlInput.value) form.requestSubmit();
    } catch (e) { urlInput.focus(); }
  });
}

/* ---- step 1 -> 2: look up the reel ---- */
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const url = urlInput.value.trim();
  if (!url) return;
  hideAll();
  go.disabled = true;
  go.textContent = 'Looking up…';
  try {
    currentInfo = await api('api/info', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    renderPreview(currentInfo);
  } catch (err) {
    fail(err.message);
  } finally {
    go.disabled = false;
    go.textContent = 'Continue';
  }
});

function renderPreview(info) {
  titleEl.textContent = info.title;
  const parts = [];
  if (info.uploader) parts.push(info.uploader);
  if (info.duration) parts.push(clockTime(info.duration));
  submeta.textContent = parts.join(' · ');

  if (info.has_thumbnail) {
    thumb.src = 'thumb/' + info.id;
    thumb.onload = () => { show(thumb, true); show(thumbFallback, false); };
    thumb.onerror = () => { show(thumb, false); show(thumbFallback, true); };
  } else {
    show(thumb, false);
    show(thumbFallback, true);
  }

  quality.innerHTML = '';
  info.formats.forEach((f) => {
    const option = document.createElement('option');
    option.value = f.id;
    option.textContent = f.note ? f.label + '  ·  ' + f.note : f.label;
    quality.appendChild(option);
  });
  show(previewCard, true);
  downloadBtn.focus({ preventScroll: true });
}

backBtn.addEventListener('click', () => {
  hideAll();
  urlInput.select();
  urlInput.focus();
});

/* ---- step 2 -> 3: download ---- */
downloadBtn.addEventListener('click', async () => {
  if (!currentInfo) return;
  show(failureCard, false);
  downloadBtn.disabled = true;
  downloadBtn.textContent = 'Starting…';
  bar.classList.add('indeterminate');
  fill.style.width = '0';
  pct.textContent = '—';
  speed.textContent = '';
  eta.textContent = '';
  progLabel.textContent = quality.value === 'audio' ? 'Extracting audio' : 'Downloading';
  show(progressCard, true);
  try {
    const job = await api('api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ info_id: currentInfo.id, format_id: quality.value }),
    });
    poll(job.id);
  } catch (err) {
    fail(err.message);
  }
});

async function poll(id) {
  let job;
  try {
    job = await api('api/status/' + id);
  } catch (err) {
    return fail(err.message);
  }
  if (job.state === 'error') return fail(job.error || 'Download failed.');
  if (job.state === 'done') return finish(id, job);

  if (typeof job.percent === 'number') {
    bar.classList.remove('indeterminate');
    fill.style.width = job.percent + '%';
    pct.textContent = job.percent + '%';
  } else {
    pct.textContent = job.state === 'queued' ? '' : '…';
  }
  speed.textContent = job.speed || '';
  eta.textContent = job.eta ? job.eta + ' left' : '';
  setTimeout(() => poll(id), 600);
}

function finish(id, job) {
  show(progressCard, false);
  show(previewCard, false);
  save.href = 'file/' + id;
  save.setAttribute('download', job.filename || '');
  filename.textContent = job.filename + (job.size ? '  ·  ' + mb(job.size) : '');
  doneText.textContent = 'Saved to this computer as ' + job.filename;

  if (job.is_audio) {
    show(player, false);
  } else {
    player.src = 'preview/' + id;
    show(player, true);
  }
  show(resultCard, true);
  downloadBtn.disabled = false;
  downloadBtn.textContent = 'Download';
  refreshHistory();
}

againBtn.addEventListener('click', () => {
  hideAll();
  player.pause();
  player.removeAttribute('src');
  urlInput.value = '';
  urlInput.focus();
});

/* ---- history ---- */
async function refreshHistory() {
  let items = [];
  try { items = await api('api/history'); } catch (e) { return; }
  show(historyCard, items.length > 0);
  histCount.textContent = items.length ? items.length : '';
  histList.innerHTML = '';
  items.forEach((job) => {
    const row = document.createElement('div');
    row.className = 'hist-item';
    const name = document.createElement('span');
    name.className = 'hist-name';
    name.textContent = job.filename;
    name.title = job.filename;
    const size = document.createElement('span');
    size.style.color = 'var(--muted)';
    size.style.flex = 'none';
    size.textContent = job.size ? mb(job.size) : '';
    const link = document.createElement('a');
    link.href = 'file/' + job.id;
    link.setAttribute('download', job.filename);
    link.textContent = 'Save';
    row.append(name, size, link);
    histList.appendChild(row);
  });
}
refreshHistory();

urlInput.focus();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = f"reeldl/{__version__}"
    protocol_version = "HTTP/1.1"
    jobs: JobStore  # set on the class before the server starts

    def log_message(self, fmt, *args):  # quieter than the default access log
        if self.path.startswith(("/api/status", "/api/history")):
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

    def _json(self, status, payload):
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 8192:
            raise DownloadError("Request too large.")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DownloadError("Malformed request.") from exc

    # -- routes ------------------------------------------------------------

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(HTTPStatus.OK, PAGE.encode(), "text/html; charset=utf-8")
        if path == "/api/engine":
            return self._json(
                HTTPStatus.OK,
                {
                    "engine": "yt-dlp" if _import_yt_dlp() else "builtin",
                    "ffmpeg": have_ffmpeg(),
                    "version": __version__,
                },
            )
        if path == "/api/history":
            return self._json(HTTPStatus.OK, self.jobs.history())
        if path.startswith("/api/status/"):
            return self._status(path.rsplit("/", 1)[-1])
        if path.startswith("/thumb/"):
            return self._thumb(path.rsplit("/", 1)[-1])
        if path.startswith("/file/"):
            return self._media(path.rsplit("/", 1)[-1], inline=False)
        if path.startswith("/preview/"):
            return self._media(path.rsplit("/", 1)[-1], inline=True)
        return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    do_HEAD = do_GET

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._body()
        except DownloadError as exc:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        if path == "/api/info":
            return self._info(payload)
        if path == "/api/download":
            return self._download(payload)
        return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    # -- handlers ----------------------------------------------------------

    def _info(self, payload: dict):
        try:
            info = fetch_info(str(payload.get("url", "")))
        except DownloadError as exc:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - never 500 at the user
            return self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Lookup failed: {exc}"})

        info_id = self.jobs.remember_info(info)
        return self._json(
            HTTPStatus.OK,
            {
                "id": info_id,
                "title": info["title"],
                "uploader": info["uploader"],
                "duration": info["duration"],
                "has_thumbnail": bool(info["thumbnail"]),
                "formats": info["formats"],
                "engine": info["engine"],
            },
        )

    def _download(self, payload: dict):
        format_id = str(payload.get("format_id") or "best")
        info_id = payload.get("info_id")
        info = self.jobs.info(str(info_id)) if info_id else None

        if info is None:
            # Allow a bare URL too, so the API is usable without a lookup first.
            try:
                url = normalise_url(str(payload.get("url", "")))
            except DownloadError as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            title = "Facebook reel"
        else:
            url, title = info["url"], info["title"]

        job = self.jobs.start(url, title, format_id, format_id == "audio", info=info)
        return self._json(HTTPStatus.ACCEPTED, {"id": job.id})

    def _status(self, job_id: str):
        job = self.jobs.get(job_id)
        if job is None:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown job."})
        return self._json(HTTPStatus.OK, job.as_dict())

    def _thumb(self, info_id: str):
        """Proxy the poster image so the browser never talks to Facebook itself.

        Only URLs that came back from our own metadata lookup are reachable
        here, so this can't be pointed at arbitrary hosts.
        """
        info = self.jobs.info(info_id)
        if not info or not info.get("thumbnail"):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "No thumbnail."})
        try:
            with _open(info["thumbnail"], timeout=15) as response:
                data = response.read(MAX_THUMB_BYTES)
                mime = response.headers.get("Content-Type", "image/jpeg")
        except (urllib.error.URLError, OSError):
            return self._json(HTTPStatus.BAD_GATEWAY, {"error": "Thumbnail unavailable."})
        if not mime.startswith("image/"):
            return self._json(HTTPStatus.BAD_GATEWAY, {"error": "Not an image."})
        return self._send(HTTPStatus.OK, data, mime)

    def _media(self, job_id: str, inline: bool):
        job = self.jobs.get(job_id)
        if job is None or job.state != "done" or not job.path or not job.path.exists():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "File not ready."})
        self._serve_file(job.path, inline=inline)

    # -- file serving ------------------------------------------------------

    def _serve_file(self, path: Path, inline: bool):
        size = path.stat().st_size
        name = path.name
        mime = "audio/mpeg" if path.suffix.lower() == ".mp3" else "video/mp4"

        start, end = 0, size - 1
        partial = False
        if inline and (header := self.headers.get("Range")):
            span = _parse_range(header, size)
            if span is None:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = span
            partial = True

        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if inline:
            self.send_header("Content-Disposition", "inline")
        else:
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
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # the browser moved on, e.g. seeking in the player
                remaining -= len(chunk)


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Parse a single-range `Range: bytes=…` header. iOS needs this to play video."""
    match = re.match(r"bytes=(\d*)-(\d*)$", header.strip())
    if not match:
        return None
    first, last = match.groups()
    if first:
        start = int(first)
        end = int(last) if last else size - 1
    elif last:  # suffix form: last N bytes
        start, end = max(0, size - int(last)), size - 1
    else:
        return None
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------


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


def serve(host: str, port: int, outdir: Path, open_browser: bool = True) -> int:
    Handler.jobs = JobStore(outdir)
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"Can't listen on {host}:{port} - {exc}", file=sys.stderr)
        print(f"Try another port:  python3 {Path(__file__).name} --port {port + 10}",
              file=sys.stderr)
        return 1

    local = f"http://localhost:{port}"
    print(f"reeldl {__version__}  ·  saving to {outdir.resolve()}")
    print(f"\n  On this computer:  {local}")
    if host == "0.0.0.0" and (ip := lan_address()):  # noqa: S104 - LAN access is the point
        print(f"  On your phone:     http://{ip}:{port}   (same Wi-Fi)")
    if _import_yt_dlp() is None:
        print("\n  Note: yt-dlp isn't installed, so the built-in extractor is in use.")
        print("        pip install yt-dlp   gives much better results.")
    print("\nPress Ctrl+C to stop.\n")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(local)).start()

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

    def progress(percent, speed, eta):
        if percent is None or percent == last[0]:
            return
        last[0] = percent
        bar = "#" * (percent // 4) + "." * (25 - percent // 4)
        tail = f"{speed}  {eta + ' left' if eta else ''}"
        sys.stdout.write(f"\r  [{bar}] {percent:3d}%  {tail}   ")
        sys.stdout.flush()

    try:
        path = download(url, outdir, progress=progress, audio_only=audio_only)
    except DownloadError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130

    print(f"\r  Saved: {path}  ({human_size(path.stat().st_size)}){' ' * 20}")
    return 0


def main(argv: list[str] | None = None) -> int:
    name = Path(__file__).name
    parser = argparse.ArgumentParser(
        prog="reeldl",
        description="Download a public Facebook reel to your computer or phone.",
        epilog=f"Examples:\n"
        f"  python3 {name}                       open the app in your browser\n"
        f"  python3 {name} <reel link>           download straight away\n"
        f"  python3 {name} --port 8090           use a different port\n"
        f"  python3 {name} --local-only          don't expose it to your Wi-Fi\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", nargs="?",
                        help="reel link to download; omit to open the web app")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"where to save files (default: {DEFAULT_OUTPUT}/)")
    parser.add_argument("--audio-only", action="store_true",
                        help="extract MP3 audio instead of video (needs yt-dlp + ffmpeg)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port for the web app (default: {DEFAULT_PORT})")
    parser.add_argument("--local-only", action="store_true",
                        help="bind to localhost only, so phones can't reach it")
    parser.add_argument("--no-open", action="store_true",
                        help="don't open a browser window on start")
    parser.add_argument("--version", action="version", version=f"reeldl {__version__}")
    args = parser.parse_args(argv)

    outdir = args.output.expanduser()

    # No link given (or the old `serve` keyword) means run the web app.
    if not args.url or args.url == "serve":
        host = "127.0.0.1" if args.local_only else "0.0.0.0"  # noqa: S104
        outdir.mkdir(parents=True, exist_ok=True)
        return serve(host, args.port, outdir, open_browser=not args.no_open)
    return cli_download(args.url, outdir, args.audio_only)


if __name__ == "__main__":
    sys.exit(main())
