#!/usr/bin/env bash
# =============================================================================
# outcast-worker.sh — Outcast · Background Crawler
# Version: 0.20.0
# =============================================================================
#
# PURPOSE
#   Pure data worker. Fetches YouTube RSS feeds, resolves durations, downloads
#   thumbnails, and writes video metadata into the SQLite store (outcast_db.py,
#   under $XDG_DATA_HOME/outcast/outcast.db). It communicates progress with the
#   UI through filesystem state files, while the videos themselves flow through
#   the database (the UI polls for newly-inserted rows):
#
#     CRAWLER_STATUS_FILE   one-line status string
#     outcast.db            durable video metadata (via outcast_db.py)
#     LAST_UPDATE_FILE      unix timestamp
# =============================================================================

set -uo pipefail

readonly OUTCAST_WORKER_VERSION="0.20.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR

readonly CONFIG_BASE="${XDG_CONFIG_HOME:-${HOME}/.config}"
readonly CACHE_BASE="${XDG_CACHE_HOME:-${HOME}/.cache}"

readonly CONFIG_DIR="${CONFIG_BASE}/outcast"
readonly SUBS="${CONFIG_DIR}/subscriptions"
readonly TWITCH_SUBS="${CONFIG_DIR}/twitch"
readonly KICK_SUBS="${CONFIG_DIR}/kick"
readonly CONFIG_FILE="${CONFIG_DIR}/config"

readonly CACHE_DIR="${CACHE_BASE}/outcast-feed"
readonly CACHE_IDS="${CACHE_BASE}/outcast-channel-ids"
readonly CACHE_THUMBS="${CACHE_BASE}/outcast-thumbs"
readonly SETTINGS_FILE="${CACHE_DIR}/settings"
readonly LAST_UPDATE_FILE="${CACHE_DIR}/.last_update"
readonly CRAWLER_STATUS_FILE="${CACHE_DIR}/.crawler_status"
readonly CRAWL_ERR_FILE="${CACHE_DIR}/.crawl_errors"
readonly CRAWL_LOCK="${CACHE_DIR}/.crawl.lock"
readonly CRAWL_LOG="${CACHE_DIR}/.crawl.log"

# Durable metadata lives in the SQLite store; outcast_db.py is the single
# source of truth (kept next to this script, or under /usr/lib/outcast when
# installed). OUTCAST_LIB lets the embedded Python heredocs import it.
OUTCAST_DB="${SCRIPT_DIR}/outcast_db.py"
[[ -f "$OUTCAST_DB" ]] || OUTCAST_DB="/usr/lib/outcast/outcast_db.py"
OUTCAST_LIB="$(dirname "$OUTCAST_DB")"
export OUTCAST_LIB

# Thin wrapper around the DB CLI.
db() {
    python3 "$OUTCAST_DB" "$@"
}

MAX_VIDEOS_PER_CHANNEL="30"
DATE_FROM=""
DATE_TO=""
SOURCES="youtube,twitch,kick"   # list of active platforms
TWITCH_VODS="true"        # include saved Twitch VODs
KICK_VODS="true"          # include saved Kick VODs
MAX_TWITCH_VODS="15"      # number of VODs per Twitch channel
MAX_KICK_VODS="15"        # number of VODs per Kick channel
THUMB_QUALITY="balanced"  # saver | balanced | high
THUMB_W="200"             # target width (derived from THUMB_QUALITY)
THUMB_Q="62"              # JPEG quality (derived from THUMB_QUALITY)

# Translate THUMB_QUALITY to width/quality. Legacy Spanish values
# (ahorro/equilibrado/alta) are accepted for backward compatibility.
apply_thumb_quality() {
    case "$THUMB_QUALITY" in
        saver|ahorro)         THUMB_W="160"; THUMB_Q="55" ;;
        high|alta)            THUMB_W="256"; THUMB_Q="68" ;;
        balanced|equilibrado|*) THUMB_W="200"; THUMB_Q="62" ;;
    esac
}

# Is platform $1 active in SOURCES? (compatible with the old "both")
source_enabled() {
    local s="$SOURCES"
    [[ "$s" == "both" ]] && s="youtube,twitch"
    [[ ",$s," == *",$1,"* ]]
}

load_settings() {
    local f key val
    for f in "$CONFIG_FILE" "$SETTINGS_FILE"; do
        [[ -f "$f" ]] || continue
        while IFS='=' read -r key val; do
            [[ "$key" =~ ^[[:space:]]*# ]] && continue
            [[ -z "${key// /}" ]]           && continue
            key="${key#"${key%%[![:space:]]*}"}"
            key="${key%"${key##*[![:space:]]}"}"
            val="${val#"${val%%[![:space:]]*}"}"
            val="${val%"${val##*[![:space:]]}"}"
      
            val="${val%\"}" ; val="${val#\"}"
            val="${val%\'}" ; val="${val#\'}"
            case "$key" in
                MAX_VIDEOS_PER_CHANNEL)
                    [[ "$val" =~ ^[0-9]+$ ]] && MAX_VIDEOS_PER_CHANNEL="$val" ;;
                DATE_FROM)
                    [[ "$val" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})?$ ]] && DATE_FROM="$val" ;;
                DATE_TO)
                    [[ "$val" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})?$ ]] && DATE_TO="$val"   ;;
                SOURCES)
                    [[ -n "$val" ]] && SOURCES="$val" ;;
                TWITCH_VODS)
                    case "$val" in true|false) TWITCH_VODS="$val" ;; esac ;;
                KICK_VODS)
                    case "$val" in true|false) KICK_VODS="$val" ;; esac ;;
                THUMB_QUALITY)
                    case "$val" in saver|balanced|high|ahorro|equilibrado|alta) THUMB_QUALITY="$val" ;; esac ;;
            esac
        done < "$f"
    done
}

