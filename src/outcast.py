#!/usr/bin/env python3
# =============================================================================
# outcast.py — Outcast · PySide6 GUI (Wayland/Hyprland Optimized)
# Version: 0.20.0
# -----------------------------------------------------------------------------
# A keyboard-first personal feed aggregator and player for YouTube, Twitch and
# Kick. Subscriptions are crawled by a background worker (outcast-worker.sh)
# via RSS / yt-dlp; live streams are checked by outcast-live.sh. Video metadata
# is cached as TSV and rendered through an efficient QListView model/delegate
# stack. Playback is delegated to mpv (with yt-dlp or streamlink as the
# extraction/streaming backend) through fully configurable command templates.
#
# Key features:
#   * Multi-source feed (YouTube / Twitch VODs+live / Kick) with brand-colored
#     source badges and a live indicator.
#   * Monitored playback: every launch runs under a ProcessWorker (QThread)
#     that captures stdout/stderr to a log, waits without blocking the UI and
#     classifies the result; failures surface in an error panel and banner.
#   * Configurable playback commands (Settings -> Commands): editable binary +
#     argument templates per engine x mode, with placeholders and restore
#     defaults.
#   * Incremental TSV reading by byte offset, six color themes, debounced
#     search, vim-style navigation and a YouTube search tab.
#   * Internationalization (i18n) with runtime language switching.
# =============================================================================

import os
import re
import sys
import json
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from collections import OrderedDict

import i18n
from i18n import tr
import outcast_db

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QListView, QListWidget, QListWidgetItem,
    QLabel, QStackedWidget, QComboBox, QCheckBox, QPushButton,
    QAbstractItemView, QMenu, QSizePolicy, QFrame, QPlainTextEdit,
    QStyledItemDelegate, QTabWidget, QScrollArea, QLayout,
)
from PySide6.QtCore import (
    Qt, QTimer, QSize, QAbstractListModel, QModelIndex,
    QThread, Signal, QObject, QSortFilterProxyModel, QRect, QPoint,
)
from PySide6.QtGui import (
    QPixmap, QShortcut, QKeySequence, QFont, QPainter, QColor,
    QPen, QFontMetrics, QPalette, QBrush, QKeyEvent, QPainterPath,
    QLinearGradient,
)

# =============================================================================
# Paths (XDG Base Directory aware)
# -----------------------------------------------------------------------------
# Honour $XDG_CACHE_HOME / $XDG_CONFIG_HOME with the conventional fallbacks, so
# the app behaves correctly on any distro. The worker scripts use the exact
# same bases (see outcast-worker.sh / outcast-live.sh) so both sides agree.
# =============================================================================
_CACHE_BASE  = Path(os.environ.get("XDG_CACHE_HOME")  or (Path.home() / ".cache"))
_CONFIG_BASE = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))

CACHE_DIR     = _CACHE_BASE / "outcast-feed"
# Durable video metadata lives in a SQLite DB under the XDG data dir
# (see outcast_db.py).
DB_FILE       = outcast_db.DB_FILE
DB_WAL        = Path(str(DB_FILE) + "-wal")
LIVE_TSV      = CACHE_DIR / "live.tsv"
SETTINGS_FILE = CACHE_DIR / "settings"
STATUS_FILE   = CACHE_DIR / ".crawler_status"
LIVE_STATUS_FILE = CACHE_DIR / ".live_status"
CRAWL_ERR_FILE= CACHE_DIR / ".crawl_errors"
PLAY_LOG_DIR  = CACHE_DIR / "play-logs"
THUMBS_DIR    = _CACHE_BASE / "outcast-thumbs"
WORKER_SCRIPT = Path(__file__).parent / "outcast-worker.sh"
LIVE_WORKER_SCRIPT = Path(__file__).parent / "outcast-live.sh"

# Subscription files (shared with the workers)
CONFIG_DIR        = _CONFIG_BASE / "outcast"
SUBS_FILE         = CONFIG_DIR / "subscriptions"   # YouTube (URLs)
TWITCH_SUBS_FILE  = CONFIG_DIR / "twitch"          # Twitch (names/URLs)
KICK_SUBS_FILE    = CONFIG_DIR / "kick"            # Kick (names/URLs)

# Content sources
SRC_YOUTUBE     = "youtube"
SRC_TWITCH_VOD  = "twitch_vod"
SRC_TWITCH_LIVE = "twitch_live"
SRC_KICK_VOD    = "kick_vod"
SRC_KICK_LIVE   = "kick_live"

# =============================================================================
# Tuning constants
# =============================================================================
# A process that dies before this threshold is suspected of "opening and
# closing on its own". It is only reported as an error when the log also shows
# error markers, to avoid false positives (short clips, quick manual exit, ...).
FAST_EXIT_SECONDS = 6.0
LOG_TAIL_BYTES    = 16384   # how much of the log tail we read to diagnose
MAX_ERRORS_KEPT   = 200

# Legacy (Spanish) thumbnail-quality values mapped to the current ones, so
# settings written by older versions keep working after the rename.
_THUMB_QUALITY_LEGACY = {"ahorro": "saver", "equilibrado": "balanced", "alta": "high"}


def _normalize_thumb_quality(value: str) -> str:
    """Map any legacy thumbnail-quality value to its current English key."""
    return _THUMB_QUALITY_LEGACY.get(value, value)


# =============================================================================
# Themes
# =============================================================================
THEMES = {
    "Catppuccin Mocha": {
        "bg": "#11111b", "bg_surface": "#1e1e2e", "bg_overlay": "#181825",
        "border": "#313244", "text": "#cdd6f4", "subtext": "#a6adc8",
        "accent": "#cba6f7", "accent2": "#b4befe", "green": "#a6e3a1",
        "red": "#f38ba8", "yellow": "#f9e2af",
    },
    "Tokyo Night": {
        "bg": "#1a1b26", "bg_surface": "#24283b", "bg_overlay": "#1f2335",
        "border": "#414868", "text": "#c0caf5", "subtext": "#565f89",
        "accent": "#7aa2f7", "accent2": "#bb9af7", "green": "#9ece6a",
        "red": "#f7768e", "yellow": "#e0af68",
    },
    "Gruvbox Dark": {
        "bg": "#282828", "bg_surface": "#3c3836", "bg_overlay": "#32302f",
        "border": "#504945", "text": "#ebdbb2", "subtext": "#a89984",
        "accent": "#d3869b", "accent2": "#83a598", "green": "#b8bb26",
        "red": "#fb4934", "yellow": "#fabd2f",
    },
    "Nord": {
        "bg": "#2e3440", "bg_surface": "#3b4252", "bg_overlay": "#373e4d",
        "border": "#4c566a", "text": "#eceff4", "subtext": "#d8dee9",
        "accent": "#88c0d0", "accent2": "#81a1c1", "green": "#a3be8c",
        "red": "#bf616a", "yellow": "#ebcb8b",
    },
    "Zed Dark": {
        "bg": "#1c1c1c", "bg_surface": "#252525", "bg_overlay": "#222222",
        "border": "#363636", "text": "#d4d4d4", "subtext": "#7e7e7e",
        "accent": "#52a8ff", "accent2": "#e06c75", "green": "#98c379",
        "red": "#e06c75", "yellow": "#d19a66",
    },
    "Rosé Pine": {
        "bg": "#191724", "bg_surface": "#1f1d2e", "bg_overlay": "#26233a",
        "border": "#403d52", "text": "#e0def4", "subtext": "#908caa",
        "accent": "#c4a7e7", "accent2": "#ebbcba", "green": "#9ccfd8",
        "red": "#eb6f92", "yellow": "#f6c177",
    },
}

DEFAULT_THEME = "Catppuccin Mocha"


def build_stylesheet(t: dict) -> str:
    return f"""
    QMainWindow, QWidget {{
        background-color: {t['bg']};
        color: {t['text']};
        font-family: "JetBrains Mono", "Fira Code", "Hack", monospace, sans-serif;
        font-size: 13px;
    }}
    QLineEdit {{
        background-color: {t['bg_surface']};
        border: 1px solid {t['border']};
        color: {t['text']};
        padding: 7px 10px;
        border-radius: 5px;
        font-size: 13px;
        selection-background-color: {t['accent']};
        selection-color: {t['bg']};
    }}
    QLineEdit:focus {{ border: 1px solid {t['accent']}; }}
    QListView, QListWidget {{
        background-color: {t['bg']};
        border: none;
        outline: none;
        color: {t['text']};
    }}
    QListView::item, QListWidget::item {{
        background-color: {t['bg_overlay']};
        border-radius: 6px;
        margin: 2px 0px;
        border: 1px solid transparent;
        padding: 0px;
    }}
    QListView::item:hover, QListWidget::item:hover {{
        background-color: {t['bg_surface']};
        border: 1px solid {t['border']};
    }}
    QListView::item:selected, QListWidget::item:selected {{
        background-color: {t['bg_surface']};
        border: 1px solid {t['accent']};
        color: {t['text']};
    }}
    QScrollBar:vertical {{ background: {t['bg']}; width: 8px; margin: 0; }}
    QScrollBar::handle:vertical {{
        background: {t['border']}; border-radius: 4px; min-height: 20px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {t['subtext']}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar:horizontal {{ background: {t['bg']}; height: 8px; }}
    QScrollBar::handle:horizontal {{ background: {t['border']}; border-radius: 4px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    QComboBox {{
        background-color: {t['bg_surface']};
        border: 1px solid {t['border']};
        color: {t['text']};
        padding: 5px 10px;
        border-radius: 4px;
        min-width: 130px;
    }}
    QComboBox:focus {{ border: 1px solid {t['accent']}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {t['subtext']};
        width: 0; height: 0;
    }}
    QComboBox QAbstractItemView {{
        background-color: {t['bg_surface']};
        border: 1px solid {t['border']};
        color: {t['text']};
        selection-background-color: {t['accent']};
        selection-color: {t['bg']};
        outline: none;
    }}
    QCheckBox {{ color: {t['text']}; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px;
        border: 1px solid {t['border']};
        border-radius: 3px;
        background-color: {t['bg_surface']};
    }}
    QCheckBox::indicator:checked {{
        background-color: {t['accent']};
        border-color: {t['accent']};
    }}
    QPushButton {{
        background-color: {t['accent']};
        color: {t['bg']};
        border: none;
        padding: 6px 14px;
        border-radius: 4px;
        font-weight: bold;
    }}
    QPushButton:hover {{ background-color: {t['accent2']}; }}
    QPushButton:focus {{ border: 1px solid {t['accent2']}; outline: none; }}
    QPlainTextEdit {{
        background-color: {t['bg_overlay']};
        border: 1px solid {t['border']};
        color: {t['subtext']};
        border-radius: 4px;
        font-size: 12px;
    }}
    QMenu {{
        background-color: {t['bg_surface']};
        border: 1px solid {t['border']};
        color: {t['text']};
        padding: 4px;
    }}
    QMenu::item {{ padding: 5px 20px; border-radius: 3px; }}
    QMenu::item:selected {{ background-color: {t['accent']}; color: {t['bg']}; }}
    QToolTip {{
        background-color: {t['bg_surface']};
        color: {t['text']};
        border: 1px solid {t['border']};
        padding: 4px 8px;
    }}
    """


# =============================================================================
# Configurable command templates (playback)
# -----------------------------------------------------------------------------
# Each engine x mode combination is an independent template: a binary plus an
# argument string. They are editable from Settings -> Commands so the app can
# adapt if mpv / yt-dlp / streamlink change, without touching the code.
#
# Available placeholders (a brace form is accepted too, e.g. ${RES}):
#   $URL    Full video URL
#   $ID     Video ID
#   $TITLE  Title (for the window title)
#   $RES    Selected maximum resolution (e.g. 480)
#   $SUBS   Expands to the subtitle flags when enabled, or to nothing.
#           ($SUBS only makes sense in the mpv/yt-dlp templates.)
# Note: {playerinput} is a streamlink-specific placeholder, not an outcast one.
# =============================================================================
CMD_KEYS = ("YTDLP_VIDEO", "YTDLP_AUDIO", "STREAMLINK_VIDEO", "STREAMLINK_AUDIO",
            "TWITCH_VIDEO", "TWITCH_AUDIO", "KICK_VIDEO", "KICK_AUDIO")

