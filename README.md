# 🧹 qBt Orphan Cleaner

Detects files and folders present in your qBittorrent storage directory that no longer correspond to any active torrent, and offers to delete them — either via an interactive CLI or a modern web interface.

## ✨ Features

- 🔌 Connects to qBittorrent's WebUI API (v2)
- 📂 Supports category subdirectories (e.g. `radarr/`, `sonarr/`, `upload/`)
- ⚡ Lazy size calculation — scan results appear instantly, sizes load in the background
- 🖥️ Web UI: select orphans, browse their contents, delete with confirmation
- ⚙️ Configuration panel in the web UI (no need to edit files manually)
- 📦 Zero external dependencies — pure Python stdlib

## 📋 Requirements

- Python 3.8+
- qBittorrent with WebUI enabled

## 🚀 Setup

```bash
git clone https://github.com/yoan31/qbt-orphan-cleaner.git
cd qbt-orphan-cleaner
cp .env.example .env   # then edit with your settings
```

**`.env` file:**
```env
QB_HOST=http://192.168.1.x
QB_PORT=8080
QB_USER=admin
QB_PASS=your_password
STORAGE_DIR=/mnt/downloads
WEB_PORT=9090
```

> `QB_PORT=8080` is qBittorrent's default WebUI port. `WEB_PORT=9090` is the port for this tool's own web interface.

## 💻 Usage

### CLI

```bash
python3 qbt_orphan_cleaner.py
# or via the helper script (creates a venv automatically):
./run.sh
```

Options:
- `--debug` — for each detected orphan, prints raw qBittorrent API data to help diagnose false positives

### 🌐 Web interface

```bash
./run_web.sh
# Open http://localhost:9090
```

The web interface lets you:
- 🔍 Scan for orphans with one click
- 📁 Browse the contents of orphan folders before deleting
- ☑️ Select/deselect individual items or all at once
- 🗑️ Delete with confirmation modal
- ⚙️ Edit the configuration without touching `.env` directly

## 🔍 How it works

1. 🔐 **Login** — authenticates against qBittorrent WebUI
2. 📋 **Collect known names** — fetches all torrents via `/api/v2/torrents/info`; builds a set of known basenames (`name` field + `basename(content_path)`)
3. 🗄️ **Scan storage** — lists direct children of `STORAGE_DIR`; also lists contents of detected category subdirectories (one level deep)
4. 🔎 **Diff** — anything on disk whose name is not in the known set is flagged as an orphan
5. 🗑️ **Cleanup** — interactive deletion with size display and confirmation

> **Note:** Comparison is basename-only, which makes it work correctly with a remote qBittorrent instance whose paths differ from the local mount point.

## ⚙️ Configuration

All settings can be placed in a `.env` file next to the scripts, or set as environment variables.

| Variable | Default | Description |
|---|---|---|
| `QB_HOST` | `http://localhost` | qBittorrent host URL |
| `QB_PORT` | `8080` | qBittorrent WebUI port (qBittorrent default) |
| `QB_USER` | `admin` | WebUI username |
| `QB_PASS` | `adminadmin` | WebUI password |
| `STORAGE_DIR` | `/mnt/downloads` | Root directory to scan |
| `WEB_PORT` | `9090` | Port for this tool's web interface |

Files and folders always ignored during scan: `.!qB`, `.parts`, `.tmp`, `.DS_Store`, `Thumbs.db`, `desktop.ini`, `images`, `template`, and any hidden entry (name starts with `.`).

---

# 🇫🇷 Version française

Détecte les fichiers et dossiers présents dans votre répertoire de stockage qBittorrent qui ne correspondent plus à aucun torrent actif, et propose de les supprimer — via une interface CLI interactive ou une interface web moderne.

## ✨ Fonctionnalités

- 🔌 Connexion à l'API WebUI de qBittorrent (v2)
- 📂 Prise en charge des sous-dossiers de catégorie (ex. `radarr/`, `sonarr/`, `upload/`)
- ⚡ Calcul des tailles en arrière-plan — les résultats s'affichent immédiatement
- 🖥️ Interface web : sélection, exploration des dossiers, suppression avec confirmation
- ⚙️ Panneau de configuration intégré (sans éditer `.env` manuellement)
- 📦 Aucune dépendance externe — Python stdlib uniquement

## 📋 Prérequis

- Python 3.8+
- qBittorrent avec l'interface WebUI activée

## 🚀 Installation

```bash
git clone https://github.com/yoan31/qbt-orphan-cleaner.git
cd qbt-orphan-cleaner
cp .env.example .env   # puis éditer avec vos paramètres
```

**Fichier `.env` :**
```env
QB_HOST=http://192.168.1.x
QB_PORT=8080
QB_USER=admin
QB_PASS=votre_mot_de_passe
STORAGE_DIR=/mnt/downloads
WEB_PORT=9090
```

> `QB_PORT=8080` est le port WebUI par défaut de qBittorrent. `WEB_PORT=9090` est le port de l'interface web de cet outil.

## 💻 Utilisation

### CLI

```bash
python3 qbt_orphan_cleaner.py
# ou via le script helper (crée un venv automatiquement) :
./run.sh
```

Option :
- `--debug` — affiche les données brutes de l'API qBittorrent pour chaque orphelin détecté

### 🌐 Interface web

```bash
./run_web.sh
# Ouvrir http://localhost:9090
```

L'interface web permet de :
- 🔍 Lancer un scan en un clic
- 📁 Explorer le contenu des dossiers orphelins avant suppression
- ☑️ Sélectionner/désélectionner des éléments individuellement ou tous à la fois
- 🗑️ Supprimer avec une fenêtre de confirmation
- ⚙️ Modifier la configuration sans toucher au fichier `.env`

## 🔍 Fonctionnement

1. 🔐 **Connexion** — authentification auprès de la WebUI qBittorrent
2. 📋 **Collecte des noms connus** — récupère tous les torrents via `/api/v2/torrents/info` ; construit un ensemble de basenames connus (`name` + `basename(content_path)`)
3. 🗄️ **Scan du stockage** — liste les enfants directs de `STORAGE_DIR` ; explore aussi les sous-dossiers de catégorie détectés (un niveau de profondeur)
4. 🔎 **Différence** — tout ce qui est sur le disque et absent de l'ensemble connu est signalé comme orphelin
5. 🗑️ **Nettoyage** — suppression interactive avec affichage des tailles et confirmation

> **Note :** La comparaison se fait sur le basename uniquement, ce qui fonctionne correctement avec une instance qBittorrent distante dont les chemins diffèrent du point de montage local.

## ⚙️ Configuration

Tous les paramètres peuvent être placés dans un fichier `.env` à côté des scripts, ou définis comme variables d'environnement.

| Variable | Défaut | Description |
|---|---|---|
| `QB_HOST` | `http://localhost` | URL de l'hôte qBittorrent |
| `QB_PORT` | `8080` | Port WebUI de qBittorrent (valeur par défaut) |
| `QB_USER` | `admin` | Nom d'utilisateur WebUI |
| `QB_PASS` | `adminadmin` | Mot de passe WebUI |
| `STORAGE_DIR` | `/mnt/downloads` | Répertoire racine à analyser |
| `WEB_PORT` | `9090` | Port de l'interface web de cet outil |

Fichiers et dossiers toujours ignorés lors du scan : `.!qB`, `.parts`, `.tmp`, `.DS_Store`, `Thumbs.db`, `desktop.ini`, `images`, `template`, et toute entrée cachée (nom commençant par `.`).
