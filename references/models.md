# Model Switching

To list available models and their TWD pricing:

```bash
KEY=$(python3 /root/.openclaw/get-api-key.py)
curl -s -H "x-api-key: $KEY" https://api.blave.org/openclaw/proxy/v1/models
```

When the user asks to switch models (e.g. "換成 DeepSeek Flash", "用 Opus"), follow this exact sequence:

**Step 1 — update the config:**
```python
import json
cfg = json.load(open('/root/.openclaw/openclaw.json'))
cfg['agents']['defaults']['model']['primary'] = 'deepseek/deepseek-v4-pro'  # replace with target model ID
json.dump(cfg, open('/root/.openclaw/openclaw.json', 'w'), indent=2)
```

**Step 2 — tell the user the model switched and that the gateway is restarting:**
Reply with something like: "已切換到 X，Gateway 重啟中，約 10 秒後生效 🔄"

**Step 3 — trigger a delayed gateway restart (run this bash command last):**
```bash
(sleep 5 && systemctl restart openclaw-gateway.service) &
```

The 5-second delay ensures your reply is delivered before the gateway goes down. The user's next message will land on the new model.

Available model IDs:
- `anthropic/claude-haiku-4-5-20251001`
- `anthropic/claude-sonnet-5`
- `anthropic/claude-opus-4-8`
- `deepseek/deepseek-v4-flash` (peak hour 2x: Beijing 09-12 / 14-18)
- `deepseek/deepseek-v4-pro` (peak hour 2x: Beijing 09-12 / 14-18)

**Do NOT tell the user "no restart needed" — a restart is always required for the new model to take effect.**