# Maps each command key to an i18n catalog key; resolved with tr() at display.
CMD_HUMAN = {
    "YTDLP_VIDEO":      "cmd.YTDLP_VIDEO",
    "YTDLP_AUDIO":      "cmd.YTDLP_AUDIO",
    "STREAMLINK_VIDEO": "cmd.STREAMLINK_VIDEO",
    "STREAMLINK_AUDIO": "cmd.STREAMLINK_AUDIO",
    "TWITCH_VIDEO":     "cmd.TWITCH_VIDEO",
    "TWITCH_AUDIO":     "cmd.TWITCH_AUDIO",
    "KICK_VIDEO":       "cmd.KICK_VIDEO",
    "KICK_AUDIO":       "cmd.KICK_AUDIO",
}

DEFAULT_COMMANDS = {
    "BIN_YTDLP_VIDEO":  "mpv",
    "ARGS_YTDLP_VIDEO": (
        "--ytdl-format=bestvideo[height<=$RES][ext=mp4]+bestaudio[ext=m4a]"
        "/best[height<=$RES]/best --force-window=immediate "
        "--title=$TITLE --cache=yes $SUBS $URL"
    ),
    "BIN_YTDLP_AUDIO":  "mpv",
    "ARGS_YTDLP_AUDIO": (
        "--ytdl-format=bestaudio[abr<=$ABR]/bestaudio/best --no-video "
        "--audio-display=no --title=$TITLE --cache=yes $URL"
    ),
    "BIN_STREAMLINK_VIDEO":  "streamlink",
    "ARGS_STREAMLINK_VIDEO": (
        '--player-no-close --player mpv '
        '--player-args "--force-window=immediate {playerinput}" '
        '-- $URL ${RES}p,best,worst'
    ),
    "BIN_STREAMLINK_AUDIO":  "streamlink",
    "ARGS_STREAMLINK_AUDIO": (
        '--player-no-close --player mpv '
        '--player-args "--no-video --audio-display=no {playerinput}" '
        '-- $URL audio,bestaudio,best'
    ),
    # Twitch: streamlink handles both live streams (twitch.tv/channel) and VODs
    # (twitch.tv/videos/id). Ad filtering is already automatic in modern
    # streamlink, so --twitch-disable-ads is not needed.
    "BIN_TWITCH_VIDEO":  "streamlink",
    "ARGS_TWITCH_VIDEO": (
        '--twitch-supported-codecs h264,h265,av1 '
        '--player-no-close --player mpv '
        '--player-args "--force-window=immediate {playerinput}" '
        '-- $URL ${RES}p,720p,best,worst'
    ),
    "BIN_TWITCH_AUDIO":  "streamlink",
    "ARGS_TWITCH_AUDIO": (
        '--player-no-close --player mpv '
        '--player-args "--no-video --audio-display=no {playerinput}" '
        '-- $URL audio_only,audio,best,worst'
    ),
    # Kick: by default via mpv+yt-dlp (yt-dlp supports Kick reliably and handles
    # both kick.com/channel live streams and kick.com/channel/videos/uuid VODs).
    "BIN_KICK_VIDEO":  "mpv",
    "ARGS_KICK_VIDEO": (
        "--ytdl-format=best[height<=$RES]/best --force-window=immediate "
        "--title=$TITLE --cache=yes $URL"
    ),
    "BIN_KICK_AUDIO":  "mpv",
    "ARGS_KICK_AUDIO": (
        "--ytdl-format=bestaudio[abr<=$ABR]/bestaudio/best --no-video "
        "--audio-display=no --title=$TITLE --cache=yes $URL"
    ),
}

# Subtitle flags that $SUBS expands to when subtitles are enabled.
SUBS_TOKENS = [
    "--sub-auto=fuzzy",
    "--ytdl-raw-options-append=write-auto-subs=",
    "--ytdl-raw-options-append=sub-langs=es.*,en.*",
]


class CommandTemplateError(Exception):
    """Invalid command template (empty binary, unbalanced quotes, ...)."""


def expand_command_args(args_str: str, ctx: list[tuple[str, str]],
                        subs_tokens: list[str]) -> list[str]:
    """Tokenize with shlex and substitute placeholders token by token.

    `ctx` is an ordered list of (placeholder, value); brace variants must come
    before the non-brace ones. `$SUBS` expands to 0..n tokens.
    """
    try:
        raw = shlex.split(args_str)
    except ValueError as exc:
        raise CommandTemplateError(f"malformed arguments ({exc})")
    out: list[str] = []
    for tok in raw:
        if tok in ("$SUBS", "${SUBS}"):
            out.extend(subs_tokens)
            continue
        for key, val in ctx:
            if key in tok:
                tok = tok.replace(key, val)
        out.append(tok)
    return out


# =============================================================================
# FlowLayout — lays children out in a row and wraps to a new line when needed.
# (Adapted from the canonical Qt example; makes the button bar responsive.)
# =============================================================================
class FlowLayout(QLayout):
    def __init__(self, parent=None, margin: int = 0, spacing: int = 6):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self._items: list = []

    def __del__(self):
        while self.count():
            self.takeAt(0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(),
                      margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect, test_only):
        x = rect.x()
        y = rect.y()
        line_height = 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


class FlowBar(QWidget):
    """A FlowLayout container that reserves the height it needs for its width.

    Without this, when the window is narrow the vertical layout does not give it
    enough height for the wrapped rows and the buttons get clipped / disappear.
    """
    def __init__(self, spacing: int = 6, parent=None):
        super().__init__(parent)
        self._flow = FlowLayout(self, margin=0, spacing=spacing)
        sp = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.setSizePolicy(sp)

    def flow(self) -> FlowLayout:
        return self._flow

    def resizeEvent(self, event):
        self.setMinimumHeight(self._flow.heightForWidth(self.width()))
        super().resizeEvent(event)


# =============================================================================
# Playback log diagnosis
# =============================================================================
def _short(line: str, n: int = 200) -> str:
    line = re.sub(r"\s+", " ", line).strip()
    return line if len(line) <= n else line[: n - 1] + "…"


# Patterns ordered by specificity. The second item is an i18n key, or None to
# use the matched log line itself (msg=None).
_ERROR_PATTERNS = [
    (re.compile(r"sign in to confirm", re.I), "err.reason.signin"),
    (re.compile(r"video unavailable", re.I), "err.reason.unavailable"),
    (re.compile(r"private video", re.I), "err.reason.private"),
    (re.compile(r"members[- ]only", re.I), "err.reason.members"),
    (re.compile(r"this live event|premieres in|will begin in", re.I),
     "err.reason.not_started"),
    (re.compile(r"removed by the uploader|account .*terminated", re.I),
     "err.reason.removed"),
    (re.compile(r"no playable streams|no plugin can handle url", re.I),
     "err.reason.no_streams"),
    (re.compile(r"http error 403|forbidden", re.I), "err.reason.forbidden"),
    (re.compile(r"http error 404|not found", re.I), "err.reason.not_found"),
    (re.compile(r"unable to (open|read) url|failed to open|errors when loading file|can not open",
                re.I), "err.reason.cant_open"),
    (re.compile(r"unable to download|unable to extract", re.I),
     "err.reason.extract_failed"),
    (re.compile(r"^\s*error[: ]", re.I), None),
    (re.compile(r"\bERROR\b", re.I), None),
]


def classify_log(log_text: str) -> str | None:
    """Return a human-readable failure reason, or None if no error is recognized."""
    if not log_text:
        return None
    lines = [l for l in (ln.strip() for ln in log_text.splitlines()) if l]
    for pat, msg in _ERROR_PATTERNS:
        for l in lines:
            if pat.search(l):
                return tr(msg) if msg else _short(l)
    return None


