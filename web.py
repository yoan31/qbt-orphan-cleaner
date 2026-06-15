#!/usr/bin/env python3
"""
web.py — Interface web pour qBt Orphan Cleaner.
Démarrage : ./run_web.sh
"""

import base64
import hmac
import io
import json
import os
import shutil
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qbt_orphan_cleaner as _qbt

WEB_PORT = int(os.environ.get("WEB_PORT", "9090"))
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
__version__ = _qbt.__version__

MAX_JSON_BODY = 1_000_000
MAX_PATHS_PER_REQUEST = 1000


class WebError(Exception):
    """Erreur contrôlée renvoyable à l'interface web."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _is_loopback_host(host):
    return host in {"127.0.0.1", "::1", "localhost"}


def _storage_root_real():
    return os.path.realpath(_qbt.STORAGE_DIR)


def _path_is_within(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _safe_storage_path(path, *, allow_root=False):
    if not isinstance(path, str) or not path.strip():
        raise WebError("Chemin invalide")

    norm = os.path.abspath(os.path.normpath(path))
    root = _storage_root_real()

    if not allow_root and norm == root:
        raise WebError("Chemin racine non autorisé", 403)

    if os.path.islink(norm):
        parent = os.path.realpath(os.path.dirname(norm))
        if not _path_is_within(parent, root):
            raise WebError("Chemin non autorisé", 403)
        return norm

    real = os.path.realpath(norm)
    if not _path_is_within(real, root):
        raise WebError("Chemin non autorisé", 403)

    return real


def _entry_to_orphan(entry):
    return {
        "name": entry.name,
        "rel_path": entry.rel_path,
        "abs_path": entry.path,
        "real_path": entry.real_path,
        "is_dir": entry.is_dir,
        "size": -1,
        "size_human": "\u2026",
        "category": entry.category,
    }


def _orphan_sort_key(orphan):
    return (orphan["category"], not orphan["is_dir"], orphan["name"].lower())


def _current_orphan_realpaths():
    try:
        client = _qbt.QBittorrentClient()
        client.login()
        scan = _qbt.scan_orphans(client)
    except _qbt.QbtError as e:
        raise WebError(str(e), 502)
    current = {}
    for entry in scan["orphans"]:
        try:
            current[_safe_storage_path(entry.path)] = _entry_to_orphan(entry)
        except WebError:
            continue
    return current


# ── Logique métier ─────────────────────────────────────────────────────────────

def run_scan():
    """Lance le scan ; retourne les orphelins sans taille (calcul lazy) + stats qBt."""
    try:
        client = _qbt.QBittorrentClient()
        client.login()
        scan = _qbt.scan_orphans(client)
    except _qbt.QbtError as e:
        return {"error": str(e)}

    torrents = scan["torrents"]
    orphans = [_entry_to_orphan(entry) for entry in scan["orphans"]]
    orphans.sort(key=_orphan_sort_key)

    torrent_size = sum(t.get("size", 0) for t in torrents)
    download_speed = sum(t.get("dlspeed", 0) for t in torrents)
    upload_speed = sum(t.get("upspeed", 0) for t in torrents)
    active_torrents = sum(
        1 for t in torrents
        if t.get("dlspeed", 0) > 0 or t.get("upspeed", 0) > 0
    )
    try:
        usage = shutil.disk_usage(_qbt.STORAGE_DIR)
        disk = {
            "total": usage.total, "used": usage.used, "free": usage.free,
            "total_h": _qbt.format_size(usage.total),
            "used_h":  _qbt.format_size(usage.used),
            "free_h":  _qbt.format_size(usage.free),
            "pct": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        }
    except OSError:
        disk = None

    return {
        "orphans": orphans,
        "qbt_url": _qbt.BASE_URL,
        "storage_dir": _qbt.STORAGE_DIR,
        "torrent_count": len(torrents),
        "torrent_size": torrent_size,
        "torrent_size_h": _qbt.format_size(torrent_size),
        "active_torrent_count": active_torrents,
        "download_speed": download_speed,
        "download_speed_h": f"{_qbt.format_size(download_speed)}/s",
        "upload_speed": upload_speed,
        "upload_speed_h": f"{_qbt.format_size(upload_speed)}/s",
        "tracker_warnings": scan["tracker_warnings"],
        "inactive_torrents": scan["inactive_torrents"],
        "last_activity_days": _qbt.LAST_ACTIVITY_DAYS,
        "disk": disk,
    }


def compute_sizes(paths):
    """Calcule les tailles pour une liste de chemins absolus."""
    result = {}
    for path in paths:
        try:
            norm = _safe_storage_path(path)
            if os.path.isfile(norm) and not os.path.islink(norm):
                s = os.path.getsize(norm)
            elif os.path.isdir(norm) and not os.path.islink(norm):
                s = 0
                for dp, _, fns in os.walk(norm):
                    for fn in fns:
                        try:
                            s += os.path.getsize(os.path.join(dp, fn))
                        except OSError:
                            pass
            else:
                s = 0
        except OSError:
            s = 0
        except WebError:
            continue
        result[path] = {"size": s, "size_human": _qbt.format_size(s)}
    return result


def browse_dir(path):
    """Liste le contenu d'un répertoire. Retourne None si chemin non autorisé."""
    try:
        norm_path = _safe_storage_path(path, allow_root=True)
    except WebError:
        return None
    if os.path.islink(norm_path):
        return []
    if not os.path.isdir(norm_path):
        return []
    entries = []
    try:
        with os.scandir(norm_path) as it:
            items = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            entries = [
                {"name": e.name, "abs_path": e.path, "is_dir": e.is_dir(follow_symlinks=False)}
                for e in items
            ]
    except OSError:
        pass
    return entries


def do_delete(paths):
    """Supprime les chemins validés. Retourne {deleted, errors}."""
    deleted, errors = [], []
    try:
        current_orphans = _current_orphan_realpaths()
    except WebError as e:
        return {"deleted": deleted, "errors": [{"path": "", "error": str(e)}]}

    for path in paths:
        try:
            norm_path = _safe_storage_path(path)
        except WebError as e:
            errors.append({"path": path, "error": str(e)})
            continue
        if norm_path not in current_orphans:
            errors.append({"path": path, "error": "Chemin non orphelin"})
            continue
        try:
            norm_path = _safe_storage_path(path)
            if os.path.isdir(norm_path) and not os.path.islink(norm_path):
                shutil.rmtree(norm_path)
            else:
                os.remove(norm_path)
            deleted.append(path)
        except OSError as e:
            errors.append({"path": path, "error": str(e)})
    return {"deleted": deleted, "errors": errors}


def delete_torrent_with_files(torrent_hash):
    """Supprime un torrent dans qBittorrent avec ses fichiers associés."""
    if not isinstance(torrent_hash, str) or not torrent_hash.strip():
        return {"ok": False, "error": "Hash torrent manquant"}
    torrent_hash = torrent_hash.strip()
    try:
        client = _qbt.QBittorrentClient()
        client.login()
        torrents = client.get_torrents()
        inactive_hashes = {
            t.get("hash")
            for t in _qbt.check_inactive_torrents(torrents)
            if t.get("hash")
        }
        if torrent_hash not in inactive_hashes:
            return {"ok": False, "error": "Torrent non inactif ou introuvable"}
        client.delete_torrent(torrent_hash, delete_files=True)
        return {"ok": True, "hash": torrent_hash}
    except _qbt.QbtError as e:
        return {"ok": False, "error": str(e)}


def get_config():
    return {
        "QB_HOST": _qbt.QB_HOST,
        "QB_PORT": str(_qbt.QB_PORT),
        "QB_USER": _qbt.QB_USER,
        "QB_PASS": _qbt.QB_PASS,
        "STORAGE_DIR": _qbt.STORAGE_DIR,
        "LAST_ACTIVITY_DAYS": str(_qbt.LAST_ACTIVITY_DAYS),
        "WEB_PORT": str(WEB_PORT),
    }


