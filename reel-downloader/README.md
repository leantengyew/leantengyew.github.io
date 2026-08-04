# Reel Downloader

A small app for saving public Facebook reels to your computer or your phone.

It's one Python file with no required dependencies. Run it and it opens a
localhost web app in your browser; the same page works from your phone over
Wi-Fi.

```bash
python3 reeldl.py
```

```
reeldl 2.0.0  ·  saving to /home/you/reel-downloader/downloads

  On this computer:  http://localhost:8080
  On your phone:     http://192.168.1.42:8080   (same Wi-Fi)
```

Paste a reel link, check the preview, pick a quality, download. The finished
video plays right in the page, and **Save to this device** puts it in your
Downloads folder.

## Why it isn't just a web page

A page hosted on GitHub Pages can't download a reel on its own. Browsers block
cross-origin requests, so JavaScript on `leantengyew.github.io` is not allowed
to fetch anything from `facebook.com` — the request fails before it starts.
Every "paste a link" downloader site works by sending your link to a server they
run. This app makes *your* machine that server, so nothing is sent to a third
party.

## Install

You need Python 3.9 or newer. Check with `python3 --version`.

```bash
git clone https://github.com/leantengyew/leantengyew.github.io.git
cd leantengyew.github.io/reel-downloader
pip install -r requirements.txt      # optional but strongly recommended
```

`requirements.txt` installs [yt-dlp](https://github.com/yt-dlp/yt-dlp), which
tracks Facebook's frequent markup changes. Without it the app falls back to a
built-in extractor that works for many reels but breaks more often, and offers
only HD/SD rather than a full quality list. The header shows which engine is
live.

[ffmpeg](https://ffmpeg.org/download.html) is also optional. With it you get
the highest-quality streams merged together and the audio-only option; without
it you get the best single pre-merged file, which for reels is usually fine.

## Using the app

Start it with `python3 reeldl.py` and your browser opens automatically.

1. **Paste a link.** In the Facebook app, tap **Share → Copy link** on the reel.
   On localhost there's a **Paste** button that reads the clipboard for you.
2. **Check the preview.** Title, creator, duration and thumbnail confirm you got
   the right reel before anything downloads.
3. **Pick a quality.** Every resolution Facebook offers, with file sizes. Audio
   only (MP3) appears when ffmpeg is installed.
4. **Download.** Live progress with speed and time remaining.
5. **Save.** The video plays inline so you can check it, and **Save to this
   device** writes it to your Downloads folder.

Everything from this session is listed under **This session's downloads**, so
you can re-save a file without fetching it again.

### On your phone

Open the `http://192.168.1.42:8080`-style address the app printed, on the same
Wi-Fi, with the computer awake.

- **iPhone:** tap Save, choose **Download**. The file lands in Files →
  Downloads. Open it there and share it to Photos for your camera roll.
- **Android:** it goes straight to your Downloads folder.

The clipboard **Paste** button only appears on `localhost`, because browsers
restrict clipboard access on plain-HTTP addresses. On your phone, paste into the
field the normal way.

### Options

```bash
python3 reeldl.py --port 8090      # if 8080 is taken
python3 reeldl.py --local-only     # this computer only, no phone access
python3 reeldl.py --no-open        # don't open a browser on start
python3 reeldl.py -o ~/Movies      # save somewhere else
```

The server binds to your whole local network so your phone can reach it. Anyone
else on that Wi-Fi can use it too, so prefer a network you trust and stop it
with Ctrl+C when you're done. There is no authentication — it is a personal
tool, not something to expose to the internet.

## Command line

Skip the UI entirely by passing a link:

```bash
python3 reeldl.py https://www.facebook.com/reel/123456789
python3 reeldl.py <link> -o ~/Movies
python3 reeldl.py <link> --audio-only
python3 reeldl.py --help
```

## When it fails

- **"Facebook did not serve this video to an anonymous request"** — the reel is
  private, friends-only, or age/region restricted. The app signs in as nobody,
  so it only ever sees what a logged-out visitor sees. This is by design.
- **"Couldn't find a video on that page"** — usually means Facebook changed its
  page markup. `pip install -U yt-dlp` fixes this most of the time.
- **Nothing loads on your phone** — the computer's firewall is likely blocking
  the port, or the phone is on a different network (guest Wi-Fi and mobile data
  both count as different).

## A note on what you download

Downloading a public reel to watch offline is the intended use. The person who
made that video still owns it, so re-uploading it or passing it off as your own
isn't yours to do, and Facebook's terms don't allow bulk or automated scraping.
This app downloads one reel at a time on purpose.

## How it works

The browser never talks to Facebook — it only talks to the local server, which
does the fetching.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/info` | Look up title, thumbnail and available qualities without downloading |
| `POST /api/download` | Start a download; returns a job id |
| `GET /api/status/<id>` | Percent, speed and ETA while it runs |
| `GET /preview/<id>` | The finished file, inline, with HTTP range support so it plays |
| `GET /file/<id>` | The same file as an attachment, for saving |
| `GET /thumb/<id>` | Poster image, proxied so the browser makes no Facebook requests |
| `GET /api/history` | Everything downloaded this session |

Files and thumbnails are addressed by ID rather than by path or URL, so the
server can neither be induced to hand out arbitrary files from your disk nor be
used as an open proxy to arbitrary hosts.
