# Model Switching

Model IDs change over time (new Claude generations, provider updates) — never rely on a memorized
or previously-seen ID. The proxy's `/v1/models` response is the single source of truth; always
fetch it fresh before switching.

**The switching mechanism is runtime-dependent — determine which RUNTIME this machine is FIRST,
then follow only that section.** The other fleet's procedure fails outright here, it does not
degrade gracefully:

- **Blave Agent runtime** — primary signal (works on every OS): each turn's system prompt
  carries the runtime-injected 「查詢 / 切換模型（本 runtime 專屬規則）」 section. Layout
  confirmation: the file `<base>/openclaw.json` exists, where `<base>` is `/opt/blave-agent`
  on Linux and `C:\blave-agent` on Windows (check the file, not just the directory — same
  detection as `lib/notify.py` and `references/deployment.md`). On these machines
  `/root/.openclaw` does not exist at all, so `get-api-key.py` and the `openclaw.json` edit
  below can only fail.
- **Old BlaveClaw runtime** — no injected switching section in the system prompt; workspace
  under `/root/.openclaw/workspace` (this runtime is Linux-only); `get-api-key.py` exists
  there.

Do NOT reply "已切換到 X" until the verification for your runtime passes. If you skip
verification, you are guessing — and a wrong guess will crash the user's next message with a
404, not just fail silently.

---

## Blave Agent runtime

**Query the model list and pricing:**
```bash
curl -s -H "x-api-key: $ANTHROPIC_API_KEY" https://api.blave.org/openclaw/proxy/v1/models
```
`$ANTHROPIC_API_KEY` is already in your environment (this runtime's proxy token — it
authenticates every model call you make). Do not look for `get-api-key.py` (old-runtime only,
not present here) and do not use the Blave API key from the workspace `.env` (that is a
different credential system; it cannot query the proxy). Match the user's request against the
`id` field of the returned models — note some carry a peak-hour pricing multiplier (see `note`
field, e.g. DeepSeek 2x during Beijing 09:00-12:00 / 14:00-18:00).

**Switch:** every turn's system prompt carries a runtime-injected section
(「查詢 / 切換模型（本 runtime 專屬規則）」) containing the exact switch command with your
current session id already filled in:
```bash
python3 <runtime dir>/set_model.py <session_id> <model_id>
```
Run the injected command verbatim, substituting only `<model_id>` with the full id exactly as
returned by `/v1/models` (e.g. `anthropic/claude-sonnet-5`). Never guess or hand-construct the
session id or the runtime path — the preference is stored per session
(`/opt/blave-agent/state/model_prefs.json` on Linux, `C:\blave-agent\state\model_prefs.json`
on Windows), so a wrong session id switches nobody, silently.
If the injected section is missing from your context, tell the user you cannot switch right
now instead of improvising a path.

**To verify an id before switching** (the same pre-flight lookup the old runtime's gateway
performs — no gateway exists on this runtime, this is just a proxy-side check):
```bash
curl -s -H "x-api-key: $ANTHROPIC_API_KEY" https://api.blave.org/openclaw/proxy/v1/models/<id>
```
Accepts both the bare id (`claude-sonnet-5`) and the prefixed form
(`anthropic/claude-sonnet-5`). 200 means the model is wired up; if it 404s even though the id
was in the list, do NOT proceed — tell the user the switch isn't available rather than
reporting success.

**No restart exists or is needed on this runtime.** Every turn is a fresh process: the switch
takes effect from the NEXT message, and the current turn keeps running on the old model. Tell
the user exactly that — "switched, applies from your next message" — never claim it is already
live for this turn, and never mention a gateway restart (there is no `openclaw-gateway`
service here).

---

## Old BlaveClaw runtime

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

**Do NOT tell the user "no restart needed" — on this runtime a restart is always required for the new model to take effect.**
