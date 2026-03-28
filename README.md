# qBt Orphan Cleaner

Detects files and folders present in your qBittorrent storage directory that no longer correspond to any active torrent, and offers to delete them — either via an interactive CLI or a modern web interface.

## Features

- Connects to qBittorrent's WebUI API (v2)
- Supports category subdirectories (e.g. `radarr/`, `sonarr/`, `upload/`)
- Lazy size calculation — scan results appear instantly, sizes load in the background
- Web UI: select orphans, browse their contents, delete with confirmation
- Configuration panel in the web UI (no need to edit files manually)
- Zero external dependencies — pure Python stdlib

## Requirements

- Python 3.8+
- qBittorrent with WebUI enabled

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/qbt-orphan-cleaner.git
cd qbt-orphan-cleaner
cp .env.example .env   # then edit with your settings
```

**`.env` file:**
```env
QB_HOST=http://192.168.1.x
QB_PORT=8090
QB_USER=admin
QB_PASS=your_password
STORAGE_DIR=/mnt/downloads
WEB_PORT=8080
```

## Usage

### CLI

```bash
python3 qbt_orphan_cleaner.py
# or via the helper script (creates a venv automatically):
./run.sh
```

Options:
- `--debug` — for each detected orphan, prints raw qBittorrent API data to help diagnose false positives

### Web interface

```bash
./run_web.sh
# Open http://localhost:8080
```

The web interface lets you:
- Scan for orphans with one click
- Browse the contents of orphan folders before deleting
- Select/deselect individual items or all at once
- Edit the configuration without touching `.env` directly

## How it works

1. **Login** — authenticates against qBittorrent WebUI
2. **Collect known names** — fetches all torrents via `/api/v2/torrents/info`; builds a set of known basenames (`name` field + `basename(content_path)`)
3. **Scan storage** — lists direct children of `STORAGE_DIR`; also lists contents of detected category subdirectories (one level deep)
4. **Diff** — anything on disk whose name is not in the known set is flagged as an orphan
5. **Cleanup** — interactive deletion with size display and confirmation

> **Note:** Comparison is basename-only, which makes it work correctly with a remote qBittorrent instance whose paths differ from the local mount point.

## Configuration

All settings can be placed in a `.env` file next to the scripts, or set as environment variables.

| Variable | Default | Description |
|---|---|---|
| `QB_HOST` | `http://192.168.1.149` | qBittorrent host URL |
| `QB_PORT` | `8090` | qBittorrent WebUI port |
| `QB_USER` | `admin` | WebUI username |
| `QB_PASS` | `adminadmin` | WebUI password |
| `STORAGE_DIR` | `/mnt/downloads` | Root directory to scan |
| `WEB_PORT` | `8080` | Port for the web interface |

Files and folders always ignored during scan: `.!qB`, `.parts`, `.tmp`, `.DS_Store`, `Thumbs.db`, `desktop.ini`, `images`, `template`, and any hidden entry (name starts with `.`).
