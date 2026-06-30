# BlaveClaw Config

Workspace config for BlaveClaw agents. Contains AGENTS.md, shared library, strategy template, manager system, and reference docs.

Fresh installs are handled automatically by the provisioning script — no manual steps needed.

## Updating an existing workspace

Tell your agent:

> Clone https://github.com/Blave-TW/blaveclaw-config to /tmp/oc-config and use it as **reference** to update the workspace at /root/.openclaw/workspace. For each file below, compare the repo version with the local version and apply only what is missing or outdated — do not blindly overwrite.
>
> - `AGENTS.md`, `CLAUDE.md` — replace wholesale (these are config, not user-edited)
> - `references/` — for each file, check if a local version exists; if it does, read both and patch in anything missing; if it does not, copy it in
> - `strategies/TEMPLATE_A.py`, `strategies/TEMPLATE_C.py` — replace wholesale
> - `lib/` — add any canonical files that are missing locally; **never touch** `lib/order_*.py` or `lib/account_*.py` (user-created); if you modified a canonical file (e.g. `lib/runner.py`), read both versions and manually merge the new changes in
> - `manager/` — replace wholesale (user edits live in `portfolio_config.json`, not in the scripts)
> - `examples/` — replace wholesale
>
> When done, remove /tmp/oc-config.

## Files

- `AGENTS.md` — agent instructions
- `CLAUDE.md` — Claude Code context (points to AGENTS.md)
- `strategies/TEMPLATE_A.py` — base template for all Type A strategies
- `strategies/TEMPLATE_C.py` — base template for all Type C portfolio strategies
- `lib/` — shared library (runner, data, execute, pnl, portfolio, strategy, analysis, param_scan, validation, notify)
- `lib/account_TEMPLATE.py` — template for exchange account libraries (copy to `lib/account_{exchange}.py`)
- `manager/` — portfolio management system (optimizer, reconciler)
- `examples/` — reference strategy implementations
- `references/` — deployment flow, strategy code rules, lib signatures, model switching
