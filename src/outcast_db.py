# =============================================================================
# outcast_db.py — Outcast · SQLite metadata store (shared by UI and worker)
# -----------------------------------------------------------------------------
# The durable video metadata lives in a single SQLite database under the XDG
# data directory ($XDG_DATA_HOME/outcast/outcast.db, i.e. ~/.local/share/...),
# because it is user data that cannot be regenerated once a VOD disappears
# upstream. Regenerable artifacts (thumbnails, logs, status files, live.tsv)
# stay in the cache directory.
#
# This module is imported by the PySide6 UI (reads + user deletions) and by the
# Bash crawler's embedded Python (writes), and also exposes a small CLI so the
# shell worker can drive it directly:
#
#     python3 outcast_db.py init
#     python3 outcast_db.py cached-ids
#     python3 outcast_db.py insert-line "<7-column TSV line>"
#     python3 outcast_db.py trim <max_per_channel>
#     python3 outcast_db.py date-filter <from> <to>
#     python3 outcast_db.py count
#
# Concurrency: WAL mode lets the single writer (the worker, or the UI on a user
# deletion) run alongside readers across processes; busy_timeout absorbs the
# rare contention. We never auto-VACUUM, so a row's implicit rowid stays stable
# and the UI can poll for new rows with "WHERE rowid > :last".
# =============================================================================

import os
import sqlite3
import time
from pathlib import Path

# --- Paths (kept in sync with outcast.py / the worker scripts) ---------------
_DATA_BASE = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
DB_DIR  = _DATA_BASE / "outcast"
DB_FILE = DB_DIR / "outcast.db"

DEFAULT_SOURCE = "youtube"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    channel       TEXT NOT NULL DEFAULT '',
    channel_id    TEXT NOT NULL DEFAULT '',
    date          TEXT NOT NULL DEFAULT '',
    duration      TEXT NOT NULL DEFAULT '?',
    source        TEXT NOT NULL DEFAULT 'youtube',
    url           TEXT NOT NULL DEFAULT '',
    fetched_at    INTEGER NOT NULL DEFAULT 0,
    last_seen_at  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_videos_channel_date ON videos(channel, date);
CREATE INDEX IF NOT EXISTS idx_videos_source       ON videos(source);
CREATE INDEX IF NOT EXISTS idx_videos_date         ON videos(date);
"""


# -----------------------------------------------------------------------------
# Connection
# -----------------------------------------------------------------------------
def connect(db_path: Path | str = DB_FILE) -> sqlite3.Connection:
    """Open (creating if needed) the database with sane pragmas and schema."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.executescript(_SCHEMA)
    con.commit()
    return con


# -----------------------------------------------------------------------------
# Row helpers
# -----------------------------------------------------------------------------
def _row_to_dict(r: sqlite3.Row) -> dict:
    """Map a DB row to the dict shape the UI model/delegate expect."""
    vid_id = r["id"]
    source = r["source"] or DEFAULT_SOURCE
    url = r["url"] or ""
    if not url and source == DEFAULT_SOURCE:
        url = f"https://www.youtube.com/watch?v={vid_id}"
    return {
        "vid_id":   vid_id,
        "title":    r["title"],
        "channel":  r["channel"],
        "date":     r["date"],
        "duration": r["duration"] or "?",
        "source":   source,
        "url":      url,
    }


def parse_tsv_line(line: str) -> dict | None:
    """Parse a TSV line: id,title,channel,date,duration,source,url[,channel_id].

    The trailing channel_id column is optional so legacy 7-column lines still
    parse (channel_id then defaults to '').
    """
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 4:
        return None
    vid_id = parts[0].strip()
    if not vid_id:
        return None
    source = parts[5] if len(parts) > 5 and parts[5] else DEFAULT_SOURCE
    url    = parts[6] if len(parts) > 6 and parts[6] else ""
    if not url and source == DEFAULT_SOURCE:
        url = f"https://www.youtube.com/watch?v={vid_id}"
    return {
        "vid_id":     vid_id,
        "title":      parts[1],
        "channel":    parts[2],
        "date":       parts[3],
        "duration":   parts[4] if len(parts) > 4 else "?",
        "source":     source,
        "url":        url,
        "channel_id": parts[7] if len(parts) > 7 else "",
    }


# -----------------------------------------------------------------------------
# Writes
# -----------------------------------------------------------------------------
def upsert(con: sqlite3.Connection, video: dict, commit: bool = True) -> None:
    """Insert a new video, or refresh its metadata if it already exists.

    On conflict we refresh the fields that can legitimately change upstream
    (title/channel/date — e.g. a video the uploader later renamed), while
    preserving an already-known duration or channel_id when the incoming row
    carries only a placeholder ('?' duration or empty channel_id). This is what
    lets a metadata-only refresh pass fix renamed videos without having to
    re-resolve their duration.
    """
    now = int(time.time())
    con.execute(
        """
        INSERT INTO videos
            (id, title, channel, channel_id, date, duration, source, url,
             fetched_at, last_seen_at)
        VALUES (:vid_id, :title, :channel, :channel_id, :date, :duration,
                :source, :url, :now, :now)
        ON CONFLICT(id) DO UPDATE SET
            title        = excluded.title,
            channel      = excluded.channel,
            date         = excluded.date,
            channel_id   = CASE WHEN excluded.channel_id <> ''
                                THEN excluded.channel_id ELSE videos.channel_id END,
            duration     = CASE WHEN excluded.duration NOT IN ('', '?')
                                THEN excluded.duration ELSE videos.duration END,
            url          = CASE WHEN excluded.url <> ''
                                THEN excluded.url ELSE videos.url END,
            last_seen_at = :now
        """,
        {
            "vid_id":     video["vid_id"],
            "title":      video.get("title", ""),
            "channel":    video.get("channel", ""),
            "channel_id": video.get("channel_id", ""),
            "date":       video.get("date", ""),
            "duration":   video.get("duration", "?") or "?",
            "source":     video.get("source", DEFAULT_SOURCE) or DEFAULT_SOURCE,
            "url":        video.get("url", ""),
            "now":        now,
        },
    )
    if commit:
        con.commit()


