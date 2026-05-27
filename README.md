# ReClip

A self-hosted, open-source video and audio downloader with a clean web UI. Paste links from YouTube, TikTok, Instagram, Twitter/X, and 1000+ other sites — download as MP4 or MP3.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

https://github.com/user-attachments/assets/419d3e50-c933-444b-8cab-a9724986ba05

![ReClip MP3 Mode](assets/preview-mp3.png)

## Features

- Download videos from 1000+ supported sites (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))
- MP4 video or MP3 audio extraction
- Quality/resolution picker
- Bulk downloads — paste multiple URLs at once
- Automatic URL deduplication
- Clean, responsive UI — no frameworks, no build step
- Optional DocControl naming and XerahS hosted URL handoff
- Single Python file backend

## Quick Start

```bash
brew install yt-dlp ffmpeg    # or apt install ffmpeg && pip install yt-dlp
git clone https://github.com/averygan/reclip.git
cd reclip
./reclip.sh
```

Open **http://localhost:8899**.

Or with Docker:

```bash
docker build -t reclip . && docker run -p 8899:8899 reclip
```

## Usage

1. Paste one or more video URLs into the input box
2. Choose **MP4** (video) or **MP3** (audio)
3. Click **Fetch** to load video info and thumbnails
4. Select quality/resolution if available
5. Click **Download** on individual videos, or **Download All**

## XerahS Integration

This fork can hand completed downloads to the local `xerahs` CLI without failing the ReClip download if the upload step has a problem.

Environment variables:

```bash
XERAHS_ENABLED=true                 # opt in to post-download XerahS work
XERAHS_BIN=xerahs                   # CLI path or command name
XERAHS_UPLOAD_ENABLED=true          # run: xerahs upload <file> --json
XERAHS_COPY_TO_WATCH=false          # copy to the ReClip watch folder before upload
XERAHS_RECLIP_WATCH_FOLDER=/path    # optional; otherwise asks: xerahs reclip status --json
XERAHS_TIMEOUT_SECONDS=300          # upload timeout
```

Recommended local setup:

```bash
xerahs reclip use-default-watch-folder
XERAHS_ENABLED=true XERAHS_UPLOAD_ENABLED=true ./reclip.sh
```

`/api/status/<job_id>` includes an optional `xerahs` object with upload URL, copy path, or error metadata. The browser UI shows the upload URL or a soft XerahS error next to the completed download.

## DocControl Integration

ReClip can ask DocControl to preview and allocate the final controlled filename before handing the file to XerahS. The allocated filename becomes the local download name, the OneDrive watch-folder copy name, and the XerahS upload name.

Environment variables:

```bash
DOCCONTROL_ENABLED=true
DOCCONTROL_REQUIRED=true
DOCCONTROL_REPO=/Users/mike/Projects/KovaForge/DocControl
DOCCONTROL_BIN=/Users/mike/Projects/KovaForge/DocControl/tools/doccontrol/doccontrol.py
DOCCONTROL_CONFIG=/Users/mike/.config/doccontrol/config.json
DOCCONTROL_PROJECT=Family
DOCCONTROL_LEVEL1=MIC
DOCCONTROL_LEVEL2=ENT
DOCCONTROL_LEVEL3=VIS
```

The browser UI also exposes Project, Level 1, Level 2, Level 3, and free-text fields. When those fields are set, ReClip previews the next DocControl filename before download and allocates only after the media download succeeds.

## Supported Sites

Anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), including:

YouTube, TikTok, Instagram, Twitter/X, Reddit, Facebook, Vimeo, Twitch, Dailymotion, SoundCloud, Loom, Streamable, Pinterest, Tumblr, Threads, LinkedIn, and many more.

## Stack

- **Backend:** Python + Flask
- **Frontend:** Vanilla HTML/CSS/JS (single file, no build step)
- **Download engine:** [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/)
- **Dependencies:** 2 (Flask, yt-dlp)

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.

## License

[MIT](LICENSE)
