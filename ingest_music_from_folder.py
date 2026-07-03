"""Ingest MP3 files from input_music/ into MySQL using music_app_v2 schema.

Features
- Dedupe by songs.audio_hash = SHA256(file bytes)
- Reads embedded metadata from MP3 (title, artist(s), album, language/genre if present)
- Extracts embedded cover art (thumbnail) into storage/thumbnails as <audio_hash>.jpg
- Copies MP3 into storage/music with stable naming

Requires external tools:
- ffprobe (from ffmpeg)
- ffmpeg (for thumbnail extraction)

Requires Python packages:
  pip install mysql-connector-python mutagen

Run:
  python ingest_music_from_folder.py

Env vars:
  DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
  INPUT_DIR (default ./input_music)
  MUSIC_DIR (default ./storage/music)
  THUMB_DIR (default ./storage/thumbnails)
"""

from __future__ import annotations

import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from mutagen.mp3 import MP3
from mutagen.id3 import ID3

import mysql.connector


def get_env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"(^-+|-+$)", "", s)
    return s


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ffprobe_duration_seconds(mp3_path: Path) -> Optional[int]:
    try:
        # duration comes as float string
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(mp3_path),
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        d = float(out.strip())
        if not (d >= 0):
            return None
        return int(d)
    except Exception:
        return None


