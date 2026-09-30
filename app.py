import io
import json
import subprocess
import time
import uuid
from pathlib import Path

from flask import Flask, render_template, request, abort, send_file
from werkzeug.utils import secure_filename

import fingerprint

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".3gp", ".flv", ".wmv"}
MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500 MB
UPLOAD_TTL_SECONDS = 30 * 60  # file yang belum diproses lebih dari ini akan dibuang

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
fingerprint.init_db()


def human_size(num_bytes):
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def human_duration(seconds_str):
    try:
        seconds = float(seconds_str)
    except (TypeError, ValueError):
        return None
    hrs, rem = divmod(int(seconds), 3600)
    mins, secs = divmod(rem, 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def probe_video(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe gagal membaca file")
    return json.loads(result.stdout)


def summarize_metadata(raw: dict, filesize: int) -> dict:
    fmt = raw.get("format", {})
    fmt_tags = fmt.get("tags", {}) or {}
    streams = raw.get("streams", []) or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})

    def tag(*keys):
        for k in keys:
            for source in (fmt_tags, video_stream.get("tags", {}) or {}):
                if k in source and source[k]:
                    return source[k]
        return None

    summary = {
        "title": tag("title", "Title"),
        "description": tag("description", "comment", "Comment"),
        "creation_time": tag("creation_time", "date"),
        "duration": human_duration(fmt.get("duration")),
        "duration_raw": fmt.get("duration"),
        "filesize": human_size(int(fmt.get("size", filesize))),
        "bitrate": fmt.get("bit_rate"),
        "format_name": fmt.get("format_long_name"),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "video_codec": video_stream.get("codec_long_name") or video_stream.get("codec_name"),
        "fps": video_stream.get("avg_frame_rate"),
        "audio_codec": audio_stream.get("codec_long_name") or audio_stream.get("codec_name"),
        "audio_channels": audio_stream.get("channels"),
        "camera_make": tag("com.apple.quicktime.make", "make"),
        "camera_model": tag("com.apple.quicktime.model", "model"),
        "gps": tag("com.apple.quicktime.location.ISO6709", "location"),
        "encoder": tag("encoder"),
    }

    # Semua tag mentah, untuk siapa yang butuh detail lengkap tanpa terkecuali
    all_tags = {}
    all_tags.update(fmt_tags)
    if video_stream.get("tags"):
        all_tags.update({f"video:{k}": v for k, v in video_stream["tags"].items()})
    if audio_stream.get("tags"):
        all_tags.update({f"audio:{k}": v for k, v in audio_stream["tags"].items()})

    summary["all_tags"] = all_tags
    return summary


def detect_youtube_origin(metadata: dict) -> dict:
    all_tags = metadata.get("all_tags", {}) or {}
    tag_text = " ".join(str(v) for v in all_tags.values()).lower()
    encoder = (metadata.get("encoder") or "").lower()
    compatible_brands = str(all_tags.get("compatible_brands", "")).lower()
    vendor_id = str(
        all_tags.get("video:vendor_id", all_tags.get("vendor_id", ""))
    ).lower()

    checks = [
        ("Handler name mengandung \"produced by Google Inc.\"", "produced by google inc" in tag_text),
        ("Encoder memakai Lavf (libavformat/FFmpeg)", encoder.startswith("lavf")),
        ("Compatible brand memuat \"av01\" (varian transcode YouTube)", "av01" in compatible_brands),
        ("Vendor ID generik / kosong (bukan langsung dari kamera)", vendor_id in ("", "[0][0][0][0]", "0000")),
    ]
    matched = [desc for desc, ok in checks if ok]
    return {
        "likely_youtube": len(matched) >= 3,
        "matched": matched,
        "score": len(matched),
        "total": len(checks),
    }


def cleanup_stale_uploads():
    cutoff = time.time() - UPLOAD_TTL_SECONDS
    for f in UPLOAD_DIR.glob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def strip_all_metadata(src: Path, dst: Path):
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-map_metadata", "-1",
        "-map_metadata:s:v", "-1",
        "-map_metadata:s:a", "-1",
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "medium",
        "-c:a", "aac",
        "-b:a", "192k",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg gagal menghapus metadata")


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    cleanup_stale_uploads()

    file = request.files.get("video")
    if not file or file.filename == "":
        abort(400, "Tidak ada file yang diunggah")

    filename = secure_filename(file.filename)
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        abort(400, f"Format {ext} tidak didukung")

    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / unique_name
    file.save(dest)

    try:
        raw = probe_video(dest)
        metadata = summarize_metadata(raw, dest.stat().st_size)
        duration = float(metadata.get("duration_raw") or 0) or None
        frame_hashes = fingerprint.extract_frame_hashes(dest, duration)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        abort(422, f"Gagal membaca metadata: {exc}")

    origin = detect_youtube_origin(metadata)
    similar_matches = fingerprint.find_similar(frame_hashes)
    fingerprint.save_fingerprint(filename, duration, frame_hashes)

    return render_template(
        "result.html",
        filename=filename,
        metadata=metadata,
        origin=origin,
        similar_matches=similar_matches,
        unique_name=unique_name,
    )


@app.route("/strip/<unique_name>", methods=["POST"])
def strip(unique_name):
    unique_name = secure_filename(unique_name)
    src = UPLOAD_DIR / unique_name
    if not src.exists():
        abort(404, "File sudah tidak tersedia di server (mungkin sudah dibersihkan). Unggah ulang untuk mencoba lagi.")

    stripped = UPLOAD_DIR / f"stripped_{unique_name}"
    try:
        strip_all_metadata(src, stripped)
        data = stripped.read_bytes()
    except Exception as exc:
        abort(422, f"Gagal menghapus metadata: {exc}")
    finally:
        src.unlink(missing_ok=True)
        stripped.unlink(missing_ok=True)

    download_name = f"no-metadata_{Path(unique_name).name}"
    return send_file(io.BytesIO(data), as_attachment=True, download_name=download_name, mimetype="video/mp4")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
