"""
Polls every minute (via cron) for the newly-closed bar a strategy needs, and
only runs the strategy once the data has actually landed.

Why: a plain hourly (or any-interval) cron fires at the bar boundary itself,
but the exchange/Blave API doesn't always have that bar ready that instant —
a fixed "run 5 minutes late" buffer either wastes minutes on fast days or
isn't enough on slow ones. This checks freshness directly instead of guessing
a fixed delay, and stays quiet once the bar for this cycle is processed.

Type A and Type C only. Type B strategies have no INTERVAL/fetch_data
contract to poll — they keep the old fixed-cadence cron straight to
run_strategy.sh (Linux) / strategy.py (Windows), see references/deployment.md.

Cron (replaces the old "N * * * *  bash manager/run_strategy.sh <name>"):
  * * * * * cd $BLAVECLAW_HOME/workspace && python3 manager/wait_for_bar.py <name>

Cross-platform by design (Capital/群益 strategies only run on Windows, which
has no bash): the actual strategy subprocess is launched with sys.executable
(whatever interpreter is running this script) directly against strategy.py —
this file does NOT shell out to manager/run_strategy.sh, since that script is
bash-only. It reimplements the same crash-safety contract in Python instead
(capture output, log + Telegram-alert on non-zero exit via
manager/alert_failure.alert(), touch the heartbeat on success) so both
platforms get identical protection.

State: state/bar_wait/<name>.json
  last_processed_bar : ISO timestamp of the bar this strategy already ran on
                        (fast-path exit — no fetch, no log — once this matches
                        the currently-expected bar)
  pending_since       : ISO timestamp of the first poll that found the
                         CURRENTLY-expected bucket not yet ready (reset once
                         it lands)
  last_seen_bar       : ISO timestamp of the latest bar actually observed in
                         fetch_data() on the last check, whether or not it
                         was enough to be "ready" (Type C: the straggler
                         symbol's last_valid_index — see _check_freshness).
                         Used to tell "still landing" apart from "market's
                         closed / data genuinely stuck" — see Market-closed
                         handling below.
  stall_alert_at      : epoch seconds of the last stale-data alert actually
                         sent for the CURRENT episode (None = never alerted
                         this episode yet). This one field is the sole "have
                         we already alerted" sentinel — it also drives the
                         poll throttle below and the STALL_REALERT_SECONDS
                         repeat cadence. Deliberately NOT derived by comparing
                         last_seen_bar against some other stored value: an
                         earlier version compared two independently-nullable
                         fields for equality, which silently NEVER alerted at
                         all for a symbol that had zero data ever (None ==
                         None reads as "already handled") and, separately,
                         could get stuck permanently un-repeating after a
                         state-file upgrade left the two fields already
                         equal. A single monotonic timestamp has neither
                         failure mode.
  last_attempt_failed_at : epoch seconds of the last failed run attempt (a
                            crashed run OR fetch_data() itself raising —
                            e.g. txf_composite_60m's deliberate fail-loud
                            RuntimeError on degraded dividend data), used to
                            back off retries instead of re-running (and
                            re-crashing) every single minute
  wrapper_error_alerted_at : epoch seconds of the last time THIS script (not
                              the strategy) alerted on its own uncaught error

Market-closed handling: a fixed-interval instrument like TXF doesn't trade
nights/weekends, but `_expected_closed_bar_open` doesn't know a trading
calendar — it just floors wall-clock time, so `expected` keeps advancing
hourly straight through a ~55h weekend close with no new bar ever landing.
Without special-casing this, that reads as an ordinary "still waiting" and
would re-alert once per NEW bucket forever (~55 alerts across one weekend).
Fixed by keying the poll throttle and the alert dedup to `last_seen_bar`
(whether the underlying DATA has moved) instead of to `expected` (whether
wall-clock time has moved): once alerted for a given `last_seen_bar`, further
checks throttle down to once every STALL_POLL_MINUTES instead of every
minute — and resume at full speed the moment the data moves. This needs no
trading-calendar knowledge; it reacts to observed data behavior only.

Repeat alerting during a stall: the FIRST alert for a given `last_seen_bar`
fires at STALE_TIMEOUT_MINUTES; after that, as long as it's still the SAME
`last_seen_bar` (no progress at all), a repeat fires every
STALL_REALERT_SECONDS instead of never again. This matters because
`last_seen_bar` for a genuinely delisted/permanently-halted Type C universe
member never changes — without a repeat, the very first alert would be the
LAST one ever sent, and the strategy (the whole universe, not just that one
symbol — Type C only runs once ALL columns are ready) would silently stop
rebalancing forever with no further signal that anything is wrong. The
straggler's identity (Type C: which column; Type A: n/a) is named in the
alert text specifically so this is actionable — "check if this symbol is
delisted and belongs out of UNIVERSE" — not just a timestamp to puzzle over.

Cost note: the freshness check is NOT free — it calls the strategy's real
fetch_data(), the same call the actual run makes (multiple HTTP/cache-backed
sources for a strategy like txf_composite_60m). While a bar is pending and
not yet in a detected stall this runs once a minute; see Market-closed
handling above for what happens once one is detected.

Type A strategies (fetch_data → single DataFrame): ready when its last row's
timestamp reaches the expected bar.
Type C strategies (fetch_data → (close_df, open_df[, ...])): ready when
EVERY column in close_df has a valid (non-NaN) observation at/after the
expected bar — wait for the whole universe, not just the first symbol to
update. Caveat (by design, not a bug to silently work around): a UNIVERSE
that includes a symbol with no data going forward at all (delisted, halted
indefinitely) will never satisfy this — the whole strategy stops rebalancing
until that symbol is removed from UNIVERSE. See "Repeat alerting" above for
why this still gets a periodic nudge instead of going silent.

Timeout: if the bar hasn't landed within STALE_TIMEOUT_MINUTES of when this
script first noticed it pending, send one Telegram alert (not a repeat every
minute) and keep polling — never give up silently.

Concurrency: a stale-safe lock file (state/bar_wait/<name>.lock) guards the
fetch+run section so an overrunning previous tick's subprocess can't overlap
with the next minute's — see _acquire_lock.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STALE_TIMEOUT_MINUTES = 15
STALL_POLL_MINUTES = 15            # once a stall is confirmed (alerted, data still hasn't moved),
                                    # only re-check this often instead of every minute
STALL_REALERT_SECONDS = 24 * 3600  # repeat the stale-data alert at most this often while the
                                    # SAME last_seen_bar persists — without this, a permanently
                                    # stuck symbol (delisted) gets exactly one alert ever, then
                                    # silence, even though the strategy never runs again
STATE_DIR = "state/bar_wait"
RUN_TIMEOUT_SECONDS = 600          # kill a strategy subprocess that hangs (e.g. a stuck order API call)
# The lock also has to outlive _check_freshness, which is NOT subprocess-timeout-bounded —
# it runs fetch_data() in-process and can legitimately take several minutes across multiple
# retried HTTP sources (e.g. txf_composite_60m pulls 60m/1d/TAIEX/dividend series). Generous
# on purpose: the lock's job is cleaning up a genuinely-dead process, not a tight SLA — being
# too short risks two ticks racing on the same bucket, which too-long doesn't.
LOCK_STALE_SECONDS = 1800
RETRY_BACKOFF_SECONDS = 300        # after a failed run OR a fetch_data() error, don't re-attempt every single minute
WRAPPER_ERROR_COOLDOWN_SECONDS = 6 * 3600         # this script's own crashes, not the strategy's


def _load_strategy_module(name):
    path = f"strategies/{name}/strategy.py"
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path} not found")
    spec = importlib.util.spec_from_file_location(f"strategy_wait_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_alert_failure_mod = None


def _alert_failure():
    """Loads manager/alert_failure.py by file path, NOT `from manager.alert_failure
    import alert` — measured on a real deployment: when this script is invoked the
    documented way (`python3 manager/wait_for_bar.py <name>`, cwd = workspace root),
    Python auto-prepends the SCRIPT'S OWN DIRECTORY (manager/) to sys.path. Since
    manager/manager.py also exists (the Manager & Reconciler CLI), `import manager`
    resolves to that single file instead of the manager/ package directory at the
    workspace root — an `ImportError` masquerading as "wait_for_bar.py itself is
    broken" while it's actually just failing to report a DIFFERENT failure. Loading
    by path sidesteps sys.path resolution entirely, same pattern as
    _load_strategy_module above and lib/allocator.py's load()."""
    global _alert_failure_mod
    if _alert_failure_mod is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert_failure.py")
        spec = importlib.util.spec_from_file_location("wait_for_bar_alert_failure", path)
        _alert_failure_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_alert_failure_mod)
    return _alert_failure_mod


