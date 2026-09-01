# Blave Agent Config

Workspace config for Blave Agent. Contains AGENTS.md, shared library, strategy template, manager system, and reference docs.

Fresh installs are handled automatically by the provisioning script — no manual steps needed.

**Maintainers: bump `VERSION` (date, `YYYY-MM-DD`, add `-b`/`-c` for same-day repushes) in the same commit as any change machines should pick up** — the platform compares each machine's reported VERSION against this repo's to light the web "update available" indicator; an unbumped push is invisible to users.

## Updating an existing workspace

Tell your agent:

> Clone https://github.com/Blave-TW/blaveclaw-config to /tmp/oc-config and use it as **reference** to update this machine's live workspace — `$BLAVECLAW_HOME/workspace`, i.e. the workspace you are running in (`/root/.openclaw/workspace` on old BlaveClaw machines, `/opt/blave-agent/workspace` on Blave Agent machines; resolution per `references/deployment.md`). For each file below, compare the repo version with the local version and apply only what is missing or outdated — do not blindly overwrite.
>
> - `AGENTS.md`, `CLAUDE.md` — replace wholesale (these are config, not user-edited)
> - `references/` — for each file, check if a local version exists; if it does, read both and patch in anything missing; if it does not, copy it in
> - `strategies/TEMPLATE_A.py`, `strategies/TEMPLATE_C.py` — replace wholesale
> - `lib/` — add any canonical files that are missing locally; if you modified a canonical file (e.g. `lib/runner.py`), read both versions and manually merge the new changes in. For `lib/order_*.py` / `lib/account_*.py`, the name alone doesn't tell you if it's user-created: if that exact filename exists in the reference clone (e.g. `order_bingx.py`, `order_sinopac.py`, `account_bingx.py`, `account_TEMPLATE.py`), merge it like any other canonical file; **never touch** one that does not exist in the reference clone — that's the user's own exchange integration
> - `manager/` — replace wholesale (user edits live in `portfolio_config.json`, not in the scripts)
> - `examples/` — replace wholesale
> - `VERSION` — copy verbatim, always last (it declares the workspace up to date; drives the web "update available" indicator)
>
> When done, remove /tmp/oc-config.

## Files

- `AGENTS.md` — agent instructions
- `CLAUDE.md` — Claude Code context (points to AGENTS.md)
- `strategies/TEMPLATE_A.py` — base template for all Type A strategies
- `strategies/TEMPLATE_C.py` — base template for all Type C portfolio strategies
- `lib/` — shared library (runner, data, execute, pnl, portfolio, strategy, analysis, param_scan, validation, notify, report)
- `lib/account_TEMPLATE.py` — template for exchange account libraries (copy to `lib/account_{exchange}.py`)
- `manager/` — portfolio management system (optimizer, reconciler)
- `examples/` — reference strategy implementations
- `references/` — deployment flow, strategy code rules, lib signatures, model switching
