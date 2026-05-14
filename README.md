# BlaveClaw Config

Workspace config for BlaveClaw agents. Contains AGENTS.md, shared library, strategy template, manager system, and reference docs.

## Update existing agent

Tell your agent to run these commands one at a time:

```bash
git clone https://github.com/Blave-TW/blaveclaw-config /tmp/oc-config
```
```bash
cp /tmp/oc-config/AGENTS.md /root/.openclaw/workspace/AGENTS.md
```
```bash
cp /tmp/oc-config/CLAUDE.md /root/.openclaw/workspace/CLAUDE.md
```
```bash
cp -r /tmp/oc-config/references/ /root/.openclaw/workspace/references/
```
```bash
cp /tmp/oc-config/strategies/TEMPLATE.py /root/.openclaw/workspace/strategies/TEMPLATE.py
```
```bash
cp -r /tmp/oc-config/lib/ /root/.openclaw/workspace/lib/
```
```bash
cp -r /tmp/oc-config/manager/ /root/.openclaw/workspace/manager/
```
```bash
cp -r /tmp/oc-config/examples/ /root/.openclaw/workspace/examples/
```
```bash
rm -rf /tmp/oc-config
```

## Files

- `AGENTS.md` — agent instructions
- `CLAUDE.md` — Claude Code context (points to AGENTS.md)
- `strategies/TEMPLATE.py` — base template for all Type A strategies
- `lib/` — shared library (runner, data, execute, pnl, portfolio, strategy, analysis, param_scan, validation, notify)
- `manager/` — portfolio management system (optimizer, reconciler)
- `examples/` — reference strategy implementations
- `references/` — deployment flow, strategy code rules