# Unit spellings actually in use across strategies/examples: '1h', '5min',
# '1d', but also the bare-'m' form ('60m', '1m') — both must resolve to the
# SAME meaning here as in manager/healthcheck.py's copy of this table; if one
# changes, change both or the two scripts silently disagree on cadence.
_INTERVAL_RE = re.compile(r"^(\d+)(min|m|h|d|w)$")
_UNIT_TO_KW = {"min": "minutes", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _interval_to_timedelta(interval):
    m = _INTERVAL_RE.match(interval)
    if not m:
        raise ValueError(f"unrecognized INTERVAL format: {interval!r}")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{_UNIT_TO_KW[unit]: n})


def _expected_closed_bar_open(interval, now):
    """Open-time label of the most recently closed bar, epoch-aligned (matches
    how the underlying kline sources bucket bars — UTC midnight / top-of-hour)."""
    td = _interval_to_timedelta(interval)
    epoch = datetime(1970, 1, 1)
    since_epoch = now - epoch
    floored = epoch + (since_epoch // td) * td
    return floored - td


def _default_state():
    return {
        "last_processed_bar": None,
        "pending_since": None,
        "last_seen_bar": None,
        "stall_alert_at": None,
        "last_attempt_failed_at": None,
        "wrapper_error_alerted_at": None,
    }


def _load_state(name):
    path = f"{STATE_DIR}/{name}.json"
    if os.path.exists(path):
        try:
            return {**_default_state(), **json.load(open(path))}
        except Exception as e:
            print(f"[wait_for_bar] {name}: state file corrupt ({e}), resetting")
    return _default_state()


def _save_state(name, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = f"{STATE_DIR}/{name}.json"
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _acquire_lock(name, _retried=False):
    """Atomic-create lock file, cross-platform (no fcntl/msvcrt — those differ
    by OS). A lock older than LOCK_STALE_SECONDS is assumed abandoned by a
    crashed/killed process and is cleared once, then retried."""
    os.makedirs(STATE_DIR, exist_ok=True)
    lock_path = f"{STATE_DIR}/{name}.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return lock_path
    except FileExistsError:
        if _retried:
            return None
        try:
            age = time.time() - os.path.getmtime(lock_path)
        except OSError:
            age = 0
        if age > LOCK_STALE_SECONDS:
            try:
                os.remove(lock_path)
            except OSError:
                pass
            return _acquire_lock(name, _retried=True)
        return None  # another run is genuinely still in flight — skip this tick


def _release_lock(lock_path):
    try:
        os.remove(lock_path)
    except OSError:
        pass


def _check_freshness(mod, expected):
    """Calls the strategy's own fetch_data() and returns (observed_ts, ready, straggler):
      observed_ts : the latest bar timestamp actually seen (None if there's
                    no data at all yet) — Type C uses the STRAGGLER symbol's
                    last_valid_index, i.e. whichever symbol is furthest
                    behind, since that's what determines whether the universe
                    made any forward progress at all.
      ready       : True if that's enough to run — Type A: observed_ts is
                    at/after `expected`. Type C: EVERY universe column has a
                    valid observation at/after `expected` (wait for ALL
                    symbols — see module docstring caveat about symbols with
                    no data going forward at all).
      straggler   : Type A: always None (no per-symbol concept). Type C: the
                    column name furthest behind — named explicitly so an alert
                    can point at the actual symbol to investigate, not just a
                    bucket timestamp.

    Raises whatever fetch_data() raises (network errors, or a strategy's own
    deliberate fail-loud guard like txf_composite_60m's degraded-dividend
    check) — the caller is responsible for treating that as a failed attempt,
    not a "not ready yet".
    """
    from dotenv import dotenv_values
    env = dotenv_values()
    hdrs = {"api-key": env.get("blave_api_key", ""), "secret-key": env.get("blave_secret_key", "")}

    data = mod.fetch_data(hdrs)
    if isinstance(data, tuple):
        close_df = data[0]
        if close_df.empty:
            return None, False, None
        last_valid = close_df.apply(lambda col: col.last_valid_index())
        if last_valid.isna().any():
            # at least one symbol has literally never had a data point
            return None, False, last_valid[last_valid.isna()].index[0]
        straggler_symbol = last_valid.idxmin()
        straggler_ts = last_valid[straggler_symbol].to_pydatetime().replace(tzinfo=None)
        ready = all(ts.to_pydatetime().replace(tzinfo=None) >= expected for ts in last_valid)
        return straggler_ts, ready, straggler_symbol
    else:
        if data.empty:
            return None, False, None
        last_ts = data.index[-1].to_pydatetime().replace(tzinfo=None)
        return last_ts, last_ts >= expected, None


def _run_strategy_protected(name):
    """Cross-platform equivalent of manager/run_strategy.sh's crash-safety
    contract — launches strategy.py with the SAME interpreter running this
    script (sys.executable, so it's whatever venv/python cron is already
    using, no hardcoded 'python3'/'bash'), catches a hang with a timeout,
    logs + Telegram-alerts on failure, touches the heartbeat on success.

    Returns True on success, False otherwise.
    """
    strategy_dir = f"strategies/{name}"
    os.makedirs(strategy_dir, exist_ok=True)
    script_path = f"{strategy_dir}/strategy.py"

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS,
        )
        output, returncode = (result.stdout or "") + (result.stderr or ""), result.returncode
    except subprocess.TimeoutExpired as e:
        output = (str(e.output) if e.output else "") + \
            f"\n[wait_for_bar] killed after exceeding {RUN_TIMEOUT_SECONDS}s timeout"
        returncode = -1

    if returncode != 0:
        try:
            with open(f"{strategy_dir}/strategy.log", "a") as f:
                f.write(output)
        except OSError:
            pass
        _alert_failure().alert(name, returncode, output)
        return False

    os.makedirs("state/heartbeat", exist_ok=True)
    Path(f"state/heartbeat/{name}").touch()
    return True


def _alert_stale(name, expected, minutes_waited, straggler_symbol):
    who = f"（卡住的是 {straggler_symbol}，若已下市/長期停牌請從 UNIVERSE 移除）" if straggler_symbol is not None else ""
    try:
        from lib.notify import send_text
        send_text(
            f"⚠️ {name}：{expected.strftime('%Y-%m-%d %H:%M')} 那根 K 棒等了 "
            f"{minutes_waited} 分鐘還沒到位{who}，仍在重試，訊號會晚發。"
        )
    except Exception:
        pass  # best-effort — the alerter itself must never crash the cron job


def _alert_wrapper_error(name, exc):
    """wait_for_bar.py's OWN failure (bad INTERVAL, fetch_data raising, import
    error, etc.) — this happens BEFORE run_strategy's protection ever
    engages, so without this the failure would be entirely silent (cron
    swallows the traceback, no Telegram, nothing)."""
    state_path = f"{STATE_DIR}/{name}.json"
    now = time.time()
    try:
        state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    except Exception:
        state = {}
    last = state.get("wrapper_error_alerted_at") or 0
    if now - last < WRAPPER_ERROR_COOLDOWN_SECONDS:
        return
    try:
        from lib.notify import send_text
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1500:]
        send_text(f"⚠️ wait_for_bar.py 本身出錯（{name}），排程可能完全停擺：\n\n{tb}")
    except Exception:
        pass
    state["wrapper_error_alerted_at"] = now
    try:
        _save_state(name, {**_default_state(), **state})
    except Exception:
        pass