write_default_config() {
    [[ -f "$CONFIG_FILE" ]] && return 0
    mkdir -p "$CONFIG_DIR"
    cat > "$CONFIG_FILE" << 'EOF'
# Maximum number of videos to keep per channel. 0 = unlimited.
MAX_VIDEOS_PER_CHANNEL="30"
DATE_FROM=""
DATE_TO=""
# Active sources (comma-separated list): youtube,twitch,kick
SOURCES="youtube,twitch,kick"
# Include saved VODs (last 15 per channel): true | false
# Channels: ~/.config/outcast/twitch  and  ~/.config/outcast/kick (one per line)
TWITCH_VODS="true"
KICK_VODS="true"
EOF
}

status_write() {
    local tmp
    tmp=$(mktemp "${CACHE_DIR}/.status_tmp_XXXX") || return 1
    printf '%s' "$1" > "$tmp"
    mv "$tmp" "$CRAWLER_STATUS_FILE"
}

log_msg() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$CRAWL_LOG"
}

ensure_db() {
    # Creates the database file and schema if they do not exist yet.
    db init
}

cached_video_ids() {
    db cached-ids
}

resolve_channel_id() {
    local url="$1"
    local hash cachefile
    hash=$(printf '%s' "$url" | md5sum | cut -d' ' -f1)
    cachefile="${CACHE_IDS}/${hash}"

    if [[ -f "$cachefile" && -s "$cachefile" ]]; then
        cat "$cachefile"
        return 0
    fi

    log_msg "Resolving channel ID for: $url"
    local id
    id=$(curl -s --max-time 15 "$url" \
        | grep -o '"externalId":"[^"]*"' \
        | head -1 | cut -d'"' -f4)

    if [[ -n "$id" ]]; then
        printf '%s' "$id" > "$cachefile"
        printf '%s' "$id"
        log_msg "  → resolved to: $id"
    else
        log_msg "  → FAILED to resolve channel ID (URL: $url)"
    fi
}

fetch_rss() {
    local channel_id="$1"
    local rss_url="https://www.youtube.com/feeds/videos.xml?channel_id=${channel_id}"
    log_msg "  Fetching RSS: $rss_url"

    curl -s --max-time 20 "$rss_url" \
    | python3 -c "
import sys, xml.etree.ElementTree as ET
ns = {
    'atom': 'http://www.w3.org/2005/Atom',
    'yt':   'http://www.youtube.com/xml/schemas/2015',
}
# Read RAW BYTES (not text): YouTube feeds are UTF-8 and declare their own
# encoding, so letting ElementTree decode from the bytes avoids locale-mangled
# 'invalid token' errors on feeds with accented titles.
data = sys.stdin.buffer.read()
if not data or not data.strip():
    sys.exit(0)   # empty body -> empty feed, not an error
head = data.lstrip()[:20].lower()
if not head.startswith(b'<'):
    sys.stderr.write('  RSS: non-XML response (rate-limited or error page?)\n')
    sys.exit(0)
try:
    root = ET.fromstring(data)
except ET.ParseError as exc:
    sys.stderr.write('  RSS parse error: ' + str(exc) + '\n')
    sys.exit(0)
ch_node = root.find('atom:author/atom:name', ns)
ch_name = ch_node.text.strip() if ch_node is not None and ch_node.text else 'Unknown'
for entry in root.findall('atom:entry', ns):
    vid_node  = entry.find('yt:videoId', ns)
    titl_node = entry.find('atom:title', ns)
    pub_node  = entry.find('atom:published', ns)
    if vid_node is None or pub_node is None or not vid_node.text:
        continue
    vid  = vid_node.text.strip()
    titl = (titl_node.text or '').replace('\t', ' ').strip() if titl_node is not None else ''
    pub  = (pub_node.text or '')[:16].replace('T', ' ')
    if vid:
        print(vid + '\t' + titl + '\t' + ch_name + '\t' + pub)
" 2>>"$CRAWL_LOG"
}

fetch_duration_single() {
    local vid_id="$1"
    local raw
    raw=$(nice -n 19 yt-dlp \
        --no-playlist \
        --skip-download \
        --print "%(duration)s" \
        -- "https://www.youtube.com/watch?v=${vid_id}" 2>/dev/null) || true

    if [[ "$raw" =~ ^[0-9]+$ ]]; then
        local secs=$(( raw ))
        if   (( secs >= 3600 )); then
            printf '%dh %dm %ds' "$((secs/3600))" "$(( (secs%3600)/60 ))" "$((secs%60))"
        elif (( secs >= 60 )); then
            printf '%dm %ds' "$((secs/60))" "$((secs%60))"
        else
            printf '%ds' "$secs"
        fi
    else
        printf '?'
    fi
}

