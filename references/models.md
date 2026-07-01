# Model Switching

Model IDs change over time (new Claude generations, provider updates) — never rely on a memorized
or previously-seen ID. The proxy's `/v1/models` response is the single source of truth; always
fetch it fresh before switching.

Do NOT reply "已切換到 X" until Step 3's verification returns 200. If you skip straight to a reply
without running Step 1-3, you are guessing — and a wrong guess will crash the user's next message
with a 404, not just fail silently.

When the user asks to switch models (e.g. "換成 DeepSeek Flash", "用 Opus"), follow this exact sequence:

**Step 1 — fetch the current model list and pricing:**
```bash
KEY=$(python3 /root/.openclaw/get-api-key.py)
curl -s -H "x-api-key: $KEY" https://api.blave.org/openclaw/proxy/v1/models
```
Match the user's request (e.g. "Opus", "DeepSeek Flash") against the `id` field of the returned
models — note some carry a peak-hour pricing multiplier (see `note` field, e.g. DeepSeek 2x during
Beijing 09:00-12:00 / 14:00-18:00).

**Step 2 — update the config with the exact `id` string from Step 1:**
```python
import json
cfg = json.load(open('/root/.openclaw/openclaw.json'))
cfg['agents']['defaults']['model']['primary'] = '<id from /v1/models response>'
json.dump(cfg, open('/root/.openclaw/openclaw.json', 'w'), indent=2)
```

**Step 3 — verify the exact id before restarting:**
```bash
curl -s -H "x-api-key: $KEY" https://api.blave.org/openclaw/proxy/v1/models/<id from Step 1>
```
This single-model lookup is the same pre-flight check the gateway itself runs internally before
accepting a model. Accepts both the bare id (`claude-sonnet-5`) and the prefixed form
(`anthropic/claude-sonnet-5`). If it returns 200, the switch will work after restart. If it 404s
even though the id was present in the Step 1 list, do NOT proceed — the model isn't actually wired
up on the proxy side yet. Tell the user the switch isn't available rather than reporting success.

**Step 4 — tell the user the model switched and that the gateway is restarting:**
Reply with something like: "已切換到 X，Gateway 重啟中，約 10 秒後生效 🔄"

**Step 5 — trigger a delayed gateway restart (run this bash command last):**
```bash
(sleep 5 && systemctl restart openclaw-gateway.service) &
```

The 5-second delay ensures your reply is delivered before the gateway goes down. The user's next message will land on the new model.

**Do NOT tell the user "no restart needed" — a restart is always required for the new model to take effect.**
