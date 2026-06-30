# Model Switching

To list available models and their TWD pricing:

```bash
KEY=$(python3 /root/.openclaw/get-api-key.py)
curl -s -H "x-api-key: $KEY" https://api.blave.org/openclaw/proxy/v1/models
```

When the user asks to switch models (e.g. "換成 DeepSeek Flash", "用 Opus"), update `/root/.openclaw/openclaw.json`:

```python
import json
cfg = json.load(open('/root/.openclaw/openclaw.json'))
cfg['agents']['defaults']['model']['primary'] = 'opencode-go/deepseek-v4-flash'
json.dump(cfg, open('/root/.openclaw/openclaw.json', 'w'), indent=2)
```

Available model IDs:
- `anthropic/claude-haiku-4-5-20251001`
- `anthropic/claude-sonnet-4-6`
- `anthropic/claude-opus-4-8`
- `anthropic/claude-fable-5`
- `opencode-go/deepseek-v4-flash` (peak hour 2x: Beijing 09-12 / 14-18)
- `opencode-go/deepseek-v4-pro` (peak hour 2x: Beijing 09-12 / 14-18)

After updating, tell the user: "Settings updated. The new model takes effect immediately — no restart needed."
