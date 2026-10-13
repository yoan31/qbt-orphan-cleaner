#!/usr/bin/env python3
"""
qbt_orphan_cleaner.py
---------------------
Detects files present in a storage directory that no longer correspond to any
active download in qBittorrent, then offers interactive deletion.

Dependencies: none (standard library only)
"""

import argparse
import csv
from dataclasses import dataclass
import json
import http.cookiejar
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


__version__ = "1.0.4"


class QbtError(Exception):
    """Error while communicating with qBittorrent or in configuration."""


def _load_env():
    """Load environment variables from a .env file located next to the script.

    The file is optional; missing or malformed lines are ignored.  Variables are
    added to `os.environ` only if they are not already defined, preserving any
    values the user may have set externally.
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

_load_env()


def _env_int(name: str, default: int) -> int:
    """Return an environment variable as ``int`` with a fallback to ``default``.

    ``default`` is returned when the variable is missing or cannot be cast to an integer.
    """
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default

# ─────────────────────────────────────────────
#  CONFIGURATION  (via .env or environment variables)
# ─────────────────────────────────────────────
QB_HOST = os.environ.get("QB_HOST", "http://localhost")
QB_PORT = int(os.environ.get("QB_PORT", "8080"))
QB_USER = os.environ.get("QB_USER", "admin")
QB_PASS = os.environ.get("QB_PASS", "adminadmin")
STORAGE_DIR = os.environ.get("STORAGE_DIR", "/mnt/downloads")
LAST_ACTIVITY_DAYS = _env_int("LAST_ACTIVITY_DAYS", 30)

IGNORE_EXTENSIONS = {".!qB", ".parts", ".tmp"}
IGNORE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "images", "template"}
# ─────────────────────────────────────────────


BASE_URL = f"{QB_HOST}:{QB_PORT}"

TRACKER_STATUS_LABELS = {
    0: "Disabled",
    1: "Not contacted yet",
    2: "Working",
    3: "Updating",
    4: "Not working",
}


@dataclass(frozen=True)
class DiskEntry:
    name: str
    path: str
    rel_path: str
    category: str
    is_dir: bool
    real_path: str


class QBittorrentClient:
    def __init__(self):
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

    def _post_response(self, path, data=None):
        url = f"{BASE_URL}{path}"
        payload = urllib.parse.urlencode(data or {}).encode()
        req = urllib.request.Request(url, data=payload)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.opener.open(req, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            raise QbtError(f"HTTP error {e.code} contacting qBittorrent: {e.reason}. Body: {body}")
        except urllib.error.URLError as e:
            raise QbtError(f"Unable to contact qBittorrent: {e}")

    def _post(self, path, data=None):
        _, body = self._post_response(path, data)
        return body

    def _get(self, path):
        url = f"{BASE_URL}{path}"
        req = urllib.request.Request(url)
        try:
            with self.opener.open(req, timeout=10) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            raise QbtError(f"HTTP error {e.code} during GET {path}: {e.reason}. Body: {body}")
        except urllib.error.URLError as e:
            raise QbtError(f"Request failed ({path}): {e}")

    def login(self):
        """Authenticate against the qBittorrent WebUI.

        qBittorrent <= 5.1 returns HTTP 200 with the plain text ``Ok.`` on
        success. qBittorrent >= 5.2 returns HTTP 204 with an empty body instead.
        Both responses establish the session through the cookie jar.
        """
        status, result = self._post_response(
            "/api/v2/auth/login",
            {"username": QB_USER, "password": QB_PASS},
        )
        if status != 204 and result.strip() != "Ok.":
            raise QbtError(f"Authentication failed (response: {result!r})")
        print("[OK] Connected to qBittorrent WebUI")

    def get_torrents(self):
        raw = self._get("/api/v2/torrents/info")
        return json.loads(raw)

    def get_trackers(self, torrent_hash):
        query = urllib.parse.urlencode({"hash": torrent_hash})
        raw = self._get(f"/api/v2/torrents/trackers?{query}")
        return json.loads(raw)

    def delete_torrent(self, torrent_hash, delete_files=True):
        self._post("/api/v2/torrents/delete", {
            "hashes": torrent_hash,
            "deleteFiles": "true" if delete_files else "false",
        })


def tracker_status_label(status):
    """Return a stable label for a qBittorrent tracker status."""
    if isinstance(status, int):
        return TRACKER_STATUS_LABELS.get(status, str(status))
    if isinstance(status, str):
        return status
    return "Unknown"


def tracker_is_working(tracker):
    """qBittorrent normally exposes Working with status code 2."""
    status = tracker.get("status")
    if status == 2:
        return True
    if isinstance(status, str) and status.strip().lower() == "working":
        return True
    return False


def check_torrent_trackers(client, torrents):
    """
    Return the torrents that have no tracker with the Working status.

    Each item contains the torrent name, its hash and the known trackers, so it
    can be reported from the CLI or the web interface. Callers that only care
    about a few torrents should filter ``torrents`` beforehand to avoid a
    per-torrent HTTP request for the whole library.
    """
    warnings = []
    for torrent in torrents:
        torrent_hash = torrent.get("hash", "")
        name = torrent.get("name", torrent_hash or "(unnamed torrent)")
        if not torrent_hash:
            warnings.append({
                "name": name,
                "hash": "",
                "state": torrent.get("state", ""),
                "reason": "missing hash",
                "trackers": [],
            })
            continue

        try:
            trackers = client.get_trackers(torrent_hash)
        except QbtError as e:
            warnings.append({
                "name": name,
                "hash": torrent_hash,
                "state": torrent.get("state", ""),
                "reason": str(e),
                "trackers": [],
            })
            continue

        if any(tracker_is_working(tracker) for tracker in trackers):
            continue

        warnings.append({
            "name": name,
            "hash": torrent_hash,
            "state": torrent.get("state", ""),
            "reason": "no Working tracker",
            "trackers": [
                {
                    "url": tracker.get("url", ""),
                    "status": tracker_status_label(tracker.get("status")),
                    "message": tracker.get("msg", ""),
                }
                for tracker in trackers
            ],
        })
    return warnings


def print_tracker_warnings(tracker_warnings):
    if not tracker_warnings:
        print("[OK] All torrents have at least one Working tracker")
        return

    print(f"[WARN] {len(tracker_warnings)} torrent(s) without a Working tracker:")
    for warning in tracker_warnings:
        print(f"  - {warning['name']} ({warning['reason']})")
        trackers = warning.get("trackers") or []
        if not trackers:
            continue
        for tracker in trackers:
            url = tracker.get("url") or "(tracker without URL)"
            status = tracker.get("status") or "Unknown"
            message = tracker.get("message") or ""
            suffix = f" — {message}" if message else ""
            print(f"      {status}: {url}{suffix}")


def format_timestamp(timestamp):
    if not timestamp:
        return "never"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(timestamp)))
    except (OSError, OverflowError, ValueError, TypeError):
        return str(timestamp)


def check_inactive_torrents(torrents, max_inactive_days=None):
    """
    Return the torrents whose last activity exceeds the configured threshold.

    qBittorrent exposes last_activity as a Unix timestamp. A value of 0 means no
    known activity and is therefore reported when the check is enabled.
    """
    days = LAST_ACTIVITY_DAYS if max_inactive_days is None else max_inactive_days
    if days <= 0:
        return []

    now = int(time.time())
    cutoff_seconds = days * 24 * 60 * 60
    inactive = []
    for torrent in torrents:
        try:
            last_activity = int(torrent.get("last_activity") or 0)
        except (TypeError, ValueError):
            last_activity = 0
        inactive_seconds = now - last_activity if last_activity else None
        if inactive_seconds is not None and inactive_seconds <= cutoff_seconds:
            continue

        inactive.append({
            "name": torrent.get("name", torrent.get("hash", "(unnamed torrent)")),
            "hash": torrent.get("hash", ""),
            "state": torrent.get("state", ""),
            "size": torrent.get("size", 0),
            "size_h": format_size(torrent.get("size", 0)),
            "last_activity": last_activity,
            "last_activity_h": format_timestamp(last_activity),
            "inactive_days": None if inactive_seconds is None else inactive_seconds // (24 * 60 * 60),
            "threshold_days": days,
        })
    return inactive


def print_inactive_torrent_warnings(inactive_torrents, max_inactive_days=None):
    days = LAST_ACTIVITY_DAYS if max_inactive_days is None else max_inactive_days
    if days <= 0:
        print("[INFO] Last activity check disabled")
        return

    if not inactive_torrents:
        print(f"[OK] No torrent inactive for more than {days} day(s)")
        return

    print(f"[WARN] {len(inactive_torrents)} torrent(s) inactive for more than {days} day(s):")
    for torrent in inactive_torrents:
        inactive_days = torrent.get("inactive_days")
        days = "never active" if inactive_days is None else f"{inactive_days} day(s)"
        print(f"  - {torrent['name']} — last activity: {torrent['last_activity_h']} ({days})")


def _storage_root_real():
    return os.path.realpath(STORAGE_DIR)


def _path_is_within(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _entry_from_direntry(entry, norm_storage, category=""):
    return DiskEntry(
        name=entry.name,
        path=entry.path,
        rel_path=os.path.relpath(entry.path, norm_storage),
        category=category,
        is_dir=entry.is_dir(follow_symlinks=False),
        real_path=os.path.realpath(entry.path),
    )


def _known_names_for_torrent(torrent):
    names = set()
    name = torrent.get("name", "")
    content_path = torrent.get("content_path", "").rstrip("/\\")
    if name:
        names.add(name)
    if content_path:
        names.add(os.path.basename(content_path))
    return names


def collect_known_files(client):
    """
    Return (known_by_scope, category_dirs, torrents).

    known_by_scope maps each scope to its known names:
      ""       – direct children of STORAGE_DIR
      "radarr" – direct children of the STORAGE_DIR/radarr subfolder

    A torrent's scope is derived from basename(save_path) only when that
    basename matches an existing local subfolder of STORAGE_DIR.
    """
    norm_storage = os.path.normpath(STORAGE_DIR)
    known_by_scope = {"": set()}
    category_dirs = {}

    torrents = client.get_torrents()
    print(f"[INFO] {len(torrents)} torrent(s) found in qBittorrent")

    for t in torrents:
        save_path = t.get("save_path", "").rstrip("/\\")
        scope = ""

        # Remote compatibility: only basename(save_path) is compared to the local mount.
        save_basename = os.path.basename(save_path) if save_path else ""
        if save_basename:
            local_cat = os.path.join(norm_storage, save_basename)
            if os.path.isdir(local_cat) and not os.path.islink(local_cat):
                scope = save_basename
                category_dirs[scope] = os.path.normpath(local_cat)

        known_by_scope.setdefault(scope, set()).update(_known_names_for_torrent(t))

    return known_by_scope, category_dirs, torrents


def scan_storage(directory, category_dirs):
    """
    Return the list of DirEntry candidates for orphan detection.
    Performs a 2-level scan:
      Level 0 – direct children of `directory`:
        - Hidden entries (name starting with '.') are ignored.
        - Entries in IGNORE_NAMES or with an IGNORE_EXTENSIONS extension are ignored.
        - Category directories (present in `category_dirs`) are not added: their
          contents are scanned instead (level 1).
      Level 1 – direct children of each category directory:
        - Same filters, no further recursion.
    Backward compatible: if `category_dirs` is empty, only level 0 is scanned.
    """
    if not os.path.isdir(directory):
        raise QbtError(f"Directory not found: {directory}")
    norm_storage = os.path.normpath(directory)
    category_map = dict(category_dirs) if isinstance(category_dirs, dict) else {
        os.path.basename(os.path.normpath(path)): os.path.normpath(path)
        for path in category_dirs
    }
    category_paths = {os.path.normpath(path): name for name, path in category_map.items()}

    def _should_skip(entry):
        if entry.name.startswith("."):
            return True
        if entry.name in IGNORE_NAMES:
            return True
        if entry.is_file(follow_symlinks=False):
            _, ext = os.path.splitext(entry.name)
            if ext in IGNORE_EXTENSIONS:
                return True
        return False

    entries = []
    with os.scandir(directory) as it:
        for entry in it:
            if _should_skip(entry):
                continue
            entry_path = os.path.normpath(entry.path)
            category = category_paths.get(entry_path)
            if entry.is_dir(follow_symlinks=False) and category:
                try:
                    with os.scandir(entry.path) as inner_it:
                        for inner in inner_it:
                            if not _should_skip(inner):
                                entries.append(_entry_from_direntry(inner, norm_storage, category))
                except OSError as e:
                    print(f"[WARN] Unable to scan {entry.path}: {e}")
                continue
            entries.append(_entry_from_direntry(entry, norm_storage))
    return entries


def entry_is_known(entry, known_by_scope):
    return entry.name in known_by_scope.get(entry.category or "", set())


def find_orphans(entries, known_by_scope):
    return [entry for entry in entries if not entry_is_known(entry, known_by_scope)]


def scan_orphans(client):
    known_by_scope, category_dirs, torrents = collect_known_files(client)
    tracker_warnings = check_torrent_trackers(client, torrents)
    inactive_torrents = check_inactive_torrents(torrents)
    entries = scan_storage(STORAGE_DIR, category_dirs)
    orphans = find_orphans(entries, known_by_scope)
    return {
        "known_by_scope": known_by_scope,
        "category_dirs": category_dirs,
        "torrents": torrents,
        "tracker_warnings": tracker_warnings,
        "inactive_torrents": inactive_torrents,
        "entries": entries,
        "orphans": orphans,
    }


def format_size(size_bytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} Po"


def _entry_path(entry):
    """Return the absolute filesystem path for a DiskEntry or DirEntry."""
    return entry.path


def _entry_name(entry):
    """Return the display name of a DiskEntry or DirEntry."""
    return entry.name


def _entry_is_dir(entry):
    """Return True if entry is a directory. Supports both os.DirEntry and DiskEntry."""
    is_dir = getattr(entry, "is_dir", False)
    if callable(is_dir):
        return is_dir(follow_symlinks=False)
    return bool(is_dir)


def _entry_rel_path(entry):
    if isinstance(entry, DiskEntry):
        return entry.rel_path
    return os.path.relpath(entry.path, os.path.normpath(STORAGE_DIR))


def export_report(orphans, path):
    """Export the orphan list to CSV or JSON depending on the extension of `path`."""
    ext = os.path.splitext(path)[1].lower()
    rows = [
        {
            "type": "dir" if _entry_is_dir(entry) else "file",
            "name": _entry_name(entry),
            "rel_path": _entry_rel_path(entry),
            "abs_path": _entry_path(entry),
            "size_bytes": size,
            "size_human": format_size(size),
            "category": getattr(entry, "category", ""),
        }
        for entry, size in orphans
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        if ext == ".json":
            json.dump(rows, f, ensure_ascii=False, indent=2)
        else:
            writer = csv.DictWriter(f, fieldnames=["type", "name", "rel_path", "abs_path", "size_bytes", "size_human", "category"])
            writer.writeheader()
            writer.writerows(rows)
    print(f"[INFO] Report exported → {path} ({len(rows)} entry/entries)")


def get_entry_size(entry):
    try:
        path = _entry_path(entry)
        if os.path.isfile(path) and not os.path.islink(path):
            return os.path.getsize(path)
        elif _entry_is_dir(entry):
            total = 0
            for dirpath, _, filenames in os.walk(path):
                for fn in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        pass
            return total
    except OSError:
        return 0


def interactive_cleanup(orphans):
    """
    Present the orphan files and offer deletion by choice.
    """
    print("\n" + "═" * 60)
    print(f"  {len(orphans)} ORPHAN file(s)/folder(s) detected")
    print("═" * 60)

    total_size = 0
    for i, (entry, size) in enumerate(orphans, 1):
        kind = "📁" if _entry_is_dir(entry) else "📄"
        rel = _entry_rel_path(entry)
        print(f"  [{i:>3}] {kind} {rel}  ({format_size(size)})")
        total_size += size

    print(f"\n  Total recoverable size: {format_size(total_size)}")
    print("═" * 60)

    print("\nOptions:")
    print("  [number]   → delete a specific file  (e.g. 1 3 5)")
    print("  [a]        → delete everything")
    print("  [q]        → quit without deleting anything")

    while True:
        choice = input("\nYour choice: ").strip().lower()

        if choice == "q":
            print("No deletion performed. Goodbye.")
            break

        elif choice == "a":
            confirm = input(f"⚠️  Delete all {len(orphans)} entries? (yes/no): ").strip().lower()
            if confirm in {"oui", "yes", "y"}:
                _delete_entries([e for e, _ in orphans])
            else:
                print("Cancelled.")

        else:
            # Selection by numbers separated by spaces
            try:
                indices = [int(x) - 1 for x in choice.split()]
                selected = []
                for idx in indices:
                    if 0 <= idx < len(orphans):
                        selected.append(orphans[idx][0])
                    else:
                        print(f"  [!] Number {idx + 1} invalid, ignored.")
                if selected:
                    names = ", ".join(_entry_name(e) for e in selected)
                    confirm = input(f"Delete: {names}? (yes/no): ").strip().lower()
                    if confirm in {"oui", "yes", "y"}:
                        _delete_entries(selected)
                    else:
                        print("Cancelled.")
            except ValueError:
                print("  [!] Unrecognized input, please try again.")


def _delete_entries(entries):
    root = _storage_root_real()
    for entry in entries:
        path = os.path.abspath(os.path.normpath(_entry_path(entry)))
        real_path = os.path.realpath(path)
        parent_real = os.path.realpath(os.path.dirname(path))
        if not _path_is_within(parent_real, root) or (not os.path.islink(path) and not _path_is_within(real_path, root)):
            print(f"  [✗] Deletion refused outside storage: {_entry_name(entry)}")
            continue
        if not os.path.lexists(path):
            print(f"  [✗] Not found, skipped: {_entry_name(entry)}")
            continue
        try:
            if _entry_is_dir(entry):
                if os.path.islink(path):
                    os.remove(path)
                else:
                    shutil.rmtree(path)
            else:
                os.remove(path)
            print(f"  [✓] Deleted: {_entry_name(entry)}")
        except OSError as e:
            print(f"  [✗] Deletion error {_entry_name(entry)}: {e}")


def save_env(config: dict):
    """Save the keys of `config` to the .env file.
    Existing keys are updated; new ones are appended."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = f.readlines()

    updated = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in config:
            new_lines.append(f"{key}={config[key]}\n")
            updated.add(key)
        else:
            new_lines.append(line)

    for key, val in config.items():
        if key not in updated:
            new_lines.append(f"{key}={val}\n")

    with open(env_path, "w") as f:
        f.writelines(new_lines)


