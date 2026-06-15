#!/usr/bin/env python3
"""
qbt_orphan_cleaner.py
---------------------
Détecte les fichiers présents dans un répertoire de stockage qui ne correspondent
à aucun téléchargement actif dans qBittorrent, puis propose leur suppression interactive.

Dépendances : aucune (stdlib uniquement)
"""

import argparse
import csv
from dataclasses import dataclass
import json
import http.cookiejar
import io
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


__version__ = "1.0.2"


class QbtError(Exception):
    """Erreur de communication avec qBittorrent ou de configuration."""


def _load_env():
    """Charge les variables depuis un fichier .env situé à côté du script."""
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


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default

# ─────────────────────────────────────────────
#  CONFIGURATION  (via .env ou variables d'environnement)
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

    def _post(self, path, data=None):
        url = f"{BASE_URL}{path}"
        payload = urllib.parse.urlencode(data or {}).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.opener.open(req, timeout=10) as resp:
                return resp.read().decode()
        except urllib.error.URLError as e:
            raise QbtError(f"Impossible de contacter qBittorrent : {e}")

    def _get(self, path):
        url = f"{BASE_URL}{path}"
        req = urllib.request.Request(url)
        try:
            with self.opener.open(req, timeout=10) as resp:
                return resp.read().decode()
        except urllib.error.URLError as e:
            raise QbtError(f"Requête échouée ({path}) : {e}")

    def login(self):
        result = self._post("/api/v2/auth/login", {"username": QB_USER, "password": QB_PASS})
        if result.strip() != "Ok.":
            raise QbtError(f"Authentification échouée (réponse : {result!r})")
        print("[OK] Connecté à qBittorrent WebUI")

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
    """Retourne un libellé stable pour le status tracker qBittorrent."""
    if isinstance(status, int):
        return TRACKER_STATUS_LABELS.get(status, str(status))
    if isinstance(status, str):
        return status
    return "Unknown"


def tracker_is_working(tracker):
    """qBittorrent expose normalement Working avec le code 2."""
    status = tracker.get("status")
    if status == 2:
        return True
    if isinstance(status, str) and status.strip().lower() == "working":
        return True
    return False


def check_torrent_trackers(client, torrents):
    """
    Retourne les torrents qui n'ont aucun tracker avec le status Working.

    Chaque élément contient le nom du torrent, son hash et les trackers connus,
    afin de pouvoir le signaler côté CLI ou interface web.
    """
    warnings = []
    for torrent in torrents:
        torrent_hash = torrent.get("hash", "")
        name = torrent.get("name", torrent_hash or "(torrent sans nom)")
        if not torrent_hash:
            warnings.append({
                "name": name,
                "hash": "",
                "state": torrent.get("state", ""),
                "reason": "hash manquant",
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
            "reason": "aucun tracker Working",
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
        print("[OK] Tous les torrents ont au moins un tracker Working")
        return

    print(f"[AVERT] {len(tracker_warnings)} torrent(s) sans tracker Working :")
    for warning in tracker_warnings:
        print(f"  - {warning['name']} ({warning['reason']})")
        trackers = warning.get("trackers") or []
        if not trackers:
            continue
        for tracker in trackers:
            url = tracker.get("url") or "(tracker sans URL)"
            status = tracker.get("status") or "Unknown"
            message = tracker.get("message") or ""
            suffix = f" — {message}" if message else ""
            print(f"      {status}: {url}{suffix}")


def format_timestamp(timestamp):
    if not timestamp:
        return "jamais"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(timestamp)))
    except (OSError, OverflowError, ValueError, TypeError):
        return str(timestamp)


