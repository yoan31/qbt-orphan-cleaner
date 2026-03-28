# qbt_orphan_cleaner — Contexte projet

## Objectif
Script Python CLI qui détecte les fichiers/dossiers présents dans un répertoire de stockage **flat** (un seul niveau, pas de sous-dossiers) qui ne correspondent à aucun téléchargement actif dans qBittorrent, puis propose leur suppression de façon interactive.

## Fichier principal
`qbt_orphan_cleaner.py` — stdlib Python uniquement, aucune dépendance externe.

## Configuration (tête de fichier)
```python
QB_HOST     = "http://192.168.1.149"
QB_PORT     = 8090
QB_USER     = "admin"
QB_PASS     = "adminadmin"
STORAGE_DIR = "/mnt/downloads"

IGNORE_EXTENSIONS = {".!qB", ".parts", ".tmp"}
IGNORE_NAMES      = {".DS_Store", "Thumbs.db", "desktop.ini"}
```

## Architecture
- **`QBittorrentClient`** : HTTP client maison (urllib + CookieJar) qui s'authentifie via `/api/v2/auth/login` et interroge l'API qBittorrent v2.
- **`collect_known_files()`** : récupère tous les torrents via `/api/v2/torrents/info` + les fichiers de chaque torrent via `/api/v2/torrents/files?hash=<hash>`. Construit un `set` des **basenames** connus.
- **`scan_storage()`** : `os.scandir()` à un seul niveau sur `STORAGE_DIR`, filtre les extensions et noms ignorés.
- **`interactive_cleanup()`** : affiche les orphelins numérotés avec taille, propose suppression par numéro / tout / quitter. Confirmation `oui/non` avant toute suppression.
- **`_delete_entries()`** : `shutil.rmtree` pour les dossiers, `os.remove` pour les fichiers.

## Flux d'exécution
1. Login WebUI → 2. Collecte noms connus → 3. Scan disque → 4. Diff → 5. Rapport interactif

## Points d'attention / Limitations connues
- Le stockage est supposé **flat** : un seul niveau de profondeur. Si un torrent dépose un dossier dans `STORAGE_DIR`, seul le nom du dossier racine est comparé (pas son contenu interne).
- La comparaison est faite sur le **basename** uniquement (pas le chemin complet). Fonctionne bien en flat, à revoir si on passe en arborescence.
- Pas de mode dry-run explicite (à ajouter si besoin).
- Pas de log fichier (tout va sur stdout).

## Pistes d'évolution possibles
- `--dry-run` flag (lister sans possibilité de supprimer)
- `--config` pour externaliser la config dans un fichier `.ini` ou `.env`
- Export du rapport en CSV/JSON
- Support arborescence multi-niveaux (récursif)
- Mode non-interactif pour cron (ex: `--auto-delete` avec confirmation CLI)