def _tick(name):
    mod = _load_strategy_module(name)
    interval = getattr(mod, "INTERVAL", None)
    if not interval:
        print(f"[wait_for_bar] {name}: no INTERVAL in strategy.py — Type B strategies "
              f"don't use wait_for_bar.py, check references/deployment.md")
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expected = _expected_closed_bar_open(interval, now)
    expected_iso = expected.isoformat()

    state = _load_state(name)

    # Fast path: this bar's already been run — stay quiet, no fetch, no log, no lock.
    if state.get("last_processed_bar") == expected_iso:
        return

    # Backoff: don't hammer a run (or a fetch_data() that itself keeps raising)
    # every single minute right after it just failed.
    last_failed = state.get("last_attempt_failed_at")
    if last_failed and time.time() - last_failed < RETRY_BACKOFF_SECONDS:
        return

    # Stall throttle: once we've confirmed a stall (alerted at least once this
    # episode — see stall_alert_at), stop polling every minute — this is what a
    # TXF weekend close looks like. Resumes full speed the moment data moves.
    if state.get("stall_alert_at") is not None:
        if now.minute % STALL_POLL_MINUTES != 0:
            return

    lock_path = _acquire_lock(name)
    if lock_path is None:
        return  # a previous tick's run is still in flight — skip, don't overlap
    try:
        try:
            observed_ts, ready, straggler_symbol = _check_freshness(mod, expected)
        except Exception as e:
            # fetch_data() itself raised — network error, or a strategy's own
            # deliberate fail-loud guard (e.g. txf_composite_60m's degraded-
            # dividend check). This is NOT a wait_for_bar.py bug, so route it
            # through the normal failed-run alert (its own 24h cooldown) —
            # not main()'s "wrapper itself is broken" alert — and back off
            # like any other failed attempt instead of retrying every minute.
            output = f"fetch_data() raised while checking freshness: {e}\n" + traceback.format_exc()
            _alert_failure().alert(name, "fetch_error", output)
            state["last_attempt_failed_at"] = time.time()
            _save_state(name, state)
            return

        observed_iso = observed_ts.isoformat() if observed_ts is not None else None

        if ready:
            if _run_strategy_protected(name):
                state["last_processed_bar"] = expected_iso
                state["pending_since"] = None
                state["last_seen_bar"] = observed_iso
                state["stall_alert_at"] = None
                state["last_attempt_failed_at"] = None
            else:
                state["last_attempt_failed_at"] = time.time()
            _save_state(name, state)
            return

        # Not ready yet. Data moved forward (even if still not enough) since the
        # last check → this is fresh lag, not a stall; clear stall_alert_at so a
        # NEW stall (if this stalls again at the new position) alerts fresh.
        # observed_iso is None whenever the strategy has NEVER returned any data
        # at all (empty fetch, or a Type C column with zero valid rows) — that
        # must NOT be read as "no progress, still None" and skipped here; it's
        # handled correctly below purely because stall_alert_at (not this block)
        # is what decides "already alerted", so a permanently-None observed_iso
        # still gets exactly one alert then a 24h repeat, never zero.
        if observed_iso is not None and observed_iso != state.get("last_seen_bar"):
            state["last_seen_bar"] = observed_iso
            state["stall_alert_at"] = None

        if state.get("pending_since") is None:
            state["pending_since"] = now.isoformat()
            _save_state(name, state)
            return

        pending_since = datetime.fromisoformat(state["pending_since"])
        waited = now - pending_since
        if waited >= timedelta(minutes=STALE_TIMEOUT_MINUTES):
            stall_alert_at = state.get("stall_alert_at")
            # stall_alert_at is None ⇒ never alerted for this episode yet (covers
            # both a brand-new stall AND a strategy that has NEVER had a single
            # data point — see comment above, that must not silently never-alert).
            due = stall_alert_at is None or (time.time() - stall_alert_at >= STALL_REALERT_SECONDS)
            if due:
                _alert_stale(name, expected, int(waited.total_seconds() // 60), straggler_symbol)
                state["stall_alert_at"] = time.time()
        _save_state(name, state)
    finally:
        _release_lock(lock_path)


def main():
    if len(sys.argv) < 2:
        print("usage: wait_for_bar.py <strategy_name>")
        return
    name = sys.argv[1]
    try:
        _tick(name)
    except Exception as e:
        print(f"[wait_for_bar] {name}: uncaught error: {e}")
        traceback.print_exc()
        _alert_wrapper_error(name, e)


if __name__ == "__main__":
    main()
