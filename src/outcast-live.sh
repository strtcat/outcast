#!/usr/bin/env bash
# =============================================================================
# outcast-live.sh — Outcast · Twitch/Kick live checker (single-shot)
# Version: 0.20.0
# =============================================================================
#
# PURPOSE
#   Lightweight, single-shot worker. Reads the Twitch channel list from
#   ${CONFIG_DIR}/twitch and, for each one, checks with `streamlink --json`
#   whether it is live RIGHT NOW. No API key or OAuth required.
#
#   It writes the results (only the channels that are live) to live.tsv using
#   the same 7-column format the app uses, and downloads the public live
#   preview. It talks to the UI through files:
#
#     LIVE_STATUS_FILE   one-line status (running:i:n | done:N | idle)
#     LIVE_TSV           live.tsv (written atomically: temp + mv)
#
#   The "is live" check relies on `streamlink --json` returning the available
#   streams when the channel is broadcasting, and an object with "error" when
#   it is offline or has no plugin.
# =============================================================================

set -uo pipefail

readonly LIVE_WORKER_VERSION="0.20.0"

readonly CONFIG_BASE="${XDG_CONFIG_HOME:-${HOME}/.config}"
readonly CACHE_BASE="${XDG_CACHE_HOME:-${HOME}/.cache}"

readonly CONFIG_DIR="${CONFIG_BASE}/outcast"
readonly TWITCH_SUBS="${CONFIG_DIR}/twitch"
readonly KICK_SUBS="${CONFIG_DIR}/kick"

readonly CACHE_DIR="${CACHE_BASE}/outcast-feed"
readonly CACHE_THUMBS="${CACHE_BASE}/outcast-thumbs"
readonly LIVE_TSV="${CACHE_DIR}/live.tsv"
readonly LIVE_STATUS_FILE="${CACHE_DIR}/.live_status"
readonly LIVE_LOCK="${CACHE_DIR}/.live.lock"
readonly LIVE_LOG="${CACHE_DIR}/.live.log"

# Max seconds per channel check and number of checks run in parallel.
readonly STREAMLINK_TIMEOUT="12"
readonly MAX_PARALLEL="6"

status_write() {
    local tmp
    tmp=$(mktemp "${CACHE_DIR}/.livestatus_tmp_XXXX") || return 1
    printf '%s' "$1" > "$tmp"
    mv "$tmp" "$LIVE_STATUS_FILE"
}

log_msg() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LIVE_LOG"
}

_setup_dirs() {
    mkdir -p "$CACHE_DIR" "$CACHE_THUMBS" "$CONFIG_DIR"
    if [[ -f "$LIVE_LOG" ]]; then
        local lines
        lines=$(wc -l < "$LIVE_LOG" | tr -d ' ')
        if (( lines > 300 )); then
            local tmp_log
            tmp_log=$(mktemp "${CACHE_DIR}/.livelog_tmp_XXXX")
            tail -200 "$LIVE_LOG" > "$tmp_log" && mv "$tmp_log" "$LIVE_LOG"
        fi
    fi
}

