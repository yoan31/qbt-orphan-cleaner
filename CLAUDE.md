# CLAUDE.md

## Project

`qbt_orphan_cleaner.py` — single-file Python CLI. Detects orphans in a flat qBittorrent storage dir and offers interactive deletion. Stdlib only, zero external deps.

## Running

```bash
python3 qbt_orphan_cleaner.py
```

Edit the config block at the top of `qbt_orphan_cleaner.py`:

```python
QB_HOST     = "http://192.168.1.149"
QB_PORT     = 8090
QB_USER     = "admin"
QB_PASS     = "adminadmin"
STORAGE_DIR = "/mnt/downloads"

IGNORE_EXTENSIONS = {".!qB", ".parts", ".tmp"}
IGNORE_NAMES      = {".DS_Store", "Thumbs.db", "desktop.ini"}
```

`BASE_URL` is derived as `f"{QB_HOST}:{QB_PORT}"`. Requires qBittorrent WebUI enabled.

## Lint / Syntax Check

```bash
python3 -m py_compile qbt_orphan_cleaner.py
python3 -m pylint qbt_orphan_cleaner.py
```

## Architecture

Pipeline: **Login → Collect known files → Scan storage → Diff → Interactive cleanup**

| Component | Role |
|---|---|
| `QBittorrentClient` | `urllib` + `CookieJar` HTTP client. Auth via `/api/v2/auth/login` |
| `collect_known_files()` | Fetches `/api/v2/torrents/info` + `/api/v2/torrents/files?hash=<hash>`; builds `set` of known basenames |
| `scan_storage()` | Single-level `os.scandir()` on `STORAGE_DIR`; filters `IGNORE_EXTENSIONS` / `IGNORE_NAMES` |
| `interactive_cleanup()` | Numbered orphan list with sizes; accepts numbers / `a` / `q`; confirm with `oui` |
| `_delete_entries()` | `shutil.rmtree` for dirs, `os.remove` for files |

## Key Constraints

- **Flat storage only**: one depth level. Root dir name compared, not internal contents.
- **Basename comparison**: correct for flat layout; needs rework for nested trees.
- No dry-run mode, no config file, no file logging (stdout only).

## Roadmap / Known Gaps

- `--dry-run` flag — list orphans without deleting
- `--config` — external `.ini` / `.env` config file
- `--auto-delete` — non-interactive mode for cron
- CSV/JSON report export
- Recursive multi-level storage support

<!-- caliber:managed:pre-commit -->
## Before Committing

Run `caliber refresh` before creating git commits to keep docs in sync with code changes.
After it completes, stage any modified doc files before committing:

```bash
caliber refresh && git add CLAUDE.md .claude/ .cursor/ .github/copilot-instructions.md AGENTS.md CALIBER_LEARNINGS.md 2>/dev/null
```
<!-- /caliber:managed:pre-commit -->

<!-- caliber:managed:learnings -->
## Session Learnings

Read `CALIBER_LEARNINGS.md` for patterns and anti-patterns learned from previous sessions.
These are auto-extracted from real tool usage — treat them as project-specific rules.
<!-- /caliber:managed:learnings -->
