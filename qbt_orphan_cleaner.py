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
import json
import http.cookiejar
import io
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request


__version__ = "1.0.0"


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

# ─────────────────────────────────────────────
#  CONFIGURATION  (via .env ou variables d'environnement)
# ─────────────────────────────────────────────
QB_HOST = os.environ.get("QB_HOST", "http://localhost")
QB_PORT = int(os.environ.get("QB_PORT", "8080"))
QB_USER = os.environ.get("QB_USER", "admin")
QB_PASS = os.environ.get("QB_PASS", "adminadmin")
STORAGE_DIR = os.environ.get("STORAGE_DIR", "/mnt/downloads")

IGNORE_EXTENSIONS = {".!qB", ".parts", ".tmp"}
IGNORE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "images", "template"}
# ─────────────────────────────────────────────


BASE_URL = f"{QB_HOST}:{QB_PORT}"


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


def collect_known_files(client):
    """
    Retourne (known_names, category_dirs) :
      known_names   – set plat de tous les basenames gérés par qBittorrent,
                      toutes catégories confondues.
      category_dirs – set de chemins absolus locaux des sous-répertoires de
                      STORAGE_DIR qui correspondent à un save_path qBittorrent.
                      Ces dossiers sont scannés en profondeur par scan_storage.

    Comparaison par basename uniquement (pas par chemin absolu) : fonctionne
    que qBittorrent soit local ou distant.
    """
    norm_storage = os.path.normpath(STORAGE_DIR)
    known_names = set()
    category_dirs = set()

    torrents = client.get_torrents()
    print(f"[INFO] {len(torrents)} torrent(s) trouvé(s) dans qBittorrent")

    for t in torrents:
        save_path = t.get("save_path", "").rstrip("/\\")
        content_path = t.get("content_path", "").rstrip("/\\")
        name = t.get("name", "")

        # Détection de catégorie : basename(save_path) est-il un sous-dossier existant de STORAGE_DIR ?
        save_basename = os.path.basename(save_path) if save_path else ""
        if save_basename:
            local_cat = os.path.join(norm_storage, save_basename)
            if os.path.isdir(local_cat):
                category_dirs.add(local_cat)

        # Nom du torrent (= nom du dossier racine pour les torrents multi-fichiers)
        if name:
            known_names.add(name)
        # Basename de content_path (= nom du fichier pour les single-file torrents)
        if content_path:
            known_names.add(os.path.basename(content_path))

    return known_names, category_dirs, torrents


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

    def _should_skip(entry):
        if entry.name.startswith("."):
            return True
        if entry.name in IGNORE_NAMES:
            return True
        if entry.is_file():
            _, ext = os.path.splitext(entry.name)
            if ext in IGNORE_EXTENSIONS:
                return True
        return False

    entries = []
    with os.scandir(directory) as it:
        for entry in it:
            if _should_skip(entry):
                continue
            if entry.is_dir() and os.path.normpath(entry.path) in category_dirs:
                try:
                    with os.scandir(entry.path) as inner_it:
                        for inner in inner_it:
                            if not _should_skip(inner):
                                entries.append(inner)
                except OSError as e:
                    print(f"[AVERT] Impossible de scanner {entry.path} : {e}")
                continue
            entries.append(entry)
    return entries


def format_size(size_bytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} Po"