def _validate_port(name, value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise WebError(f"{name} doit être un entier")
    if not 1 <= port <= 65535:
        raise WebError(f"{name} doit être entre 1 et 65535")
    return str(port)


def _validate_config(config):
    allowed = {"QB_HOST", "QB_PORT", "QB_USER", "QB_PASS", "STORAGE_DIR", "LAST_ACTIVITY_DAYS", "WEB_PORT"}
    filtered = {}
    for key, value in config.items():
        if key not in allowed:
            continue
        value = str(value).strip()
        if not value:
            continue
        if "\n" in value or "\r" in value:
            raise WebError(f"{key} contient un retour ligne interdit")
        filtered[key] = value

    if "QB_HOST" in filtered:
        parsed = urllib.parse.urlparse(filtered["QB_HOST"])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise WebError("QB_HOST doit être une URL http(s) valide")
        filtered["QB_HOST"] = filtered["QB_HOST"].rstrip("/")

    if "QB_PORT" in filtered:
        filtered["QB_PORT"] = _validate_port("QB_PORT", filtered["QB_PORT"])

    if "WEB_PORT" in filtered:
        filtered["WEB_PORT"] = _validate_port("WEB_PORT", filtered["WEB_PORT"])

    if "LAST_ACTIVITY_DAYS" in filtered:
        try:
            days = int(filtered["LAST_ACTIVITY_DAYS"])
        except ValueError:
            raise WebError("LAST_ACTIVITY_DAYS doit être un entier")
        if days < 0:
            raise WebError("LAST_ACTIVITY_DAYS doit être positif ou nul")
        filtered["LAST_ACTIVITY_DAYS"] = str(days)

    if "STORAGE_DIR" in filtered:
        storage = os.path.abspath(os.path.expanduser(filtered["STORAGE_DIR"]))
        if not os.path.isdir(storage):
            raise WebError("STORAGE_DIR doit être un répertoire existant")
        filtered["STORAGE_DIR"] = storage

    return filtered


def apply_config(config):
    filtered = _validate_config(config)
    _qbt.save_env(filtered)
    _qbt.reload_config()


def export_orphans(fmt):
    """Scan complet + calcul tailles, retourne (bytes, content_type, filename)."""
    data = run_scan()
    if "error" in data:
        return None, data["error"]

    orphans = data["orphans"]
    paths = [o["abs_path"] for o in orphans]
    sizes = compute_sizes(paths)
    for o in orphans:
        s = sizes.get(o["abs_path"], {})
        o["size"] = s.get("size", 0)
        o["size_human"] = s.get("size_human", "–")

    norm_storage = os.path.normpath(_qbt.STORAGE_DIR)

    if fmt == "json":
        rows = [
            {"type": "dir" if o["is_dir"] else "file",
             "name": o["name"], "rel_path": o["rel_path"],
             "abs_path": o["abs_path"],
             "size_bytes": o["size"], "size_human": o["size_human"],
             "category": o["category"]}
            for o in orphans
        ]
        content = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
        return content, "application/json", "orphans.json"
    else:
        buf = io.StringIO()
        import csv as _csv
        w = _csv.DictWriter(buf, fieldnames=["type","name","rel_path","abs_path","size_bytes","size_human","category"])
        w.writeheader()
        for o in orphans:
            w.writerow({"type": "dir" if o["is_dir"] else "file",
                        "name": o["name"], "rel_path": o["rel_path"],
                        "abs_path": o["abs_path"],
                        "size_bytes": o["size"], "size_human": o["size_human"],
                        "category": o["category"]})
        content = buf.getvalue().encode("utf-8")
        return content, "text/csv; charset=utf-8", "orphans.csv"


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>qBt Orphan Cleaner v""" + __version__ + """</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#090b10;
  --surface:#10131a;
  --card:#151a24;
  --card2:#1b2230;
  --border:#252c3a;
  --border-hi:#374151;
  --primary:#6366f1;
  --primary-dim:#6366f11a;
  --primary-glow:#6366f133;
  --danger:#ef4444;
  --success:#10b981;
  --warn:#f59e0b;
  --text:#f3f4f6;
  --text-2:#9ca3af;
  --text-3:#647084;
  --row-hover:#1a2130;
  --row-selected:#202548;
  --warning-bg:#18130b;
  --warning-card:#21180b;
  --warning-border:#5a3c0a;
  --warning-text:#f59e0b;
  --warning-soft:#fbbf24;
  --shadow:0 18px 45px rgba(0,0,0,.28);
  --r:8px;
  --r2:12px;
}
body[data-theme="light"]{
  --bg:#f4f6fb;
  --surface:#ffffff;
  --card:#ffffff;
  --card2:#f1f5f9;
  --border:#d9e0ea;
  --border-hi:#b8c3d4;
  --primary:#4f46e5;
  --primary-dim:#4f46e512;
  --primary-glow:#4f46e526;
  --danger:#dc2626;
  --success:#059669;
  --warn:#d97706;
  --text:#111827;
  --text-2:#475569;
  --text-3:#7c8798;
  --row-hover:#f1f5f9;
  --row-selected:#eef2ff;
  --warning-bg:#fff7ed;
  --warning-card:#ffffff;
  --warning-border:#fed7aa;
  --warning-text:#c2410c;
  --warning-soft:#ea580c;
  --shadow:0 18px 40px rgba(15,23,42,.10);
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;font-size:14px;line-height:1.5;transition:background .2s,color .2s}

