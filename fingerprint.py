import json
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import imagehash
from PIL import Image

DB_PATH = Path(__file__).resolve().parent / "fingerprints.db"
FRAME_COUNT = 8
SIMILARITY_THRESHOLD = 85.0  # >= ini dianggap kemungkinan duplikat
MIN_REPORT_THRESHOLD = 60.0  # di bawah ini tidak dianggap relevan untuk ditampilkan


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS video_fingerprints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_filename TEXT,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            duration REAL,
            frame_hashes TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def extract_frame_hashes(video_path: Path, duration, count: int = FRAME_COUNT) -> list:
    if not duration or duration <= 0:
        return []
    tmp_dir = Path(tempfile.mkdtemp(prefix="frames_"))
    hashes = []
    try:
        for i in range(count):
            timestamp = duration * (i + 1) / (count + 1)
            frame_path = tmp_dir / f"frame_{i:02d}.jpg"
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{timestamp:.3f}",
                "-i", str(video_path),
                "-vframes", "1",
                "-q:v", "3",
                str(frame_path),
            ]
            subprocess.run(cmd, capture_output=True, timeout=30)
            if frame_path.exists():
                with Image.open(frame_path) as img:
                    hashes.append(str(imagehash.phash(img)))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return hashes


def _frame_similarity(hashes_a: list, hashes_b: list) -> float:
    n = min(len(hashes_a), len(hashes_b))
    if n == 0:
        return 0.0
    total_sim = 0.0
    for i in range(n):
        ha = imagehash.hex_to_hash(hashes_a[i])
        hb = imagehash.hex_to_hash(hashes_b[i])
        max_bits = ha.hash.size
        distance = ha - hb
        total_sim += 1 - (distance / max_bits)
    return (total_sim / n) * 100


def find_similar(hashes: list, exclude_id=None) -> list:
    if not hashes:
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, original_filename, uploaded_at, duration, frame_hashes FROM video_fingerprints"
    ).fetchall()
    conn.close()

    matches = []
    for row in rows:
        if exclude_id is not None and row["id"] == exclude_id:
            continue
        stored_hashes = json.loads(row["frame_hashes"])
        similarity = _frame_similarity(hashes, stored_hashes)
        if similarity >= MIN_REPORT_THRESHOLD:
            matches.append({
                "filename": row["original_filename"],
                "uploaded_at": row["uploaded_at"],
                "similarity": round(similarity, 1),
                "is_duplicate": similarity >= SIMILARITY_THRESHOLD,
            })
    matches.sort(key=lambda m: m["similarity"], reverse=True)
    return matches


def save_fingerprint(filename: str, duration, hashes: list) -> None:
    if not hashes:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO video_fingerprints (original_filename, duration, frame_hashes) VALUES (?, ?, ?)",
        (filename, duration, json.dumps(hashes)),
    )
    conn.commit()
    conn.close()