def _read_tail(path: str | Path, nbytes: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# =============================================================================
# Process worker — launches and monitors a subprocess off the UI thread
# =============================================================================
class ProcessWorker(QThread):
    """
    Launches `args`, redirects stdout+stderr to `log_path`, and emits `result`
    when the process ends. The child is created in its own session, so closing
    the app does NOT kill it; the worker simply stops watching it.
    """
    result = Signal(dict)

    # internal codes (not from the binaries)
    RC_MISSING   = -101   # binary not found
    RC_SPAWN_ERR = -102   # OSError on launch

    def __init__(self, *, kind: str, args: list[str], label: str,
                 log_path: Path, backend: str = "", parent=None):
        super().__init__(parent)
        self.kind = kind          # "play" | "crawl"
        self.args = args
        self.label = label
        self.log_path = Path(log_path)
        self.backend = backend
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None

    def request_stop(self):
        self._stop.set()

    def run(self):
        start = time.monotonic()
        try:
            logf = open(self.log_path, "wb")
        except OSError:
            logf = None

        try:
            self._proc = subprocess.Popen(
                self.args,
                stdout=(logf or subprocess.DEVNULL),
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            if logf:
                logf.close()
            self._emit(self.RC_MISSING, 0.0, "", missing=self.args[0])
            return
        except OSError as e:
            if logf:
                logf.close()
            self._emit(self.RC_SPAWN_ERR, 0.0, str(e))
            return

        # The child now has its own duplicated fd; close ours.
        if logf:
            logf.close()

        ret = None
        while not self._stop.is_set():
            ret = self._proc.poll()
            if ret is not None:
                break
            self.msleep(200)

        if ret is None:
            # The app is closing: we leave the player running.
            return

        elapsed = time.monotonic() - start
        tail = _read_tail(self.log_path, LOG_TAIL_BYTES)
        self._emit(ret, elapsed, tail)

    def _emit(self, returncode: int, elapsed: float, log_tail: str, missing=None):
        self.result.emit({
            "kind": self.kind,
            "label": self.label,
            "backend": self.backend,
            "returncode": returncode,
            "elapsed": elapsed,
            "log_tail": log_tail,
            "missing": missing,
            "log_path": str(self.log_path),
        })


class ThumbFetcher(QThread):
    """Downloads YouTube mqdefault thumbnails by id (for search results) in the
    background, without blocking the UI."""
    done = Signal()

    def __init__(self, ids: list[str], parent=None):
        super().__init__(parent)
        self._ids = list(ids)
        self._stop = threading.Event()

    def request_stop(self):
        self._stop.set()

    def run(self):
        try:
            THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.done.emit()
            return
        if shutil.which("curl") is None:
            self.done.emit()
            return
        for vid in self._ids:
            if self._stop.is_set():
                break
            path = THUMBS_DIR / f"{vid}.jpg"
            if path.exists():
                continue
            url = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
            try:
                subprocess.run(
                    ["curl", "-s", "--max-time", "10", url, "-o", str(path)],
                    timeout=12, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
        self.done.emit()


def fmt_duration(secs) -> str:
    try:
        secs = int(float(secs))
    except (TypeError, ValueError):
        return "?"
    if secs <= 0:
        return "?"
    if secs >= 3600:
        return f"{secs//3600}h {(secs%3600)//60}m {secs%60}s"
    if secs >= 60:
        return f"{secs//60}m {secs%60}s"
    return f"{secs}s"


# =============================================================================
# Thumbnail cache (LRU, max 300 entries in memory)
# =============================================================================
class PixmapCache:
    MAX = 300

    def __init__(self):
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()

    def get(self, vid_id: str, w: int, h: int) -> QPixmap | None:
        if vid_id in self._cache:
            self._cache.move_to_end(vid_id)
            return self._cache[vid_id]
        path = THUMBS_DIR / f"{vid_id}.jpg"
        if path.exists():
            raw = QPixmap(str(path))
            # The file may exist but be empty/corrupt/half-written (the worker
            # downloads thumbnails concurrently). Bail out BEFORE scaling, since
            # calling .scaled() on a null pixmap emits a Qt warning.
            if raw.isNull():
                return None
            pix = raw.scaled(
                w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            if pix.isNull():
                return None
            self._cache[vid_id] = pix
            if len(self._cache) > self.MAX:
                self._cache.popitem(last=False)
            return pix
        return None

    def evict(self, vid_id: str):
        self._cache.pop(vid_id, None)


PIXMAP_CACHE = PixmapCache()


# =============================================================================
# Data model
# =============================================================================
class VideoModel(QAbstractListModel):
    """Holds the data. Live rows (twitch_live) are stored separately and are
    ALWAYS shown at the very top; they are refreshed as a block."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._live: list[dict] = []       # live streams (volatile, always on top)
        self._data: list[dict] = []       # cached videos (from outcast.db)
        self._known_ids: set[str] = set() # ids de _data

    def rowCount(self, parent=QModelIndex()):
        return len(self._live) + len(self._data)

    def _row(self, i: int) -> dict | None:
        if i < len(self._live):
            return self._live[i]
        j = i - len(self._live)
        if j < len(self._data):
            return self._data[j]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        v = self._row(index.row())
        if v is None:
            return None
        if role == Qt.UserRole:
            return v
        if role == Qt.DisplayRole:
            return v.get("title", "")
        return None

    @staticmethod
    def parse_line(line: str) -> dict | None:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4:
            return None
        vid_id = parts[0].strip()
        if not vid_id:
            return None
        source = parts[5] if len(parts) > 5 and parts[5] else SRC_YOUTUBE
        url    = parts[6] if len(parts) > 6 and parts[6] else ""
        if not url and source == SRC_YOUTUBE:
            url = f"https://www.youtube.com/watch?v={vid_id}"
        return {
            "vid_id":   vid_id,
            "title":    parts[1],
            "channel":  parts[2],
            "date":     parts[3],
            "duration": parts[4] if len(parts) > 4 else "?",
            "source":   source,
            "url":      url,
        }

    def add_rows(self, rows: list[dict]) -> int:
        """Insert new cached rows right below the live block."""
        fresh = [r for r in rows if r["vid_id"] not in self._known_ids]
        if not fresh:
            return 0
        at = len(self._live)
        self.beginInsertRows(QModelIndex(), at, at + len(fresh) - 1)
        self._data[0:0] = fresh
        for r in fresh:
            self._known_ids.add(r["vid_id"])
        self.endInsertRows()
        return len(fresh)

    def set_live_rows(self, rows: list[dict]):
        """Replace the live block (at the top of the list)."""
        if self._live:
            self.beginRemoveRows(QModelIndex(), 0, len(self._live) - 1)
            old = self._live
            self._live = []
            self.endRemoveRows()
            for r in old:
                PIXMAP_CACHE.evict(r["vid_id"])   # las previews caducan
        if rows:
            self.beginInsertRows(QModelIndex(), 0, len(rows) - 1)
            self._live = rows
            self.endInsertRows()

    def remove_by_vid_id(self, vid_id: str) -> bool:
        for i, v in enumerate(self._data):
            if v["vid_id"] == vid_id:
                at = len(self._live) + i
                self.beginRemoveRows(QModelIndex(), at, at)
                del self._data[i]
                self._known_ids.discard(vid_id)
                self.endRemoveRows()
                return True
        return False

    def clear(self):
        """Clear only the cached rows (keeps the live streams)."""
        if not self._data:
            return
        at = len(self._live)
        self.beginRemoveRows(QModelIndex(), at, at + len(self._data) - 1)
        self._data.clear()
        self._known_ids.clear()
        self.endRemoveRows()

    @property
    def total_count(self):
        return len(self._data)

    @property
    def live_count(self):
        return len(self._live)

    def distinct_channels(self, sources: set[str]) -> list[tuple]:
        """Distinct channels in the cache (only _data) for the given sources,
        as [(name, count), ...] sorted by name."""
        counts: dict[str, int] = {}
        for v in self._data:
            if v.get("source", SRC_YOUTUBE) in sources:
                ch = v.get("channel", "") or "(desconocido)"
                counts[ch] = counts.get(ch, 0) + 1
        return sorted(counts.items(), key=lambda kv: kv[0].lower())

    def remove_by_channel(self, channel: str, sources: set[str]) -> list[str]:
        """Remove a channel/source's cached rows from the model. Returns
        the list of removed vid_id values (so they can also be removed from the TSV)."""
        victims = [v["vid_id"] for v in self._data
                   if v.get("channel", "") == channel
                   and v.get("source", SRC_YOUTUBE) in sources]
        for vid in victims:
            self.remove_by_vid_id(vid)
        return victims


# =============================================================================
# Proxy model for search filtering
# =============================================================================
class VideoFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._needle = ""
        self._allowed = {SRC_YOUTUBE, SRC_TWITCH_VOD, SRC_TWITCH_LIVE}

    def _invalidate(self):
        # invalidateFilter()/invalidateRowsFilter() are marked as deprecated
        # in PySide6; invalidate() is the equivalent public API.
        self.invalidate()

    def set_needle(self, text: str):
        self._needle = text.lower().strip()
        self._invalidate()

    def set_allowed_sources(self, allowed: set[str]):
        self._allowed = set(allowed)
        self._invalidate()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        src = self.sourceModel()
        vid = src.data(src.index(source_row, 0), Qt.UserRole)
        if vid is None:
            return False
        if vid.get("source", SRC_YOUTUBE) not in self._allowed:
            return False
        if not self._needle:
            return True
        return (self._needle in vid["title"].lower()
                or self._needle in vid["channel"].lower())


# =============================================================================
# Delegate — paints each row with QPainter
# =============================================================================
THUMB_W, THUMB_H = 142, 80
ROW_H            = THUMB_H + 16
PAD              = 8


class VideoItemDelegate(QStyledItemDelegate):
    def __init__(self, theme: dict, parent=None):
        super().__init__(parent)
        self.t = theme
        self._title_font = QFont()
        self._title_font.setBold(True)
        self._title_font.setPointSize(10)
        self._meta_font = QFont()
        self._meta_font.setPointSize(9)

    def update_theme(self, theme: dict):
        self.t = theme

    def sizeHint(self, option, index):
        return QSize(0, ROW_H + PAD * 2)

    def paint(self, painter: QPainter, option, index):
        vid = index.data(Qt.UserRole)
        if vid is None:
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        r = option.rect.adjusted(2, PAD // 2, -2, -(PAD // 2))

        is_selected = bool(option.state & option.state.State_Selected)
        is_hover    = bool(option.state & option.state.State_MouseOver)

        bg_col = QColor(
            self.t["bg_surface"] if (is_selected or is_hover) else self.t["bg_overlay"]
        )
        painter.setBrush(QBrush(bg_col))
        painter.setPen(QPen(QColor(self.t["accent"]), 1) if is_selected else Qt.NoPen)
        painter.drawRoundedRect(r, 6, 6)

        thumb_rect = QRect(r.x() + PAD, r.y() + (r.height() - THUMB_H) // 2, THUMB_W, THUMB_H)
        pix = PIXMAP_CACHE.get(vid["vid_id"], THUMB_W, THUMB_H)
        if pix:
            painter.save()
            clip_path = QPainterPath()
            clip_path.addRoundedRect(thumb_rect, 4, 4)
            painter.setClipPath(clip_path)
            src_w, src_h = pix.width(), pix.height()
            dx = (src_w - THUMB_W) // 2
            dy = (src_h - THUMB_H) // 2
            painter.drawPixmap(thumb_rect, pix, QRect(dx, dy, THUMB_W, THUMB_H))
            painter.restore()
        else:
            painter.setBrush(QBrush(QColor(self.t["border"])))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(thumb_rect, 4, 4)
            painter.setPen(QColor(self.t["subtext"]))
            painter.setFont(self._meta_font)
            painter.drawText(thumb_rect, Qt.AlignCenter, "▶")

        text_x = thumb_rect.right() + PAD * 2
        text_w = r.right() - text_x - PAD

        # -- Source / live badge --------------------------------------------
        # Painted in the top-right corner (text side), never over the
        # thumbnail, and without changing the row size. Real brand colors
        # with a subtle vertical gradient.
        source = vid.get("source", SRC_YOUTUBE)
        live_dot = False
        if source == SRC_TWITCH_LIVE:
            # Live: Twitch color + red "on air" dot.
            badge_text = tr("badge.live")
            grad_top, grad_bot, txt_col = "#a970ff", "#772ce8", "#ffffff"
            live_dot = True
        elif source == SRC_TWITCH_VOD:
            # Twitch brand purple (#9146FF).
            badge_text = "Twitch"
            grad_top, grad_bot, txt_col = "#9d5cff", "#7d2df0", "#ffffff"
        elif source == SRC_KICK_LIVE:
            # Kick live: brand green (#53FC18), dark text.
            badge_text = tr("badge.live")
            grad_top, grad_bot, txt_col = "#53fc18", "#1ed760", "#0b3d00"
            live_dot = True
        elif source == SRC_KICK_VOD:
            badge_text = "Kick"
            grad_top, grad_bot, txt_col = "#53fc18", "#13c40a", "#0b3d00"
        else:
            # YouTube brand red (#FF0000 -> #CC0000).
            badge_text = "YouTube"
            grad_top, grad_bot, txt_col = "#ff3333", "#cc0000", "#ffffff"

        badge_font = QFont(self._meta_font)
        badge_font.setBold(True)
        fm_badge = QFontMetrics(badge_font)
        bpad_x, bpad_y = 8, 4
        dot_space = 14 if live_dot else 0
        bw = fm_badge.horizontalAdvance(badge_text) + bpad_x * 2 + dot_space
        bh = fm_badge.height() + bpad_y
        badge_rect = QRect(r.right() - PAD - bw, r.y() + PAD, bw, bh)

        grad = QLinearGradient(badge_rect.topLeft(), badge_rect.bottomLeft())
        grad.setColorAt(0.0, QColor(grad_top))
        grad.setColorAt(1.0, QColor(grad_bot))
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(badge_rect, bh // 2, bh // 2)

        text_rect = badge_rect
        if live_dot:
            # "On air" dot in the text color (contrasts with the background).
            dot_d = 6
            dot_x = badge_rect.left() + bpad_x
            dot_y = badge_rect.center().y() - dot_d // 2
            painter.setBrush(QBrush(QColor(txt_col)))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QRect(dot_x, dot_y, dot_d, dot_d))
            text_rect = badge_rect.adjusted(dot_space, 0, 0, 0)

        painter.setFont(badge_font)
        painter.setPen(QColor(txt_col))
        painter.drawText(text_rect, Qt.AlignCenter, badge_text)

        # The title must not overlap the badge: we clip its width.
        title_w = max(40, badge_rect.left() - text_x - PAD)

        painter.setFont(self._title_font)
        painter.setPen(QColor(self.t["text"]))
        fm_title = QFontMetrics(self._title_font)
        title_rect = QRect(text_x, r.y() + PAD, title_w, fm_title.height() * 2 + 4)
        painter.drawText(title_rect, Qt.TextWordWrap | Qt.AlignTop, vid["title"])

        painter.setFont(self._meta_font)
        painter.setPen(QColor(self.t["subtext"]))
        fm_meta = QFontMetrics(self._meta_font)
        meta_y = title_rect.bottom() + 6
        meta_rect = QRect(text_x, meta_y, text_w, fm_meta.height() + 4)
        meta_text = f"  {vid['channel']}    {vid['date'][:10]}    ⏱ {vid['duration']}"
        painter.drawText(meta_rect, Qt.AlignVCenter | Qt.AlignLeft, meta_text)

        painter.restore()


# =============================================================================
# Settings row widget
# =============================================================================
class SettingsRowWidget(QWidget):
    def __init__(self, label_text: str, control: QWidget, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        lbl = QLabel(label_text)
        lbl.setFont(QFont("sans-serif", 10))
        layout.addWidget(lbl)
        layout.addStretch()
        layout.addWidget(control)


# =============================================================================
# Subscription management tab (YouTube / Twitch)
# =============================================================================
class ChannelsTab(QWidget):
    """Manages the subscriptions of a source: add/remove channels and delete
    from the cache every video of a specific channel."""

    def __init__(self, app, label: str, subs_file: Path,
                 sources: set[str], placeholder: str, parent=None):
        super().__init__(parent)
        self.app = app
        self.subs_file = subs_file
        self.sources = sources

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(8)

        # -- Add -----------------------------------------------------------
        add_row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText(placeholder)
        self.input.returnPressed.connect(self._add)
        add_row.addWidget(self.input, 1)
        add_btn = QPushButton(tr("btn.add"))
        add_btn.clicked.connect(self._add)
        add_row.addWidget(add_btn)
        root.addLayout(add_row)

        # -- Subscriptions list ----------------------------------------------
        subs_lbl = QLabel(tr("channels.subscriptions"))
        subs_lbl.setStyleSheet("font-weight: bold; padding-top: 4px;")
        root.addWidget(subs_lbl)
        self.subs_list = QListWidget()
        self.subs_list.setSelectionMode(QListWidget.NoSelection)
        root.addWidget(self.subs_list, 3)

        # -- Per-channel cache -----------------------------------------------
        cache_lbl = QLabel(tr("channels.cached_by_channel"))
        cache_lbl.setStyleSheet("font-weight: bold; padding-top: 4px;")
        root.addWidget(cache_lbl)
        self.cache_list = QListWidget()
        self.cache_list.setSelectionMode(QListWidget.NoSelection)
        root.addWidget(self.cache_list, 2)

        self.reload()

    # ----------------------------------------------------------------------
    def reload(self):
        self._reload_subs()
        self._reload_cache_channels()

    def _row_widget(self, text: str, buttons: list[tuple]) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(6)
        lbl = QLabel(text)
        lbl.setWordWrap(False)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(lbl, 1)
        for caption, slot in buttons:
            b = QPushButton(caption)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(slot)
            lay.addWidget(b)
        return w

    def _reload_subs(self):
        self.subs_list.clear()
        entries = read_subscriptions(self.subs_file)
        if not entries:
            item = QListWidgetItem(self.subs_list)
            empty = QLabel(tr("channels.no_channels"))
            empty.setStyleSheet("color: gray; padding: 8px;")
            item.setSizeHint(empty.sizeHint())
            self.subs_list.setItemWidget(item, empty)
            return
        for entry in entries:
            item = QListWidgetItem(self.subs_list)
            w = self._row_widget(entry, [
                (tr("btn.remove"), lambda _=False, e=entry: self._remove(e)),
            ])
            item.setSizeHint(QSize(0, 40))
            self.subs_list.setItemWidget(item, w)

    def _reload_cache_channels(self):
        self.cache_list.clear()
        channels = self.app.distinct_cache_channels(self.sources)
        if not channels:
            item = QListWidgetItem(self.cache_list)
            empty = QLabel(tr("channels.no_cached"))
            empty.setStyleSheet("color: gray; padding: 8px;")
            item.setSizeHint(empty.sizeHint())
            self.cache_list.setItemWidget(item, empty)
            return
        for name, n in channels:
            item = QListWidgetItem(self.cache_list)
            w = self._row_widget(tr("channels.video_count", name=name, n=n), [
                (tr("btn.clear_cache"), lambda _=False, c=name: self._clear_cache(c)),
            ])
            item.setSizeHint(QSize(0, 40))
            self.cache_list.setItemWidget(item, w)

    # ----------------------------------------------------------------------
    def _add(self):
        entry = self.input.text().strip()
        if not entry:
            return
        entries = read_subscriptions(self.subs_file)
        if entry in entries:
            self.app.status_bar.setText(tr("status.channel_in_list"))
            self.input.clear()
            return
        entries.append(entry)
        if write_subscriptions(self.subs_file, entries):
            self.input.clear()
            self._reload_subs()
            self.app.status_bar.setText(tr("status.channel_added", name=entry[:50]))

    def _remove(self, entry: str):
        entries = [e for e in read_subscriptions(self.subs_file) if e != entry]
        if write_subscriptions(self.subs_file, entries):
            self._reload_subs()
            self.app.status_bar.setText(tr("status.channel_removed", name=entry[:50]))

    def _clear_cache(self, channel: str):
        removed = self.app.delete_cached_by_channel(channel, self.sources)
        self._reload_cache_channels()
        self.app.status_bar.setText(tr("status.cache_cleared", n=removed, channel=channel[:40]))


# =============================================================================
# Keyboard-aware QListView subclass
# =============================================================================
class VideoListView(QListView):
    search_requested = Signal()
    delete_requested = Signal(dict)
    subscribe_requested = Signal(dict)

    def __init__(self, parent=None, mode: str = "feed"):
        super().__init__(parent)
        self._menu_mode = mode   # "feed" | "search"

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key_Slash:
            self.search_requested.emit()
            return
        if key == Qt.Key_Up:
            cur = self.currentIndex()
            if not cur.isValid() or cur.row() == 0:
                self.search_requested.emit()
                return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        index = self.indexAt(event.pos())
        if not index.isValid():
            return
        vid = index.data(Qt.UserRole)
        if not vid:
            return
        menu = QMenu(self)
        act_play = menu.addAction(tr("menu.play"))
        act_sub = act_delete = None
        if self._menu_mode == "search":
            act_sub = menu.addAction(tr("menu.subscribe"))
        else:
            act_delete = menu.addAction(tr("menu.delete"))
        chosen = menu.exec(event.globalPos())
        if chosen == act_play:
            self.activated.emit(index)
        elif act_delete is not None and chosen == act_delete:
            self.delete_requested.emit(vid)
        elif act_sub is not None and chosen == act_sub:
            self.subscribe_requested.emit(vid)



def read_subscriptions(path: Path) -> list[str]:
    """Read the non-empty, non-comment lines of a subscriptions file."""
    out = []
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    except OSError:
        pass
    return out


def write_subscriptions(path: Path, entries: list[str]) -> bool:
    """Write the list (one per line), atomically."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(entries) + ("\n" if entries else ""), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        return False


# =============================================================================
# Search input with vim-style focus behaviour
# =============================================================================
class SearchLineEdit(QLineEdit):
    escape_pressed = Signal()
    list_focus_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key_Escape:
            self.escape_pressed.emit()
            return
        if key == Qt.Key_Down:
            self.list_focus_requested.emit()
            return
        super().keyPressEvent(event)


# =============================================================================
# Main Application Window
# =============================================================================
class OutcastApp(QMainWindow):
    PAGE_FEED, PAGE_SETTINGS, PAGE_ERRORS = 0, 1, 2

    def __init__(self):
        super().__init__()

        self.settings: dict[str, str] = {
            "PLAYER":     "yt-dlp",
            "PLAY_MODE":  "video",
            "RESOLUTION": "480",
            "SUBTITLES":  "false",
            "THEME":      DEFAULT_THEME,
            "MAX_VIDEOS_PER_CHANNEL": "30",
            "DATE_FROM":  "",
            "DATE_TO":    "",
            "SOURCES":    "youtube,twitch,kick",   # active platforms
            "TWITCH_VODS": "true",     # include saved Twitch VODs
            "KICK_VODS":   "true",     # include saved Kick VODs
            "AUDIO_QUALITY": "best",   # best | 320 | 256 | 192 | 128 | 96 | 64
            "THUMB_QUALITY": "balanced",  # saver | balanced | high
            "LANGUAGE":   "",          # UI language code; empty => auto-detect
        }
        self.settings.update(DEFAULT_COMMANDS)   # default templates
        self._load_settings()                    # the file, if present, wins

        # Resolve the UI language as early as possible (before any tr() use).
        self._init_language()

        self._theme_name = self.settings.get("THEME", DEFAULT_THEME)
        if self._theme_name not in THEMES:
            self._theme_name = DEFAULT_THEME
        self.t = THEMES[self._theme_name]

        # Runtime state
        self._workers: list[ProcessWorker] = []
        self._errors: list[dict] = []
        self._crawl_new_count = 0
        self._has_notify = shutil.which("notify-send") is not None
        self._live_running = False
        self._live_last_count = 0
        self._search_busy = False
        self._thumb_threads: list[QThread] = []

        # DB incremental tracking (rowid-based) + file-mtime guard.
        self._db_max_rowid = 0
        self._db_mtimes: dict[str, float] = {}

        # live status tracking
        self._live_status_seen = ""

        # crawl-errors file tracking
        self._err_ino: int | None = None
        self._err_offset = 0
        self._err_partial = ""

        PLAY_LOG_DIR.mkdir(parents=True, exist_ok=True)

        # Open the metadata database (creates it + schema on first run).
        self._db = outcast_db.connect()

        self.setWindowTitle(tr("title.feed"))
        self.resize(900, 640)
        self.setStyleSheet(build_stylesheet(self.t))

        self._model = VideoModel()
        self._proxy = VideoFilterProxy()
        self._proxy.setSourceModel(self._model)

        self._shortcuts: list[QShortcut] = []
        self._build_ui()
        self._wire_shortcuts()

        # Timers
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(self._apply_search_filter)
        self._post_build_wiring()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(400)

        self._banner_timer = QTimer(self)
        self._banner_timer.setSingleShot(True)
        self._banner_timer.timeout.connect(self._hide_banner)

        self._apply_allowed_sources()
        self._full_reload()
        self._load_live_from_disk()      # live streams from a previous session, if any
        self.video_list.setFocus()
        if self._proxy.rowCount() > 0:
            self.video_list.setCurrentIndex(self._proxy.index(0, 0))

        # Check live streams at startup (after the UI is shown).
        QTimer.singleShot(600, self.check_live)

    # =========================================================================
    # UI construction
    # =========================================================================
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.status_bar = QLabel(tr("status.starting"))
        self.status_bar.setStyleSheet(f"color: {self.t['subtext']}; padding: 2px 4px;")
        self.status_bar.setFixedHeight(20)
        root.addWidget(self.status_bar)

        # Action buttons (FlowBar -> wrap onto several rows when they don't
        # fit and reserve the needed height, so they don't get clipped)
        self.btn_bar_widget = FlowBar(spacing=6)
        btn_bar = self.btn_bar_widget.flow()
        self._action_buttons = []
        self._add_action_btn(btn_bar, tr("btn.settings"),    self.toggle_settings)
        self._add_action_btn(btn_bar, tr("btn.update"), self.force_update)
        self._add_action_btn(btn_bar, tr("btn.live"),   self.check_live)
        self.errors_btn = self._add_action_btn(btn_bar, tr("btn.errors"), self.toggle_errors)
        self._add_action_btn(btn_bar, tr("btn.quit"),      self.close)
        root.addWidget(self.btn_bar_widget)

        self._build_feed_page()
        self._build_settings_page()
        self._build_errors_page()
        self._refresh_error_indicator()

    def _build_feed_page(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(4)

        # Error banner (hidden by default) -- shared across the whole page
        self.banner = QFrame()
        self.banner.setVisible(False)
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(10, 6, 6, 6)
        self.banner_label = QLabel("")
        self.banner_label.setWordWrap(True)
        bl.addWidget(self.banner_label, 1)
        banner_details = QPushButton(tr("btn.details"))
        banner_details.setFixedHeight(24)
        banner_details.clicked.connect(self.toggle_errors)
        bl.addWidget(banner_details)
        banner_close = QPushButton("✕")
        banner_close.setFixedSize(24, 24)
        banner_close.clicked.connect(self._hide_banner)
        bl.addWidget(banner_close)
        self._style_banner()
        page_layout.addWidget(self.banner)

        self.main_tabs = QTabWidget()
        self.main_tabs.addTab(self._build_feed_tab(), tr("tab.feed"))
        self.main_tabs.addTab(self._build_search_tab(), tr("tab.youtube_search"))
        page_layout.addWidget(self.main_tabs, 1)

        self.stack.addWidget(page)

    def _build_feed_tab(self) -> QWidget:
        feed_widget = QWidget()
        feed_layout = QVBoxLayout(feed_widget)
        feed_layout.setContentsMargins(0, 4, 0, 0)
        feed_layout.setSpacing(4)

        self.search_input = SearchLineEdit()
        self.search_input.setPlaceholderText(tr("placeholder.filter"))
        self.search_input.setFocusPolicy(Qt.ClickFocus)
        self.search_input.escape_pressed.connect(self._search_escape)
        self.search_input.list_focus_requested.connect(self._focus_list)
        feed_layout.addWidget(self.search_input)

        self._delegate = VideoItemDelegate(self.t)
        self.video_list = VideoListView()
        self.video_list.setModel(self._proxy)
        self.video_list.setItemDelegate(self._delegate)
        self.video_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.video_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.video_list.setUniformItemSizes(True)
        self.video_list.setSpacing(2)
        self.video_list.setMouseTracking(True)
        self.video_list.activated.connect(self._on_video_activated)
        self.video_list.search_requested.connect(self._focus_search)
        self.video_list.delete_requested.connect(self._delete_video)

        del_shortcut = QShortcut(QKeySequence(Qt.Key_Delete), self.video_list)
        del_shortcut.activated.connect(self._delete_selected)

        feed_layout.addWidget(self.video_list)
        return feed_widget

    def _build_search_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)

        row = QHBoxLayout()
        self.yt_query = QLineEdit()
        self.yt_query.setPlaceholderText(tr("placeholder.youtube"))
        self.yt_query.returnPressed.connect(self.run_youtube_search)
        row.addWidget(self.yt_query, 1)
        self.yt_search_btn = QPushButton(tr("btn.search"))
        self.yt_search_btn.clicked.connect(self.run_youtube_search)
        row.addWidget(self.yt_search_btn)
        layout.addLayout(row)

        # Independent model/proxy/list for search results.
        self._search_model = VideoModel()
        self._search_proxy = VideoFilterProxy()
        self._search_proxy.setSourceModel(self._search_model)

        self._search_delegate = VideoItemDelegate(self.t)
        self.search_results = VideoListView(mode="search")
        self.search_results.setModel(self._search_proxy)
        self.search_results.setItemDelegate(self._search_delegate)
        self.search_results.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.search_results.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.search_results.setUniformItemSizes(True)
        self.search_results.setSpacing(2)
        self.search_results.setMouseTracking(True)
        self.search_results.activated.connect(self._on_search_activated)
        self.search_results.subscribe_requested.connect(self._subscribe_to_channel)
        layout.addWidget(self.search_results)
        return w

    def _build_settings_page(self):
        settings_widget = QWidget()
        settings_layout = QVBoxLayout(settings_widget)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(6)

        self._settings_title_lbl = QLabel(tr("settings.title"))
        self._settings_title_lbl.setStyleSheet(
            f"font-size: 15px; font-weight: bold; color: {self.t['accent']}; padding: 6px 0;"
        )
        settings_layout.addWidget(self._settings_title_lbl)

        self.settings_tabs = QTabWidget()
        self.settings_tabs.addTab(self._build_general_tab(), tr("tab.general"))
        self.settings_tabs.addTab(self._build_commands_tab(), tr("tab.commands"))
        self.yt_channels_tab = ChannelsTab(
            self, "YouTube", SUBS_FILE, {SRC_YOUTUBE},
            tr("placeholder.youtube_channel"))
        self.tw_channels_tab = ChannelsTab(
            self, "Twitch", TWITCH_SUBS_FILE, {SRC_TWITCH_VOD, SRC_TWITCH_LIVE},
            tr("placeholder.twitch_channel"))
        self.kick_channels_tab = ChannelsTab(
            self, "Kick", KICK_SUBS_FILE, {SRC_KICK_VOD, SRC_KICK_LIVE},
            tr("placeholder.kick_channel"))
        self.settings_tabs.addTab(self.yt_channels_tab, tr("tab.channels_youtube"))
        self.settings_tabs.addTab(self.tw_channels_tab, tr("tab.channels_twitch"))
        self.settings_tabs.addTab(self.kick_channels_tab, tr("tab.channels_kick"))
        settings_layout.addWidget(self.settings_tabs, 1)

        btn_save = QPushButton(tr("btn.save_return"))
        btn_save.clicked.connect(self.save_settings)
        settings_layout.addWidget(btn_save)

        self.stack.addWidget(settings_widget)

    def _build_general_tab(self) -> QWidget:
        self.settings_list = QListWidget()
        self.settings_list.setSelectionMode(QListWidget.NoSelection)
        self.settings_list.setFocusPolicy(Qt.StrongFocus)

        self.ctrl_player = QComboBox()
        self.ctrl_player.addItems(["yt-dlp", "streamlink"])
        self.ctrl_player.setCurrentText(self.settings.get("PLAYER", "yt-dlp"))

        self.ctrl_mode = QComboBox()
        self.ctrl_mode.addItems(["video", "audio"])
        self.ctrl_mode.setCurrentText(self.settings.get("PLAY_MODE", "video"))

        self.ctrl_res = QComboBox()
        self.ctrl_res.addItems(["144", "240", "360", "480", "720", "1080", "1440", "2160"])
        self.ctrl_res.setCurrentText(self.settings.get("RESOLUTION", "480"))

        self.ctrl_audio_q = QComboBox()
        self._audio_q_map = [
            (tr("audio.best"), "best"),
            ("~320 kbps", "320"), ("~256 kbps", "256"), ("~192 kbps", "192"),
            ("~128 kbps", "128"), ("~96 kbps", "96"), ("~64 kbps", "64"),
        ]
        self.ctrl_audio_q.addItems([lbl for lbl, _ in self._audio_q_map])
        cur_aq = self.settings.get("AUDIO_QUALITY", "best")
        self.ctrl_audio_q.setCurrentText(
            next((lbl for lbl, v in self._audio_q_map if v == cur_aq), tr("audio.best")))

        self.ctrl_subs = QCheckBox(tr("check.enable"))
        self.ctrl_subs.setChecked(self.settings.get("SUBTITLES") == "true")

        self.ctrl_thumb_q = QComboBox()
        self._thumb_q_map = [
            (tr("thumb.save"), "saver"),
            (tr("thumb.balanced"),   "balanced"),
            (tr("thumb.high"),          "high"),
        ]
        self.ctrl_thumb_q.addItems([lbl for lbl, _ in self._thumb_q_map])
        cur_tq = _normalize_thumb_quality(self.settings.get("THUMB_QUALITY", "balanced"))
        self.ctrl_thumb_q.setCurrentText(
            next((lbl for lbl, v in self._thumb_q_map if v == cur_tq), tr("thumb.balanced")))

        self.ctrl_theme = QComboBox()
        self.ctrl_theme.addItems(list(THEMES.keys()))
        self.ctrl_theme.setCurrentText(self._theme_name)
        self.ctrl_theme.currentTextChanged.connect(self._preview_theme)

        self.ctrl_lang = QComboBox()
        cur_lang = self.settings.get("LANGUAGE", i18n.get_language())
        for code, name in i18n.available_languages():
            self.ctrl_lang.addItem(name, code)
        idx = self.ctrl_lang.findData(cur_lang)
        if idx >= 0:
            self.ctrl_lang.setCurrentIndex(idx)

        enabled = self._enabled_sources()
        self.ctrl_src_youtube = QCheckBox("YouTube")
        self.ctrl_src_youtube.setChecked("youtube" in enabled)
        self.ctrl_src_twitch = QCheckBox("Twitch")
        self.ctrl_src_twitch.setChecked("twitch" in enabled)
        self.ctrl_src_kick = QCheckBox("Kick")
        self.ctrl_src_kick.setChecked("kick" in enabled)
        sources_box = QWidget()
        sbl = QHBoxLayout(sources_box)
        sbl.setContentsMargins(0, 0, 0, 0); sbl.setSpacing(14)
        sbl.addWidget(self.ctrl_src_youtube); sbl.addWidget(self.ctrl_src_twitch)
        sbl.addWidget(self.ctrl_src_kick)

        self.ctrl_twitch_vods = QCheckBox("Twitch")
        self.ctrl_twitch_vods.setChecked(self.settings.get("TWITCH_VODS", "true") == "true")
        self.ctrl_kick_vods = QCheckBox("Kick")
        self.ctrl_kick_vods.setChecked(self.settings.get("KICK_VODS", "true") == "true")
        vods_box = QWidget()
        vbl = QHBoxLayout(vods_box)
        vbl.setContentsMargins(0, 0, 0, 0); vbl.setSpacing(14)
        vbl.addWidget(self.ctrl_twitch_vods); vbl.addWidget(self.ctrl_kick_vods)

        self._add_setting_row(tr("setting.engine"),      self.ctrl_player)
        self._add_setting_row(tr("setting.play_mode"),   self.ctrl_mode)
        self._add_setting_row(tr("setting.max_res"),  self.ctrl_res)
        self._add_setting_row(tr("setting.audio_quality"),       self.ctrl_audio_q)
        self._add_setting_row(tr("setting.thumb_quality"),  self.ctrl_thumb_q)
        self._add_setting_row(tr("setting.subtitles"), self.ctrl_subs)
        self._add_setting_row(tr("setting.sources"),   sources_box)
        self._add_setting_row(tr("setting.include_vods"), vods_box)
        self._add_setting_row(tr("setting.theme"),        self.ctrl_theme)
        self._add_setting_row(tr("setting.language"),     self.ctrl_lang)
        return self.settings_list

    def _build_commands_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)

        legend = QLabel(tr("commands.legend"))
        legend.setWordWrap(True)
        legend.setStyleSheet(f"color: {self.t['subtext']}; padding: 2px;")
        layout.addWidget(legend)

        self._cmd_edits: dict[str, tuple[QLineEdit, QLineEdit]] = {}
        for key in CMD_KEYS:
            layout.addWidget(self._make_cmd_group(key))

        reset_btn = QPushButton(tr("btn.reset_commands"))
        reset_btn.clicked.connect(self.reset_commands)
        layout.addWidget(reset_btn)
        layout.addStretch(1)

        scroll.setWidget(container)
        return scroll

    def _make_cmd_group(self, key: str) -> QWidget:
        box = QFrame()
        box.setStyleSheet(
            f"QFrame {{ background-color: {self.t['bg_overlay']}; "
            f"border: 1px solid {self.t['border']}; border-radius: 6px; }}"
        )
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(5)

        header = QLabel(tr(CMD_HUMAN[key]))
        header.setStyleSheet(
            f"color: {self.t['accent']}; font-weight: bold; border: none;"
        )
        v.addWidget(header)

        bin_edit = QLineEdit(self.settings.get(f"BIN_{key}", DEFAULT_COMMANDS[f"BIN_{key}"]))
        bin_row = QHBoxLayout()
        bl = QLabel(tr("label.binary"))
        bl.setFixedWidth(64)
        bl.setStyleSheet("border: none;")
        bin_row.addWidget(bl)
        bin_row.addWidget(bin_edit, 1)
        v.addLayout(bin_row)

        args_edit = QLineEdit(self.settings.get(f"ARGS_{key}", DEFAULT_COMMANDS[f"ARGS_{key}"]))
        args_row = QHBoxLayout()
        al = QLabel(tr("label.args"))
        al.setFixedWidth(64)
        al.setStyleSheet("border: none;")
        args_row.addWidget(al)
        args_row.addWidget(args_edit, 1)
        v.addLayout(args_row)

        self._cmd_edits[key] = (bin_edit, args_edit)
        return box

    def reset_commands(self):
        for key, (bin_edit, args_edit) in self._cmd_edits.items():
            bin_edit.setText(DEFAULT_COMMANDS[f"BIN_{key}"])
            args_edit.setText(DEFAULT_COMMANDS[f"ARGS_{key}"])
        self.status_bar.setText(tr("status.commands_reset"))

    def _build_errors_page(self):
        errors_widget = QWidget()
        layout = QVBoxLayout(errors_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._errors_title_lbl = QLabel(tr("errors.title"))
        self._errors_title_lbl.setStyleSheet(
            f"font-size: 15px; font-weight: bold; color: {self.t['red']}; padding: 6px 0;"
        )
        layout.addWidget(self._errors_title_lbl)

        self.error_list = QListWidget()
        self.error_list.currentRowChanged.connect(self._on_error_selected)
        layout.addWidget(self.error_list, 1)

        self.error_detail = QPlainTextEdit()
        self.error_detail.setReadOnly(True)
        self.error_detail.setFixedHeight(180)
        self.error_detail.setPlaceholderText(tr("errors.select_hint"))
        layout.addWidget(self.error_detail)

        row = QHBoxLayout()
        btn_open = QPushButton(tr("btn.open_log"))
        btn_open.clicked.connect(self._open_selected_log)
        btn_clear = QPushButton(tr("btn.clear"))
        btn_clear.clicked.connect(self._clear_errors)
        btn_back = QPushButton(tr("btn.back"))
        btn_back.clicked.connect(lambda: self.stack.setCurrentIndex(self.PAGE_FEED))
        row.addWidget(btn_open)
        row.addWidget(btn_clear)
        row.addStretch()
        row.addWidget(btn_back)
        layout.addLayout(row)

        self.stack.addWidget(errors_widget)

    def _wire_shortcuts(self):
        # Drop any shortcuts from a previous build (language switch rebuilds the
        # central widget; QShortcuts are parented to the window, not the widget,
        # so they must be cleared explicitly to avoid duplicates).
        for sc in getattr(self, "_shortcuts", []):
            sc.setParent(None)
            sc.deleteLater()
        self._shortcuts = []

        bindings = [
            ("Ctrl+,", self.toggle_settings),
            ("Ctrl+S", self.toggle_settings),
            ("Ctrl+U", self.force_update),
            ("Ctrl+L", self.check_live),
            ("Ctrl+F", self._focus_youtube_search),
            ("Ctrl+E", self.toggle_errors),
            ("Ctrl+Q", self.close),
            ("Ctrl+C", self.close),
            ("Escape", self._global_escape),
            ("/",      self._vim_slash),
        ]
        for seq, slot in bindings:
            self._shortcuts.append(QShortcut(QKeySequence(seq), self, slot))

    # =========================================================================
    # Internationalization (i18n)
    # =========================================================================
    def _init_language(self):
        """Resolve and apply the active UI language during construction.

        Precedence: explicit LANGUAGE setting -> $LANG/$LC_ALL locale -> base
        language ('en'). The resolved code is written back to self.settings so
        the Settings combo shows the real value.
        """
        code = (self.settings.get("LANGUAGE") or "").strip()
        if not code or not i18n.has_language(code):
            code = self._detect_system_language()
        if not i18n.has_language(code):
            code = i18n.BASE_LANGUAGE
        i18n.set_language(code)
        self.settings["LANGUAGE"] = code

    @staticmethod
    def _detect_system_language() -> str:
        """Best-effort language code from the environment locale."""
        for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
            val = os.environ.get(var, "")
            if val:
                code = val.split(".")[0].split("_")[0].strip().lower()
                if code and i18n.has_language(code):
                    return code
        return i18n.BASE_LANGUAGE

    def _post_build_wiring(self):
        """Connections that must be re-established after a UI (re)build."""
        self.search_input.textChanged.connect(self._search_timer.start)

    def change_language(self, code: str):
        """Switch the UI language at runtime and rebuild the interface.

        Falls back gracefully to an 'applies after restart' message if the
        rebuild raises for any reason (the language is still persisted).
        """
        if not i18n.has_language(code):
            return
        i18n.set_language(code)
        self.settings["LANGUAGE"] = code
        try:
            self._rebuild_ui()
        except Exception:
            self.status_bar.setText(tr("status.lang_restart"))

    def _rebuild_ui(self):
        """Tear down and rebuild the central widget in the active language.

        Persistent state (model/proxy, error history, current page) is kept;
        only the view layer is recreated so every translated string refreshes.
        """
        current_page = self.stack.currentIndex() if hasattr(self, "stack") else self.PAGE_FEED

        old = self.centralWidget()
        self._build_ui()
        self._wire_shortcuts()
        self._post_build_wiring()
        if old is not None:
            old.setParent(None)
            old.deleteLater()

        self.setStyleSheet(build_stylesheet(self.t))
        self._apply_allowed_sources()
        self._rebuild_error_list()
        self._update_status_idle()

        if current_page == self.PAGE_SETTINGS:
            self.stack.setCurrentIndex(self.PAGE_FEED)
            current_page = self.PAGE_FEED
        else:
            self.stack.setCurrentIndex(current_page)
        self._set_window_title_for_page(current_page)
        self.video_list.setFocus()

    def _set_window_title_for_page(self, idx: int):
        if idx == self.PAGE_SETTINGS:
            self.setWindowTitle(tr("title.settings"))
        elif idx == self.PAGE_ERRORS:
            self.setWindowTitle(tr("title.errors"))
        else:
            self.setWindowTitle(tr("title.feed"))

    def _rebuild_error_list(self):
        """Repopulate the error panel widget from the kept error history."""
        self.error_list.clear()
        for e in self._errors:
            icon = "▶" if e["kind"] == "play" else "🔄"
            item = QListWidgetItem(
                f"{e['time']}  {icon}  {e['label'][:48]} — {e['reason']}")
            item.setForeground(
                QColor(self.t["red"] if e["critical"] else self.t["yellow"]))
            self.error_list.addItem(item)
        self._refresh_error_indicator()

    # =========================================================================
    # UI helpers
    # =========================================================================
    def _add_action_btn(self, layout: QHBoxLayout, text: str, callback) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.TabFocus)
        self._style_action_btn(btn)
        btn.clicked.connect(callback)
        layout.addWidget(btn)
        self._action_buttons.append(btn)
        return btn

    def _style_action_btn(self, btn: QPushButton):
        t = self.t
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {t['bg_surface']};
                color: {t['subtext']};
                border: 1px solid {t['border']};
                padding: 4px 12px;
                border-radius: 4px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {t['bg_overlay']};
                color: {t['text']};
                border: 1px solid {t['border']};
            }}
            QPushButton:focus {{
                background-color: {t['bg_overlay']};
                color: {t['text']};
                border: 1px solid {t['accent']};
                outline: none;
            }}
            QPushButton:pressed {{
                background-color: {t['accent']};
                color: {t['bg']};
                border: 1px solid {t['accent']};
            }}
        """)

    def _style_banner(self):
        t = self.t
        self.banner.setStyleSheet(
            f"QFrame {{ background-color: {t['bg_surface']}; "
            f"border: 1px solid {t['red']}; border-radius: 6px; }}"
        )
        self.banner_label.setStyleSheet(f"color: {t['red']}; border: none;")

    def _add_setting_row(self, label: str, control: QWidget):
        item = QListWidgetItem(self.settings_list)
        w = SettingsRowWidget(label, control)
        item.setSizeHint(QSize(0, 52))
        self.settings_list.setItemWidget(item, w)

    def _notify(self, summary: str, body: str = "", icon: str | None = None):
        if not self._has_notify:
            return
        args = ["notify-send", "-a", tr("notify.app_name")]
        if icon:
            args += ["-i", icon]
        args += [summary, body]
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    # =========================================================================
    # Theme
    # =========================================================================
    def _preview_theme(self, name: str):
        if name not in THEMES:
            return
        self.t = THEMES[name]
        self._theme_name = name
        self.setStyleSheet(build_stylesheet(self.t))
        self._delegate.update_theme(self.t)
        self.video_list.viewport().update()
        self.status_bar.setStyleSheet(f"color: {self.t['subtext']}; padding: 2px 4px;")
        self._settings_title_lbl.setStyleSheet(
            f"font-size: 15px; font-weight: bold; color: {self.t['accent']}; padding: 6px 0;"
        )
        self._errors_title_lbl.setStyleSheet(
            f"font-size: 15px; font-weight: bold; color: {self.t['red']}; padding: 6px 0;"
        )
        self._style_banner()
        for btn in self._action_buttons:
            self._style_action_btn(btn)
        self._refresh_error_indicator()

    def _apply_theme(self, name: str):
        self._preview_theme(name)

    # =========================================================================
    # Search
    # =========================================================================
    def _apply_search_filter(self):
        self._proxy.set_needle(self.search_input.text())

    def _focus_search(self):
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _focus_list(self):
        self.video_list.setFocus()
        if self._proxy.rowCount() > 0:
            self.video_list.setCurrentIndex(self._proxy.index(0, 0))

    def _search_escape(self):
        if self.search_input.text():
            self.search_input.clear()
        else:
            self.video_list.setFocus()

    def _vim_slash(self):
        if self.stack.currentIndex() != self.PAGE_FEED:
            return
        if self.main_tabs.currentIndex() == 1:
            self.yt_query.setFocus()
            self.yt_query.selectAll()
        else:
            self._focus_search()

    def _focus_youtube_search(self):
        if self.stack.currentIndex() != self.PAGE_FEED:
            self.stack.setCurrentIndex(self.PAGE_FEED)
        self.main_tabs.setCurrentIndex(1)
        self.yt_query.setFocus()
        self.yt_query.selectAll()

    # =========================================================================
    # Navigation
    # =========================================================================
    def _global_escape(self):
        idx = self.stack.currentIndex()
        if idx in (self.PAGE_SETTINGS, self.PAGE_ERRORS):
            self.stack.setCurrentIndex(self.PAGE_FEED)
            self.setWindowTitle(tr("title.feed"))
            self.video_list.setFocus()
        elif self.search_input.hasFocus():
            self._search_escape()

    def toggle_settings(self):
        if self.stack.currentIndex() != self.PAGE_SETTINGS:
            # Refresh the cached channel list in case it changed after a crawl.
            if hasattr(self, "yt_channels_tab"):
                self.yt_channels_tab.reload()
                self.tw_channels_tab.reload()
                self.kick_channels_tab.reload()
            self.stack.setCurrentIndex(self.PAGE_SETTINGS)
            self.setWindowTitle(tr("title.settings"))
            self.ctrl_player.setFocus()
        else:
            self.stack.setCurrentIndex(self.PAGE_FEED)
            self.setWindowTitle(tr("title.feed"))
            self.video_list.setFocus()

    def toggle_errors(self):
        if self.stack.currentIndex() != self.PAGE_ERRORS:
            self.stack.setCurrentIndex(self.PAGE_ERRORS)
            self.setWindowTitle(tr("title.errors"))
            self._hide_banner()
            self.error_list.setFocus()
            if self.error_list.count() and self.error_list.currentRow() < 0:
                self.error_list.setCurrentRow(0)
        else:
            self.stack.setCurrentIndex(self.PAGE_FEED)
            self.setWindowTitle(tr("title.feed"))
            self.video_list.setFocus()

    # =========================================================================
    # Video actions
    # =========================================================================
    def _on_video_activated(self, index: QModelIndex):
        vid = index.data(Qt.UserRole)
        if vid:
            self.play_video(vid)

    def _delete_thumb(self, vid_id: str):
        """Delete a video's cached thumbnail (if it exists)."""
        if not vid_id:
            return
        try:
            (THUMBS_DIR / f"{vid_id}.jpg").unlink(missing_ok=True)
        except OSError:
            pass

    def _delete_video(self, vid: dict):
        self._model.remove_by_vid_id(vid["vid_id"])
        try:
            outcast_db.delete_ids(self._db, [vid["vid_id"]])
        except Exception:
            pass
        self._delete_thumb(vid["vid_id"])
        self.status_bar.setText(tr("status.deleted", title=vid['title'][:60]))

    # -- Per-channel cache management (used by the channel tabs) ----------
    def distinct_cache_channels(self, sources: set[str]) -> list[tuple]:
        return self._model.distinct_channels(sources)

    def delete_cached_by_channel(self, channel: str, sources: set[str]) -> int:
        victims = self._model.remove_by_channel(channel, sources)
        if not victims:
            return 0
        # Remove those rows from the database.
        try:
            outcast_db.delete_ids(self._db, victims)
        except Exception:
            pass
        # Delete the thumbnails of the removed videos.
        for vid_id in victims:
            self._delete_thumb(vid_id)
        return len(victims)

    def _delete_selected(self):
        index = self.video_list.currentIndex()
        if not index.isValid():
            return
        vid = index.data(Qt.UserRole)
        if vid:
            self._delete_video(vid)

    # =========================================================================
    # Data loading (incremental via rowid, from the SQLite store)
    # =========================================================================
    def _full_reload(self):
        self._model.clear()
        try:
            rows = outcast_db.load_all(self._db)
        except Exception:
            rows = []
        self._model.add_rows(rows)
        self._reset_db_tracking()
        self._update_status_idle()

    def _reset_db_tracking(self):
        try:
            self._db_max_rowid = outcast_db.max_rowid(self._db)
        except Exception:
            self._db_max_rowid = 0
        self._refresh_db_mtimes()

    def _refresh_db_mtimes(self) -> bool:
        """Update cached mtimes of the DB (and its WAL); return True on change."""
        changed = False
        for p in (DB_FILE, DB_WAL):
            try:
                m = p.stat().st_mtime
            except OSError:
                m = 0.0
            if self._db_mtimes.get(str(p)) != m:
                self._db_mtimes[str(p)] = m
                changed = True
        return changed

    def _read_db_increment(self) -> int:
        """Pick up rows the worker appended since the last poll (rowid-based).

        A cheap file-mtime guard avoids querying SQLite when nothing changed."""
        if not self._refresh_db_mtimes():
            return 0
        try:
            rows, new_max = outcast_db.load_since(self._db, self._db_max_rowid)
        except Exception:
            return 0
        if new_max > self._db_max_rowid:
            self._db_max_rowid = new_max
        if not rows:
            return 0
        return self._model.add_rows(rows)

    def _update_status_idle(self):
        n = self._model.total_count
        live = self._model.live_count
        chunks = []
        if live:
            chunks.append(tr("status.live_n", n=live))
        chunks.append(tr("status.cached_n", n=n))
        lu = self._format_last_update()
        if lu:
            chunks.append(tr("status.updated_ago", when=lu))
        self.status_bar.setText("   ·   ".join(chunks))

    def _format_last_update(self) -> str:
        try:
            ts = int((CACHE_DIR / ".last_update").read_text().strip())
        except (OSError, ValueError):
            return ""
        delta = int(time.time()) - ts
        if delta < 0:
            return ""
        if delta < 90:
            return tr("time.moments_ago")
        if delta < 3600:
            return tr("time.min_ago", n=delta // 60)
        if delta < 86400:
            return tr("time.hours_ago", n=delta // 3600)
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    # =========================================================================
    # Fuentes
    # =========================================================================
    def _enabled_sources(self) -> set[str]:
        """Plataformas activas, subconjunto de {youtube, twitch, kick}."""
        raw = self.settings.get("SOURCES", "youtube,twitch,kick")
        if raw == "both":
            return {"youtube", "twitch"}
        if raw in ("youtube", "twitch", "kick"):
            return {raw}
        valid = {"youtube", "twitch", "kick"}
        return {s.strip() for s in raw.split(",") if s.strip() in valid}

    def _apply_allowed_sources(self):
        plat = self._enabled_sources()
        allowed: set[str] = set()
        if "youtube" in plat:
            allowed.add(SRC_YOUTUBE)
        if "twitch" in plat:
            allowed |= {SRC_TWITCH_VOD, SRC_TWITCH_LIVE}
        if "kick" in plat:
            allowed |= {SRC_KICK_VOD, SRC_KICK_LIVE}
        self._proxy.set_allowed_sources(allowed)

    # =========================================================================
    # Twitch — live streams
    # =========================================================================
    def check_live(self):
        plat = self._enabled_sources()
        if not ({"twitch", "kick"} & plat):
            self.status_bar.setText(tr("status.live_disabled"))
            return
        if self._live_running:
            return
        worker = LIVE_WORKER_SCRIPT
        if not worker.exists():
            worker = Path("/usr/lib/outcast/outcast-live.sh")
        if not worker.exists():
            self._report_error("crawl", tr("label.live"), tr("err.live_script_missing"),
                               "", critical=True)
            return
        if "twitch" in plat and shutil.which("streamlink") is None:
            self._report_error("crawl", tr("label.live"),
                               tr("err.streamlink_missing_twitch"),
                               "", critical=True)
            return
        if "kick" in plat and shutil.which("yt-dlp") is None:
            self._report_error("crawl", tr("label.live"),
                               tr("err.ytdlp_missing_kick"),
                               "", critical=True)
            return
        try:
            subprocess.Popen(
                [str(worker), "--check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            self._report_error("crawl", tr("label.live"),
                               tr("err.live_launch_failed", e=e), "", critical=True)
            return
        self._live_running = True
        self.status_bar.setText(tr("status.checking_live"))

    def _load_live_from_disk(self):
        rows = []
        if LIVE_TSV.exists():
            try:
                with open(LIVE_TSV, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        r = VideoModel.parse_line(line)
                        if r is not None:
                            if r.get("source") not in (SRC_TWITCH_LIVE, SRC_KICK_LIVE):
                                r["source"] = SRC_TWITCH_LIVE
                            rows.append(r)
            except OSError:
                pass
        self._model.set_live_rows(rows)
        self._live_last_count = len(rows)

    def _poll_live(self):
        if not LIVE_STATUS_FILE.exists():
            return
        try:
            status = LIVE_STATUS_FILE.read_text().strip()
        except OSError:
            return
        if status.startswith("running"):
            self._live_running = True
            self._live_status_seen = status
            return
        if status == self._live_status_seen:
            return
        self._live_status_seen = status
        if status.startswith("done"):
            self._load_live_from_disk()
            self._live_running = False
            n = self._model.live_count
            if n:
                self.status_bar.setText(tr("status.live_count", n=n))
            else:
                self._update_status_idle()
            try:
                LIVE_STATUS_FILE.write_text("idle")
            except OSError:
                pass

    # =========================================================================
    # YouTube search (tab)
    # =========================================================================
    def run_youtube_search(self):
        query = self.yt_query.text().strip()
        if not query or self._search_busy:
            return
        binary = ("yt-dlp" if shutil.which("yt-dlp")
                  else "youtube-dl" if shutil.which("youtube-dl") else None)
        if binary is None:
            self._report_error("play", tr("label.youtube_search"),
                               tr("err.ytdlp_missing_search"), "", critical=True)
            return
        args = [binary, f"ytsearch20:{query}", "--flat-playlist", "-j",
                "--no-warnings", "--ignore-errors"]
        ts = datetime.now().strftime("%H%M%S")
        out = PLAY_LOG_DIR / f"search-{ts}.json"
        self._search_busy = True
        self.yt_search_btn.setEnabled(False)
        self.status_bar.setText(tr("status.searching", q=query[:40]))
        worker = ProcessWorker(kind="search", args=args, label=query,
                               log_path=out, parent=self)
        worker.result.connect(self._on_proc_finished)
        worker.finished.connect(worker.deleteLater)
        self._workers.append(worker)
        worker.start()

    def _parse_search_output(self, path: str) -> list[dict]:
        rows = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return rows
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            vid = obj.get("id") or ""
            if not vid:
                continue
            rows.append({
                "vid_id":   vid,
                "title":    obj.get("title") or vid,
                "channel":  obj.get("channel") or obj.get("uploader")
                            or obj.get("uploader_id") or "",
                "channel_url": obj.get("channel_url") or obj.get("uploader_url") or "",
                "channel_id":  obj.get("channel_id") or "",
                "date":     obj.get("upload_date") or "",
                "duration": fmt_duration(obj.get("duration")),
                "source":   SRC_YOUTUBE,
                "url":      obj.get("url") or obj.get("webpage_url")
                            or f"https://www.youtube.com/watch?v={vid}",
            })
        return rows

    def _fetch_thumbs_async(self, ids: list[str], view):
        t = ThumbFetcher(ids, parent=self)
        t.done.connect(lambda: view.viewport().update())
        t.finished.connect(lambda: (self._thumb_threads.remove(t)
                                    if t in self._thumb_threads else None))
        t.finished.connect(t.deleteLater)
        self._thumb_threads.append(t)
        t.start()

    def _on_search_activated(self, index: QModelIndex):
        vid = index.data(Qt.UserRole)
        if vid:
            self.play_video(vid)

    def _subscribe_to_channel(self, vid: dict):
        """Add the channel from the search result to the YouTube
        subscriptions. Prefers the channel URL; otherwise derives it from the
        channel_id; as a last resort uses the name."""
        entry = (vid.get("channel_url") or "").strip()
        if not entry:
            cid = (vid.get("channel_id") or "").strip()
            if cid:
                entry = f"https://www.youtube.com/channel/{cid}"
        if not entry:
            name = (vid.get("channel") or "").strip()
            if name:
                # No reliable URL or id: use the handle as a channel search.
                entry = name if name.startswith("@") else f"https://www.youtube.com/@{name}"
        if not entry:
            self.status_bar.setText(tr("status.channel_undetermined"))
            return

        entries = read_subscriptions(SUBS_FILE)
        if entry in entries:
            self.status_bar.setText(tr("status.already_subscribed", name=entry[:50]))
            return
        entries.append(entry)
        if write_subscriptions(SUBS_FILE, entries):
            ch_name = vid.get("channel") or entry
            self.status_bar.setText(tr("status.channel_subscribed", name=ch_name[:50]))
            self._notify(tr("notify.channel_added_yt"), ch_name[:80])
            if hasattr(self, "yt_channels_tab"):
                self.yt_channels_tab.reload()
        else:
            self.status_bar.setText(tr("status.subs_write_failed"))

    # =========================================================================
    # Polling (status + live TSV + crawl errors)
    # =========================================================================
    def _poll(self):
        # 1. New rows the worker appended to the DB
        added = self._read_db_increment()
        if added:
            self._crawl_new_count += added

        # 2. Crawl errors written by the worker
        self._drain_crawl_errors()

        # 3. Live worker status (independent of the crawler)
        self._poll_live()

        # 4. Estado del crawler
        if not STATUS_FILE.exists():
            return
        try:
            status = STATUS_FILE.read_text().strip()
        except OSError:
            return

        if status.startswith("running"):
            parts = status.split(":")
            prog = f"[{parts[1]}/{parts[2]}]" if len(parts) == 3 else ""
            if self._crawl_new_count:
                self.status_bar.setText(tr("status.updating_new", prog=prog, n=self._crawl_new_count))
            else:
                self.status_bar.setText(tr("status.updating", prog=prog))
        elif status.startswith("error"):
            msg = status.split(":", 1)[1] if ":" in status else tr("err.crawler_failure")
            self._report_error("crawl", tr("label.crawler"), _short(msg), "", critical=True)
            self._crawl_new_count = 0
            self._update_status_idle()
            self._write_status_idle()
        elif status.startswith("done"):
            parts = status.split(":")
            errs = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
            self._full_reload()   # aplica dedup/orden/recorte del worker
            self._crawl_new_count = 0
            if errs:
                self.status_bar.setText(
                    tr("status.update_done_errs", n=errs)
                )
            self._write_status_idle()
        elif status == "idle":
            self._crawl_new_count = 0
            self._update_status_idle()

    def _write_status_idle(self):
        try:
            STATUS_FILE.write_text("idle")
        except OSError:
            pass

    def _drain_crawl_errors(self):
        """Read new lines from .crawl_errors (format: url<TAB>reason)."""
        if not CRAWL_ERR_FILE.exists():
            return
        try:
            st = CRAWL_ERR_FILE.stat()
        except OSError:
            return
        if self._err_ino is None or st.st_ino != self._err_ino or st.st_size < self._err_offset:
            self._err_ino = st.st_ino
            self._err_offset = 0
            self._err_partial = ""
        if st.st_size == self._err_offset:
            return
        try:
            with open(CRAWL_ERR_FILE, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._err_offset)
                chunk = f.read()
                self._err_offset = f.tell()
        except OSError:
            return

        data = self._err_partial + chunk
        pieces = data.split("\n")
        self._err_partial = pieces.pop()
        for line in pieces:
            if not line.strip():
                continue
            url, _, reason = line.partition("\t")
            self._report_error("crawl", url or tr("label.channel"), reason or tr("err.failed"), "",
                               silent_banner=True)

    # =========================================================================
    # Error reporting / panel
    # =========================================================================
    def _report_error(self, kind: str, label: str, reason: str, detail: str,
                       critical: bool = False, log_path: str = "",
                       silent_banner: bool = False):
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "kind": kind, "label": label, "reason": reason,
            "detail": detail, "log_path": log_path, "critical": critical,
        }
        self._errors.append(entry)
        if len(self._errors) > MAX_ERRORS_KEPT:
            self._errors = self._errors[-MAX_ERRORS_KEPT:]

        icon = "▶" if kind == "play" else "🔄"
        item = QListWidgetItem(f"{entry['time']}  {icon}  {label[:48]} — {reason}")
        item.setForeground(QColor(self.t["red"] if critical else self.t["yellow"]))
        self.error_list.addItem(item)

        self._refresh_error_indicator()
        if not silent_banner:
            self._show_banner(tr("banner.error", label=label[:40], reason=reason))
        self._notify(tr("notify.play_error") if kind == "play" else tr("notify.crawl_error"),
                     f"{label}: {reason}", icon="dialog-error")

    def _refresh_error_indicator(self):
        n = len(self._errors)
        self.errors_btn.setText(tr("btn.errors_n", n=n) if n else tr("btn.errors"))
        if n:
            self.errors_btn.setStyleSheet(
                f"QPushButton {{ background-color: {self.t['bg_surface']}; "
                f"color: {self.t['red']}; border: 1px solid {self.t['red']}; "
                f"padding: 4px 12px; border-radius: 4px; font-size: 12px; }}"
                f"QPushButton:hover {{ color: {self.t['text']}; }}"
            )
        else:
            self._style_action_btn(self.errors_btn)

    def _on_error_selected(self, row: int):
        if 0 <= row < len(self._errors):
            e = self._errors[row]
            tail = e["detail"]
            if not tail and e["log_path"]:
                tail = _read_tail(e["log_path"], LOG_TAIL_BYTES)
            header = f"[{e['time']}] {e['label']}\n{e['reason']}\n"
            if e["log_path"]:
                header += f"log: {e['log_path']}\n"
            self.error_detail.setPlainText(header + "\n" + (tail or tr("errors.no_log")))

    def _open_selected_log(self):
        row = self.error_list.currentRow()
        if 0 <= row < len(self._errors):
            path = self._errors[row]["log_path"]
            if path and Path(path).exists() and shutil.which("xdg-open"):
                try:
                    subprocess.Popen(["xdg-open", path],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass

    def _clear_errors(self):
        self._errors.clear()
        self.error_list.clear()
        self.error_detail.clear()
        self._refresh_error_indicator()

    def _show_banner(self, text: str):
        self.banner_label.setText(text)
        self.banner.setVisible(True)
        self._banner_timer.start(15000)

    def _hide_banner(self):
        self.banner.setVisible(False)

    # =========================================================================
    # Settings
    # =========================================================================
    @staticmethod
    def _unquote(v: str) -> str:
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            return v[1:-1]
        return v

    def _load_settings(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if SETTINGS_FILE.exists():
            try:
                with open(SETTINGS_FILE, "r") as f:
                    for line in f:
                        line = line.rstrip("\n")
                        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        # Command templates (BIN_/ARGS_) are stored without
                        # outer quotes so embedded quotes are preserved
                        # (e.g. streamlink --player-args "...").
                        if k.strip().startswith(("BIN_", "ARGS_")):
                            self.settings[k.strip()] = v
                        else:
                            self.settings[k.strip()] = self._unquote(v)
            except OSError:
                pass

    def save_settings(self):
        self.settings["PLAYER"]     = self.ctrl_player.currentText()
        self.settings["PLAY_MODE"]  = self.ctrl_mode.currentText()
        self.settings["RESOLUTION"] = self.ctrl_res.currentText()
        self.settings["AUDIO_QUALITY"] = dict(
            (lbl, v) for lbl, v in self._audio_q_map
        ).get(self.ctrl_audio_q.currentText(), "best")
        self.settings["THUMB_QUALITY"] = dict(
            (lbl, v) for lbl, v in self._thumb_q_map
        ).get(self.ctrl_thumb_q.currentText(), "balanced")
        self.settings["SUBTITLES"]  = "true" if self.ctrl_subs.isChecked() else "false"
        self.settings["THEME"]      = self.ctrl_theme.currentText()
        new_lang = self.ctrl_lang.currentData() or self.settings.get("LANGUAGE", i18n.BASE_LANGUAGE)
        lang_changed = new_lang != self.settings.get("LANGUAGE")
        self.settings["LANGUAGE"]   = new_lang
        plats = []
        if self.ctrl_src_youtube.isChecked(): plats.append("youtube")
        if self.ctrl_src_twitch.isChecked():  plats.append("twitch")
        if self.ctrl_src_kick.isChecked():    plats.append("kick")
        self.settings["SOURCES"]     = ",".join(plats)
        self.settings["TWITCH_VODS"] = "true" if self.ctrl_twitch_vods.isChecked() else "false"
        self.settings["KICK_VODS"]   = "true" if self.ctrl_kick_vods.isChecked() else "false"

        for key, (bin_edit, args_edit) in self._cmd_edits.items():
            self.settings[f"BIN_{key}"]  = bin_edit.text().strip()
            self.settings[f"ARGS_{key}"] = args_edit.text().strip()

        try:
            with open(SETTINGS_FILE, "w") as f:
                for k, v in self.settings.items():
                    if k.startswith(("BIN_", "ARGS_")):
                        f.write(f"{k}={v}\n")        # no outer quotes
                    else:
                        f.write(f'{k}="{v}"\n')
        except OSError as e:
            self.status_bar.setText(tr("status.settings_save_error", e=e))
            self._report_error("crawl", tr("label.settings"), tr("err.settings_save_failed", e=e), "", critical=True)
            return

        self._apply_theme(self.settings["THEME"])
        self._apply_allowed_sources()
        self._notify(tr("notify.settings_saved"))
        if lang_changed:
            # Rebuilds the whole UI in the new language and returns to the feed.
            self.change_language(new_lang)
        else:
            self.toggle_settings()

    # =========================================================================
    # Worker / update
    # =========================================================================
    def force_update(self):
        worker = WORKER_SCRIPT
        if not worker.exists():
            worker = Path("/usr/lib/outcast/outcast-worker.sh")
        if not worker.exists():
            self.status_bar.setText(tr("status.worker_not_found"))
            self._report_error("crawl", tr("label.update"),
                               tr("err.worker_script_missing"), "", critical=True)
            return
        try:
            subprocess.Popen(
                [str(worker), "--crawl-only"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            self._report_error("crawl", tr("label.update"),
                               tr("err.worker_launch_failed", e=e), "", critical=True)
            return
        self.status_bar.setText(tr("status.update_started"))
        # The update shortcut also refreshes the Twitch live streams.
        self.check_live()

    # =========================================================================
    # Playback (monitorizada)
    # =========================================================================
    def _build_play_args(self, video_data: dict) -> tuple[str, list[str]]:
        """Build the command from the configurable template.

        Returns (main_binary, full_args). Raises CommandTemplateError if the
        template is invalid.
        """
        backend = self.settings.get("PLAYER", "yt-dlp")
        mode    = self.settings.get("PLAY_MODE", "video")
        res     = self.settings.get("RESOLUTION", "480")
        subs_on = self.settings.get("SUBTITLES", "false") == "true"
        source  = video_data.get("source", SRC_YOUTUBE)

        # Audio quality: "best" -> uncapped (very high cap); otherwise kbps.
        aq = self.settings.get("AUDIO_QUALITY", "best")
        abr = "9999" if aq in ("", "best") else aq

        if source in (SRC_TWITCH_LIVE, SRC_TWITCH_VOD):
            base = "TWITCH"          # Twitch always via streamlink
        elif source in (SRC_KICK_LIVE, SRC_KICK_VOD):
            base = "KICK"            # Kick via mpv+yt-dlp (default)
        else:
            base = "STREAMLINK" if backend == "streamlink" else "YTDLP"
        key = base + ("_AUDIO" if mode == "audio" else "_VIDEO")

        bin_field  = self.settings.get(f"BIN_{key}",  DEFAULT_COMMANDS[f"BIN_{key}"])
        args_field = self.settings.get(f"ARGS_{key}", DEFAULT_COMMANDS[f"ARGS_{key}"])

        vid_id = video_data["vid_id"]
        url    = video_data.get("url") or f"https://www.youtube.com/watch?v={vid_id}"
        title  = f"outcast · {video_data.get('title', vid_id)}"

        # Braced variants before unbraced ones (both are accepted).
        ctx = [
            ("${URL}", url),   ("$URL", url),
            ("${ID}", vid_id), ("$ID", vid_id),
            ("${TITLE}", title), ("$TITLE", title),
            ("${RES}", res),   ("$RES", res),
            ("${ABR}", abr),   ("$ABR", abr),
        ]
        subs_tokens = SUBS_TOKENS if subs_on else []

        try:
            bin_tokens = shlex.split(bin_field)
        except ValueError as exc:
            raise CommandTemplateError(f"malformed binary field ({exc})")
        if not bin_tokens:
            raise CommandTemplateError("the binary field is empty")

        arg_tokens = expand_command_args(args_field, ctx, subs_tokens)
        return bin_tokens[0], bin_tokens + arg_tokens

    def play_video(self, video_data: dict):
        title  = video_data.get("title", video_data["vid_id"])
        source = video_data.get("source", SRC_YOUTUBE)
        if source in (SRC_TWITCH_LIVE, SRC_TWITCH_VOD):
            player = "streamlink (twitch)"
        elif source in (SRC_KICK_LIVE, SRC_KICK_VOD):
            player = "mpv (kick)"
        else:
            player = self.settings.get("PLAYER", "yt-dlp")

        try:
            binary, args = self._build_play_args(video_data)
        except CommandTemplateError as e:
            self._report_error("play", title,
                               tr("err.template_invalid", e=e),
                               "", critical=True)
            return

        # The binary can be a name in PATH or an absolute executable path.
        if shutil.which(binary) is None and not (
            os.path.isfile(binary) and os.access(binary, os.X_OK)
        ):
            self._report_error("play", title,
                               tr("err.binary_not_found_template", bin=binary),
                               "", critical=True)
            return

        ts = datetime.now().strftime("%H%M%S")
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(video_data["vid_id"]))[:40]
        log_path = PLAY_LOG_DIR / f"{safe_id}-{ts}.log"

        worker = ProcessWorker(kind="play", args=args, label=title,
                               log_path=log_path, backend=player, parent=self)
        worker.result.connect(self._on_proc_finished)
        worker.finished.connect(worker.deleteLater)
        self._workers.append(worker)
        worker.start()

        self.status_bar.setText(tr("status.playing", player=player, title=title[:55]))
        self._notify(tr("notify.starting", player=player), title, icon="mpv")

    def _on_proc_finished(self, p: dict):
        w = self.sender()
        if isinstance(w, ProcessWorker) and w in self._workers:
            self._workers.remove(w)

        if p["kind"] == "search":
            self._search_busy = False
            self.yt_search_btn.setEnabled(True)
            if p.get("missing"):
                self._report_error("play", tr("label.youtube_search"),
                                   tr("err.binary_missing", bin=p['missing']), p["log_tail"],
                                   critical=True, log_path=p["log_path"])
                return
            rows = self._parse_search_output(p["log_path"])
            self._search_model.clear()
            self._search_model.add_rows(rows)
            if rows:
                self.status_bar.setText(tr("status.search_results", n=len(rows), q=p['label'][:40]))
                self._fetch_thumbs_async([r["vid_id"] for r in rows], self.search_results)
                if self._search_proxy.rowCount() > 0:
                    self.search_results.setCurrentIndex(self._search_proxy.index(0, 0))
            else:
                reason = classify_log(p["log_tail"]) or tr("status.no_results")
                self.status_bar.setText(tr("status.search_reason", reason=reason, q=p['label'][:40]))
            return

        if p["kind"] != "play":
            return

        label, rc = p["label"], p["returncode"]
        elapsed, tail = p["elapsed"], p["log_tail"]
        log_path = p["log_path"]

        if p.get("missing"):
            self._report_error("play", label, tr("err.binary_missing", bin=p['missing']),
                               tail, critical=True, log_path=log_path)
            return
        if rc == ProcessWorker.RC_SPAWN_ERR:
            self._report_error("play", label, tr("err.spawn_failed"),
                               tail, critical=True, log_path=log_path)
            return

        reason = classify_log(tail)

        if rc != 0:
            if reason:
                msg = reason
            elif elapsed < FAST_EXIT_SECONDS:
                msg = tr("err.fast_exit", secs=elapsed, rc=rc)
            else:
                msg = tr("err.exit_code", rc=rc)
            self._report_error("play", label, msg, tail, log_path=log_path)
        elif elapsed < FAST_EXIT_SECONDS and reason:
            # Clean exit (0) but very fast and with error markers -> real failure.
            self._report_error("play", label, reason, tail, log_path=log_path)
        else:
            # Success (or normal manual close). We do not bother with a banner.
            self.status_bar.setText(tr("status.finished", title=label[:55]))

    # =========================================================================
    # Shutdown
    # =========================================================================
    def closeEvent(self, event):
        # Ask the watchers to stop; the players keep running
        # (they are in their own session and we never kill them).
        for w in list(self._workers):
            w.request_stop()
        for t in list(self._thumb_threads):
            t.request_stop()
        for w in list(self._workers):
            w.wait(800)
        for t in list(self._thumb_threads):
            t.wait(800)
        try:
            self._db.close()
        except Exception:
            pass
        super().closeEvent(event)


# =============================================================================
# Entry point
# =============================================================================
if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")

    app = QApplication(sys.argv)
    app.setApplicationName("outcast")

    window = OutcastApp()
    # Set after construction so the language resolved in __init__ is honored.
    app.setApplicationDisplayName(tr("app.display_name"))
    window.show()
    sys.exit(app.exec())
