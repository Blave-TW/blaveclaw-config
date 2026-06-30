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
cfg['agents']['defaults']['model']['primary'] = 'anthropic/deepseek-v4-flash'
json.dump(cfg, open('/root/.openclaw/openclaw.json', 'w'), indent=2)
```

Model ID format: `anthropic/{id}` — use the `id` field from the `/v1/models` response.

After updating, tell the user: "Settings updated. Run `systemctl restart openclaw-gateway` in the terminal to apply the change."

**NEVER run `systemctl restart openclaw-gateway` yourself** — it kills the current session.