download_thumbnails() {
    local ids_file="$1" 

    [[ ! -f "$ids_file" || ! -s "$ids_file" ]] && return 0

    python3 - "$ids_file" "$CACHE_THUMBS" "$CRAWL_LOG" "$THUMB_W" "$THUMB_Q" << 'PYEOF'
import sys, os, shutil, subprocess, threading

ids_file, thumb_dir, log_file = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    THUMB_W = int(sys.argv[4]); THUMB_Q = int(sys.argv[5])
except (IndexError, ValueError):
    THUMB_W, THUMB_Q = 200, 62

def log(msg):
    try:
        with open(log_file, 'a') as lf:
            lf.write(msg + '\n')
    except OSError:
        pass

# Detect a standard image tool to resize/compress thumbnails ("infinite"
# cache -> ultra-light). If none is available, the original image is kept.
# Each file is processed only once (when downloaded).
def _detect_tool():
    if shutil.which('magick'):  return 'magick'
    if shutil.which('convert'): return 'convert'   # ImageMagick 6
    if shutil.which('gm'):      return 'gm'         # GraphicsMagick
    if shutil.which('ffmpeg'):  return 'ffmpeg'
    return None
TOOL = _detect_tool()

def optimize(path):
    if not TOOL or not os.path.exists(path) or os.path.getsize(path) == 0:
        return
    tmp = path + '.opt.jpg'
    if TOOL == 'magick':
        cmd = ['magick', path, '-resize', f'{THUMB_W}x>', '-strip', '-quality', str(THUMB_Q), tmp]
    elif TOOL == 'convert':
        cmd = ['convert', path, '-resize', f'{THUMB_W}x>', '-strip', '-quality', str(THUMB_Q), tmp]
    elif TOOL == 'gm':
        cmd = ['gm', 'convert', path, '-resize', f'{THUMB_W}x>', '-strip', '-quality', str(THUMB_Q), tmp]
    else:  # ffmpeg
        cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-i', path,
               '-vf', f"scale='min({THUMB_W},iw)':-2", '-q:v', '6', tmp]
    try:
        r = subprocess.run(cmd, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
        else:
            if os.path.exists(tmp): os.remove(tmp)
    except Exception:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass

def download_thumb(vid_id):
    # Only YouTube ids (11 base64-url characters) are valid for i.ytimg.com.
    # A Twitch/Kick id would return a gray 120x90 placeholder.
    if len(vid_id) != 11 or any(
        c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-'
        for c in vid_id
    ):
        return
    path = os.path.join(thumb_dir, f'{vid_id}.jpg')
    if os.path.exists(path):
        return
    url = f'https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg'
    try:
        result = subprocess.run(
            ['curl', '-s', '--max-time', '10', url, '-o', path],
            timeout=12, capture_output=True
        )
        if result.returncode != 0:
            log(f'  Thumb download failed for {vid_id}: curl exit {result.returncode}')
            return
        optimize(path)
    except Exception as exc:
        log(f'  Thumb download exception for {vid_id}: {exc}')

try:
    with open(ids_file) as f:
        ids = [l.strip() for l in f if l.strip()]
except FileNotFoundError:
    ids = []

threads = [threading.Thread(target=download_thumb, args=(vid_id,)) for vid_id in ids]
for t in threads: t.start()
for t in threads: t.join()
log(f'  Downloaded/verified thumbnails for {len(ids)} video(s). Tool={TOOL or "ninguna"}.')
PYEOF
}

# Download thumbnails from explicit URLs (format: id<TAB>url). Used by Twitch
# VODs, whose thumbnails do not follow the YouTube pattern.
download_thumbnails_explicit() {
    local map_file="$1"
    [[ ! -f "$map_file" || ! -s "$map_file" ]] && return 0
    python3 - "$map_file" "$CACHE_THUMBS" "$CRAWL_LOG" "$THUMB_W" "$THUMB_Q" << 'PYEOF'
import sys, os, re, shutil, subprocess, threading
map_file, thumb_dir, log_file = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    THUMB_W = int(sys.argv[4]); THUMB_Q = int(sys.argv[5])
except (IndexError, ValueError):
    THUMB_W, THUMB_Q = 200, 62

def log(msg):
    try:
        with open(log_file, 'a') as lf:
            lf.write(msg + '\n')
    except OSError:
        pass

def _detect_tool():
    if shutil.which('magick'):  return 'magick'
    if shutil.which('convert'): return 'convert'
    if shutil.which('gm'):      return 'gm'
    if shutil.which('ffmpeg'):  return 'ffmpeg'
    return None
TOOL = _detect_tool()

def optimize(path):
    if not TOOL or not os.path.exists(path) or os.path.getsize(path) == 0:
        return
    tmp = path + '.opt.jpg'
    if TOOL == 'magick':
        cmd = ['magick', path, '-resize', f'{THUMB_W}x>', '-strip', '-quality', str(THUMB_Q), tmp]
    elif TOOL == 'convert':
        cmd = ['convert', path, '-resize', f'{THUMB_W}x>', '-strip', '-quality', str(THUMB_Q), tmp]
    elif TOOL == 'gm':
        cmd = ['gm', 'convert', path, '-resize', f'{THUMB_W}x>', '-strip', '-quality', str(THUMB_Q), tmp]
    else:
        cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-i', path,
               '-vf', f"scale='min({THUMB_W},iw)':-2", '-q:v', '6', tmp]
    try:
        r = subprocess.run(cmd, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
        else:
            if os.path.exists(tmp): os.remove(tmp)
    except Exception:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass

def fix_size(url):
    # Twitch VOD thumbnails arrive as ".../thumb/thumb0-0x0.jpg". Even though
    # the full image is sometimes served anyway, asking for a real size is more
    # reliable and lighter. Also covers "%{width}x%{height}".
    url = url.replace('%{width}x%{height}', '640x360')
    url = re.sub(r'-0x0(\.\w+)', r'-640x360\1', url)
    url = re.sub(r'/0x0/', '/640x360/', url)
    return url

def grab(vid_id, url):
    if not url or url in ('NA', 'None'):
        return
    url = fix_size(url)
    path = os.path.join(thumb_dir, f'{vid_id}.jpg')
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    try:
        subprocess.run(['curl', '-sL', '--max-time', '12', url, '-o', path],
                       timeout=14, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(path) and os.path.getsize(path) == 0:
            os.remove(path)
            log(f'  Empty thumb for {vid_id} ({url})')
            return
        optimize(path)
    except Exception as exc:
        log(f'  Thumb failed for {vid_id}: {exc}')

pairs = []
try:
    with open(map_file) as f:
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) >= 2 and parts[0]:
                pairs.append((parts[0], parts[1]))
except FileNotFoundError:
    pass

threads = [threading.Thread(target=grab, args=p) for p in pairs]
for t in threads: t.start()
for t in threads: t.join()
log(f'  Downloaded/verified {len(pairs)} thumbnail(s). Tool={TOOL or "none"}.')
PYEOF
}

