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
# Canonical output path for finished mp4/mp3 files (synced via OneDrive).
# Override with the RECLIP_DOWNLOAD_DIR env var if needed.
DEFAULT_DOWNLOAD_DIR = "/home/mike/OneDrive/AI/kf-github-repos/reclip/videos"
DOWNLOAD_DIR = os.environ.get("RECLIP_DOWNLOAD_DIR", DEFAULT_DOWNLOAD_DIR)
# Keep job state files outside the synced folder to avoid OneDrive
# uploading/syncing transient .json files and causing conflicts.
JOB_STATE_DIR = os.environ.get(
    "RECLIP_JOB_STATE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".jobs"),
)
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


# --- DocControl / XerahS helpers (restored from KovaForge/reclip@2d3eed3
# after they were dropped by the botched upstream merge in 8161f22). -------
def xerahs_settings():
    return {
        "enabled": env_flag("XERAHS_ENABLED", False),
        "bin": os.environ.get("XERAHS_BIN", "xerahs"),
        "upload": env_flag("XERAHS_UPLOAD_ENABLED", True),
        "copy_to_watch": env_flag("XERAHS_COPY_TO_WATCH", False),
        "watch_folder": os.environ.get("XERAHS_RECLIP_WATCH_FOLDER"),
        "timeout": env_int("XERAHS_TIMEOUT_SECONDS", 300),
        "approved_mp4_host": os.environ.get(
            "XERAHS_APPROVED_MP4_HOST", "mike.getsharex.com"
        ),
    }


def default_doccontrol_bin():
    repo_root = os.environ.get("DOCCONTROL_REPO", "/home/mike/Work/KovaForge/DocControl")
    return os.environ.get(
        "DOCCONTROL_BIN",
        os.path.join(repo_root, "tools", "doccontrol", "doccontrol.py"),
    )


def doccontrol_settings():
    config_path = os.environ.get("DOCCONTROL_CONFIG")
    settings = {
        "enabled": env_flag("DOCCONTROL_ENABLED", False),
        "required": env_flag("DOCCONTROL_REQUIRED", False),
        "bin": default_doccontrol_bin(),
        "project": os.environ.get("DOCCONTROL_PROJECT"),
        "level1": os.environ.get("DOCCONTROL_LEVEL1"),
        "level2": os.environ.get("DOCCONTROL_LEVEL2"),
        "level3": os.environ.get("DOCCONTROL_LEVEL3"),
        "level4": os.environ.get("DOCCONTROL_LEVEL4"),
        "level5": os.environ.get("DOCCONTROL_LEVEL5"),
        "level6": os.environ.get("DOCCONTROL_LEVEL6"),
    }
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
            for key, value in file_cfg.items():
                if value is not None:
                    settings[key] = value
        except (OSError, json.JSONDecodeError):
            pass
    return settings


def doccontrol_command(settings, args):
    return [sys.executable, settings["bin"], *args]


def run_doccontrol(settings, args):
    if not settings.get("enabled"):
        return {"enabled": False}
    cmd = doccontrol_command(settings, args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"enabled": True, "error": str(exc)}
    if result.returncode != 0:
        return {
            "enabled": True,
            "error": result.stderr.strip().split("\n")[-1] or "doccontrol failed",
        }
    out = result.stdout.strip()
    if not out:
        return {"enabled": True, "result": None}
    try:
        return {"enabled": True, "result": json.loads(out)}
    except json.JSONDecodeError:
        return {"enabled": True, "raw": out}


def clean_free_text(value):
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return "".join(c for c in cleaned if c not in r'\/:*?"<>|')


def requested_doccontrol(job):
    req = job.get("doccontrol_request")
    if not isinstance(req, dict):
        return None
    return req


def doccontrol_payload(job, ext):
    req = requested_doccontrol(job) or {}
    settings = doccontrol_settings()
    if not (settings["enabled"] or req):
        return None, settings
    project = req.get("project") or settings.get("project")
    if not project:
        return None, settings
    payload = {"project": project, "free_text": clean_free_text(req.get("freeText", ""))}
    for level in ("level1", "level2", "level3", "level4", "level5", "level6"):
        value = req.get(level) or settings.get(level)
        if value:
            payload[level] = value
    return payload, settings


def doccontrol_args(command, payload):
    args = [command, "--project", payload["project"]]
    for level in ("level1", "level2", "level3", "level4", "level5", "level6"):
        value = payload.get(level)
        if value:
            args += [f"--{level}", value]
    if payload.get("free_text"):
        args += ["--free-text", payload["free_text"]]
    return args


def allocate_doccontrol_name(job_id, job, ext):
    payload, settings = doccontrol_payload(job, ext)
    if not payload:
        return None
    result = run_doccontrol(settings, doccontrol_args("allocate-name", payload))
    if not result.get("enabled"):
        return None
    name = (result.get("result") or {}).get("name")
    if not name:
        return None
    return f"{name}{ext}"


def query_xerahs_watch_folder(settings):
    if not settings.get("bin"):
        return None
    try:
        result = subprocess.run(
            [settings["bin"], "reclip", "status", "--json"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return info.get("watch_folder") or info.get("watchFolder")


def copy_to_watch_folder(source_path, display_name, watch_folder):
    if not watch_folder:
        return None
    os.makedirs(watch_folder, exist_ok=True)
    target = os.path.join(watch_folder, display_name)
    counter = 1
    while os.path.exists(target):
        stem, e = os.path.splitext(display_name)
        target = os.path.join(watch_folder, f"{stem}_{counter}{e}")
        counter += 1
    shutil.copy2(source_path, target)
    return target


def upload_with_xerahs(source_path, display_name, settings, as_file=False):
    cmd = [settings["bin"], "upload", source_path, "--name", display_name, "--json"]
    if as_file:
        cmd.append("--as-file")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=settings.get("timeout", 300),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"error": str(exc)}
    out = result.stdout.strip()
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {"raw": out, "stderr": result.stderr.strip()}
    if result.returncode != 0:
        data.setdefault("error", result.stderr.strip().split("\n")[-1] or "xerahs upload failed")
    return data


def approved_mp4_url(url, host):
    if not url:
        return False
    return url.startswith(f"https://{host}/")


def run_xerahs_bridge(job_id, job, file_path):
    settings = xerahs_settings()
    metadata = {"enabled": settings["enabled"]}
    if not settings["enabled"]:
        job["xerahs"] = metadata
        save_job(job_id)
        return
    display_name = job.get("filename") or os.path.basename(file_path)
    stem, ext = os.path.splitext(display_name)
    display_name = f"{stem}-{uuid.uuid4().hex[:8]}{ext}"
    job["xerahs_display_name"] = display_name
    as_file = ext.lower() == ".mp4"
    if settings.get("copy_to_watch"):
        watch_folder = settings.get("watch_folder") or query_xerahs_watch_folder(settings)
        if not watch_folder:
            metadata["error"] = "XERAHS_RECLIP_WATCH_FOLDER is not set"
            job["xerahs"] = metadata
            save_job(job_id)
            return
        copied = copy_to_watch_folder(file_path, display_name, watch_folder)
        if copied:
            metadata["copied_path"] = copied
    if settings.get("upload"):
        result = upload_with_xerahs(file_path, display_name, settings, as_file=as_file)
        if as_file and "url" in result and not approved_mp4_url(
            result["url"], settings["approved_mp4_host"]
        ):
            raise RuntimeError(
                f"XerahS upload did not return the approved MP4 file-host URL "
                f"(expected https://{settings['approved_mp4_host']}/...)"
            )
        metadata.update(result)
    job["xerahs"] = metadata
    save_job(job_id)


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
