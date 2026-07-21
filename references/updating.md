# Updating the Workspace

Trigger: user says 更新 blaveclaw / 更新系統 / 更新 config / update blaveclaw / update workspace — no link required.

## 1. Skill

Run: `npx -y skills add https://github.com/Blave-TW/blave-quant-skill -a openclaw -s blave-quant -y`

## 2. Config

Clone https://github.com/Blave-TW/blaveclaw-config to `/tmp/oc-config` as **reference only** — never as the live workspace. Then follow the "Updating an existing workspace" section of its `README.md` exactly: compare file by file, apply only what's missing or outdated.

**Hard rule, no exceptions:** never blindly overwrite `lib/` wholesale. For `lib/order_*.py` / `lib/account_*.py`, the filename alone doesn't tell you if it's user-created — check whether that exact filename exists in the reference clone: if it does (e.g. `order_bingx.py`, `order_sinopac.py`, `account_bingx.py`, `account_TEMPLATE.py` — official broker libs shipped in the repo), merge it like any other `lib/` file (same as `lib/runner.py`: preserve local edits, pull in upstream fixes). Only skip a file entirely — never touch it — if it does **not** exist in the reference clone at all; that's the user's own exchange integration.

Remove `/tmp/oc-config` when done.

Report exactly what changed (or that everything was already up to date) — don't claim "updated" without checking.