def export_report(orphans, path):
    """Exporte la liste des orphelins en CSV ou JSON selon l'extension de `path`."""
    ext = os.path.splitext(path)[1].lower()
    norm_storage = os.path.normpath(STORAGE_DIR)
    rows = [
        {
            "type": "dir" if entry.is_dir() else "file",
            "name": entry.name,
            "rel_path": os.path.relpath(entry.path, norm_storage),
            "abs_path": entry.path,
            "size_bytes": size,
            "size_human": format_size(size),
        }
        for entry, size in orphans
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        if ext == ".json":
            json.dump(rows, f, ensure_ascii=False, indent=2)
        else:
            writer = csv.DictWriter(f, fieldnames=["type", "name", "rel_path", "abs_path", "size_bytes", "size_human"])
            writer.writeheader()
            writer.writerows(rows)
    print(f"[INFO] Rapport exporté → {path} ({len(rows)} entrée(s))")


def get_entry_size(entry):
    try:
        if entry.is_file(follow_symlinks=False):
            return entry.stat().st_size
        elif entry.is_dir(follow_symlinks=False):
            total = 0
            for dirpath, _, filenames in os.walk(entry.path):
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
    norm_storage = os.path.normpath(STORAGE_DIR)
    for i, (entry, size) in enumerate(orphans, 1):
        kind = "📁" if entry.is_dir() else "📄"
        rel = os.path.relpath(entry.path, norm_storage)
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
                    names = ", ".join(e.name for e in selected)
                    confirm = input(f"Supprimer : {names} ? (oui/non) : ").strip().lower()
                    if confirm in {"oui", "yes", "y"}:
                        _delete_entries(selected)
                    else:
                        print("Annulé.")
            except ValueError:
                print("  [!] Entrée non reconnue, réessayez.")


def _delete_entries(entries):
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.remove(entry.path)
            print(f"  [✓] Supprimé : {entry.name}")
        except OSError as e:
            print(f"  [✗] Erreur suppression {entry.name} : {e}")


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
    global QB_HOST, QB_PORT, QB_USER, QB_PASS, STORAGE_DIR, BASE_URL
    for k in ("QB_HOST", "QB_PORT", "QB_USER", "QB_PASS", "STORAGE_DIR"):
        os.environ.pop(k, None)
    _load_env()
    QB_HOST = os.environ.get("QB_HOST", "http://localhost")
    QB_PORT = int(os.environ.get("QB_PORT", "8080"))
    QB_USER = os.environ.get("QB_USER", "admin")
    QB_PASS = os.environ.get("QB_PASS", "adminadmin")
    STORAGE_DIR = os.environ.get("STORAGE_DIR", "/mnt/downloads")
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

        known_names, category_dirs, torrents = collect_known_files(client)
        print(f"[INFO] {len(known_names)} nom(s) référencé(s) par les torrents")
        if category_dirs:
            cat_names = ", ".join(sorted(os.path.basename(p) for p in category_dirs))
            print(f"[INFO] {len(category_dirs)} répertoire(s) de catégorie : {cat_names}")

        entries = scan_storage(STORAGE_DIR, category_dirs)
    except QbtError as e:
        print(f"[ERREUR] {e}")
        sys.exit(1)
    print(f"[INFO] {len(entries)} entrée(s) trouvée(s) dans {STORAGE_DIR}")

    orphans = [
        (entry, get_entry_size(entry))
        for entry in entries
        if entry.name not in known_names
    ]

    if args.debug:
        norm_storage = os.path.normpath(STORAGE_DIR)
        print("\n" + "─" * 60)
        print("  MODE DEBUG — analyse des orphelins détectés")
        print("─" * 60)
        for entry, _ in orphans:
            rel = os.path.relpath(entry.path, norm_storage)
            print(f"\n[DEBUG] {rel!r}")
            print(f"  nom recherché : {entry.name!r}")
            matches = [
                t for t in torrents
                if entry.name.lower() in t.get("name", "").lower()
                or entry.name.lower() in t.get("content_path", "").lower()
                or entry.name.lower() in t.get("save_path", "").lower()
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
        norm_storage = os.path.normpath(STORAGE_DIR)
        print("\n" + "─" * 60)
        print("  MODE DRY-RUN — aucune suppression effectuée")
        print("─" * 60)
        total = 0
        for i, (entry, size) in enumerate(orphans, 1):
            kind = "📁" if entry.is_dir() else "📄"
            rel = os.path.relpath(entry.path, norm_storage)
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