check_live() {
    exec 9>"$LIVE_LOCK"
    if ! flock -n 9; then
        log_msg "check_live: another check is already running. Exiting."
        return 0
    fi

    log_msg "========== Live check started =========="

    local have_streamlink=0 have_ytdlp=0
    command -v streamlink >/dev/null 2>&1 && have_streamlink=1
    command -v yt-dlp     >/dev/null 2>&1 && have_ytdlp=1

    local has_twitch=0 has_kick=0
    [[ -f "$TWITCH_SUBS" ]] && grep -qvE '^\s*(#|$)' "$TWITCH_SUBS" 2>/dev/null && has_twitch=1
    [[ -f "$KICK_SUBS"   ]] && grep -qvE '^\s*(#|$)' "$KICK_SUBS"   2>/dev/null && has_kick=1

    # Twitch needs streamlink; Kick needs yt-dlp. If there is nothing to check
    # (no channels or no tools), empty live.tsv and exit.
    if [[ ( "$has_twitch" -eq 0 || "$have_streamlink" -eq 0 ) && \
          ( "$has_kick"   -eq 0 || "$have_ytdlp"     -eq 0 ) ]]; then
        log_msg "Nothing to check (twitch=$has_twitch/streamlink=$have_streamlink, kick=$has_kick/ytdlp=$have_ytdlp)."
        : > "${LIVE_TSV}.tmp" && mv "${LIVE_TSV}.tmp" "$LIVE_TSV"
        status_write 'done:0'
        flock -u 9; return 0
    fi

    local tw_total kk_total total
    tw_total=$(grep -cvE '^\s*(#|$)' "$TWITCH_SUBS" 2>/dev/null || echo 0)
    kk_total=$(grep -cvE '^\s*(#|$)' "$KICK_SUBS"   2>/dev/null || echo 0)
    total=$(( ${tw_total:-0} + ${kk_total:-0} ))
    status_write "running:0:${total}"

    local tmp_out count
    tmp_out=$(mktemp "${CACHE_DIR}/.live_tmp_XXXX")

    # The heavy lifting (streamlink/yt-dlp + parallel preview downloads) is done
    # by Python; bash only orchestrates and reports the status.
    count=$(python3 - "$TWITCH_SUBS" "$KICK_SUBS" "$CACHE_THUMBS" "$tmp_out" \
                     "$STREAMLINK_TIMEOUT" "$MAX_PARALLEL" "$LIVE_LOG" \
                     "$have_streamlink" "$have_ytdlp" << 'PYEOF'
import sys, os, json, subprocess, time
from concurrent.futures import ThreadPoolExecutor

(twitch_subs, kick_subs, thumb_dir, out_path, timeout_s, parallel, log_file,
 have_streamlink, have_ytdlp) = sys.argv[1:10]
timeout_s = int(timeout_s)
parallel  = max(1, int(parallel))
have_streamlink = have_streamlink == '1'
have_ytdlp = have_ytdlp == '1'

def log(msg):
    try:
        with open(log_file, 'a') as lf:
            lf.write(msg + '\n')
    except OSError:
        pass

def channel_name(line):
    s = line.strip()
    if not s or s.startswith('#'):
        return None
    s = s.split()[0]
    if '/' in s:
        s = s.rstrip('/').split('/')[-1]
    s = s.strip().lower()
    if not s or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in s):
        return None
    return s

def read_channels(path):
    out, seen = [], set()
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                ch = channel_name(line)
                if ch and ch not in seen:
                    seen.add(ch); out.append(ch)
    except OSError:
        pass
    return out

def sanitize(text):
    return (text or '').replace('\t', ' ').replace('\n', ' ').strip()

def grab(url, path):
    if not url:
        return
    try:
        subprocess.run(['curl', '-sL', '--max-time', '10', url, '-o', path],
                       timeout=12, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(path) and os.path.getsize(path) == 0:
            os.remove(path)
    except Exception:
        pass

# -- Twitch (streamlink --json) --------------------------------------------
def check_twitch(ch):
    url = f'https://www.twitch.tv/{ch}'
    try:
        proc = subprocess.run(['streamlink', '--json', f'twitch.tv/{ch}'],
                              capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        log(f'  twitch/{ch}: timeout'); return None
    except Exception as exc:
        log(f'  twitch/{ch}: streamlink error: {exc}'); return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if isinstance(data, dict) and data.get('error'):
        return None
    if not (isinstance(data, dict) and data.get('streams')):
        return None
    meta = data.get('metadata') or {}
    title    = sanitize(meta.get('title')) or f'{ch} live'
    author   = sanitize(meta.get('author')) or ch
    category = sanitize(meta.get('category'))
    # The "live" status is conveyed by the source column (twitch_live); the
    # duration column carries the category only, so it stays language-neutral.
    dur = category or '—'
    now = time.strftime('%Y-%m-%d %H:%M')
    grab(f'https://static-cdn.jtvnw.net/previews-ttv/live_user_{ch}-320x180.jpg',
         os.path.join(thumb_dir, f'live_{ch}.jpg'))
    log(f'  twitch/{ch}: LIVE — {title}')
    return f'live_{ch}\t{title}\t{author}\t{now}\t{dur}\ttwitch_live\t{url}'

# -- Kick (yt-dlp) ---------------------------------------------------------
def check_kick(ch):
    url = f'https://kick.com/{ch}'
    fmt = '%(is_live)s@@@%(title)s@@@%(uploader,channel,uploader_id)s@@@%(thumbnail)s@@@%(categories.0,genre)s'
    try:
        proc = subprocess.run(
            ['yt-dlp', '--no-warnings', '--quiet', '--no-playlist', '--print', fmt, url],
            capture_output=True, text=True, timeout=timeout_s + 8)
    except subprocess.TimeoutExpired:
        log(f'  kick/{ch}: timeout'); return None
    except Exception as exc:
        log(f'  kick/{ch}: yt-dlp error: {exc}'); return None
    out = (proc.stdout or '').strip()
    if proc.returncode != 0 or not out:
        err = (proc.stderr or '').strip().lower()
        if '403' in err or 'impersonat' in err:
            log(f'  kick/{ch}: 403/impersonation — needs curl_cffi (pip install curl_cffi).')
        return None
    parts = out.splitlines()[0].split('@@@')
    is_live = parts[0].strip().lower() if parts else ''
    if is_live not in ('true', '1'):
        return None
    title    = sanitize(parts[1]) if len(parts) > 1 else ''
    author   = sanitize(parts[2]) if len(parts) > 2 else ch
    thumb    = parts[3].strip()   if len(parts) > 3 else ''
    category = sanitize(parts[4]) if len(parts) > 4 else ''
    title    = title or f'{ch} live'
    author   = author if author and author != 'NA' else ch
    dur = category if category and category != 'NA' else '—'
    now = time.strftime('%Y-%m-%d %H:%M')
    if thumb and thumb not in ('NA', 'None'):
        grab(thumb, os.path.join(thumb_dir, f'live_kick_{ch}.jpg'))
    log(f'  kick/{ch}: LIVE — {title}')
    return f'live_kick_{ch}\t{title}\t{author}\t{now}\t{dur}\tkick_live\t{url}'

jobs = []
if have_streamlink:
    jobs += [('tw', ch) for ch in read_channels(twitch_subs)]
if have_ytdlp:
    jobs += [('kk', ch) for ch in read_channels(kick_subs)]

def run(job):
    kind, ch = job
    return check_twitch(ch) if kind == 'tw' else check_kick(ch)

rows = []
with ThreadPoolExecutor(max_workers=parallel) as ex:
    for res in ex.map(run, jobs):
        if res:
            rows.append(res)

with open(out_path, 'w', encoding='utf-8') as f:
    if rows:
        f.write('\n'.join(rows) + '\n')

print(len(rows))
PYEOF
)
    count="${count:-0}"
    [[ "$count" =~ ^[0-9]+$ ]] || count=0

    # Atomic publish of live.tsv.
    mv "$tmp_out" "$LIVE_TSV"

    status_write "done:${count}"
    log_msg "Check finished. Live channels: $count / $total"
    log_msg "========== Live done =========="

    flock -u 9
}

case "${1:-}" in
    --check)
        _setup_dirs
        [[ -f "$LIVE_STATUS_FILE" ]] || status_write 'idle'
        check_live
        exit 0
        ;;
    --version|-V)
        printf 'outcast-live %s\n' "$LIVE_WORKER_VERSION"
        exit 0
        ;;
    --help|-h)
        printf 'Usage: %s --check\n' "$0"
        exit 0
        ;;
    *)
        exit 1
        ;;
esac