def extract_thumbnail(mp3_path: Path, out_jpg: Path) -> bool:
    out_jpg.parent.mkdir(parents=True, exist_ok=True)

    try:
        id3 = ID3(str(mp3_path))
        apic_frames = [frame for frame in id3.getall('APIC') if getattr(frame, 'data', None)]
        if apic_frames:
            with out_jpg.open('wb') as f:
                f.write(apic_frames[0].data)
            return out_jpg.exists() and out_jpg.stat().st_size > 0
    except Exception:
        pass

    # Fallback: use a separate thumbnail file saved alongside the mp3
    for ext in ['.jpg', '.jpeg', '.png', '.webp']:
        candidate = mp3_path.with_suffix(ext)
        if candidate.exists():
            try:
                if candidate.suffix.lower() == '.webp':
                    subprocess.check_call(
                        [
                            'ffmpeg',
                            '-y',
                            '-i',
                            str(candidate),
                            str(out_jpg),
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    shutil.copy2(candidate, out_jpg)
                return out_jpg.exists() and out_jpg.stat().st_size > 0
            except Exception:
                continue

    try:
        subprocess.check_call(
            [
                'ffmpeg',
                '-y',
                '-i',
                str(mp3_path),
                '-map',
                '0:v:0',
                str(out_jpg),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return out_jpg.exists() and out_jpg.stat().st_size > 0
    except Exception:
        return False


def safe_filename(s: str) -> str:
    # Replace Windows-forbidden filename characters
    return re.sub(r"[<>:\\\"/\\|?*]", "_", s)



def cleanup_sidecar_files(mp3_path: Path) -> None:
    for ext in ['.jpg', '.jpeg', '.png', '.webp']:
        candidate = mp3_path.with_suffix(ext)
        try:
            if candidate.exists():
                candidate.unlink()
        except Exception:
            pass

def get_id3_text(id3: ID3, frame_id: str) -> Optional[str]:
    if frame_id not in id3:
        return None
    v = id3.getall(frame_id)
    if not v:
        return None
    # id3 v2.3 uses .text; be defensive
    item = v[0]
    if hasattr(item, "text"):
        t = item.text
        if isinstance(t, (list, tuple)) and t:
            return str(t[0])
        return str(t)
    if hasattr(item, "value"):
        return str(item.value)
    return str(item)


def get_id3_artist_list(id3: ID3) -> list[str]:
    # Common frames: TPE1 (artists)
    if "TPE1" not in id3:
        return []
    v = id3.getall("TPE1")
    if not v:
        return []
    item = v[0]
    if hasattr(item, "text"):
        # mutagen gives list[str] for multi-value
        t = item.text
        if isinstance(t, (list, tuple)):
            # Some tags include '/' or ';'
            out: list[str] = []
            for x in t:
                for part in re.split(r"[;/,&]", str(x)):
                    part = part.strip()
                    if part:
                        out.append(part)
            return out
        s = str(t)
        return [p.strip() for p in re.split(r"[;/,&]", s) if p.strip()]
    return []


@dataclass
class SongMeta:
    title: str
    artists: list[str]
    album: str
    duration: Optional[int]
    bitrate: Optional[int]
    sample_rate: Optional[int]


def read_metadata(mp3_path: Path) -> SongMeta:
    audio = MP3(str(mp3_path))
    id3 = ID3(str(mp3_path)) if mp3_path.exists() else ID3()

    title = get_id3_text(id3, "TIT2") or mp3_path.stem
    artists = get_id3_artist_list(id3)
    album = get_id3_text(id3, "TALB") or "Single"

    # bitrate/sample_rate from MP3 info when available
    duration = int(audio.info.length) if getattr(audio.info, "length", None) else None
    bitrate = getattr(audio.info, "bitrate", None)
    sample_rate = getattr(audio.info, "sample_rate", None)

    return SongMeta(
        title=str(title).strip(),
        artists=[a.strip() for a in (artists or []) if str(a).strip()] or ["Unknown"],
        album=str(album).strip() or "Single",
        duration=duration,
        bitrate=int(bitrate) if bitrate else None,
        sample_rate=int(sample_rate) if sample_rate else None,
    )


def get_or_create(cur, table: str, unique_key: str, unique_val, create_cols: dict) -> int:
    cur.execute(f"SELECT id FROM {table} WHERE {unique_key} = %s LIMIT 1", (unique_val,))
    row = cur.fetchone()
    if row:
        return int(row[0])

    cols = {unique_key: unique_val, **(create_cols or {})}
    col_names = list(cols.keys())
    placeholders = ",".join(["%s"] * len(col_names))
    sql = f"INSERT INTO {table} ({','.join(col_names)}) VALUES ({placeholders})"
    cur.execute(sql, tuple(cols[c] for c in col_names))
    return int(cur.lastrowid)


def main() -> None:
    DB_HOST = get_env("DB_HOST", "127.0.0.1")
    DB_PORT = int(get_env("DB_PORT", "3306"))
    DB_USER = get_env("DB_USER", "root")
    DB_PASSWORD = get_env("DB_PASSWORD", "")
    DB_NAME = get_env("DB_NAME", "music_app_v2")

    INPUT_DIR = Path(get_env("INPUT_DIR", "./input_music")).resolve()
    MUSIC_DIR = Path(get_env("MUSIC_DIR", "./storage/music")).resolve()
    THUMB_DIR = Path(get_env("THUMB_DIR", "./storage/thumbnails")).resolve()

    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    #mp3_files = sorted(INPUT_DIR.glob("*.mp3"))
    mp3_files = sorted(INPUT_DIR.rglob("*.mp3"))
    print(f"Found {len(mp3_files)} mp3 files in {INPUT_DIR}")

    conn = mysql.connector.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
    )

    try:
        cur = conn.cursor()

        # defaults
        era_id = 5  # 2000s
        language_name = "Hindi"
        language_code = "hi"
        language_native_name = "हिन्दी"
        region_name = "India"
        genre_name = "Unknown"

        for idx, mp3_path in enumerate(mp3_files, start=1):
            base = mp3_path.name
            audio_hash = sha256_file(mp3_path)

            cur.execute("SELECT id FROM songs WHERE audio_hash = %s LIMIT 1", (audio_hash,))
            if cur.fetchone():
                print(f"[{idx}/{len(mp3_files)}] Duplicate skipped")
                mp3_path.unlink(missing_ok=True)
                cleanup_sidecar_files(mp3_path)
                continue

            meta = read_metadata(mp3_path)

            duration = meta.duration
            if duration is None:
                duration = ffprobe_duration_seconds(mp3_path)

            # create language/region/genre
            language_id = get_or_create(
                cur,
                "languages",
                "name",
                language_name,
                {"code": language_code, "native_name": language_native_name},
            )
            region_id = get_or_create(cur, "regions", "name", region_name, {})
            genre_id = get_or_create(cur, "genres", "name", genre_name, {})

            # artists
            artist_ids: list[int] = []
            for an in meta.artists:
                a_slug = slugify(an) or "unknown-artist"
                artist_id = get_or_create(
                    cur,
                    "artists",
                    "slug",
                    a_slug,
                    {"name": an, "slug": a_slug, "region_id": region_id},
                )
                artist_ids.append(artist_id)

            # album by slug
            album_title = meta.album
            album_slug = slugify(album_title) or "single"
            cur.execute("SELECT id FROM albums WHERE slug = %s LIMIT 1", (album_slug,))
            row = cur.fetchone()
            if row:
                album_id = int(row[0])
            else:
                primary_artist_id = artist_ids[0] if artist_ids else None
                cur.execute(
                    "INSERT INTO albums (artist_id, title, slug, release_year, cover_image) VALUES (%s,%s,%s,%s,%s)",
                    (primary_artist_id, album_title, album_slug, None, None),
                )
                album_id = int(cur.lastrowid)

            # copy mp3 into storage/music
            dest_audio_name = f"{str(idx).zfill(2)} - {safe_filename(meta.title)}.mp3"
            dest_audio_path = MUSIC_DIR / dest_audio_name
            if not dest_audio_path.exists():
                shutil.copy2(mp3_path, dest_audio_path)

            # extract thumbnail
            thumb_jpg = THUMB_DIR / f"{audio_hash}.jpg"
            has_thumb = extract_thumbnail(mp3_path, thumb_jpg)
            #thumbnail_path = str(thumb_jpg) if has_thumb else None
            thumbnail_path = (
                "storage/thumbnails/" + thumb_jpg.name
                if has_thumb else None
            )

            if has_thumb:
                cur.execute(
                    "UPDATE albums SET cover_image=%s WHERE id=%s AND (cover_image IS NULL OR cover_image='')",
                    #(str(thumb_jpg), album_id),
                    ("storage/thumbnails/" + thumb_jpg.name, album_id),
                )

            # song insert
            song_slug = slugify(meta.title) or "song"
            file_size = mp3_path.stat().st_size
            bitrate = meta.bitrate
            sample_rate = meta.sample_rate

            # songs.audio_hash is UNIQUE in schema.
            # If this script was interrupted and rerun, we may reach an mp3 that already
            # exists (even if earlier we didn't detect it due to timing/partial inserts).
            cur.execute("SELECT id FROM songs WHERE audio_hash=%s LIMIT 1", (audio_hash,))
            row = cur.fetchone()
            if row:
                song_id = int(row[0])
                print(f"[{idx}/{len(mp3_files)}] Skipping song insert (already exists by audio_hash): {meta.title}")
            else:
                cur.execute(
                    """
                    INSERT INTO songs
                      (album_id, language_id, era_id, title, slug, duration, bitrate, sample_rate, file_size, audio_hash, file_path, thumbnail_path, play_count, created_at)
                    VALUES
                      (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0, current_timestamp())
                    """,
                    (
                        album_id,
                        language_id,
                        era_id,
                        meta.title,
                        song_slug,
                        duration,
                        bitrate,
                        sample_rate,
                        file_size,
                        audio_hash,
                        "storage/music/" + dest_audio_path.name,
                        thumbnail_path,
                    ),
                )
                song_id = int(cur.lastrowid)


            # relations
            for a_idx, artist_id in enumerate(artist_ids):
                # song_artists has PRIMARY KEY (song_id, artist_id)
                # Re-runs should not fail; update role if it already exists.
                cur.execute(
                    """
                    INSERT INTO song_artists (song_id, artist_id, role)
                    VALUES (%s,%s,%s)
                    ON DUPLICATE KEY UPDATE role = VALUES(role)
                    """,
                    (song_id, artist_id, "primary" if a_idx == 0 else "featured"),
                )

            cur.execute(
                "INSERT INTO song_genres (song_id, genre_id) VALUES (%s,%s)",
                (song_id, genre_id),
            )

            conn.commit()
            print(f"[{idx}/{len(mp3_files)}] Inserted: {meta.title}")
            mp3_path.unlink(missing_ok=True)
            cleanup_sidecar_files(mp3_path)

        print("Done.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()