/* ── Header ── */
header{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 28px;height:64px;
  background:var(--surface);
  border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:100;
  box-shadow:0 1px 0 rgba(0,0,0,.04);
}
.hd-brand{display:flex;align-items:center;gap:12px}
.hd-logo{font-size:22px;line-height:1;filter:drop-shadow(0 0 8px #6366f155)}
.hd-title{font-size:15px;font-weight:700;letter-spacing:-.01em}
.ver-pill{
  background:var(--primary-dim);border:1px solid var(--border-hi);
  color:var(--primary);border-radius:999px;
  padding:2px 10px;font-size:11px;font-weight:600;letter-spacing:.02em;
}
.hd-right{display:flex;align-items:center;gap:8px}
.conn-pill{
  display:flex;align-items:center;gap:7px;
  padding:5px 12px;border-radius:999px;font-size:12px;
  background:var(--card);border:1px solid var(--border);color:var(--text-2);
  transition:.2s;
}
.conn-dot{width:7px;height:7px;border-radius:50%;background:var(--text-3);flex-shrink:0;transition:all .4s}
.conn-dot.ok{background:var(--success);box-shadow:0 0 7px #10b98170}
.conn-dot.err{background:var(--danger);box-shadow:0 0 7px #ef444470}

/* ── Buttons ── */
.btn{
  display:inline-flex;align-items:center;gap:6px;
  padding:7px 15px;border-radius:var(--r);border:none;
  cursor:pointer;font-size:13px;font-weight:500;
  font-family:inherit;transition:all .15s;white-space:nowrap;
}
.btn:disabled{opacity:.3;cursor:not-allowed;box-shadow:none!important;transform:none!important}
.btn-primary{background:var(--primary);color:#fff;box-shadow:0 2px 12px var(--primary-glow)}
.btn-primary:hover:not(:disabled){background:#5254d4;transform:translateY(-1px);box-shadow:0 4px 18px var(--primary-glow)}
.btn-danger{background:var(--danger);color:#fff}
.btn-danger:hover:not(:disabled){background:#dc2626;transform:translateY(-1px)}
.btn-ghost{background:transparent;color:var(--text-2);border:1px solid var(--border)}
.btn-ghost:hover:not(:disabled){background:var(--card);border-color:var(--border-hi);color:var(--text)}
.btn-sm{padding:5px 11px;font-size:12px}

/* ── Dashboard ── */
.dash{
  max-width:1180px;margin:0 auto;width:100%;
  display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;
  padding:22px 28px 0;
}
.dash-card{
  background:var(--card);border:1px solid var(--border);
  border-radius:var(--r2);padding:18px 20px;
  display:flex;flex-direction:column;gap:12px;
  transition:border-color .2s,box-shadow .2s,transform .2s;
  box-shadow:var(--shadow);
}
.dash-card:hover{border-color:var(--border-hi);transform:translateY(-1px)}
.dc-header{display:flex;align-items:center;justify-content:space-between}
.dc-icon{
  width:34px;height:34px;border-radius:9px;
  display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;
}
.dc-icon.blue{background:var(--primary-dim);box-shadow:0 0 0 1px var(--border-hi);color:var(--primary)}
.dc-icon.green{background:#10b98118;box-shadow:0 0 0 1px #10b98140;color:var(--success)}
.dc-icon.red{background:#ef444418;box-shadow:0 0 0 1px #ef444440;color:var(--danger)}
.dc-icon.amber{background:#f59e0b18;box-shadow:0 0 0 1px #f59e0b40;color:var(--warn)}
.dc-icon.purple{background:#8b5cf618;box-shadow:0 0 0 1px #8b5cf640;color:#a78bfa}
.dc-icon.cyan{background:#06b6d418;box-shadow:0 0 0 1px #06b6d440;color:#22d3ee}
.dc-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:var(--text-3)}
.dc-body{display:flex;flex-direction:column;gap:6px}
.dc-stat{display:flex;align-items:baseline;gap:6px}
.dc-value{font-size:26px;font-weight:800;line-height:1;letter-spacing:-.02em;color:var(--text)}
.dc-value.danger{color:var(--danger)}
.dc-value.success{color:var(--success)}
.dc-unit{font-size:12px;color:var(--text-3)}
.dc-sub{font-size:11.5px;color:var(--text-2)}
/* disk bar */
.disk-bar{height:4px;border-radius:999px;background:var(--border);overflow:hidden;margin-top:2px}
.disk-fill{height:100%;border-radius:999px;background:var(--primary);transition:width .6s ease}
.disk-fill.warn{background:var(--warn)}
.disk-fill.danger{background:var(--danger)}
.dc-disk-row{display:flex;justify-content:space-between;font-size:11px;color:var(--text-3)}

/* orphan card spans full width */
.dash-card.orphan-card{
  grid-column:1/-1;
  flex-direction:row;align-items:center;gap:0;padding:14px 20px;
}
.orphan-stats{display:flex;gap:32px;flex:1}
.ostat{display:flex;flex-direction:column;gap:2px}
.ostat-value{font-size:22px;font-weight:800;line-height:1;color:var(--text)}
.ostat-value.danger{color:var(--danger)}
.ostat-label{font-size:11px;color:var(--text-3);font-weight:600;text-transform:uppercase;letter-spacing:.07em}
.orphan-actions{display:flex;align-items:center;gap:8px;flex-shrink:0}

/* ── Main ── */
main{max-width:1180px;margin:0 auto;padding:16px 28px 48px;width:100%}

/* States */
.state{
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:14px;padding:90px 20px;color:var(--text-3);text-align:center;
}
.state-ic{width:60px;height:60px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:26px}
.state-ic.spin{border:2px solid var(--border);border-top-color:var(--primary);animation:spin .8s linear infinite}
.state-ic.ok{background:#0a1c10;color:var(--success)}
.state-ic.err{background:#190a0a;color:var(--danger)}
.state h2{font-size:18px;color:var(--text);font-weight:700}
.state p{font-size:13px;max-width:260px}
@keyframes spin{to{transform:rotate(360deg)}}

/* Scan panels */
.scan-warnings{
  background:var(--warning-bg);border:1px solid var(--warning-border);color:var(--warning-text);
  border-radius:var(--r2);padding:14px;margin-bottom:14px;box-shadow:var(--shadow);
}
.scan-warnings.inactive{background:var(--warning-bg);border-color:var(--warning-border);color:var(--warning-text)}
.tw-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}
.tw-title{font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;color:var(--warning-soft)}
.tw-count{font-size:12px;color:var(--warning-text)}
.tw-list{display:grid;gap:8px}
.tw-item{background:var(--warning-card);border:1px solid var(--warning-border);border-radius:9px;padding:10px 12px}
.tw-row{display:flex;align-items:center;justify-content:space-between;gap:10px}
.tw-main{min-width:0;flex:1}
.tw-name{font-size:13px;font-weight:700;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tw-reason{font-size:11px;color:var(--warning-text);margin-top:2px}
.tw-size{display:grid;gap:1px;text-align:right;color:var(--text);white-space:nowrap}
.tw-size-label{font-size:9px;font-weight:800;color:var(--text-3);text-transform:uppercase;letter-spacing:.07em}
.tw-size-value{font-size:13px;font-weight:800;color:var(--warning-soft)}
.tw-actions{display:flex;align-items:center;gap:8px;flex-shrink:0}
.tw-trackers{margin-top:6px;display:grid;gap:3px}
.tw-tracker{font-size:11px;color:var(--warning-text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.btn-torrent-del{padding:5px 9px;font-size:11px}
.scan-warnings.inactive .tw-count{color:var(--warning-text)}
.scan-warnings.inactive .tw-name{color:var(--text)}
.scan-warnings.inactive .tw-reason{color:var(--warning-text)}

/* ── Table ── */
.table-wrap{
  background:var(--card);border:1px solid var(--border);
  border-radius:var(--r2);overflow:hidden;box-shadow:var(--shadow);
}
.table-bar{
  display:flex;align-items:center;justify-content:space-between;
  padding:10px 14px;border-bottom:1px solid var(--border);
  background:var(--surface);flex-wrap:wrap;gap:8px;
}
.t-left{display:flex;align-items:center;gap:10px;flex:1;min-width:0}
.t-right{display:flex;align-items:center;gap:8px;flex-shrink:0}
.sel-info{font-size:12px;color:var(--text-3);white-space:nowrap}
.filter-input{
  flex:1;max-width:220px;background:var(--bg);
  border:1px solid var(--border);border-radius:6px;
  color:var(--text);padding:5px 10px;font-size:12px;
  font-family:inherit;outline:none;transition:border .15s;
}
.filter-input:focus{border-color:var(--primary)}
.filter-input::placeholder{color:var(--text-3)}

input[type=checkbox]{width:15px;height:15px;accent-color:var(--primary);cursor:pointer;flex-shrink:0}

/* Category header */
.cat-hd{
  display:flex;align-items:center;gap:10px;
  padding:5px 14px;background:var(--bg);border-bottom:1px solid var(--border);
  font-size:10px;font-weight:800;color:var(--primary);
  letter-spacing:.1em;text-transform:uppercase;
}
.cat-hd::after{content:'';flex:1;height:1px;background:var(--border)}

/* Row */
.row{display:flex;align-items:center;border-bottom:1px solid var(--border);transition:background .1s}
.row:last-child{border-bottom:none}
.row:hover{background:var(--row-hover)}
.row.sel{background:var(--row-selected)}
.row.sel:hover{background:var(--row-selected)}
.c-cb{padding:12px 6px 12px 14px;flex-shrink:0}
.c-ic{padding:12px 6px;font-size:15px;flex-shrink:0;user-select:none}
.c-nm{flex:1;padding:10px 6px;min-width:0;cursor:pointer}
.c-nm .name{font-size:13px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500}
.c-nm .sub{font-size:11px;color:var(--text-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
.c-sz{padding:12px 10px;font-size:12px;color:var(--text-3);white-space:nowrap;text-align:right;flex-shrink:0;min-width:76px;font-variant-numeric:tabular-nums}
.c-exp{padding:10px 12px 10px 4px;flex-shrink:0}
.btn-exp{
  background:none;border:1px solid var(--border);color:var(--text-3);
  cursor:pointer;border-radius:5px;width:21px;height:21px;
  font-size:8px;display:flex;align-items:center;justify-content:center;
  transition:all .15s;flex-shrink:0;padding:0;
}
.btn-exp:hover{background:var(--border-hi);color:var(--text)}
.btn-exp.open{transform:rotate(90deg);border-color:var(--primary);color:var(--primary)}
.btn-exp:disabled{opacity:.3;cursor:default}

/* Browse panel */
.browse-panel{background:var(--bg);border-bottom:1px dashed var(--border)}
.bp-item{display:flex;align-items:center;gap:8px;padding:5px 14px 5px 50px;font-size:12px;color:var(--text-3);border-bottom:1px solid var(--bg)}
.bp-item:last-child{border-bottom:none}
.bp-nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}

/* Footer */
.table-foot{
  display:flex;justify-content:space-between;align-items:center;
  padding:9px 14px;background:var(--surface);border-top:1px solid var(--border);
  font-size:12px;color:var(--text-3);
}
.kbd{
  display:inline-flex;align-items:center;gap:3px;font-size:10px;color:var(--text-3);
}
.kbd kbd{
  background:var(--card2);border:1px solid var(--border-hi);border-radius:4px;
  padding:1px 5px;font-size:10px;font-family:monospace;color:var(--text-2);
}

/* Delete glow */
#btn-del:not(:disabled){animation:glow-r 2.4s ease-in-out infinite}
@keyframes glow-r{
  0%,100%{box-shadow:0 0 8px #ef444438,0 0 18px #ef444415}
  50%{box-shadow:0 0 16px #ef444460,0 0 30px #ef444422}
}

/* ── Modals ── */
.modal{position:fixed;inset:0;z-index:200;display:flex;align-items:center;justify-content:center;padding:16px}
.modal-bd{position:absolute;inset:0;background:rgba(0,0,0,.75);backdrop-filter:blur(6px)}
.modal-box{
  position:relative;background:var(--card);border:1px solid var(--border-hi);
  border-radius:var(--r2);padding:26px;max-width:440px;width:100%;
  max-height:82vh;overflow-y:auto;
  box-shadow:0 30px 80px #00000080;
  animation:slideUp .18s ease;
}
@keyframes slideUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
.modal-box h2{font-size:16px;font-weight:700;margin-bottom:8px}
.modal-desc{color:var(--text-2);font-size:13px;margin-bottom:16px}
.modal-files{background:var(--bg);border:1px solid var(--border);border-radius:7px;padding:6px 0;margin-bottom:20px;max-height:180px;overflow-y:auto}
.modal-file{display:flex;align-items:center;gap:8px;padding:5px 12px;font-size:12px;color:var(--text-2)}
.modal-actions{display:flex;justify-content:flex-end;gap:8px}

/* Config */
.cfg-fields{display:grid;gap:12px;margin-bottom:16px}
.cfg-field{display:grid;gap:5px}
.cfg-field label{font-size:10.5px;color:var(--text-3);font-weight:700;text-transform:uppercase;letter-spacing:.07em}
.cfg-field input{background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:8px 10px;font-size:13px;width:100%;font-family:inherit;outline:none;transition:border .15s}
.cfg-field input:focus{border-color:var(--primary)}
.cfg-note{font-size:11px;color:var(--text-3);background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin-bottom:16px}

/* Toast */
.toast{position:fixed;bottom:22px;right:22px;padding:12px 18px;border-radius:var(--r);font-size:13px;font-weight:500;z-index:300;max-width:320px;animation:fadeIn .2s ease;pointer-events:none}
.toast.ok{background:#091811;border:1px solid var(--success);color:#6ee7b7}
.toast.err{background:#190909;border:1px solid var(--danger);color:#fca5a5}
body[data-theme="light"] .toast.ok{background:#ecfdf5;color:#047857}
body[data-theme="light"] .toast.err{background:#fef2f2;color:#b91c1c}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}

.hidden{display:none!important}
@media(max-width:980px){
  .dash{grid-template-columns:1fr 1fr}
}
@media(max-width:680px){
  header,.dash,main{padding-left:14px;padding-right:14px}
  header{height:auto;min-height:64px;gap:10px;flex-wrap:wrap;padding-top:10px;padding-bottom:10px}
  .hd-right{width:100%;justify-content:flex-start;overflow-x:auto;padding-bottom:2px}
  .dash{grid-template-columns:1fr 1fr}
  .dash-card.orphan-card{grid-column:1/-1;flex-direction:column;align-items:flex-start;gap:14px}
  .tw-row{align-items:flex-start;flex-direction:column}
  .tw-actions{width:100%;justify-content:space-between}
  .tw-size{text-align:left}
  .table-foot .kbd{display:none}
}
@media(max-width:460px){
  .dash{grid-template-columns:1fr}
  .dc-value{font-size:24px}
}
</style>
</head>
<body>

<header>
  <div class="hd-brand">
    <span class="hd-logo">🧹</span>
    <span class="hd-title">qBt Orphan Cleaner</span>
    <span class="ver-pill">v""" + __version__ + """</span>
  </div>
  <div class="hd-right">
    <div class="conn-pill">
      <span class="conn-dot" id="dot"></span>
      <span id="status-txt">–</span>
    </div>
    <button class="btn btn-ghost" id="btn-theme" title="Toggle theme">☾ Dark</button>
    <button class="btn btn-ghost" id="btn-cfg">⚙ Config</button>
    <button class="btn btn-primary" id="btn-scan">↻ Scan</button>
  </div>
</header>

<!-- Dashboard (hidden until first scan) -->
<div class="dash hidden" id="dash">

  <!-- qBittorrent card -->
  <div class="dash-card">
    <div class="dc-header">
      <div class="dc-label">qBittorrent</div>
      <div class="dc-icon blue">⬇</div>
    </div>
    <div class="dc-body">
      <div class="dc-stat">
        <div class="dc-value" id="qbt-count">–</div>
        <div class="dc-unit">torrents</div>
      </div>
      <div class="dc-sub" id="qbt-size">–</div>
    </div>
  </div>

  <!-- Active torrents card -->
  <div class="dash-card">
    <div class="dc-header">
      <div class="dc-label">Active</div>
      <div class="dc-icon purple">▶</div>
    </div>
    <div class="dc-body">
      <div class="dc-stat">
        <div class="dc-value" id="active-count">–</div>
        <div class="dc-unit">active</div>
      </div>
      <div class="dc-sub" id="active-sub">–</div>
    </div>
  </div>

  <!-- Transfer card -->
  <div class="dash-card">
    <div class="dc-header">
      <div class="dc-label">Transfer</div>
      <div class="dc-icon cyan">↕</div>
    </div>
    <div class="dc-body">
      <div class="dc-stat">
        <div class="dc-value" id="dl-speed">–</div>
        <div class="dc-unit">down</div>
      </div>
      <div class="dc-sub" id="up-speed">–</div>
    </div>
  </div>

  <!-- Disk card -->
  <div class="dash-card">
    <div class="dc-header">
      <div class="dc-label">Storage</div>
      <div class="dc-icon green">💾</div>
    </div>
    <div class="dc-body">
      <div class="dc-stat">
        <div class="dc-value" id="disk-free">–</div>
        <div class="dc-unit">free</div>
      </div>
      <div class="disk-bar"><div class="disk-fill" id="disk-fill" style="width:0%"></div></div>
      <div class="dc-disk-row">
        <span id="disk-used-lbl">–</span>
        <span id="disk-pct-lbl">–</span>
      </div>
    </div>
  </div>

  <!-- Orphans card (full-width row) -->
  <div class="dash-card orphan-card">
    <div class="orphan-stats">
      <div class="ostat">
        <div class="ostat-value danger" id="ostat-count">0</div>
        <div class="ostat-label">Orphans</div>
      </div>
      <div class="ostat">
        <div class="ostat-value" id="ostat-size">–</div>
        <div class="ostat-label">Recoverable</div>
      </div>
      <div class="ostat">
        <div class="ostat-value" id="ostat-sel">0</div>
        <div class="ostat-label">Selected</div>
      </div>
    </div>
    <div class="orphan-actions">
      <button class="btn btn-ghost btn-sm" onclick="window.location='/api/export?format=csv'" title="Export CSV">&#8675; CSV</button>
      <button class="btn btn-ghost btn-sm" onclick="window.location='/api/export?format=json'" title="Export JSON">&#8675; JSON</button>
    </div>
  </div>
</div>

<main>
  <div id="tracker-warnings" class="scan-warnings hidden">
    <div class="tw-head">
      <div class="tw-title">Tracker check</div>
      <div class="tw-count" id="tw-count"></div>
    </div>
    <div class="tw-list" id="tw-list"></div>
  </div>
  <div id="activity-warnings" class="scan-warnings inactive hidden">
    <div class="tw-head">
      <div class="tw-title">Last activity</div>
      <div class="tw-count" id="aw-count"></div>
    </div>
    <div class="tw-list" id="aw-list"></div>
  </div>
  <!-- Loading -->
  <div id="v-loading" class="state">
    <div class="state-ic spin"></div>
    <h2>Scanning…</h2>
    <p>Connecting to qBittorrent and scanning storage.</p>
  </div>
  <!-- Clean -->
  <div id="v-empty" class="state hidden">
    <div class="state-ic ok">✓</div>
    <h2>Storage is clean</h2>
    <p>No orphan files detected.</p>
  </div>
  <!-- Error -->
  <div id="v-error" class="state hidden">
    <div class="state-ic err">✗</div>
    <h2>Connection error</h2>
    <p id="error-msg">Unable to reach qBittorrent.</p>
  </div>
  <!-- Results -->
  <div id="v-results" class="hidden">
    <div class="table-wrap">
      <div class="table-bar">
        <div class="t-left">
          <input type="checkbox" id="sel-all" title="Select all (Ctrl+A)">
          <input class="filter-input" id="filter-input" type="search" placeholder="Filter by name…">
          <span class="sel-info" id="sel-info"></span>
        </div>
        <div class="t-right">
          <button class="btn btn-danger" id="btn-del" disabled>🗑 Delete</button>
        </div>
      </div>
      <div id="orphan-list"></div>
      <div class="table-foot">
        <span id="ttl-count">–</span>
        <div class="kbd"><kbd>Esc</kbd> close &nbsp;·&nbsp; <kbd>Del</kbd> delete selection &nbsp;·&nbsp; <kbd>Ctrl A</kbd> select all</div>
        <span id="ttl-size">–</span>
      </div>
    </div>
  </div>
</main>

<!-- Confirm delete modal -->
<div class="modal hidden" id="modal">
  <div class="modal-bd" id="modal-bd"></div>
  <div class="modal-box">
    <h2>⚠ Confirm deletion</h2>
    <p class="modal-desc" id="modal-sum"></p>
    <div class="modal-files" id="modal-list"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" id="modal-cancel">Cancel</button>
      <button class="btn btn-danger" id="modal-ok">Delete permanently</button>
    </div>
  </div>
</div>

<!-- Config modal -->
<div class="modal hidden" id="cfg-modal">
  <div class="modal-bd" id="cfg-modal-bd"></div>
  <div class="modal-box" style="max-width:400px">
    <h2>⚙ Settings</h2>
    <p class="modal-desc">Saved to <code style="background:var(--bg);padding:1px 6px;border-radius:4px;font-size:11px;border:1px solid var(--border)">.env</code></p>
    <div class="cfg-fields">
      <div class="cfg-field"><label>qBittorrent Host</label><input id="cfg-QB_HOST" type="text" placeholder="http://192.168.1.x"></div>
      <div class="cfg-field"><label>Port</label><input id="cfg-QB_PORT" type="number" placeholder="8080"></div>
      <div class="cfg-field"><label>Username</label><input id="cfg-QB_USER" type="text" placeholder="admin"></div>
      <div class="cfg-field"><label>Password</label><input id="cfg-QB_PASS" type="password" placeholder="••••••••"></div>
      <div class="cfg-field"><label>Storage directory</label><input id="cfg-STORAGE_DIR" type="text" placeholder="/mnt/downloads"></div>
      <div class="cfg-field"><label>Inactive after days</label><input id="cfg-LAST_ACTIVITY_DAYS" type="number" min="0" placeholder="30"></div>
      <div class="cfg-field"><label>Web port</label><input id="cfg-WEB_PORT" type="number" placeholder="9090"></div>
    </div>
    <p class="cfg-note">ℹ Changing the web port requires a server restart.</p>
    <div class="modal-actions">
      <button class="btn btn-ghost" id="cfg-cancel">Cancel</button>
      <button class="btn btn-primary" id="cfg-save">✓ Save</button>
    </div>
  </div>
</div>

<div class="toast hidden" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let orphans = [];
let selected = new Set();
const rowMap = new Map();
const szMap  = new Map();
const browsePanels = new Map();
let browseOpen = new Set();

function applyTheme(theme) {
  const next = theme === 'light' ? 'light' : 'dark';
  document.body.dataset.theme = next;
  localStorage.setItem('qbt-theme', next);
  const btn = $('btn-theme');
  if (btn) btn.textContent = next === 'light' ? '☀ Light' : '☾ Dark';
}

applyTheme(localStorage.getItem('qbt-theme') || 'dark');

function fmtSize(b) {
  if (b < 0) return '…';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(1) + '\u00a0' + u[i];
}
function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function toast(msg, type, ms=3500) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast ' + type;
  clearTimeout(t._tid);
  t._tid = setTimeout(() => t.classList.add('hidden'), ms);
}

function selectedSize() {
  return orphans.filter(o => selected.has(o.abs_path))
                .reduce((s,o) => s + Math.max(0, o.size), 0);
}

function updateDash(data) {
  // qBt
  $('qbt-count').textContent = data.torrent_count >= 0 ? data.torrent_count : '–';
  $('qbt-size').textContent = data.torrent_count >= 0 ? data.torrent_size_h + ' managed' : 'unavailable';
  // activity
  $('active-count').textContent = data.active_torrent_count >= 0 ? data.active_torrent_count : '–';
  $('active-sub').textContent = data.torrent_count >= 0 ? 'of ' + data.torrent_count + ' torrents' : 'unavailable';
  $('dl-speed').textContent = data.download_speed_h || '–';
  $('up-speed').textContent = data.upload_speed_h ? data.upload_speed_h + ' up' : '–';
  // disk
  const d = data.disk;
  if (d) {
    $('disk-free').textContent = d.free_h;
    $('disk-used-lbl').textContent = d.used_h + ' used of ' + d.total_h;
    $('disk-pct-lbl').textContent = d.pct + '%';
    const fill = $('disk-fill');
    fill.style.width = d.pct + '%';
    fill.className = 'disk-fill' + (d.pct > 90 ? ' danger' : d.pct > 75 ? ' warn' : '');
  }
  // orphans
  $('ostat-count').textContent = data.orphans.length;
  $('ostat-size').textContent = '…';
  $('ostat-sel').textContent = 0;
  $('dash').classList.remove('hidden');
}

function renderTrackerWarnings(warnings) {
  const box = $('tracker-warnings');
  const list = $('tw-list');
  warnings = Array.isArray(warnings) ? warnings : [];
  list.innerHTML = '';

  if (warnings.length === 0) {
    box.classList.add('hidden');
    return;
  }

  $('tw-count').textContent = warnings.length + ' torrent(s) without Working tracker';
  warnings.forEach(w => {
    const item = document.createElement('div');
    item.className = 'tw-item';

    const name = document.createElement('div');
    name.className = 'tw-name';
    name.title = w.hash || '';
    name.textContent = w.name || '(unnamed torrent)';
    item.appendChild(name);

    const reason = document.createElement('div');
    reason.className = 'tw-reason';
    reason.textContent = w.reason || 'No Working tracker';
    item.appendChild(reason);

    const trackers = Array.isArray(w.trackers) ? w.trackers.slice(0, 4) : [];
    if (trackers.length > 0) {
      const trackerList = document.createElement('div');
      trackerList.className = 'tw-trackers';
      trackers.forEach(t => {
        const row = document.createElement('div');
        row.className = 'tw-tracker';
        const msg = t.message ? ' - ' + t.message : '';
        row.textContent = (t.status || 'Unknown') + ': ' + (t.url || '(no URL)') + msg;
        trackerList.appendChild(row);
      });
      item.appendChild(trackerList);
    }

    list.appendChild(item);
  });
  box.classList.remove('hidden');
}

function renderActivityWarnings(inactive, thresholdDays) {
  const box = $('activity-warnings');
  const list = $('aw-list');
  inactive = Array.isArray(inactive) ? inactive : [];
  list.innerHTML = '';

  if (thresholdDays <= 0 || inactive.length === 0) {
    box.classList.add('hidden');
    return;
  }

  $('aw-count').textContent = inactive.length + ' torrent(s) inactive for over ' + thresholdDays + ' days';
  inactive.forEach(t => {
    const item = document.createElement('div');
    item.className = 'tw-item';
    item.dataset.hash = t.hash || '';

    const row = document.createElement('div');
    row.className = 'tw-row';
    const main = document.createElement('div');
    main.className = 'tw-main';
    const name = document.createElement('div');
    name.className = 'tw-name';
    name.title = t.hash || '';
    name.textContent = t.name || '(unnamed torrent)';
    main.appendChild(name);

    const reason = document.createElement('div');
    reason.className = 'tw-reason';
    const age = t.inactive_days === null || t.inactive_days === undefined
      ? 'never active'
      : t.inactive_days + ' days inactive';
    reason.textContent = 'Last activity: ' + (t.last_activity_h || 'never') + ' - ' + age;
    main.appendChild(reason);

    const actions = document.createElement('div');
    actions.className = 'tw-actions';
    const size = document.createElement('div');
    size.className = 'tw-size';
    size.title = 'Recoverable space';
    const sizeLabel = document.createElement('div');
    sizeLabel.className = 'tw-size-label';
    sizeLabel.textContent = 'Recoverable';
    const sizeValue = document.createElement('div');
    sizeValue.className = 'tw-size-value';
    sizeValue.textContent = t.size_h || fmtSize(t.size || 0);
    size.appendChild(sizeLabel);
    size.appendChild(sizeValue);
    actions.appendChild(size);

    const del = document.createElement('button');
    del.className = 'btn btn-danger btn-torrent-del';
    del.type = 'button';
    del.textContent = 'Supprimer';
    del.disabled = !t.hash;
    del.title = 'Supprimer le torrent et ses fichiers';
    del.addEventListener('click', () => deleteInactiveTorrent(t, del));
    actions.appendChild(del);

    row.appendChild(main);
    row.appendChild(actions);
    item.appendChild(row);

    list.appendChild(item);
  });
  box.classList.remove('hidden');
}

async function deleteInactiveTorrent(torrent, btn) {
  const name = torrent.name || '(unnamed torrent)';
  const size = torrent.size_h || fmtSize(torrent.size || 0);
  if (!confirm('Supprimer le torrent "' + name + '" et ses fichiers associes ?\\nEspace recuperable: ' + size)) {
    return;
  }
  btn.disabled = true;
  btn.textContent = '...';
  try {
    const r = await fetch('/api/delete-torrent', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({hash: torrent.hash})
    });
    const data = await r.json();
    if (!data.ok) {
      toast(data.error || 'Torrent delete failed', 'err');
      btn.disabled = false;
      btn.textContent = 'Supprimer';
      return;
    }
    toast('Torrent supprime: ' + name, 'ok');
    doScan();
  } catch(e) {
    toast('Network error: ' + e.message, 'err');
    btn.disabled = false;
    btn.textContent = 'Supprimer';
  }
}

function updateKpi() {
  const n = selected.size;
  const anyUnknown = orphans.filter(o => selected.has(o.abs_path)).some(o => o.size < 0);
  const sz = selectedSize();
  $('ostat-sel').textContent = n;
}

function updateSel() {
  const n = selected.size;
  const anyUnknown = orphans.filter(o => selected.has(o.abs_path)).some(o => o.size < 0);
  const sz = selectedSize();
  const visibleCount = orphans.filter(o => !rowMap.get(o.abs_path)?.classList.contains('hidden')).length;
  $('sel-all').indeterminate = n > 0 && n < visibleCount;
  $('sel-all').checked = visibleCount > 0 && n >= visibleCount;
  const btn = $('btn-del');
  btn.disabled = n === 0;
  if (n > 0) {
    btn.textContent = anyUnknown
      ? '🗑 Delete (' + n + ')'
      : '🗑 Delete (' + fmtSize(sz) + ')';
  } else {
    btn.textContent = '🗑 Delete';
  }
  $('sel-info').textContent = n > 0
    ? n + ' selected' + (anyUnknown ? '' : ' · ' + fmtSize(sz))
    : '';
  rowMap.forEach((row, path) => row.classList.toggle('sel', selected.has(path)));
  updateKpi();
}

function togglePath(path) {
  if (selected.has(path)) selected.delete(path);
  else selected.add(path);
  const cb = rowMap.get(path)?.querySelector('input[type=checkbox]');
  if (cb) cb.checked = selected.has(path);
  updateSel();
}

function applyFilter() {
  const q = ($('filter-input')?.value || '').toLowerCase().trim();
  rowMap.forEach((row, path) => {
    const o = orphans.find(x => x.abs_path === path);
    if (!o) return;
    const match = !q || o.name.toLowerCase().includes(q) || o.rel_path.toLowerCase().includes(q);
    row.classList.toggle('hidden', !match);
  });
  // hide/show category headers
  document.querySelectorAll('.cat-hd').forEach(hd => {
    const cat = hd.dataset.cat;
    const anyVisible = orphans.filter(o => (o.category||'') === cat)
                              .some(o => !rowMap.get(o.abs_path)?.classList.contains('hidden'));
    hd.classList.toggle('hidden', !anyVisible);
  });
  updateSel();
}

function renderOrphans(data) {
  orphans = data.orphans;
  selected.clear();
  rowMap.clear();
  szMap.clear();
  browsePanels.clear();
  browseOpen.clear();

  $('dot').className = 'conn-dot ok';
  $('status-txt').textContent = 'Connected';
  $('ttl-count').textContent = orphans.length + ' orphan(s)';
  $('ttl-size').textContent = '…';
  $('ostat-count').textContent = orphans.length;
  $('ostat-size').textContent = '…';
  updateDash(data);
  renderTrackerWarnings(data.tracker_warnings);
  renderActivityWarnings(data.inactive_torrents, data.last_activity_days);

  const list = $('orphan-list');
  list.innerHTML = '';

  if (orphans.length === 0) { show('v-empty'); return; }

  const groups = {};
  orphans.forEach(o => { (groups[o.category || ''] ||= []).push(o); });
  const keys = Object.keys(groups).sort((a,b) => a===''?-1:b===''?1:a.localeCompare(b));

  keys.forEach(cat => {
    if (cat !== '') {
      const sep = document.createElement('div');
      sep.className = 'cat-hd';
      sep.dataset.cat = cat;
      sep.textContent = cat;
      list.appendChild(sep);
    }
    groups[cat].forEach(o => {
      const row = document.createElement('div');
      row.className = 'row';
      row.dataset.path = o.abs_path;
      const displayName = o.category ? o.name : o.rel_path;

      const cbDiv = document.createElement('div');
      cbDiv.className = 'c-cb';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.addEventListener('change', e => { e.stopPropagation(); togglePath(o.abs_path); });
      cbDiv.addEventListener('click', e => e.stopPropagation());
      cbDiv.appendChild(cb);

      const icDiv = document.createElement('div');
      icDiv.className = 'c-ic';
      icDiv.textContent = o.is_dir ? '📁' : '📄';

      const nmDiv = document.createElement('div');
      nmDiv.className = 'c-nm';
      const nameEl = document.createElement('div');
      nameEl.className = 'name';
      nameEl.title = o.rel_path;
      nameEl.textContent = displayName;
      nmDiv.appendChild(nameEl);
      if (o.category) {
        const subEl = document.createElement('div');
        subEl.className = 'sub';
        subEl.textContent = o.rel_path;
        nmDiv.appendChild(subEl);
      }

      const szDiv = document.createElement('div');
      szDiv.className = 'c-sz';
      szDiv.textContent = o.size_human;
      szMap.set(o.abs_path, szDiv);

      row.appendChild(cbDiv);
      row.appendChild(icDiv);
      row.appendChild(nmDiv);
      row.appendChild(szDiv);

      if (o.is_dir) {
        const expDiv = document.createElement('div');
        expDiv.className = 'c-exp';
        const expBtn = document.createElement('button');
        expBtn.className = 'btn-exp';
        expBtn.title = 'Browse folder';
        expBtn.textContent = '▶';
        expBtn.addEventListener('click', e => {
          e.stopPropagation();
          toggleBrowse(o.abs_path, expBtn, list, row);
        });
        expDiv.appendChild(expBtn);
        row.appendChild(expDiv);
      }

      nmDiv.addEventListener('click', () => togglePath(o.abs_path));
      icDiv.addEventListener('click', () => togglePath(o.abs_path));
      szDiv.addEventListener('click', () => togglePath(o.abs_path));

      rowMap.set(o.abs_path, row);
      list.appendChild(row);
    });
  });

  show('v-results');
  updateSel();
  fetchSizes();
}

function show(id) {
  ['v-loading','v-empty','v-error','v-results'].forEach(v => $(v).classList.add('hidden'));
  $(id).classList.remove('hidden');
}

// ── Lazy sizes ─────────────────────────────────────────────
async function fetchSizes() {
  if (orphans.length === 0) return;
  const paths = orphans.map(o => o.abs_path);
  try {
    const r = await fetch('/api/sizes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({paths})
    });
    const data = await r.json();
    let total = 0;
    orphans.forEach(o => {
      const s = data.sizes && data.sizes[o.abs_path];
      if (s !== undefined) {
        o.size = s.size;
        o.size_human = s.size_human;
        total += s.size;
        const el = szMap.get(o.abs_path);
        if (el) el.textContent = s.size_human;
      }
    });
    const fmt = fmtSize(total);
    $('ttl-size').textContent = fmt + ' recoverable';
    $('ostat-size').textContent = fmt;
    updateSel();
  } catch(_) { /* sizes unavailable */ }
}

// ── Scan ───────────────────────────────────────────────────
async function doScan() {
  show('v-loading');
  $('tracker-warnings').classList.add('hidden');
  $('activity-warnings').classList.add('hidden');
  $('btn-scan').disabled = true;
  try {
    const r = await fetch('/api/scan');
    const data = await r.json();
    if (data.error) {
      $('dot').className = 'conn-dot err';
      $('status-txt').textContent = 'Error';
      $('error-msg').textContent = data.error;
      show('v-error');
    } else {
      renderOrphans(data);
    }
  } catch(e) {
    $('dot').className = 'conn-dot err';
    $('error-msg').textContent = 'Network error: ' + e.message;
    show('v-error');
  } finally {
    $('btn-scan').disabled = false;
  }
}

// ── Browse ─────────────────────────────────────────────────
async function toggleBrowse(path, btn, list, row) {
  const existing = browsePanels.get(path);
  if (existing) {
    const open = browseOpen.has(path);
    existing.classList.toggle('hidden', open);
    browseOpen[open ? 'delete' : 'add'](path);
    btn.classList.toggle('open', !open);
    return;
  }
  btn.disabled = true;
  btn.textContent = '⧗';
  try {
    const r = await fetch('/api/browse?' + new URLSearchParams({path}));
    const entries = await r.json();
    const panel = document.createElement('div');
    panel.className = 'browse-panel';
    if (entries.error) {
      const d = document.createElement('div');
      d.className = 'bp-item';
      d.style.color = '#ef4444';
      d.textContent = entries.error;
      panel.appendChild(d);
    } else if (!Array.isArray(entries) || entries.length === 0) {
      const d = document.createElement('div');
      d.className = 'bp-item';
      d.textContent = 'Empty folder';
      panel.appendChild(d);
    } else {
      entries.forEach(e => {
        const d = document.createElement('div');
        d.className = 'bp-item';
        const ic = document.createElement('span');
        ic.textContent = e.is_dir ? '📁' : '📄';
        const nm = document.createElement('span');
        nm.className = 'bp-nm';
        nm.title = e.abs_path;
        nm.textContent = e.name;
        d.appendChild(ic);
        d.appendChild(nm);
        panel.appendChild(d);
      });
    }
    row.insertAdjacentElement('afterend', panel);
    browsePanels.set(path, panel);
    browseOpen.add(path);
    btn.classList.add('open');
  } catch(e) {
    toast('Browse error: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '▶';
  }
}

// ── Delete ─────────────────────────────────────────────────
function openModal() {
  const sel = orphans.filter(o => selected.has(o.abs_path));
  const sz  = sel.reduce((s,o) => s + Math.max(0, o.size), 0);
  const unknownSz = sel.some(o => o.size < 0);
  $('modal-sum').textContent = sel.length + ' item(s)'
    + (unknownSz ? '' : ' · ' + fmtSize(sz))
    + ' will be permanently deleted.';
  const ml = $('modal-list');
  ml.innerHTML = '';
  sel.forEach(o => {
    const d = document.createElement('div');
    d.className = 'modal-file';
    d.innerHTML = '<span>' + (o.is_dir?'📁':'📄') + '</span><span>' + esc(o.rel_path) + '</span>';
    ml.appendChild(d);
  });
  $('modal').classList.remove('hidden');
}

async function doDelete() {
  $('modal').classList.add('hidden');
  const paths = [...selected];
  $('btn-scan').disabled = true;
  try {
    const r = await fetch('/api/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({paths})
    });
    const data = await r.json();
    data.deleted.forEach(p => {
      selected.delete(p);
      orphans = orphans.filter(o => o.abs_path !== p);
      browsePanels.get(p)?.remove();
      browsePanels.delete(p);
      browseOpen.delete(p);
      rowMap.get(p)?.remove();
      rowMap.delete(p);
      szMap.delete(p);
    });
    document.querySelectorAll('.cat-hd').forEach(sep => {
      if (!orphans.some(o => (o.category||'') === sep.dataset.cat)) sep.remove();
    });
    const remaining = orphans.reduce((s,o) => s + Math.max(0,o.size), 0);
    $('ttl-count').textContent = orphans.length + ' orphan(s)';
    $('ttl-size').textContent = fmtSize(remaining) + ' recoverable';
    $('ostat-count').textContent = orphans.length;
    $('ostat-size').textContent = fmtSize(remaining);
    if (orphans.length === 0) show('v-empty');
    updateSel();
    if (data.errors.length === 0) {
      toast('✓ ' + data.deleted.length + ' item(s) deleted', 'ok');
    } else {
      toast(data.deleted.length + ' deleted, ' + data.errors.length + ' error(s)', 'err');
    }
  } catch(e) {
    toast('Network error: ' + e.message, 'err');
  } finally {
    $('btn-scan').disabled = false;
  }
}

// ── Config ─────────────────────────────────────────────────
async function openConfig() {
  try {
    const r = await fetch('/api/config');
    const cfg = await r.json();
    ['QB_HOST','QB_PORT','QB_USER','QB_PASS','STORAGE_DIR','LAST_ACTIVITY_DAYS','WEB_PORT'].forEach(k => {
      const el = $('cfg-' + k);
      if (el) el.value = cfg[k] || '';
    });
  } catch(e) { toast('Error loading settings', 'err'); return; }
  $('cfg-modal').classList.remove('hidden');
}

async function saveConfig() {
  const config = {};
  ['QB_HOST','QB_PORT','QB_USER','QB_PASS','STORAGE_DIR','LAST_ACTIVITY_DAYS','WEB_PORT'].forEach(k => {
    const el = $('cfg-' + k);
    if (el && el.value.trim()) config[k] = el.value.trim();
  });
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(config)
    });
    const data = await r.json();
    if (data.ok) {
      $('cfg-modal').classList.add('hidden');
      toast('Settings saved – rescanning…', 'ok', 2500);
      doScan();
    } else {
      toast(data.error || 'Failed to save settings', 'err');
    }
  } catch(e) { toast('Network error: ' + e.message, 'err'); }
}

// ── Events ──────────────────────────────────────────────────
$('btn-scan').addEventListener('click', doScan);
$('btn-cfg').addEventListener('click', openConfig);
$('btn-theme').addEventListener('click', () => {
  applyTheme(document.body.dataset.theme === 'light' ? 'dark' : 'light');
});
$('sel-all').addEventListener('change', e => {
  const visible = orphans.filter(o => !rowMap.get(o.abs_path)?.classList.contains('hidden'));
  if (e.target.checked) visible.forEach(o => selected.add(o.abs_path));
  else selected.clear();
  rowMap.forEach((row, path) => {
    const cb = row.querySelector('input[type=checkbox]');
    if (cb) cb.checked = selected.has(path);
  });
  updateSel();
});
$('btn-del').addEventListener('click', openModal);
$('modal-cancel').addEventListener('click', () => $('modal').classList.add('hidden'));
$('modal-bd').addEventListener('click',     () => $('modal').classList.add('hidden'));
$('modal-ok').addEventListener('click', doDelete);
$('cfg-cancel').addEventListener('click',   () => $('cfg-modal').classList.add('hidden'));
$('cfg-modal-bd').addEventListener('click', () => $('cfg-modal').classList.add('hidden'));
$('cfg-save').addEventListener('click', saveConfig);

// ── Filter ─────────────────────────────────────────────────
$('filter-input')?.addEventListener('input', applyFilter);

// ── Keyboard shortcuts ──────────────────────────────────────
document.addEventListener('keydown', e => {
  // Escape — close any open modal
  if (e.key === 'Escape') {
    $('modal').classList.add('hidden');
    $('cfg-modal').classList.add('hidden');
    return;
  }
  // Ignore shortcuts when typing in an input
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  // Delete / Backspace — open delete modal if items selected
  if ((e.key === 'Delete' || e.key === 'Backspace') && selected.size > 0) {
    e.preventDefault();
    openModal();
    return;
  }
  // Ctrl+A — select all visible
  if (e.key === 'a' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    const visible = orphans.filter(o => !rowMap.get(o.abs_path)?.classList.contains('hidden'));
    visible.forEach(o => selected.add(o.abs_path));
    rowMap.forEach((row, path) => {
      const cb = row.querySelector('input[type=checkbox]');
      if (cb) cb.checked = selected.has(path);
    });
    updateSel();
  }
});

doScan();
</script>
</body>
</html>
"""


# ── Serveur HTTP ───────────────────────────────────────────────────────────────

_WEB_AUTH = os.environ.get("WEB_AUTH", "")  # format "user:pass"


class Handler(BaseHTTPRequestHandler):

    def _check_auth(self):
        """Retourne True si auth OK ou non configurée. Envoie 401 et retourne False sinon."""
        if not _WEB_AUTH:
            return True
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                if hmac.compare_digest(decoded, _WEB_AUTH):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="qBt Orphan Cleaner"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
        elif path == "/api/version":
            self._json({"version": __version__})
        elif path == "/api/scan":
            self._json(run_scan())
        elif path == "/api/config":
            self._json(get_config())
        elif path == "/api/export":
            fmt = (qs.get("format") or ["csv"])[0].lower()
            if fmt not in ("csv", "json"):
                self._json({"error": "format must be csv or json"}, 400)
                return
            result = export_orphans(fmt)
            if len(result) == 2:  # error
                self._json({"error": result[1]}, 500)
                return
            content, ctype, filename = result
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        elif path.startswith("/api/browse"):
            p = (qs.get("path") or [""])[0]
            if not p:
                self._json({"error": "Paramètre 'path' manquant"}, 400)
                return
            result = browse_dir(p)
            if result is None:
                self._json({"error": "Chemin non autorisé"}, 403)
            else:
                self._json(result)
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        if not self._check_auth():
            return
        try:
            body = self._read_json_body()
        except WebError as e:
            self._json({"error": str(e)}, e.status)
            return
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "JSON invalide"}, 400)
            return

        if not isinstance(body, dict):
            self._json({"error": "Corps JSON invalide"}, 400)
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/delete":
            paths = body.get("paths", [])
            if not isinstance(paths, list):
                self._json({"error": "paths doit être une liste"}, 400)
                return
            if len(paths) > MAX_PATHS_PER_REQUEST:
                self._json({"error": "Trop de chemins demandés"}, 413)
                return
            self._json(do_delete(paths))

        elif path == "/api/delete-torrent":
            torrent_hash = body.get("hash", "")
            result = delete_torrent_with_files(torrent_hash)
            self._json(result, 200 if result.get("ok") else 400)

        elif path == "/api/sizes":
            paths = body.get("paths", [])
            if not isinstance(paths, list):
                self._json({"error": "paths doit être une liste"}, 400)
                return
            if len(paths) > MAX_PATHS_PER_REQUEST:
                self._json({"error": "Trop de chemins demandés"}, 413)
                return
            self._json({"sizes": compute_sizes(paths)})

        elif path == "/api/config":
            try:
                apply_config(body)
                self._json({"ok": True})
            except WebError as e:
                self._json({"error": str(e)}, e.status)
            except Exception as e:
                self._json({"error": str(e)}, 500)

        else:
            self._json({"error": "Not found"}, 404)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise WebError("Content-Length invalide")
        if length <= 0:
            raise WebError("Corps JSON manquant")
        if length > MAX_JSON_BODY:
            raise WebError("Corps JSON trop volumineux", 413)
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send(self, status, ctype, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self._send(status, "application/json; charset=utf-8", body)

    def log_message(self, fmt, *args):  # noqa: suppress default HTTP log
        pass


def main():
    if not _is_loopback_host(WEB_HOST) and not _WEB_AUTH:
        raise SystemExit("WEB_AUTH est requis lorsque WEB_HOST n'est pas localhost")
    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler)
    local_url = f"http://localhost:{WEB_PORT}"
    print("=" * 50)
    print("  qBt Orphan Cleaner — Interface Web")
    print("=" * 50)
    print(f"  URL        : {local_url}")
    print(f"  Stockage   : {_qbt.STORAGE_DIR}")
    print(f"  qBittorrent: {_qbt.BASE_URL}")
    print("  Ctrl+C pour arrêter")
    print("=" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur.")


if __name__ == "__main__":
    main()