def insert_batch(con: sqlite3.Connection, path: str) -> int:
    """Upsert every TSV line in *path* in a single transaction.

    Used by the crawler's metadata-refresh pass: one Python process and one
    commit per channel instead of one per video.
    """
    n = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                v = parse_tsv_line(line)
                if v is not None:
                    upsert(con, v, commit=False)
                    n += 1
    except FileNotFoundError:
        return 0
    con.commit()
    return n


def delete_ids(con: sqlite3.Connection, ids) -> int:
    """Delete the given video ids. Returns the number of rows removed."""
    ids = [i for i in ids if i]
    if not ids:
        return 0
    cur = con.cursor()
    total = 0
    # Chunk to stay well under SQLite's variable limit.
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        cur.execute(f"DELETE FROM videos WHERE id IN ({placeholders})", chunk)
        total += cur.rowcount
    con.commit()
    return total


def delete_by_channel(con: sqlite3.Connection, channel: str, sources) -> list[str]:
    """Delete every cached row of a channel within the given sources.

    Returns the removed ids (so callers can also drop their thumbnails)."""
    sources = list(sources)
    if not sources:
        return []
    placeholders = ",".join("?" * len(sources))
    rows = con.execute(
        f"SELECT id FROM videos WHERE channel = ? AND source IN ({placeholders})",
        [channel, *sources],
    ).fetchall()
    ids = [r["id"] for r in rows]
    delete_ids(con, ids)
    return ids


def trim(con: sqlite3.Connection, max_per_channel: int) -> int:
    """Keep only the newest `max_per_channel` videos per channel.

    Mirrors the worker's old MAX_VIDEOS_PER_CHANNEL behaviour (partition by
    channel name, newest first). A value <= 0 means unlimited (no-op)."""
    if max_per_channel is None or max_per_channel <= 0:
        return 0
    cur = con.cursor()
    cur.execute(
        """
        DELETE FROM videos
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY channel
                           ORDER BY date DESC, rowid DESC
                       ) AS rn
                FROM videos
            )
            WHERE rn > ?
        )
        """,
        (max_per_channel,),
    )
    con.commit()
    return cur.rowcount


def date_filter(con: sqlite3.Connection, date_from: str, date_to: str) -> int:
    """Delete rows whose date (first 10 chars) falls outside [from, to].

    Empty bounds are ignored. Matches the worker's old apply_date_filter."""
    date_from = (date_from or "").strip()
    date_to   = (date_to or "").strip()
    if not date_from and not date_to:
        return 0
    cur = con.cursor()
    removed = 0
    if date_from:
        cur.execute("DELETE FROM videos WHERE substr(date,1,10) < ?", (date_from,))
        removed += cur.rowcount
    if date_to:
        cur.execute("DELETE FROM videos WHERE substr(date,1,10) > ?", (date_to,))
        removed += cur.rowcount
    con.commit()
    return removed


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------
def load_all(con: sqlite3.Connection) -> list[dict]:
    """All videos, newest first (matches the worker's old sort by date desc)."""
    rows = con.execute(
        "SELECT * FROM videos ORDER BY date DESC, rowid DESC"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def load_since(con: sqlite3.Connection, last_rowid: int) -> tuple[list[dict], int]:
    """Rows inserted after `last_rowid`, in insertion order.

    Returns (rows, new_max_rowid). Used by the UI to pick up videos the worker
    appended during an in-progress crawl, without a full reload."""
    rows = con.execute(
        "SELECT rowid AS _rid, * FROM videos WHERE rowid > ? ORDER BY rowid ASC",
        (last_rowid,),
    ).fetchall()
    if not rows:
        return [], last_rowid
    new_max = rows[-1]["_rid"]
    return [_row_to_dict(r) for r in rows], new_max


def max_rowid(con: sqlite3.Connection) -> int:
    r = con.execute("SELECT COALESCE(MAX(rowid), 0) AS m FROM videos").fetchone()
    return int(r["m"])


def all_ids(con: sqlite3.Connection) -> list[str]:
    return [r["id"] for r in con.execute("SELECT id FROM videos").fetchall()]


def count(con: sqlite3.Connection) -> int:
    r = con.execute("SELECT COUNT(*) AS c FROM videos").fetchone()
    return int(r["c"])


# -----------------------------------------------------------------------------
# CLI (used by the Bash worker)
# -----------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: outcast_db.py <command> [args]", flush=True)
        return 2
    cmd = argv[0]
    con = connect()
    try:
        if cmd == "init":
            return 0
        if cmd == "cached-ids":
            for vid in all_ids(con):
                print(vid)
            return 0
        if cmd == "insert-line":
            if len(argv) < 2:
                return 2
            v = parse_tsv_line(argv[1])
            if v is not None:
                upsert(con, v)
            return 0
        if cmd == "insert-batch":
            if len(argv) < 2:
                return 2
            insert_batch(con, argv[1])
            return 0
        if cmd == "trim":
            trim(con, int(argv[1]) if len(argv) > 1 else 0)
            return 0
        if cmd == "date-filter":
            date_filter(con, argv[1] if len(argv) > 1 else "",
                             argv[2] if len(argv) > 2 else "")
            return 0
        if cmd == "count":
            print(count(con))
            return 0
        print(f"unknown command: {cmd}", flush=True)
        return 2
    finally:
        con.close()


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
