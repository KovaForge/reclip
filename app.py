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


def xerahs_settings():
    return {
        "enabled": env_flag("XERAHS_ENABLED", False),
        "bin": os.environ.get("XERAHS_BIN", "xerahs"),
        "upload": env_flag("XERAHS_UPLOAD_ENABLED", True),
        "copy_to_watch": env_flag("XERAHS_COPY_TO_WATCH", False),
        "watch_folder": os.environ.get("XERAHS_RECLIP_WATCH_FOLDER"),
        "timeout": env_int("XERAHS_TIMEOUT_SECONDS", 300),
    }


def default_doccontrol_bin():
    repo = os.environ.get("DOCCONTROL_REPO", "/Users/mike/Projects/KovaForge/DocControl")
    candidates = [
        os.path.join(repo, "bin", "doccontrol"),
        os.path.join(repo, "tools", "doccontrol", "doccontrol.py"),
        "doccontrol",
    ]
    for candidate in candidates:
        if candidate == "doccontrol" or os.path.exists(candidate):
            return candidate
    return candidates[-1]


def doccontrol_settings():
    return {
        "enabled": env_flag("DOCCONTROL_ENABLED", False),
        "required": env_flag("DOCCONTROL_REQUIRED", False),
        "bin": os.environ.get("DOCCONTROL_BIN", default_doccontrol_bin()),
        "base_url": os.environ.get("DOCCONTROL_BASE_URL"),
        "timeout": env_int("DOCCONTROL_TIMEOUT_SECONDS", 30),
        "project": os.environ.get("DOCCONTROL_PROJECT"),
        "level1": os.environ.get("DOCCONTROL_LEVEL1"),
        "level2": os.environ.get("DOCCONTROL_LEVEL2"),
        "level3": os.environ.get("DOCCONTROL_LEVEL3"),
        "level4": os.environ.get("DOCCONTROL_LEVEL4"),
        "level5": os.environ.get("DOCCONTROL_LEVEL5"),
        "level6": os.environ.get("DOCCONTROL_LEVEL6"),
    }


def doccontrol_command(settings, args):
    bin_path = settings["bin"]
    command = [bin_path] + args
    if bin_path.endswith(".py"):
        command = [sys.executable, bin_path] + args
    if settings.get("base_url"):
        command += ["--base-url", settings["base_url"]]
    return command


def run_doccontrol(settings, args):
    result = subprocess.run(
        doccontrol_command(settings, args),
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
        if stderr:
            try:
                error_json = json.loads(stderr)
                error = error_json.get("error")
            except json.JSONDecodeError:
                error = stderr
        raise RuntimeError(error or stdout or f"doccontrol exited with {result.returncode}")

    if not isinstance(parsed, dict):
        raise RuntimeError("doccontrol did not return JSON")

    return parsed


def clean_free_text(value):
    cleaned = re.sub(r"\s+", " ", (value or "").strip())
    cleaned = "".join(c for c in cleaned if c not in r'\/:*?"<>|')
    return cleaned[:90].strip()


def requested_doccontrol(job):
    request_data = job.get("doccontrol_request")
    return request_data if isinstance(request_data, dict) else {}


def doccontrol_payload(job, ext):
    settings = doccontrol_settings()
    request_data = requested_doccontrol(job)
    enabled = settings["enabled"] or bool(request_data)
    if not enabled:
        return None, settings

    payload = {
        "project": request_data.get("project") or settings["project"],
        "level1": request_data.get("level1") or settings["level1"],
        "level2": request_data.get("level2") or settings["level2"],
        "level3": request_data.get("level3") or settings["level3"],
        "level4": request_data.get("level4") or settings["level4"],
        "level5": request_data.get("level5") or settings["level5"],
        "level6": request_data.get("level6") or settings["level6"],
        "free_text": clean_free_text(request_data.get("freeText") or request_data.get("free_text") or job.get("title") or "ReClip video"),
        "extension": (ext or "").lstrip(".") or None,
        "original_query": request_data.get("originalQuery") or request_data.get("original_query") or job.get("url"),
        "force": bool(request_data.get("force")),
    }

    missing = [key for key in ("project", "level1", "level2", "level3") if not payload.get(key)]
    if missing:
        if settings["required"] or request_data:
            raise RuntimeError(f"DocControl missing required field(s): {', '.join(missing)}")
        return None, settings

    return payload, settings


def doccontrol_args(command, payload):
    args = [
        command,
        "--project", str(payload["project"]),
        "--level1", payload["level1"],
        "--level2", payload["level2"],
        "--level3", payload["level3"],
        "--free-text", payload["free_text"],
    ]
    for level in ("level4", "level5", "level6"):
        if payload.get(level):
            args += [f"--{level}", payload[level]]
    if payload.get("extension"):
        args += ["--extension", payload["extension"]]
    if payload.get("original_query"):
        args += ["--original-query", payload["original_query"]]
    return args


def allocate_doccontrol_name(job_id, job, ext):
    payload, settings = doccontrol_payload(job, ext)
    if not payload:
        return None

    metadata = {
        "enabled": True,
        "project": payload["project"],
        "levels": {key: payload[key] for key in ("level1", "level2", "level3", "level4", "level5", "level6") if payload.get(key)},
        "freeText": payload["free_text"],
        "extension": payload["extension"],
    }
    job["doccontrol"] = metadata
    save_job(job_id)

    preview = run_doccontrol(settings, doccontrol_args("preview-name", payload))
    metadata["preview"] = preview.get("preview")
    save_job(job_id)

    allocate_args = doccontrol_args("allocate-name", payload)
    if payload.get("force"):
        allocate_args.append("--force")
    allocated = run_doccontrol(settings, allocate_args)
    if allocated.get("created") is not True:
        metadata["status"] = allocated.get("status") or "blocked"
        metadata["allocation"] = allocated
        raise RuntimeError(allocated.get("message") or "DocControl allocation was not created")

    document = allocated.get("document") or {}
    file_name = document.get("fileName")
    if not file_name:
        raise RuntimeError("DocControl allocation did not return fileName")

    metadata["status"] = "created"
    metadata["document"] = document
    metadata["fileName"] = file_name
    save_job(job_id)
    return file_name


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


def upload_with_xerahs(source_path, display_name, settings, as_file=False):
    # Append 8-char random suffix to preserve descriptive name while matching XerahS UI behavior
    stem, ext = os.path.splitext(display_name)
    display_name = f"{stem}-{uuid.uuid4().hex[:8]}{ext}"
    command = [settings["bin"], "upload", source_path, "--name", display_name, "--json"]
    if as_file:
        command.append("--as-file")

    result = subprocess.run(
        command,
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


def approved_mp4_url(url):
    host = os.environ.get("XERAHS_APPROVED_MP4_HOST", "mike.getsharex.com").strip().lower()
    return isinstance(url, str) and url.lower().startswith(f"https://{host}/") and ".mp4" in url.lower()


def run_xerahs_bridge(job_id, job, file_path):
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
            as_file = os.path.splitext(display_name)[1].lower() == ".mp4"
            upload = upload_with_xerahs(upload_path, display_name, settings, as_file=as_file)
            metadata["uploaded"] = True
            metadata["upload"] = upload
            metadata["url"] = upload.get("url")
            if as_file and not approved_mp4_url(metadata["url"]):
                raise RuntimeError("XerahS upload did not return the approved MP4 file-host URL")
    except Exception as exc:
        metadata["error"] = str(exc)

    job["xerahs"] = metadata
    save_job(job_id)


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
            if title:
                safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
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