# List the last N VODs of each Twitch channel via yt-dlp (metadata only) and
# add them to the cache as source=twitch_vod rows. Does not download video.
crawl_twitch_vods() {
    local cached_ids_file="$1" all_new_ids_file="$2" tw_thumbs_file="$3"
    local count="$4" total="$5"

    if ! command -v yt-dlp >/dev/null 2>&1; then
        log_msg "yt-dlp not available — skipping Twitch VODs."
        printf '%s\t%s\n' "twitch (VODs)" "yt-dlp is required to list Twitch VODs." \
            >> "$CRAWL_ERR_FILE"
        return 0
    fi

    while IFS= read -r line; do
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
        count=$(( count + 1 ))
        status_write "running:${count}:${total}"

        # Normalize to a channel name (accepts full url, twitch.tv/x or x).
        local ch
        ch=$(printf '%s' "$line" | awk '{print $1}')
        ch="${ch%/}"
        ch="${ch##*/}"
        ch=$(printf '%s' "$ch" | tr '[:upper:]' '[:lower:]')
        [[ -z "$ch" ]] && continue

        log_msg "Twitch $ch: listing last $MAX_TWITCH_VODS VODs (archives)"

        # Step 1: flat-playlist ONLY to get the IDs (fast). In this mode
        # yt-dlp does NOT return reliable duration or date (they come as NA), so
        # we do not use them here; the real metadata is requested per VOD in
        # step 2. We explicitly request the "archives" filter = saved streams.
        local ids_raw
        ids_raw=$(nice -n 19 yt-dlp \
                --flat-playlist \
                --playlist-end "$MAX_TWITCH_VODS" \
                --ignore-errors --no-warnings \
                --print "%(id)s" \
                -- "https://www.twitch.tv/${ch}/videos?filter=archives&sort=time" \
                2>>"$CRAWL_LOG") || true

        # Fallback: some channels/versions do not respond to the filter; try
        # the /videos tab without parameters.
        if [[ -z "$ids_raw" ]]; then
            log_msg "  Twitch $ch: archives empty, trying /videos without filter."
            ids_raw=$(nice -n 19 yt-dlp \
                    --flat-playlist \
                    --playlist-end "$MAX_TWITCH_VODS" \
                    --ignore-errors --no-warnings \
                    --print "%(id)s" \
                    -- "https://www.twitch.tv/${ch}/videos" 2>>"$CRAWL_LOG") || true
        fi

        local id_count
        id_count=$(printf '%s\n' "$ids_raw" | grep -cve '^[[:space:]]*$' 2>/dev/null || true)
        id_count="${id_count:-0}"
        log_msg "  Twitch $ch: flat-playlist returned ${id_count} ID(s)."

        if [[ -z "$ids_raw" ]]; then
            log_msg "  Twitch $ch: no VODs, or yt-dlp lacks support/update."
            printf '%s\t%s\n' "$ch (Twitch)" \
                "No VODs (channel has no saved streams, or yt-dlp is outdated?)." \
                >> "$CRAWL_ERR_FILE"
            continue
        fi

        # Step 2: for each new (uncached) VOD we request real metadata with a
        # normal call (not flat). We use was_live/is_live to discard the ongoing
        # live stream, not the duration.
        local before_n after_n
        before_n=$(wc -l < "$all_new_ids_file" 2>/dev/null | tr -d ' '); before_n="${before_n:-0}"
        local vid_id
        while IFS= read -r vid_id; do
            vid_id="${vid_id//[[:space:]]/}"
            [[ -z "$vid_id" || "$vid_id" == "NA" ]] && continue

            # The VOD ID comes with a leading "v" (e.g. v2795816522), but
            # yt-dlp stores the id as a number and the URL twitch.tv/videos/<n>
            # needs ONLY the number: if the "v" is left in, yt-dlp treats it as
            # a channel name (twitch:stream) and fails with "videos does not
            # exist". We normalize to a number so dedup across crawls works.
            local vod_num="${vid_id#v}"

            if grep -qxF "$vod_num" "$cached_ids_file" 2>/dev/null; then
                continue
            fi

            local meta
            meta=$(nice -n 19 yt-dlp \
                    --no-playlist --skip-download \
                    --ignore-errors --no-warnings \
                    --print "%(id)s@@@%(title)s@@@%(uploader,channel,uploader_id)s@@@%(upload_date,release_date)s@@@%(duration)s@@@%(thumbnail)s@@@%(webpage_url)s@@@%(is_live)s@@@%(was_live)s" \
                    -- "https://www.twitch.tv/videos/${vod_num}" 2>>"$CRAWL_LOG") || true

            [[ -z "$meta" ]] && { log_msg "  Twitch $ch: no metadata for $vid_id"; continue; }

            # Process the row in Python (preserves empty fields and formats).
            local vod_in
            vod_in=$(mktemp /tmp/outcast-vodin-XXXX)
            printf '%s\n' "$meta" > "$vod_in"
            python3 - \
                "$ch" "" "$cached_ids_file" "$all_new_ids_file" \
                "$tw_thumbs_file" "$CRAWL_LOG" "$vod_in" << 'PYEOF'
import sys, datetime, os
ch, cache_file, cached_ids_file, all_new_file, tw_thumbs_file, log_file, vod_in = sys.argv[1:8]
sys.path.insert(0, os.environ.get('OUTCAST_LIB', '/usr/lib/outcast'))
import outcast_db

def log(msg):
    try:
        with open(log_file, 'a') as lf:
            lf.write(msg + '\n')
    except OSError:
        pass

def fmt_dur(raw):
    try:
        s = int(float(raw))
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    if s >= 3600:
        return f'{s//3600}h {(s%3600)//60}m {s%60}s'
    if s >= 60:
        return f'{s//60}m {s%60}s'
    return f'{s}s'

try:
    with open(vod_in, encoding='utf-8', errors='replace') as fh:
        line = fh.read().rstrip('\n')
except OSError:
    sys.exit(0)
if not line:
    sys.exit(0)

# yt-dlp does NOT interpret "\t" in --print (it would print it literally), so
# we use an unlikely sentinel as the field separator.
parts = line.split('@@@')
if len(parts) < 5:
    log(f'  Twitch {ch}: unexpected yt-dlp output ({len(parts)} fields) — skipped.')
    sys.exit(0)
vid, title, uploader, update, dur = parts[0:5]
thumb   = parts[5] if len(parts) > 5 else ''
wurl    = parts[6] if len(parts) > 6 else ''
is_live = parts[7] if len(parts) > 7 else ''
vid = vid.strip()
if not vid or vid == 'NA':
    sys.exit(0)
# canonical numeric id (strips the leading "v" from Twitch VODs).
if vid.startswith('v') and vid[1:].isdigit():
    vid = vid[1:]

# Discard the ONGOING live stream (not a finished VOD).
if is_live.strip().lower() in ('true', '1'):
    log(f'  Twitch {ch}: {vid} is the ongoing live stream — skipped.')
    sys.exit(0)

dur_h = fmt_dur(dur) or '?'
if update and len(update) == 8 and update.isdigit():
    date_iso = f'{update[0:4]}-{update[4:6]}-{update[6:8]}'
else:
    date_iso = datetime.date.today().isoformat()
title    = (title or '').replace('\t', ' ').strip() or vid
uploader = (uploader or '').replace('\t', ' ').strip()
if not uploader or uploader == 'NA':
    uploader = ch
if not wurl or wurl == 'NA':
    wurl = f'https://www.twitch.tv/videos/{vid}'

con = outcast_db.connect()
outcast_db.upsert(con, {
    'vid_id': vid, 'title': title, 'channel': uploader, 'channel_id': '',
    'date': date_iso, 'duration': dur_h, 'source': 'twitch_vod', 'url': wurl,
})
con.close()
with open(cached_ids_file, 'a', encoding='utf-8') as fh:
    fh.write(vid + '\n')
with open(all_new_file, 'a', encoding='utf-8') as fh:
    fh.write(vid + '\n')
if thumb and thumb not in ('NA', 'None'):
    with open(tw_thumbs_file, 'a', encoding='utf-8') as fh:
        fh.write(f'{vid}\t{thumb}\n')
log(f'  Twitch {ch}: + VOD {vid} ({dur_h})')
PYEOF
            rm -f "$vod_in"
            sleep 0.3
        done <<< "$ids_raw"

        after_n=$(wc -l < "$all_new_ids_file" 2>/dev/null | tr -d ' '); after_n="${after_n:-0}"
        log_msg "  Twitch $ch: +$(( after_n - before_n )) new VOD(s) added."

    done < "$TWITCH_SUBS"
}

