import os
import uuid
import glob
import json
import re
import shutil
import subprocess
import sys
import threading
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
JOB_STATE_DIR = os.path.join(DOWNLOAD_DIR, "jobs")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(JOB_STATE_DIR, exist_ok=True)

jobs = {}


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def job_state_path(job_id):
    return os.path.join(JOB_STATE_DIR, f"{job_id}.json")


def save_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return
    path = job_state_path(job_id)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


def load_job(job_id):
    path = job_state_path(job_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            job = json.load(f)
        jobs[job_id] = job
        return job
    except (OSError, json.JSONDecodeError):
        return None
def parse_ytdlp_json(stdout):
    """Parse yt-dlp JSON output.

    With ``-j`` yt-dlp prints one JSON object per line. Some extractors
    emit multiple videos even with ``--no-playlist``, so stdout contains
    several objects and a plain ``json.loads`` raises "Extra data".
    Return the first valid object.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        return json.loads(line)
    raise ValueError("yt-dlp returned no data")


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            save_job(job_id)
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            save_job(job_id)
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        allocated_name = allocate_doccontrol_name(job_id, job, ext)
        if allocated_name:
            job["filename"] = allocated_name
        else:
            title = job.get("title", "").strip()
            # Sanitize title for filename (upstream approach, 100-char limit)
            if title:
                safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:100].strip()
                job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
            else:
                job["filename"] = os.path.basename(chosen)

        save_job(job_id)
        run_xerahs_bridge(job_id, job, chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
        save_job(job_id)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        save_job(job_id)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = parse_ytdlp_json(result.stdout)

        # Build quality options: keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--flat-playlist", "-J", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)
        entries = info.get("entries", [])
        urls = [entry.get("url") for entry in entries if entry.get("url")]
        return jsonify({"urls": urls})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")
    doccontrol_request = data.get("doccontrol") if isinstance(data.get("doccontrol"), dict) else None

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}
    if doccontrol_request:
        jobs[job_id]["doccontrol_request"] = doccontrol_request
    save_job(job_id)

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/doccontrol/preview", methods=["POST"])
def preview_doccontrol_name():
    data = request.json or {}
    doccontrol_request = data.get("doccontrol") if isinstance(data.get("doccontrol"), dict) else {}
    format_choice = data.get("format", "video")
    ext = ".mp3" if format_choice == "audio" else ".mp4"
    job = {
        "url": data.get("url", ""),
        "title": data.get("title", ""),
        "doccontrol_request": doccontrol_request,
    }

    try:
        payload, settings = doccontrol_payload(job, ext)
        if not payload:
            return jsonify({"enabled": False})
        preview = run_doccontrol(settings, doccontrol_args("preview-name", payload))
        return jsonify({
            "enabled": True,
            "project": payload["project"],
            "levels": {key: payload[key] for key in ("level1", "level2", "level3", "level4", "level5", "level6") if payload.get(key)},
            "freeText": payload["free_text"],
            "preview": preview.get("preview"),
        })
    except Exception as e:
        return jsonify({"enabled": True, "error": str(e)}), 400


@app.route("/api/doccontrol/defaults")
def doccontrol_defaults():
    settings = doccontrol_settings()
    return jsonify({
        "enabled": settings["enabled"],
        "required": settings["required"],
        "project": settings["project"],
        "level1": settings["level1"],
        "level2": settings["level2"],
        "level3": settings["level3"],
        "level4": settings["level4"],
        "level5": settings["level5"],
        "level6": settings["level6"],
    })


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id) or load_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "doccontrol": job.get("doccontrol"),
        "xerahs": job.get("xerahs"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id) or load_job(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
