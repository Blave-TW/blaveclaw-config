"""Bind a trading venue from chat — the pasted keys go to the platform runtime's
own `.env` writer (`command_listener._cmd_credentials`: flock against
first-boot, atomic 0600 write, other-venue eviction, `credentials.ui.json`
manifest, rebind halt), so a chat bind and a web bind leave the machine in the
identical state. Never write `.env` by hand: a hand-written pair is invisible
to `lib/venue_wiring` (not in the manifest) and skips the eviction/halt path.

Only venues with shipped `lib/account_*` + `lib/order_*` (web CX_VENUES crypto
group). Taiwan brokers have their own onboarding docs, paper has no keys, and
any other exchange is bound from the web 自動下單 page.

Nothing here returns or logs a key value.
"""
import importlib.util
import os
import re

VENUES = {
    "binance": ("API_KEY", "SECRET_KEY"),
    "bingx": ("API_KEY", "SECRET_KEY"),
    "okx": ("API_KEY", "SECRET_KEY", "PASSPHRASE"),
    "gateio": ("API_KEY", "SECRET_KEY"),
}
_ELSEWHERE = {
    "capital": "references/capital-broker.md",
    "sinopac": "references/sinopac-broker.md",
    "president": "references/president-broker.md",
    "paper": "the web 自動下單 page (paper has no keys)",
}
_WS_MARK_RE = re.compile(r"\s")


def _workspace():
    return os.path.abspath(
        os.environ.get("BLAVE_AGENT_WORKSPACE")
        or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def _runtime_listener():
    """The runtime lives beside the workspace at `<base>/current` (Linux
    `/opt/blave-agent`, Windows `C:\\blave-agent`) — not on sys.path."""
    ws = _workspace()
    path = os.path.join(os.path.dirname(ws), "current", "command_listener.py")
    if not os.path.isfile(path):
        raise RuntimeError(f"platform runtime not found at {path}")
    spec = importlib.util.spec_from_file_location("_blave_runtime_command_listener", path)
    # the module resolves its workspace at import time
    os.environ.setdefault("BLAVE_AGENT_WORKSPACE", ws)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bound_ids(cl):
    try:
        with open(os.path.join(cl.WORKSPACE, ".env")) as f:
            lines = f.read().splitlines()
    except OSError:
        return set()
    return {i.lower() for i in cl._venue_cred_ids(lines)}


def bind(venue_id, env):
    """Bind `venue_id` with `env` = {"{ID}_API_KEY": ..., "{ID}_SECRET_KEY": ...,
    ("{ID}_PASSPHRASE": ... for okx)}. Exactly this venue's key names, nothing
    else. Returns a summary without key values: {"venue", "written" (env
    names), "bound" (venue ids with a complete pair in `.env`), "evicted"
    (venue ids the bind replaced — trading was halted for them)}."""
    vid = str(venue_id or "").strip().lower()
    if vid in _ELSEWHERE:
        raise ValueError(f"{vid} is not bound from chat — see {_ELSEWHERE[vid]}")
    if vid not in VENUES:
        raise ValueError(
            f"unknown venue {vid!r}; chat binding supports {', '.join(VENUES)} — "
            "other exchanges are bound from the web 自動下單 page"
        )
    if not isinstance(env, dict):
        raise ValueError("env must be a dict of {ENV_NAME: value}")
    want = [f"{vid.upper()}_{s}" for s in VENUES[vid]]
    got = {str(k).strip().upper(): v for k, v in env.items()}
    missing = [k for k in want if not got.get(k)]
    extra = sorted(set(got) - set(want))
    if missing or extra:
        raise ValueError(f"env must hold exactly {want}; missing={missing} extra={extra}")
    for k in want:
        v = got[k]
        if not isinstance(v, str) or not v.isascii() or _WS_MARK_RE.search(v):
            raise ValueError(f"{k}: value must be a single ASCII token (no whitespace)")

    cl = _runtime_listener()
    # bound set from .env pairs, not the manifest: a pre-manifest machine has no
    # manifest yet, but the runtime evicts + halts the old venue all the same
    before = _bound_ids(cl)
    cl._in_workspace(cl._cmd_credentials, {"env": {k: got[k] for k in want}})
    bound = _bound_ids(cl)
    return {
        "venue": vid,
        "written": want,
        "bound": sorted(bound),
        "evicted": sorted(before - bound),
    }