# List the last N VODs of each Kick channel using the public v2 API
# (https://kick.com/api/v2/channels/<channel>/videos) through curl_cffi, which
# does browser impersonation (Kick blocks normal requests with 403).
# All the metadata (title, duration, date, thumbnail, uuid) comes from the
# JSON, so yt-dlp is NOT needed to list or enrich -> it is fast.
crawl_kick_vods() {
    local cached_ids_file="$1" all_new_ids_file="$2" kk_thumbs_file="$3"

    # One-off curl_cffi check; if missing, a single warning and we exit.
    if ! python3 -c "import curl_cffi" >/dev/null 2>&1; then
        log_msg "Kick: curl_cffi is missing — skipping Kick VODs."
        printf '%s\t%s\n' "Kick (VODs)" \
            "curl_cffi is missing. Install it: sudo pacman -S python-curl_cffi (or pip install curl_cffi)." \
            >> "$CRAWL_ERR_FILE"
        return 0
    fi

    while IFS= read -r line; do
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

        local ch
        ch=$(printf '%s' "$line" | awk '{print $1}')
        ch="${ch%/}"; ch="${ch##*/}"
        ch=$(printf '%s' "$ch" | tr '[:upper:]' '[:lower:]')
        [[ -z "$ch" ]] && continue

        log_msg "Kick $ch: listing last $MAX_KICK_VODS VODs (API)"

        local before_n after_n
        before_n=$(wc -l < "$all_new_ids_file" 2>/dev/null | tr -d ' '); before_n="${before_n:-0}"

        python3 - \
            "$ch" "" "$cached_ids_file" "$all_new_ids_file" \
            "$kk_thumbs_file" "$CRAWL_LOG" "$CRAWL_ERR_FILE" "$MAX_KICK_VODS" << 'PYEOF'
import sys, os
ch, cache_file, cached_ids_file, all_new_file, kk_thumbs_file, log_file, err_file, max_n = sys.argv[1:9]
sys.path.insert(0, os.environ.get('OUTCAST_LIB', '/usr/lib/outcast'))
import outcast_db
try:
    max_n = int(max_n)
except ValueError:
    max_n = 15

def log(msg):
    try:
        with open(log_file, 'a') as lf:
            lf.write(msg + '\n')
    except OSError:
        pass

def report(name, msg):
    try:
        with open(err_file, 'a') as ef:
            ef.write(f'{name}\t{msg}\n')
    except OSError:
        pass

try:
    from curl_cffi import requests as creq
except ImportError:
    log(f'  Kick {ch}: curl_cffi not importable.')
    sys.exit(0)

url = f'https://kick.com/api/v2/channels/{ch}/videos'
try:
    r = creq.get(url, impersonate='chrome', timeout=25)
except Exception as exc:
    log(f'  Kick {ch}: network error: {exc}')
    report(f'{ch} (Kick)', f'Network error querying Kick: {exc}')
    sys.exit(0)

if r.status_code == 403:
    log(f'  Kick {ch}: 403 despite impersonation.')
    report(f'{ch} (Kick)', 'Kick returned 403 even with curl_cffi.')
    sys.exit(0)
if r.status_code != 200:
    log(f'  Kick {ch}: HTTP {r.status_code}.')
    report(f'{ch} (Kick)', f'Kick returned HTTP {r.status_code}.')
    sys.exit(0)

try:
    data = r.json()
except Exception:
    log(f'  Kick {ch}: non-JSON response.')
    sys.exit(0)

if not isinstance(data, list) or not data:
    # Channel with no saved VODs: normal, we do not treat it as an error.
    log(f'  Kick {ch}: no saved VODs.')
    sys.exit(0)

# Already-cached IDs (to avoid duplicates).
try:
    with open(cached_ids_file, encoding='utf-8') as fh:
        cached = set(x.strip() for x in fh if x.strip())
except OSError:
    cached = set()

def fmt_dur(ms):
    try:
        s = int(ms) // 1000
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    if s >= 3600:
        return f'{s//3600}h {(s%3600)//60}m {s%60}s'
    if s >= 60:
        return f'{s//60}m {s%60}s'
    return f'{s}s'

added = 0
con = outcast_db.connect()
for item in data[:max_n]:
    if not isinstance(item, dict):
        continue
    # The ONGOING live stream appears with is_live=true; we discard it.
    if item.get('is_live'):
        continue
    vid_obj = item.get('video') or {}
    uuid = (vid_obj.get('uuid') or '').strip()
    if not uuid:
        continue
    if uuid in cached:
        continue
    # Deleted/private VODs: skip.
    if vid_obj.get('deleted_at') or vid_obj.get('is_private'):
        continue

    title = (item.get('session_title') or '').replace('\t', ' ').replace('\n', ' ').strip() or uuid
    dur_h = fmt_dur(item.get('duration')) or '?'
    # Date 'YYYY-MM-DD HH:MM:SS' -> 'YYYY-MM-DD'.
    raw_date = (item.get('created_at') or item.get('start_time') or '').strip()
    date_iso = raw_date.split(' ')[0].split('T')[0] if raw_date else ''
    if not date_iso:
        import datetime
        date_iso = datetime.date.today().isoformat()
    thumb = ((item.get('thumbnail') or {}).get('src') or '').strip()
    wurl = f'https://kick.com/{ch}/videos/{uuid}'

    outcast_db.upsert(con, {
        'vid_id': uuid, 'title': title, 'channel': ch, 'channel_id': '',
        'date': date_iso, 'duration': dur_h, 'source': 'kick_vod', 'url': wurl,
    })
    with open(cached_ids_file, 'a', encoding='utf-8') as fh:
        fh.write(uuid + '\n')
    with open(all_new_file, 'a', encoding='utf-8') as fh:
        fh.write(uuid + '\n')
    if thumb and thumb not in ('NA', 'None'):
        with open(kk_thumbs_file, 'a', encoding='utf-8') as fh:
            fh.write(f'{uuid}\t{thumb}\n')
    cached.add(uuid)
    added += 1
    log(f'  Kick {ch}: + VOD {uuid} ({dur_h})')

con.close()
log(f'  Kick {ch}: +{added} new VOD(s) added.')
PYEOF

        after_n=$(wc -l < "$all_new_ids_file" 2>/dev/null | tr -d ' '); after_n="${after_n:-0}"

    done < "$KICK_SUBS"
}

