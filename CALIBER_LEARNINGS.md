# Caliber Learnings

Accumulated patterns and anti-patterns from development sessions.
Auto-managed by [caliber](https://github.com/caliber-ai-org/ai-setup) — do not edit manually.

- **[gotcha]** `web.py` uses **stdlib `http.server`**, not Flask — caliber may detect "Flask" as a framework but the actual routing is `if self.path == "/api/..."` inside `Handler.do_GET` / `Handler.do_POST`. Never reference Flask decorators or `@app.route` in the `web-route` skill or any `web.py` instructions.
- **[gotcha]** After each `caliber refresh`, `.github/copilot-instructions.md` gets injected into the `## Before Committing` pre-commit `git add` command in `CLAUDE.md` — this file does not exist in the project. Remove it from the command every time caliber regenerates that section to avoid scoring penalty.
- **[pattern]** `import shutil` belongs in the top-level import block of `qbt_orphan_cleaner.py`, not as a local import inside `_delete_entries()`. PEP 8 requires all imports at module top.
- **[convention]** Config block constants (`QB_HOST`, `QB_PORT`, etc.) use plain `=` without alignment padding — PEP 8 E221 disallows extra whitespace before `=`. When editing the config block, do not realign with spaces.
- **[convention]** Imports in `qbt_orphan_cleaner.py` are sorted alphabetically: `argparse`, `json`, `http.cookiejar`, `os`, `shutil`, `sys`, `urllib.error`, `urllib.parse`, `urllib.request`. Maintain this order when adding new imports.
- **[convention]** Exception subclasses with a docstring do not need `pass` — `class QbtError(Exception): """..."""`  is valid and preferred.
