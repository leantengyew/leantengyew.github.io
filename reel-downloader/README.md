# Reel Downloader

A small tool for saving public Facebook reels to your computer or your phone.

It's one Python file with no required dependencies. Run it as a command, or
start it in server mode to get a web page your phone can use over Wi-Fi.

## Why it isn't just a web page

A page hosted on GitHub Pages can't download a reel on its own. Browsers block
cross-origin requests, so JavaScript on `leantengyew.github.io` is not allowed
to fetch anything from `facebook.com` — the request fails before it starts.
Every "paste a link" website you find online works by sending your link to a
server they run. This tool makes *your* machine that server, so nothing is sent
to a third party.

## Install

You need Python 3.9 or newer. Check with `python3 --version`.

```bash
git clone https://github.com/leantengyew/leantengyew.github.io.git
cd leantengyew.github.io/reel-downloader
pip install -r requirements.txt      # optional but strongly recommended
```

`requirements.txt` installs [yt-dlp](https://github.com/yt-dlp/yt-dlp), which
tracks Facebook's frequent markup changes. Without it the tool falls back to a
built-in extractor that works for many reels but breaks more often.

Installing [ffmpeg](https://ffmpeg.org/download.html) is also optional. With it
you get the highest-quality streams merged together and MP3 extraction; without
it you get the best single pre-merged file, which for reels is usually fine.

## Use it on your computer

```bash
python3 reeldl.py https://www.facebook.com/reel/123456789
```

The video lands in `downloads/`.

```bash
python3 reeldl.py <link> -o ~/Movies     # save somewhere else
python3 reeldl.py <link> --audio-only    # MP3 instead (needs yt-dlp + ffmpeg)
python3 reeldl.py --help                 # everything else
```

## Use it on your phone

Start the server on your computer:

```bash
python3 reeldl.py serve
```

It prints two addresses:

```
  This computer:  http://localhost:8080
  Your phone:     http://192.168.1.42:8080   (same Wi-Fi)
```

Open the second one on your phone — same Wi-Fi network, computer stays awake.
Paste a reel link, tap **Download**, then tap **Save to device**.

To get the link: in the Facebook app, tap **Share → Copy link** on the reel.

- **iPhone:** choose **Download** when Safari asks. The file appears in
  Files → Downloads. Open it there and share it to Photos to get it into your
  camera roll.
- **Android:** it goes straight to your Downloads folder.

Options:

```bash
python3 reeldl.py serve --port 8090      # if 8080 is taken
python3 reeldl.py serve --local-only     # this computer only, no phone access
```

The server binds to your whole local network so your phone can reach it. Anyone
else on that Wi-Fi can use it too, so prefer a network you trust, and stop it
with Ctrl+C when you're done. There is no authentication — it is a personal
tool, not something to expose to the internet.

## When it fails

- **"Facebook did not serve this video to an anonymous request"** — the reel is
  private, friends-only, or age/region restricted. The tool signs in as nobody,
  so it only ever sees what a logged-out visitor sees. This is by design.
- **"Couldn't find a video URL on that page"** — usually means Facebook changed
  its page markup. `pip install -U yt-dlp` fixes this most of the time.
- **Nothing loads on your phone** — the computer's firewall is likely blocking
  the port, or the phone is on a different network (guest Wi-Fi and mobile data
  both count as different).

## A note on what you download

Downloading a public reel to watch offline is the intended use. The person who
made that video still owns it, so re-uploading it or passing it off as your own
isn't yours to do, and Facebook's terms don't allow bulk or automated scraping.
This tool downloads one reel at a time on purpose.

## How it works

| Piece | What it does |
| --- | --- |
| `normalise_url` | Validates the link is genuinely a Facebook host |
| `download_with_yt_dlp` | Primary engine; picks a format based on whether ffmpeg exists |
| `download_fallback` | Reads the reel page and pulls the media URL out of its inline JSON |
| `JobStore` | Runs downloads on background threads so the web UI can poll progress |
| `Handler` | Serves the page, the job API, and the finished file as an attachment |

Files are served by job ID rather than by path, so the web server has no way to
be talked into handing out arbitrary files from your disk.
