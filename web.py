#!/usr/bin/env python3
"""
web.py — Interface web pour qBt Orphan Cleaner.
Démarrage : ./run_web.sh
"""

import json
import os
import shutil
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qbt_orphan_cleaner as _qbt

WEB_PORT = int(os.environ.get("WEB_PORT", "9090"))
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
__version__ = _qbt.__version__


# ── Logique métier ─────────────────────────────────────────────────────────────

def run_scan():
    """Lance le scan ; retourne les orphelins sans taille (calcul lazy)."""
    try:
        client = _qbt.QBittorrentClient()
        client.login()
        known_names, category_dirs, _ = _qbt.collect_known_files(client)
        entries = _qbt.scan_storage(_qbt.STORAGE_DIR, category_dirs)
    except _qbt.QbtError as e:
        return {"error": str(e)}

    norm_storage = os.path.normpath(_qbt.STORAGE_DIR)
    orphans = []
    for entry in entries:
        if entry.name not in known_names:
            rel = os.path.relpath(entry.path, norm_storage)
            parts = rel.replace("\\", "/").split("/")
            category = parts[0] if len(parts) > 1 else ""
            orphans.append({
                "name":       entry.name,
                "rel_path":   rel,
                "abs_path":   entry.path,
                "is_dir":     entry.is_dir(),
                "size":       -1,
                "size_human": "\u2026",
                "category":   category,
            })

    orphans.sort(key=lambda x: (x["category"], not x["is_dir"], x["name"].lower()))

    return {
        "orphans":     orphans,
        "qbt_url":     _qbt.BASE_URL,
        "storage_dir": _qbt.STORAGE_DIR,
    }


def compute_sizes(paths):
    """Calcule les tailles pour une liste de chemins absolus."""
    result = {}
    for path in paths:
        norm = os.path.normpath(path)
        try:
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
        result[path] = {"size": s, "size_human": _qbt.format_size(s)}
    return result


def browse_dir(path):
    """Liste le contenu d'un répertoire. Retourne None si chemin non autorisé."""
    norm_storage = os.path.normpath(_qbt.STORAGE_DIR)
    norm_path = os.path.normpath(path)
    if norm_path != norm_storage and not norm_path.startswith(norm_storage + os.sep):
        return None
    if not os.path.isdir(norm_path):
        return []
    entries = []
    try:
        with os.scandir(norm_path) as it:
            items = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            for e in items:
                entries.append({
                    "name":     e.name,
                    "abs_path": e.path,
                    "is_dir":   e.is_dir(follow_symlinks=False),
                })
    except OSError:
        pass
    return entries


def do_delete(paths):
    """Supprime les chemins validés. Retourne {deleted, errors}."""
    norm_storage = os.path.normpath(_qbt.STORAGE_DIR)
    deleted, errors = [], []
    for path in paths:
        norm_path = os.path.normpath(path)
        if not norm_path.startswith(norm_storage + os.sep):
            errors.append({"path": path, "error": "Chemin non autorisé"})
            continue
        try:
            if os.path.isdir(norm_path) and not os.path.islink(norm_path):
                shutil.rmtree(norm_path)
            else:
                os.remove(norm_path)
            deleted.append(path)
        except OSError as e:
            errors.append({"path": path, "error": str(e)})
    return {"deleted": deleted, "errors": errors}


def get_config():
    return {
        "QB_HOST":     _qbt.QB_HOST,
        "QB_PORT":     str(_qbt.QB_PORT),
        "QB_USER":     _qbt.QB_USER,
        "QB_PASS":     _qbt.QB_PASS,
        "STORAGE_DIR": _qbt.STORAGE_DIR,
        "WEB_PORT":    str(WEB_PORT),
    }


