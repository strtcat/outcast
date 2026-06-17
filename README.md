# outcast

A feed reader and player for **YouTube**, **Twitch** and **Kick**, designed for Linux/Wayland (Hyprland) and a keyboard-centric workflow. It subscribes to channels, caches video metadata locally, and plays content with `mpv`.

The interface is built with **PySide6/Qt**, backed by two Bash *workers* that do the heavy lifting (feed crawling and live checks) in a decoupled way.

---

## Features

- **Three sources in a single feed**: YouTube videos (RSS), Twitch VODs and live streams, and Kick VODs and live streams. Each item carries a brand-colored badge (YouTube red, Twitch purple, Kick green) and live streams appear at the top with a "LIVE" indicator.
- **Vim-style keyboard navigation**, meant to be used without a mouse.
- **Integrated YouTube search**, with an option to subscribe to a result's channel from the context menu.
- **Configurable playback**: engine (`yt-dlp` or `streamlink`), mode (video/audio), resolution (from 144p up to 2160p) and audio quality, all editable in Settings.
- **Customizable command templates** per backend and mode, with placeholders (`$URL`, `$ID`, `$TITLE`, `$RES`, `$ABR`).
- **Optimized thumbnails** for a light cache: resized and recompressed on download, with three configurable quality levels.
- **Subscription management** from the interface itself, with separate tabs for YouTube, Twitch and Kick, and per-channel cache deletion.
- **Six color themes**: Catppuccin Mocha, Tokyo Night, Gruvbox Dark, Nord, Zed Dark and Rosé Pine.
- **Internationalization (i18n)** with runtime language switching (English and Spanish included; adding a language is just a JSON file).
- **SQLite metadata store** (WAL mode) under `~/.local/share/outcast`: fast indexed queries, atomic writes, and cheap per-channel trimming.
- **Incremental, decoupled updates**: the worker inserts videos into the database as it resolves them, and the interface polls for newly-added rows without blocking.

---

## Architecture

| Component            | Technology    | Role                                                            |
|----------------------|---------------|-----------------------------------------------------------------|
| `outcast.py`         | PySide6/Qt    | Interface: list, search, settings, playback.                    |
| `outcast-worker.sh`  | Bash + Python | Feed crawl (YouTube RSS, Twitch/Kick VODs) and thumbnails.       |
| `outcast-live.sh`    | Bash + Python | Twitch and Kick live checks.                                    |
| `outcast`            | Bash          | Launcher (open the app or update the cache headlessly).         |
| `i18n.py`            | Python        | Lightweight translation engine (JSON catalogs).                 |
| `outcast_db.py`      | Python        | SQLite metadata store, shared by the UI and the worker.         |

The interface and the workers are **decoupled**: the app polls the cache files for changes instead of receiving events. This keeps the UI responsive and allows headless cache updates.

---

## Dependencies

**Required**
- Python 3 with **PySide6**
- **mpv** (playback)
- **yt-dlp** (YouTube extraction and Kick live streams)
- **curl** (feed and thumbnail downloads)

