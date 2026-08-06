# Updating the Workspace

Trigger: user says 更新 blaveclaw / 更新 blave agent / 更新系統 / 更新 config / update blaveclaw / update blave agent / update workspace — no link required.

## 1. Skill

Nothing to install — the platform re-clones the blave-quant skill into `skills/blave-quant` once a day (a systemd timer / scheduled task on Blave Agent machines, a crontab entry on older openclaw boxes). Just check that `skills/blave-quant/SKILL.md` is there; if it is missing, say so in the report and stop — a hand-install would put it somewhere the daily job then overwrites.

## 2. Config

Clone https://github.com/Blave-TW/blaveclaw-config to `/tmp/oc-config` as **reference only** — never as the live workspace. Then follow the "Updating an existing workspace" section of its `README.md` exactly: compare file by file, apply only what's missing or outdated.

**Hard rule, no exceptions:** never blindly overwrite `lib/` wholesale. For `lib/order_*.py` / `lib/account_*.py`, the filename alone doesn't tell you if it's user-created — check whether that exact filename exists in the reference clone: if it does (e.g. `order_bingx.py`, `order_binance.py`, `order_okx.py`, `order_gateio.py`, `order_sinopac.py`, `account_bingx.py`, `account_binance.py`, `account_okx.py`, `account_gateio.py`, `account_TEMPLATE.py`, `order_TEMPLATE.py` — official broker libs shipped in the repo), merge it like any other `lib/` file (same as `lib/runner.py`: preserve local edits, pull in upstream fixes). Only skip a file entirely — never touch it — if it does **not** exist in the reference clone at all; that's the user's own exchange integration.

After the merge, copy the reference clone's `VERSION` file to the workspace root verbatim — the machine reports it to the platform and it drives the web workspace's "update available" indicator. An update that skips this step keeps telling the user an update is available.

Remove `/tmp/oc-config` when done.

Report exactly what changed (or that everything was already up to date) — don't claim "updated" without checking.