def apply_config(config):
    allowed = {"QB_HOST", "QB_PORT", "QB_USER", "QB_PASS", "STORAGE_DIR", "WEB_PORT"}
    filtered = {k: str(v).strip() for k, v in config.items() if k in allowed and str(v).strip()}
    _qbt.save_env(filtered)
    _qbt.reload_config()


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>qBt Orphan Cleaner v""" + __version__ + """</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0d0d14;color:#e2e8f0;min-height:100vh;font-size:14px}
header{display:flex;align-items:center;justify-content:space-between;padding:0 24px;height:60px;background:#16161f;border-bottom:1px solid #2a2a3a;position:sticky;top:0;z-index:100}
.hd-left{display:flex;align-items:center;gap:10px}
.logo{font-size:20px}
h1{font-size:16px;font-weight:600;color:#e2e8f0}
.hd-right{display:flex;align-items:center;gap:10px}
.status{display:flex;align-items:center;gap:6px;font-size:12px;color:#64748b}
.status-dot{width:8px;height:8px;border-radius:50%;background:#64748b;transition:background .3s;flex-shrink:0}
.status-dot.ok{background:#22c55e}
.status-dot.err{background:#ef4444}
.subheader{display:flex;align-items:center;gap:8px;padding:8px 24px;background:#13131a;border-bottom:1px solid #1e1e2a;font-size:12px;color:#64748b;flex-wrap:wrap}
.sep{color:#2a2a3a}
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:500;transition:all .15s;white-space:nowrap;font-family:inherit}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-primary{background:#5b8dee;color:#fff}
.btn-primary:hover:not(:disabled){background:#4a7de0}
.btn-danger{background:#ef4444;color:#fff}
.btn-danger:hover:not(:disabled){background:#dc2626}
.btn-secondary{background:#2a2a3a;color:#e2e8f0}
.btn-secondary:hover:not(:disabled){background:#353545}
main{max-width:860px;margin:0 auto;padding:24px 16px}
.center{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:80px 0;color:#64748b;text-align:center}
.spinner{width:36px;height:36px;border:3px solid #2a2a3a;border-top-color:#5b8dee;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.icon-circle{width:56px;height:56px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:24px;margin-bottom:8px}
.icon-ok{background:#1a2e1a;color:#22c55e}
.icon-err{background:#2d1515;color:#ef4444}
.center h2{font-size:18px;color:#e2e8f0}
.center p{font-size:13px;color:#64748b;max-width:320px}
.toolbar{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:#16161f;border:1px solid #2a2a3a;border-radius:8px 8px 0 0;flex-wrap:wrap;gap:10px}
.tb-left{display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none;font-size:13px}
.tb-right{display:flex;align-items:center;gap:12px}
.stats{font-size:12px;color:#64748b}
input[type=checkbox]{width:15px;height:15px;accent-color:#5b8dee;cursor:pointer;flex-shrink:0}
.orphan-list{background:#16161f;border:1px solid #2a2a3a;border-top:none;overflow:hidden}
.cat-sep{display:flex;align-items:center;gap:10px;padding:6px 16px;background:#0f0f1a;border-top:1px solid #2a2a3a;font-size:11px;font-weight:600;color:#5b8dee;letter-spacing:.05em;text-transform:uppercase}
.cat-sep::after{content:'';flex:1;height:1px;background:#2a2a3a}
.row{display:flex;align-items:center;border-top:1px solid #1e1e2a;transition:background .1s;-webkit-user-select:none;user-select:none}
.row:first-child{border-top:none}
.row:hover{background:#1c1c28}
.row.sel{background:#161e30}
.row .c-cb{padding:12px 8px 12px 16px;flex-shrink:0;cursor:pointer}
.row .c-ic{padding:12px 6px;font-size:16px;flex-shrink:0}
.row .c-nm{flex:1;padding:10px 8px;min-width:0;cursor:pointer}
.row .name{font-size:13px;color:#e2e8f0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .path{font-size:11px;color:#3d4a5c;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.row .c-sz{padding:12px 8px;font-size:12px;color:#64748b;white-space:nowrap;text-align:right;flex-shrink:0;min-width:72px}
.row .c-exp{padding:10px 12px 10px 0;flex-shrink:0}
.btn-exp{background:none;border:1px solid #2a2a3a;color:#64748b;cursor:pointer;border-radius:4px;width:22px;height:22px;font-size:9px;display:flex;align-items:center;justify-content:center;transition:all .2s;flex-shrink:0;padding:0}
.btn-exp:hover{background:#2a2a3a;color:#e2e8f0;border-color:#3a3a4a}
.btn-exp.open{transform:rotate(90deg);border-color:#5b8dee;color:#5b8dee}
.btn-exp:disabled{opacity:.4;cursor:default}
.browse-panel{background:#0b0b11;border-top:1px dashed #1e1e2a}
.bp-item{display:flex;align-items:center;gap:8px;padding:5px 16px 5px 54px;font-size:12px;color:#64748b;border-top:1px solid #161620}
.bp-item:first-child{border-top:none}
.bp-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.summary-bar{display:flex;justify-content:space-between;padding:10px 16px;background:#13131a;border:1px solid #2a2a3a;border-top:none;border-radius:0 0 8px 8px;font-size:12px;color:#64748b}
/* Glow delete button */
#btn-del:not(:disabled){box-shadow:0 0 8px rgba(239,68,68,.45),0 0 18px rgba(239,68,68,.2);animation:glow-pulse 2.4s ease-in-out infinite}
@keyframes glow-pulse{
  0%,100%{box-shadow:0 0 6px rgba(239,68,68,.4),0 0 14px rgba(239,68,68,.18)}
  50%{box-shadow:0 0 14px rgba(239,68,68,.65),0 0 28px rgba(239,68,68,.32)}
}
/* Modals */
.modal{position:fixed;inset:0;z-index:200;display:flex;align-items:center;justify-content:center;padding:16px}
.modal-bd{position:absolute;inset:0;background:rgba(0,0,0,.72);backdrop-filter:blur(3px)}
.modal-box{position:relative;background:#16161f;border:1px solid #2a2a3a;border-radius:10px;padding:24px;max-width:480px;width:100%;max-height:80vh;overflow-y:auto}
.modal-box h2{font-size:16px;margin-bottom:8px}
.modal-box>p{color:#64748b;font-size:13px;margin-bottom:16px}
.modal-list{background:#0d0d14;border-radius:6px;padding:8px 0;margin-bottom:20px;max-height:200px;overflow-y:auto}
.modal-item{display:flex;align-items:center;gap:8px;padding:5px 12px;font-size:12px;color:#94a3b8}
.modal-actions{display:flex;justify-content:flex-end;gap:10px}
/* Config form */
.cfg-grid{display:grid;gap:14px;margin-bottom:20px}
.cfg-row{display:grid;gap:5px}
.cfg-row label{font-size:11px;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.cfg-row input{background:#0d0d14;border:1px solid #2a2a3a;border-radius:6px;color:#e2e8f0;padding:8px 10px;font-size:13px;width:100%;font-family:inherit;outline:none;transition:border .15s}
.cfg-row input:focus{border-color:#5b8dee}
.cfg-note{font-size:11px;color:#64748b;background:#101018;border:1px solid #1e1e2a;border-radius:5px;padding:8px 10px;margin-bottom:18px}
/* Toast */
.toast{position:fixed;bottom:24px;right:24px;padding:12px 18px;border-radius:8px;font-size:13px;font-weight:500;z-index:300;animation:fadeIn .2s ease;max-width:360px;pointer-events:none}
.toast.ok{background:#14532d;border:1px solid #22c55e;color:#86efac}
.toast.err{background:#450a0a;border:1px solid #ef4444;color:#fca5a5}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.hidden{display:none!important}
@media(max-width:600px){header{padding:0 12px}main{padding:12px 8px}.hd-right .status{display:none}.subheader{padding:6px 12px}}
</style>
</head>
<body>

<header>
  <div class="hd-left">
    <span class="logo">&#129529;</span>
    <h1>qBt Orphan Cleaner <span style="font-size:11px;color:#64748b;font-weight:400">v""" + __version__ + """</span></h1>
  </div>
  <div class="hd-right">
    <div class="status">
      <span class="status-dot" id="dot"></span>
      <span id="status-txt">&ndash;</span>
    </div>
    <button class="btn btn-secondary" id="btn-cfg">&#9881; Config</button>
    <button class="btn btn-primary" id="btn-scan">&#8635; Scanner</button>
  </div>
</header>

<div class="subheader">
  <span id="info-storage">&ndash;</span>
  <span class="sep">&middot;</span>
  <span id="info-qbt">&ndash;</span>
</div>

<main>
  <div id="v-loading" class="center">
    <div class="spinner"></div>
    <span>Scan en cours&hellip;</span>
  </div>
  <div id="v-empty" class="center hidden">
    <div class="icon-circle icon-ok">&#10003;</div>
    <h2>Stockage propre</h2>
    <p>Aucun fichier orphelin d&eacute;tect&eacute;.</p>
  </div>
  <div id="v-error" class="center hidden">
    <div class="icon-circle icon-err">&#10007;</div>
    <h2>Erreur</h2>
    <p id="error-msg">Impossible de contacter qBittorrent.</p>
  </div>
  <div id="v-results" class="hidden">
    <div class="toolbar">
      <label class="tb-left">
        <input type="checkbox" id="sel-all">
        <span>Tout s&eacute;lectionner</span>
      </label>
      <div class="tb-right">
        <span class="stats" id="sel-info"></span>
        <button class="btn btn-danger" id="btn-del" disabled>&#128465; Supprimer</button>
      </div>
    </div>
    <div id="orphan-list" class="orphan-list"></div>
    <div class="summary-bar">
      <span id="ttl-count"></span>
      <span id="ttl-size"></span>
    </div>
  </div>
</main>

<!-- Modal confirmation suppression -->
<div class="modal hidden" id="modal">
  <div class="modal-bd" id="modal-bd"></div>
  <div class="modal-box">
    <h2>&#9888; Confirmer la suppression</h2>
    <p id="modal-sum"></p>
    <div class="modal-list" id="modal-list"></div>
    <div class="modal-actions">
      <button class="btn btn-secondary" id="modal-cancel">Annuler</button>
      <button class="btn btn-danger" id="modal-ok">Supprimer d&eacute;finitivement</button>
    </div>
  </div>
</div>

<!-- Modal config -->
<div class="modal hidden" id="cfg-modal">
  <div class="modal-bd" id="cfg-modal-bd"></div>
  <div class="modal-box" style="max-width:420px">
    <h2>&#9881; Configuration</h2>
    <p style="margin-bottom:20px">Modifiez les param&egrave;tres ci-dessous. La configuration est sauvegard&eacute;e dans le fichier <code>.env</code>.</p>
    <div class="cfg-grid">
      <div class="cfg-row">
        <label>qBittorrent &mdash; h&ocirc;te</label>
        <input id="cfg-QB_HOST" type="text" placeholder="http://192.168.1.149">
      </div>
      <div class="cfg-row">
        <label>qBittorrent &mdash; port</label>
        <input id="cfg-QB_PORT" type="number" placeholder="8080">
      </div>
      <div class="cfg-row">
        <label>Utilisateur</label>
        <input id="cfg-QB_USER" type="text" placeholder="admin">
      </div>
      <div class="cfg-row">
        <label>Mot de passe</label>
        <input id="cfg-QB_PASS" type="password" placeholder="••••••••">
      </div>
      <div class="cfg-row">
        <label>R&eacute;pertoire de stockage</label>
        <input id="cfg-STORAGE_DIR" type="text" placeholder="/mnt/downloads">
      </div>
      <div class="cfg-row">
        <label>Port web</label>
        <input id="cfg-WEB_PORT" type="number" placeholder="9090">
      </div>
    </div>
    <p class="cfg-note">&#9432;&nbsp; Le changement de port web n&eacute;cessite un red&eacute;marrage du serveur.</p>
    <div class="modal-actions">
      <button class="btn btn-secondary" id="cfg-cancel">Annuler</button>
      <button class="btn btn-primary" id="cfg-save">&#10003; Sauvegarder</button>
    </div>
  </div>
</div>

<div class="toast hidden" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let orphans = [];
let selected = new Set();
const rowMap = new Map();   // abs_path -> row element
const szMap  = new Map();   // abs_path -> c-sz element
const browsePanels = new Map(); // abs_path -> panel element
let browseOpen = new Set(); // abs_path of open panels

function fmtSize(b) {
  if (b < 0) return '\u2026';
  const u = ['o','Ko','Mo','Go','To'];
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
function updateSel() {
  const n = selected.size;
  const anyUnknown = orphans.filter(o => selected.has(o.abs_path)).some(o => o.size < 0);
  const sz = selectedSize();
  $('sel-all').indeterminate = n > 0 && n < orphans.length;
  $('sel-all').checked = n > 0 && n === orphans.length;
  const btn = $('btn-del');
  btn.disabled = n === 0;
  if (n > 0) {
    btn.textContent = anyUnknown
      ? '🗑 Supprimer (' + n + ')'
      : '🗑 Supprimer (' + fmtSize(sz) + ')';
  } else {
    btn.textContent = '🗑 Supprimer';
  }
  $('sel-info').textContent = n > 0
    ? n + ' s\u00e9lectionn\u00e9(s)' + (anyUnknown ? '' : ' \u00b7 ' + fmtSize(sz))
    : '';
  rowMap.forEach((row, path) => row.classList.toggle('sel', selected.has(path)));
}
function togglePath(path) {
  if (selected.has(path)) selected.delete(path);
  else selected.add(path);
  const cb = rowMap.get(path)?.querySelector('input[type=checkbox]');
  if (cb) cb.checked = selected.has(path);
  updateSel();
}

function renderOrphans(data) {
  orphans = data.orphans;
  selected.clear();
  rowMap.clear();
  szMap.clear();
  browsePanels.clear();
  browseOpen.clear();

  $('info-storage').textContent = '📂 ' + data.storage_dir;
  $('info-qbt').textContent = '🔗 ' + data.qbt_url;
  $('dot').className = 'status-dot ok';
  $('status-txt').textContent = 'Connect\u00e9';
  $('ttl-count').textContent = orphans.length + ' orphelin(s)';
  $('ttl-size').textContent = '\u2026';

  const list = $('orphan-list');
  list.innerHTML = '';

  if (orphans.length === 0) { show('v-empty'); return; }

  const groups = {};
  orphans.forEach(o => { (groups[o.category || ''] ||= []).push(o); });
  const keys = Object.keys(groups).sort((a,b) => a===''?-1:b===''?1:a.localeCompare(b));

  keys.forEach(cat => {
    if (cat !== '') {
      const sep = document.createElement('div');
      sep.className = 'cat-sep';
      sep.dataset.cat = cat;
      sep.textContent = cat;
      list.appendChild(sep);
    }
    groups[cat].forEach(o => {
      const row = document.createElement('div');
      row.className = 'row';
      row.dataset.path = o.abs_path;
      const displayName = o.category ? o.name : o.rel_path;

      // Build row contents
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
        const pathEl = document.createElement('div');
        pathEl.className = 'path';
        pathEl.textContent = o.rel_path;
        nmDiv.appendChild(pathEl);
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
        expBtn.title = 'Explorer le dossier';
        expBtn.textContent = '\u25B6';
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

// ── Lazy sizes ──────────────────────────────────────────────
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
    $('ttl-size').textContent = fmtSize(total) + ' r\u00e9cup\u00e9rables';
    updateSel();
  } catch(_) { /* sizes indisponibles */ }
}

// ── Scan ────────────────────────────────────────────────────
async function doScan() {
  show('v-loading');
  $('btn-scan').disabled = true;
  try {
    const r = await fetch('/api/scan');
    const data = await r.json();
    if (data.error) {
      $('dot').className = 'status-dot err';
      $('status-txt').textContent = 'Erreur';
      $('error-msg').textContent = data.error;
      show('v-error');
    } else {
      renderOrphans(data);
    }
  } catch(e) {
    $('dot').className = 'status-dot err';
    $('error-msg').textContent = 'Erreur r\u00e9seau\u00a0: ' + e.message;
    show('v-error');
  } finally {
    $('btn-scan').disabled = false;
  }
}

// ── Browse ──────────────────────────────────────────────────
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
  btn.textContent = '\u29D7';
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
      d.textContent = 'Dossier vide';
      panel.appendChild(d);
    } else {
      entries.forEach(e => {
        const d = document.createElement('div');
        d.className = 'bp-item';
        const ic = document.createElement('span');
        ic.textContent = e.is_dir ? '📁' : '📄';
        const nm = document.createElement('span');
        nm.className = 'bp-name';
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
    toast('Erreur exploration\u00a0: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '\u25B6';
  }
}

// ── Delete ──────────────────────────────────────────────────
function openModal() {
  const sel = orphans.filter(o => selected.has(o.abs_path));
  const sz  = sel.reduce((s,o) => s + Math.max(0, o.size), 0);
  const unknownSz = sel.some(o => o.size < 0);
  $('modal-sum').textContent = sel.length + ' \u00e9l\u00e9ment(s)'
    + (unknownSz ? '' : ' \u00b7 ' + fmtSize(sz))
    + ' supprim\u00e9(s) de fa\u00e7on irr\u00e9versible.';
  const ml = $('modal-list');
  ml.innerHTML = '';
  sel.forEach(o => {
    const d = document.createElement('div');
    d.className = 'modal-item';
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
    document.querySelectorAll('.cat-sep').forEach(sep => {
      if (!orphans.some(o => (o.category||'') === sep.dataset.cat)) sep.remove();
    });
    const remaining = orphans.reduce((s,o) => s + Math.max(0,o.size), 0);
    $('ttl-count').textContent = orphans.length + ' orphelin(s)';
    $('ttl-size').textContent = fmtSize(remaining) + ' r\u00e9cup\u00e9rables';
    if (orphans.length === 0) show('v-empty');
    updateSel();
    if (data.errors.length === 0) {
      toast('\u2713 ' + data.deleted.length + ' \u00e9l\u00e9ment(s) supprim\u00e9(s)', 'ok');
    } else {
      toast(data.deleted.length + ' supprim\u00e9(s), ' + data.errors.length + ' erreur(s)', 'err');
    }
  } catch(e) {
    toast('Erreur r\u00e9seau\u00a0: ' + e.message, 'err');
  } finally {
    $('btn-scan').disabled = false;
  }
}

// ── Config ──────────────────────────────────────────────────
async function openConfig() {
  try {
    const r = await fetch('/api/config');
    const cfg = await r.json();
    ['QB_HOST','QB_PORT','QB_USER','QB_PASS','STORAGE_DIR','WEB_PORT'].forEach(k => {
      const el = $('cfg-' + k);
      if (el) el.value = cfg[k] || '';
    });
  } catch(e) { toast('Erreur chargement config', 'err'); return; }
  $('cfg-modal').classList.remove('hidden');
}
async function saveConfig() {
  const config = {};
  ['QB_HOST','QB_PORT','QB_USER','QB_PASS','STORAGE_DIR','WEB_PORT'].forEach(k => {
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
      toast('Configuration sauvegard\u00e9e \u2013 rescan en cours\u2026', 'ok', 2500);
      doScan();
    } else {
      toast(data.error || 'Erreur de sauvegarde', 'err');
    }
  } catch(e) { toast('Erreur r\u00e9seau\u00a0: ' + e.message, 'err'); }
}

// ── Event listeners ─────────────────────────────────────────
$('btn-scan').addEventListener('click', doScan);
$('btn-cfg').addEventListener('click', openConfig);
$('sel-all').addEventListener('change', e => {
  if (e.target.checked) orphans.forEach(o => selected.add(o.abs_path));
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

doScan();
</script>
</body>
</html>
"""


# ── Serveur HTTP ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
        elif self.path == "/api/version":
            self._json({"version": __version__})
        elif self.path == "/api/scan":
            self._json(run_scan())
        elif self.path == "/api/config":
            self._json(get_config())
        elif self.path.startswith("/api/browse"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            path = (qs.get("path") or [""])[0]
            if not path:
                self._json({"error": "Paramètre 'path' manquant"}, 400)
                return
            result = browse_dir(path)
            if result is None:
                self._json({"error": "Chemin non autorisé"}, 403)
            else:
                self._json(result)
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._json({"error": "JSON invalide"}, 400)
            return

        if self.path == "/api/delete":
            paths = body.get("paths", [])
            if not isinstance(paths, list):
                self._json({"error": "paths doit être une liste"}, 400)
                return
            self._json(do_delete(paths))

        elif self.path == "/api/sizes":
            paths = body.get("paths", [])
            if not isinstance(paths, list):
                self._json({"error": "paths doit être une liste"}, 400)
                return
            self._json({"sizes": compute_sizes(paths)})

        elif self.path == "/api/config":
            if not isinstance(body, dict):
                self._json({"error": "Corps JSON invalide"}, 400)
                return
            try:
                apply_config(body)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        else:
            self._json({"error": "Not found"}, 404)

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