**Recommended / optional**
- **streamlink** — Twitch live streams and VODs (without it, Twitch is disabled)
- **python-curl_cffi** — **required for Kick VODs** (Kick's API demands browser *impersonation*; without it, Kick VODs return 403)
- An image tool to optimize thumbnails (the first available is used): **ImageMagick** (`magick`/`convert`), **GraphicsMagick** (`gm`) or **ffmpeg**
- **libnotify** — desktop notifications

On Arch Linux:

```bash
sudo pacman -S pyside6 mpv yt-dlp curl streamlink python-curl_cffi imagemagick libnotify
```

---

## Installation

### Arch Linux (PKGBUILD)

A standard `PKGBUILD` is provided. From the repository root:

```bash
makepkg -si
```

This installs:

| File                                   | Destination                          |
|----------------------------------------|--------------------------------------|
| Launcher                               | `/usr/bin/outcast`                   |
| Program logic + workers + `i18n.py` + `outcast_db.py` | `/usr/lib/outcast/`     |
| Translation catalogs                   | `/usr/share/outcast/locales/`        |
| Desktop entry                          | `/usr/share/applications/`           |
| License                                | `/usr/share/licenses/outcast/`       |

Then launch it from your application menu or with `outcast`.

### Manual / from source

You can also run it straight from a checkout — the launcher and `i18n.py` look for their files next to themselves first:

```bash
src/outcast            # open the app
src/outcast --update   # update the cache headlessly
```

---

## Subscriptions

Channels are listed one per line (name or URL) in:

| Source   | File                              |
|----------|-----------------------------------|
| YouTube  | `~/.config/outcast/subscriptions` |
| Twitch   | `~/.config/outcast/twitch`        |
| Kick     | `~/.config/outcast/kick`          |

They can also be managed from **Settings → "Channels …" tabs** (add, remove and clear per-channel cache).

The config directory honors `$XDG_CONFIG_HOME` (falling back to `~/.config`).

---

## Keyboard shortcuts

| Key          | Action                                      |
|--------------|---------------------------------------------|
| `j` / `k`    | Move down / up in the list                  |
| `Enter`      | Play the selected video                     |
| `/`          | Filter / search (context-dependent)         |
| `Ctrl+U`     | Update the cache (and check live streams)   |
| `Ctrl+L`     | Check live streams                          |
| `Ctrl+F`     | YouTube search                              |
| `Ctrl+,`     | Settings                                    |
| `Ctrl+E`     | Error panel                                 |
| `Ctrl+Q`     | Quit                                        |

In the list, **right-click** opens a context menu (play, remove from cache; in search, subscribe to the channel).

---

## Language

Outcast ships with **English** and **Spanish**. Switch at any time in **Settings → General → Language**; the change applies immediately.

Adding a language requires no code changes:

1. Copy `locales/en.json` to `locales/<code>.json` (e.g. `locales/fr.json`).
2. Translate the values and set the `_meta` block (`code` and `name`).
3. The new language appears automatically in the Language selector.

Catalogs are searched in `$OUTCAST_LOCALES`, then next to `i18n.py`, then `/usr/share/outcast/locales`.

---

## Data paths

Paths honor the XDG base-directory variables (`$XDG_DATA_HOME`, `$XDG_CONFIG_HOME`, `$XDG_CACHE_HOME`) with the usual fallbacks. Durable metadata lives in a SQLite database under the **data** dir (it cannot be regenerated once a VOD disappears upstream); everything regenerable stays in the **cache** dir, so clearing `~/.cache` is always safe.

| Path                              | Contents                                       |
|-----------------------------------|------------------------------------------------|
| `~/.local/share/outcast/outcast.db` | Video metadata (SQLite, WAL mode)            |
| `~/.config/outcast/`              | Subscriptions and configuration                |
| `~/.cache/outcast-feed/`          | State, logs, `live.tsv`                        |
| `~/.cache/outcast-thumbs/`        | Thumbnails                                      |
| `~/.cache/outcast-channel-ids/`   | Resolved channel-ID cache                      |

The live-stream path (`live.tsv`) is the only feed data kept in the cache dir; it is volatile and regenerated on every live check.

---

## Notes on Kick

Kick protects its API behind Cloudflare and blocks requests that don't mimic a real browser. That's why **Kick VODs require `curl_cffi`**: the worker queries the public API (`/api/v2/channels/<channel>/videos`) with Chrome *impersonation*. If the dependency is missing, outcast warns in the error panel and leaves the other sources working normally. Kick **live streams** are detected with `yt-dlp` (which also takes advantage of `curl_cffi` when installed).

Many Kick channels don't keep VODs or delete them after a few days; in that case the list comes back empty and outcast simply shows no VODs for that channel (it's not an error).

---

## License

MIT. See [`LICENSE`](LICENSE).