run_crawler() {
    exec 9>"$CRAWL_LOCK"
    if ! flock -n 9; then
        log_msg "run_crawler: another instance already running (lock held). Exiting."
        return 0
    fi

    log_msg "========== Crawl started =========="
    : > "$CRAWL_ERR_FILE"   # fresh errors for this pass

    # Which sources are active?
    local yt_enabled="false" tw_enabled="false" kk_enabled="false"
    local yt_total=0 tw_total=0 kk_total=0
    if source_enabled youtube && [[ -f "$SUBS" ]]; then
        yt_total=$(grep -cvE '^\s*(#|$)' "$SUBS" 2>/dev/null || true)
        yt_total="${yt_total:-0}"
        [[ "$yt_total" -gt 0 ]] && yt_enabled="true"
    fi
    if source_enabled twitch && [[ "$TWITCH_VODS" == "true" ]] && [[ -f "$TWITCH_SUBS" ]]; then
        tw_total=$(grep -cvE '^\s*(#|$)' "$TWITCH_SUBS" 2>/dev/null || true)
        tw_total="${tw_total:-0}"
        [[ "$tw_total" -gt 0 ]] && tw_enabled="true"
    fi
    if source_enabled kick && [[ "$KICK_VODS" == "true" ]] && [[ -f "$KICK_SUBS" ]]; then
        kk_total=$(grep -cvE '^\s*(#|$)' "$KICK_SUBS" 2>/dev/null || true)
        kk_total="${kk_total:-0}"
        [[ "$kk_total" -gt 0 ]] && kk_enabled="true"
    fi

    if [[ "$yt_enabled" == "false" && "$tw_enabled" == "false" && "$kk_enabled" == "false" ]]; then
        log_msg "Nothing to do (no active subscriptions for the chosen sources)."
        status_write 'idle'
        flock -u 9; return 0
    fi

    local total=$(( yt_total + tw_total + kk_total ))

    ensure_db

    local cached_ids_file
    cached_ids_file=$(mktemp /tmp/outcast-cachedids-XXXX)
    cached_video_ids > "$cached_ids_file"
    local cached_count
    cached_count=$(wc -l < "$cached_ids_file" | tr -d ' ')
    log_msg "Incremental crawl. Cached IDs: $cached_count  |  YT: $yt_total  Twitch: $tw_total"

    local all_new_ids_file yt_thumb_ids_file
    all_new_ids_file=$(mktemp /tmp/outcast-allnewids-XXXX)
    yt_thumb_ids_file=$(mktemp /tmp/outcast-ytthumbids-XXXX)

    local count=0

    if [[ "$yt_enabled" == "true" ]]; then
    while IFS= read -r url; do
        [[ -z "$url" || "$url" =~ ^[[:space:]]*# ]] && continue
        count=$(( count + 1 ))

        status_write "running:${count}:${total}"
        log_msg "Channel $count/$total: $url"

        local channel_id=""
        if printf '%s' "$url" | grep -q '/channel/UC'; then
            channel_id=$(printf '%s' "$url" | grep -oE 'UC[A-Za-z0-9_-]{22}' | head -1)
        else
            channel_id=$(resolve_channel_id "$url")
        fi

        if [[ -z "$channel_id" ]]; then
            log_msg "  Could not resolve channel ID — skipping."
            continue
        fi

        local rssfile
        rssfile=$(mktemp /tmp/outcast-rss-XXXX)
        fetch_rss "$channel_id" > "$rssfile"

        local rss_line_count
        rss_line_count=$(wc -l < "$rssfile" | tr -d ' ')
        log_msg "  RSS returned $rss_line_count entries."
        if [[ "$rss_line_count" -eq 0 ]]; then
            log_msg "  Empty RSS feed — skipping channel."
            rm -f "$rssfile"
            continue
        fi

        # From the RSS, derive three things:
        #   - new_ids : IDs not yet in the cache (get a full insert w/ duration)
        #   - rss_map : vid_id -> "title\tchannel\tdate" (for the new-video loop)
        #   - refresh : full rows for videos ALREADY cached but still in the RSS,
        #               so an upstream rename/edit refreshes the stored metadata.
        #               Duration is left as '?' (upsert preserves the known one)
        #               and channel_id is filled in (also backfills old rows).
        local new_ids_file rss_map_file refresh_file
        new_ids_file=$(mktemp /tmp/outcast-newids-XXXX)
        rss_map_file=$(mktemp /tmp/outcast-rssmap-XXXX)
        refresh_file=$(mktemp /tmp/outcast-refresh-XXXX)

        python3 - "$rssfile" "$cached_ids_file" "$new_ids_file" "$rss_map_file" "$refresh_file" "$channel_id" << 'PYEOF'
import sys

rss_path     = sys.argv[1]
cached_path  = sys.argv[2]
out_ids      = sys.argv[3]
out_map      = sys.argv[4]
out_refresh  = sys.argv[5]
channel_id   = sys.argv[6] if len(sys.argv) > 6 else ''

cached = set()
try:
    with open(cached_path) as fh:
        for line in fh:
            vid = line.strip()
            if vid:
                cached.add(vid)
except FileNotFoundError:
    pass

new_ids = []
rss_map = {}       # vid_id -> "title\tchannel\tdate"
refresh_rows = []  # full 8-col rows for already-cached videos
try:
    with open(rss_path) as fh:
        for line in fh:
            parts = line.rstrip('\n').split('\t')
            if len(parts) < 4:
                continue
            vid = parts[0].strip()
            if not vid:
                continue
            title, channel, date = parts[1], parts[2], parts[3]
            rss_map[vid] = f'{title}\t{channel}\t{date}'
            if vid in cached:
                url = f'https://www.youtube.com/watch?v={vid}'
                refresh_rows.append(
                    f'{vid}\t{title}\t{channel}\t{date}\t?\tyoutube\t{url}\t{channel_id}'
                )
            else:
                new_ids.append(vid)
except FileNotFoundError:
    pass

with open(out_ids, 'w') as fh:
    if new_ids:
        fh.write('\n'.join(new_ids) + '\n')

with open(out_map, 'w') as fh:
    for vid, meta in rss_map.items():
        fh.write(f'{vid}\t{meta}\n')

with open(out_refresh, 'w') as fh:
    if refresh_rows:
        fh.write('\n'.join(refresh_rows) + '\n')
PYEOF

        # Refresh metadata of already-known videos in one batch (one Python
        # process, one transaction). Runs for every channel, even when there
        # are no brand-new videos.
        if [[ -s "$refresh_file" ]]; then
            local refresh_count
            refresh_count=$(wc -l < "$refresh_file" | tr -d ' ')
            db insert-batch "$refresh_file"
            log_msg "  Refreshed metadata for $refresh_count existing video(s)."
        fi

        local new_count
        new_count=$(wc -l < "$new_ids_file" | tr -d ' ')
        log_msg "  New videos to fetch: $new_count"

        if [[ "$new_count" -eq 0 ]]; then
            log_msg "  No new videos for this channel."
            rm -f "$rssfile" "$new_ids_file" "$rss_map_file" "$refresh_file"
            continue
        fi

        cat "$new_ids_file" >> "$all_new_ids_file"
        cat "$new_ids_file" >> "$yt_thumb_ids_file"

        # For each new video: resolve duration, then immediately insert a
        # complete row into the DB so the UI picks it up on its next poll.
        local vid_num=0
        while IFS= read -r vid_id; do
            [[ -z "$vid_id" ]] && continue
            vid_num=$(( vid_num + 1 ))
            log_msg "    Duration $vid_num/$new_count: $vid_id"

            local dur
            dur=$(fetch_duration_single "$vid_id")

            # Look up title/channel/date from the map file
            local meta
            meta=$(grep -m1 "^${vid_id}"$'\t' "$rss_map_file" | cut -f2-)

            if [[ -n "$meta" ]]; then
                # Build the 8-column row (meta already carries the embedded
                # title<TAB>channel<TAB>date; channel_id is appended last) and
                # insert it into the DB. The UI sees the new row on its next
                # poll (<=400 ms).
                local row
                row=$(printf '%s\t%s\t%s\tyoutube\thttps://www.youtube.com/watch?v=%s\t%s' \
                      "$vid_id" "$meta" "$dur" "$vid_id" "$channel_id")
                db insert-line "$row"
                log_msg "    -> inserted into DB: $vid_id"
                # Also register in cached_ids so subsequent channels in this
                # same crawl don't re-add the same video.
                printf '%s\n' "$vid_id" >> "$cached_ids_file"
            else
                log_msg "    -> WARN: no RSS meta found for $vid_id, skipping"
            fi

            sleep 0.4
        done < "$new_ids_file"

        rm -f "$rssfile" "$new_ids_file" "$rss_map_file" "$refresh_file"

    done < "$SUBS"
    fi   # yt_enabled

    # -- Saved Twitch VODs -------------------------------------------------
    local tw_thumbs_file kk_thumbs_file
    tw_thumbs_file=$(mktemp /tmp/outcast-twthumbs-XXXX)
    kk_thumbs_file=$(mktemp /tmp/outcast-kkthumbs-XXXX)
    if [[ "$tw_enabled" == "true" ]]; then
        crawl_twitch_vods "$cached_ids_file" "$all_new_ids_file" \
                          "$tw_thumbs_file" "$count" "$total"
    fi
    # -- Saved Kick VODs ---------------------------------------------------
    if [[ "$kk_enabled" == "true" ]]; then
        crawl_kick_vods "$cached_ids_file" "$all_new_ids_file" "$kk_thumbs_file"
    fi

    log_msg "Downloading thumbnails for new videos…"
    # Twitch/Kick first (explicit URLs), and ONLY YouTube ids via ytimg.
    # (Passing Twitch/Kick ids to the YouTube download fetched a gray 120x90
    # placeholder from i.ytimg.com and covered the real thumbnail.)
    download_thumbnails_explicit "$tw_thumbs_file"
    download_thumbnails_explicit "$kk_thumbs_file"
    download_thumbnails "$yt_thumb_ids_file"
    rm -f "$tw_thumbs_file" "$kk_thumbs_file" "$yt_thumb_ids_file"

    # Final cleanup pass on the database: dedup is implicit (PK on id), and the
    # UI sorts by date, so only the date filter and the per-channel trim remain.
    db date-filter "${DATE_FROM:-}" "${DATE_TO:-}"
    db trim "$MAX_VIDEOS_PER_CHANNEL"

    rm -f "$cached_ids_file" "$all_new_ids_file"

    date +%s > "$LAST_UPDATE_FILE"

    local total_vids
    total_vids=$(db count)
    total_vids="${total_vids:-0}"

    status_write "done:${total_vids}"
    log_msg "Crawl finished. Total videos in DB: $total_vids"
    log_msg "========== Crawl done =========="

    flock -u 9
}

_setup_dirs() {
    mkdir -p "$CACHE_DIR" "$CACHE_IDS" "$CACHE_THUMBS" "$CONFIG_DIR"
    if [[ -f "$CRAWL_LOG" ]]; then
        local lines
        lines=$(wc -l < "$CRAWL_LOG" | tr -d ' ')
        if (( lines > 500 )); then
            local tmp_log
            tmp_log=$(mktemp "${CACHE_DIR}/.log_tmp_XXXX")
            tail -400 "$CRAWL_LOG" > "$tmp_log" && mv "$tmp_log" "$CRAWL_LOG"
        fi
    fi
}

case "${1:-}" in
    --crawl-only)
        _setup_dirs
        write_default_config
        load_settings
        apply_thumb_quality
        [[ -f "$CRAWLER_STATUS_FILE" ]] || status_write 'idle'
        exec 1>/dev/null
        run_crawler
        exit 0
        ;;
    --version|-V)
        printf 'outcast-worker %s\n' "$OUTCAST_WORKER_VERSION"
        exit 0
        ;;
    --help|-h)
        exit 0
        ;;
    *)
        exit 1
        ;;
esac