def reload_config():
    """Re-read the .env file and update the module globals."""
    global QB_HOST, QB_PORT, QB_USER, QB_PASS, STORAGE_DIR, LAST_ACTIVITY_DAYS, BASE_URL
    for k in ("QB_HOST", "QB_PORT", "QB_USER", "QB_PASS", "STORAGE_DIR", "LAST_ACTIVITY_DAYS"):
        os.environ.pop(k, None)
    _load_env()
    QB_HOST = os.environ.get("QB_HOST", "http://localhost")
    QB_PORT = int(os.environ.get("QB_PORT", "8080"))
    QB_USER = os.environ.get("QB_USER", "admin")
    QB_PASS = os.environ.get("QB_PASS", "adminadmin")
    STORAGE_DIR = os.environ.get("STORAGE_DIR", "/mnt/downloads")
    LAST_ACTIVITY_DAYS = _env_int("LAST_ACTIVITY_DAYS", 30)
    BASE_URL = f"{QB_HOST}:{QB_PORT}"


def main():
    parser = argparse.ArgumentParser(description="qBittorrent Orphan Cleaner")
    parser.add_argument("--debug", action="store_true",
                        help="For each orphan, print the raw qBittorrent API data")
    parser.add_argument("--dry-run", action="store_true",
                        help="List the orphans without deleting anything")
    parser.add_argument("--auto-delete", action="store_true",
                        help="Delete all orphans without interaction (for cron)")
    parser.add_argument("--output", metavar="FILE",
                        help="Export the orphans to CSV or JSON (.csv / .json)")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  qBittorrent Orphan Cleaner  v{__version__}")
    print("=" * 60)
    print(f"\nAudited directory: {STORAGE_DIR}")
    print(f"WebUI            : {BASE_URL}\n")

    try:
        client = QBittorrentClient()
        client.login()

        scan = scan_orphans(client)
        known_by_scope = scan["known_by_scope"]
        category_dirs = scan["category_dirs"]
        torrents = scan["torrents"]
        entries = scan["entries"]
        orphan_entries = scan["orphans"]

        print_tracker_warnings(scan["tracker_warnings"])
        print_inactive_torrent_warnings(scan["inactive_torrents"])
        known_count = sum(len(names) for names in known_by_scope.values())
        print(f"[INFO] {known_count} name(s) referenced by the torrents")
        if category_dirs:
            cat_names = ", ".join(sorted(category_dirs))
            print(f"[INFO] {len(category_dirs)} category folder(s): {cat_names}")
    except QbtError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
    print(f"[INFO] {len(entries)} entry/entries found in {STORAGE_DIR}")

    orphans = [
        (entry, get_entry_size(entry))
        for entry in orphan_entries
    ]

    if args.debug:
        print("\n" + "─" * 60)
        print("  DEBUG MODE — analysis of detected orphans")
        print("─" * 60)
        for entry, _ in orphans:
            rel = _entry_rel_path(entry)
            print(f"\n[DEBUG] {rel!r}")
            print(f"  searched name : {_entry_name(entry)!r}")
            print(f"  scope         : {getattr(entry, 'category', '') or '(root)'}")
            matches = [
                t for t in torrents
                if _entry_name(entry).lower() in t.get("name", "").lower()
                or _entry_name(entry).lower() in t.get("content_path", "").lower()
                or _entry_name(entry).lower() in t.get("save_path", "").lower()
            ]
            if matches:
                print(f"  → {len(matches)} partial match(es) in the API:")
                for t in matches:
                    print(f"    name         = {t.get('name', '')!r}")
                    print(f"    content_path = {t.get('content_path', '')!r}")
                    print(f"    save_path    = {t.get('save_path', '')!r}")
                    item = os.path.basename(t.get("content_path", "").rstrip("/\\")) or t.get("name", "")
                    print(f"    extracted item = {item!r}")
            else:
                print("  → no match in the API (likely orphan)")
        print("\n" + "─" * 60)
        sys.exit(0)

    if not orphans:
        print("\n[✓] No orphan file. Storage is clean!")
        sys.exit(0)

    # Optional export before any action
    if args.output:
        export_report(orphans, args.output)

    # --dry-run: display only, no deletion
    if args.dry_run:
        print("\n" + "─" * 60)
        print("  DRY-RUN MODE — no deletion performed")
        print("─" * 60)
        total = 0
        for i, (entry, size) in enumerate(orphans, 1):
            kind = "📁" if _entry_is_dir(entry) else "📄"
            rel = _entry_rel_path(entry)
            print(f"  [{i:>3}] {kind} {rel}  ({format_size(size)})")
            total += size
        print(f"\n  Total recoverable size: {format_size(total)}")
        print("─" * 60)
        sys.exit(0)

    # --auto-delete: direct deletion without interaction
    if args.auto_delete:
        print(f"\n[AUTO] Deleting {len(orphans)} orphan(s)…")
        _delete_entries([e for e, _ in orphans])
        sys.exit(0)

    interactive_cleanup(orphans)


if __name__ == "__main__":
    main()
