# BlaveClaw Config

Workspace config files for BlaveClaw agents. Contains AGENTS.md, reference docs, and strategy template.

## Update existing agent

Tell your agent to run these commands (one at a time):

```bash
git clone https://github.com/Blave-TW/blaveclaw-config /tmp/oc-config
```
```bash
cp /tmp/oc-config/AGENTS.md /root/.openclaw/workspace/AGENTS.md
```
```bash
cp -r /tmp/oc-config/references/ /root/.openclaw/workspace/references/
```
```bash
cp /tmp/oc-config/strategies/TEMPLATE.py /root/.openclaw/workspace/strategies/TEMPLATE.py
```
```bash
rm -rf /tmp/oc-config
```

Or paste into the terminal directly.

## Files

- `AGENTS.md` — agent instructions
- `references/deployment.md` — deployment confirmation flow, live bootstrap
- `references/strategy-code.md` — strategy code structure rules
- `references/strategy-report.md` — strategy report API spec
- `strategies/TEMPLATE.py` — base template for all strategies
