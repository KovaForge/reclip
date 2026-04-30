import os
import uuid
import glob
import json
import shutil
import subprocess
import threading
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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


def xerahs_settings():
    return {
        "enabled": env_flag("XERAHS_ENABLED", False),
        "bin": os.environ.get("XERAHS_BIN", "xerahs"),
        "upload": env_flag("XERAHS_UPLOAD_ENABLED", True),
        "copy_to_watch": env_flag("XERAHS_COPY_TO_WATCH", False),
        "watch_folder": os.environ.get("XERAHS_RECLIP_WATCH_FOLDER"),
        "timeout": env_int("XERAHS_TIMEOUT_SECONDS", 300),
    }


def query_xerahs_watch_folder(settings):
    if settings["watch_folder"]:
        return settings["watch_folder"]

    try:
        result = subprocess.run(
            [settings["bin"], "reclip", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if data.get("enabled"):
            return data.get("watchFolder")
    except Exception:
        return None

    return None


def copy_to_watch_folder(source_path, display_name, watch_folder):
    os.makedirs(watch_folder, exist_ok=True)
    safe_name = os.path.basename(display_name) or os.path.basename(source_path)
    target_path = os.path.join(watch_folder, safe_name)

    if os.path.exists(target_path):
        stem, ext = os.path.splitext(safe_name)
        target_path = os.path.join(watch_folder, f"{stem}-{uuid.uuid4().hex[:8]}{ext}")

    shutil.copy2(source_path, target_path)
    return target_path


def upload_with_xerahs(source_path, display_name, settings):
    result = subprocess.run(
        [settings["bin"], "upload", source_path, "--name", display_name, "--json"],
        capture_output=True,
        text=True,
        timeout=settings["timeout"],
    )

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    parsed = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None

    if result.returncode != 0:
        error = None
        if isinstance(parsed, dict):
            error = parsed.get("error")
        raise RuntimeError(error or stderr or stdout or f"xerahs exited with {result.returncode}")

    if not isinstance(parsed, dict):
        raise RuntimeError("xerahs upload did not return JSON")

    return parsed


def run_xerahs_bridge(job, file_path):
    settings = xerahs_settings()
    metadata = {
        "enabled": settings["enabled"],
        "uploaded": False,
        "copied": False,
    }

    if not settings["enabled"]:
        job["xerahs"] = metadata
        return

    watch_folder = query_xerahs_watch_folder(settings)
    if watch_folder:
        metadata["watch_folder"] = watch_folder

    display_name = job.get("filename") or os.path.basename(file_path)

    try:
        upload_path = file_path
        if settings["copy_to_watch"]:
            if not watch_folder:
                raise RuntimeError("XERAHS_COPY_TO_WATCH is enabled but no watch folder is configured")
            upload_path = copy_to_watch_folder(file_path, display_name, watch_folder)
            metadata["copied"] = True
            metadata["copied_path"] = upload_path

        if settings["upload"]:
            upload = upload_with_xerahs(upload_path, display_name, settings)
            metadata["uploaded"] = True
            metadata["upload"] = upload
            metadata["url"] = upload.get("url")
    except Exception as exc:
        metadata["error"] = str(exc)

    job["xerahs"] = metadata


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
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
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
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)

        run_xerahs_bridge(job, chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


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

        info = json.loads(result.stdout)

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


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "xerahs": job.get("xerahs"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