def check_inactive_torrents(torrents, max_inactive_days=None):
    """
    Retourne les torrents dont la dernière activité dépasse le seuil configuré.

    qBittorrent expose last_activity comme timestamp Unix. Une valeur 0 signifie
    aucune activité connue et est donc signalée lorsque le contrôle est actif.
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
            "name": torrent.get("name", torrent.get("hash", "(torrent sans nom)")),
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
        print("[INFO] Vérification dernière activité désactivée")
        return

    if not inactive_torrents:
        print(f"[OK] Aucun torrent inactif depuis plus de {days} jour(s)")
        return

    print(f"[AVERT] {len(inactive_torrents)} torrent(s) inactif(s) depuis plus de {days} jour(s) :")
    for torrent in inactive_torrents:
        inactive_days = torrent.get("inactive_days")
        days = "jamais actif" if inactive_days is None else f"{inactive_days} jour(s)"
        print(f"  - {torrent['name']} — dernière activité : {torrent['last_activity_h']} ({days})")


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
    Retourne (known_by_scope, category_dirs, torrents).

    known_by_scope mappe chaque portée à ses noms connus :
      ""       – enfants directs de STORAGE_DIR
      "radarr" – enfants directs du sous-dossier STORAGE_DIR/radarr

    La portée d'un torrent est déduite de basename(save_path) uniquement si ce
    basename correspond à un sous-dossier local existant de STORAGE_DIR.
    """
    norm_storage = os.path.normpath(STORAGE_DIR)
    known_by_scope = {"": set()}
    category_dirs = {}

    torrents = client.get_torrents()
    print(f"[INFO] {len(torrents)} torrent(s) trouvé(s) dans qBittorrent")

    for t in torrents:
        save_path = t.get("save_path", "").rstrip("/\\")
        scope = ""

        # Compatibilité remote : seul basename(save_path) est comparé au montage local.
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
    Retourne la liste des DirEntry candidates à la détection d'orphelins.
    Effectue un scan à 2 niveaux :
      Niveau 0 – enfants directs de `directory` :
        - Les entrées cachées (nom commençant par '.') sont ignorées.
        - Les entrées dans IGNORE_NAMES ou avec IGNORE_EXTENSIONS sont ignorées.
        - Les répertoires de catégorie (présents dans `category_dirs`) ne sont
          pas ajoutés : leur contenu est scanné à la place (niveau 1).
      Niveau 1 – enfants directs de chaque répertoire de catégorie :
        - Mêmes filtres, pas de récursion supplémentaire.
    Rétrocompatible : si `category_dirs` est vide, seul le niveau 0 est scanné.
    """
    if not os.path.isdir(directory):
        raise QbtError(f"Répertoire introuvable : {directory}")
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
                    print(f"[AVERT] Impossible de scanner {entry.path} : {e}")
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
    return entry.path


def _entry_name(entry):
    return entry.name


def _entry_is_dir(entry):
    is_dir = getattr(entry, "is_dir", False)
    if callable(is_dir):
        return is_dir(follow_symlinks=False)
    return bool(is_dir)


def _entry_rel_path(entry):
    if isinstance(entry, DiskEntry):
        return entry.rel_path
    return os.path.relpath(entry.path, os.path.normpath(STORAGE_DIR))


def export_report(orphans, path):
    """Exporte la liste des orphelins en CSV ou JSON selon l'extension de `path`."""
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
    print(f"[INFO] Rapport exporté → {path} ({len(rows)} entrée(s))")


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
    Présente les fichiers orphelins et propose suppression au choix.
    """
    print("\n" + "═" * 60)
    print(f"  {len(orphans)} fichier(s)/dossier(s) ORPHELIN(S) détecté(s)")
    print("═" * 60)

    total_size = 0
    for i, (entry, size) in enumerate(orphans, 1):
        kind = "📁" if _entry_is_dir(entry) else "📄"
        rel = _entry_rel_path(entry)
        print(f"  [{i:>3}] {kind} {rel}  ({format_size(size)})")
        total_size += size

    print(f"\n  Taille totale récupérable : {format_size(total_size)}")
    print("═" * 60)

    print("\nOptions :")
    print("  [numéro]   → supprimer un fichier spécifique  (ex: 1 3 5)")
    print("  [a]        → tout supprimer")
    print("  [q]        → quitter sans rien supprimer")

    while True:
        choice = input("\nVotre choix : ").strip().lower()

        if choice == "q":
            print("Aucune suppression effectuée. Au revoir.")
            break

        elif choice == "a":
            confirm = input(f"⚠️  Supprimer les {len(orphans)} entrées ? (oui/non) : ").strip().lower()
            if confirm in {"oui", "yes", "y"}:
                _delete_entries([e for e, _ in orphans])
            else:
                print("Annulé.")

        else:
            # Sélection par numéros séparés par espaces
            try:
                indices = [int(x) - 1 for x in choice.split()]
                selected = []
                for idx in indices:
                    if 0 <= idx < len(orphans):
                        selected.append(orphans[idx][0])
                    else:
                        print(f"  [!] Numéro {idx + 1} invalide, ignoré.")
                if selected:
                    names = ", ".join(_entry_name(e) for e in selected)
                    confirm = input(f"Supprimer : {names} ? (oui/non) : ").strip().lower()
                    if confirm in {"oui", "yes", "y"}:
                        _delete_entries(selected)
                    else:
                        print("Annulé.")
            except ValueError:
                print("  [!] Entrée non reconnue, réessayez.")


def _delete_entries(entries):
    root = _storage_root_real()
    for entry in entries:
        path = os.path.abspath(os.path.normpath(_entry_path(entry)))
        real_path = os.path.realpath(path)
        parent_real = os.path.realpath(os.path.dirname(path))
        if not _path_is_within(parent_real, root) or (not os.path.islink(path) and not _path_is_within(real_path, root)):
            print(f"  [✗] Refus suppression hors stockage : {_entry_name(entry)}")
            continue
        if not os.path.lexists(path):
            print(f"  [✗] Introuvable, ignoré : {_entry_name(entry)}")
            continue
        try:
            if _entry_is_dir(entry):
                shutil.rmtree(path)
            else:
                os.remove(path)
            print(f"  [✓] Supprimé : {_entry_name(entry)}")
        except OSError as e:
            print(f"  [✗] Erreur suppression {_entry_name(entry)} : {e}")


def save_env(config: dict):
    """Sauvegarde les clés de `config` dans le fichier .env.
    Les clés existantes sont mises à jour ; les nouvelles sont ajoutées."""
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
    """Relit le .env et met à jour les globals du module."""
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
                        help="Pour chaque orphelin, affiche les données brutes de l'API qBittorrent")
    parser.add_argument("--dry-run", action="store_true",
                        help="Liste les orphelins sans rien supprimer")
    parser.add_argument("--auto-delete", action="store_true",
                        help="Supprime tous les orphelins sans interaction (pour cron)")
    parser.add_argument("--output", metavar="FILE",
                        help="Exporte les orphelins en CSV ou JSON (.csv / .json)")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  qBittorrent Orphan Cleaner  v{__version__}")
    print("=" * 60)
    print(f"\nRépertoire audité : {STORAGE_DIR}")
    print(f"WebUI             : {BASE_URL}\n")

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
        print(f"[INFO] {known_count} nom(s) référencé(s) par les torrents")
        if category_dirs:
            cat_names = ", ".join(sorted(category_dirs))
            print(f"[INFO] {len(category_dirs)} répertoire(s) de catégorie : {cat_names}")
    except QbtError as e:
        print(f"[ERREUR] {e}")
        sys.exit(1)
    print(f"[INFO] {len(entries)} entrée(s) trouvée(s) dans {STORAGE_DIR}")

    orphans = [
        (entry, get_entry_size(entry))
        for entry in orphan_entries
    ]

    if args.debug:
        print("\n" + "─" * 60)
        print("  MODE DEBUG — analyse des orphelins détectés")
        print("─" * 60)
        for entry, _ in orphans:
            rel = _entry_rel_path(entry)
            print(f"\n[DEBUG] {rel!r}")
            print(f"  nom recherché : {_entry_name(entry)!r}")
            print(f"  portée        : {getattr(entry, 'category', '') or '(racine)'}")
            matches = [
                t for t in torrents
                if _entry_name(entry).lower() in t.get("name", "").lower()
                or _entry_name(entry).lower() in t.get("content_path", "").lower()
                or _entry_name(entry).lower() in t.get("save_path", "").lower()
            ]
            if matches:
                print(f"  → {len(matches)} correspondance(s) partielle(s) dans l'API :")
                for t in matches:
                    print(f"    name         = {t.get('name', '')!r}")
                    print(f"    content_path = {t.get('content_path', '')!r}")
                    print(f"    save_path    = {t.get('save_path', '')!r}")
                    item = os.path.basename(t.get("content_path", "").rstrip("/\\")) or t.get("name", "")
                    print(f"    item extrait = {item!r}")
            else:
                print("  → aucune correspondance dans l'API (orphelin probable)")
        print("\n" + "─" * 60)
        sys.exit(0)

    if not orphans:
        print("\n[✓] Aucun fichier orphelin. Le stockage est propre !")
        sys.exit(0)

    # Export optionnel avant toute action
    if args.output:
        export_report(orphans, args.output)

    # --dry-run : afficher uniquement, sans supprimer
    if args.dry_run:
        print("\n" + "─" * 60)
        print("  MODE DRY-RUN — aucune suppression effectuée")
        print("─" * 60)
        total = 0
        for i, (entry, size) in enumerate(orphans, 1):
            kind = "📁" if _entry_is_dir(entry) else "📄"
            rel = _entry_rel_path(entry)
            print(f"  [{i:>3}] {kind} {rel}  ({format_size(size)})")
            total += size
        print(f"\n  Taille totale récupérable : {format_size(total)}")
        print("─" * 60)
        sys.exit(0)

    # --auto-delete : suppression directe sans interaction
    if args.auto_delete:
        print(f"\n[AUTO] Suppression de {len(orphans)} orphelin(s)…")
        _delete_entries([e for e, _ in orphans])
        sys.exit(0)

    interactive_cleanup(orphans)


if __name__ == "__main__":
    main()
