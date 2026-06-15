# qbt_orphan_cleaner — Contexte projet

## Objectif
Script Python CLI + interface web qui détecte les fichiers/dossiers présents dans un répertoire de stockage qBittorrent, y compris un niveau de sous-dossiers de catégorie, qui ne correspondent à aucun téléchargement actif, puis propose leur suppression.

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
- **`collect_known_files()`** : récupère tous les torrents via `/api/v2/torrents/info`. Construit un index des **basenames** connus par portée : racine du stockage ou catégorie détectée via `basename(save_path)`.
- **`scan_storage()`** : `os.scandir()` sur `STORAGE_DIR`, puis un niveau dans les répertoires de catégorie détectés ; retourne des `DiskEntry` stables.
- **`scan_orphans()`** : point d'entrée commun CLI + web qui collecte qBittorrent, scanne le disque, applique la comparaison par portée et retourne les orphelins.
- **`interactive_cleanup()`** : affiche les orphelins numérotés avec taille, propose suppression par numéro / tout / quitter. Confirmation `oui/non` avant toute suppression.
- **`_delete_entries()`** : `shutil.rmtree` pour les dossiers, `os.remove` pour les fichiers.

## Flux d'exécution
1. Login WebUI → 2. Collecte noms connus par portée → 3. Scan disque → 4. Diff par portée → 5. Rapport interactif/web

## Points d'attention / Limitations connues
- Le scan reste limité à la racine + un niveau dans les catégories détectées ; pas de récursion complète.
- La comparaison est faite sur les **basenames par portée** pour rester compatible avec qBittorrent distant.
- Pas de log fichier (tout va sur stdout).

## Pistes d'évolution possibles
- `--dry-run` flag (lister sans possibilité de supprimer)
- `--config` pour externaliser la config dans un fichier `.ini` ou `.env`
- Export du rapport en CSV/JSON
- Support arborescence multi-niveaux (récursif)
- Mode non-interactif pour cron (ex: `--auto-delete` avec confirmation CLI)
