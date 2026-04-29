'''
Strategy: [strategy name]
Symbol: BTCUSDT
Interval: 1h
Logic: [entry/exit rules]
'''

import gzip, json, logging, os, requests
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from dotenv import dotenv_values

# --- Config ---
MODE            = "backtest"       # "backtest" | "paper" | "live"
STRATEGY_NAME   = "[strategy_name]"
SYMBOL          = "BTCUSDT"
INTERVAL        = "1h"
START           = "2024-01-01"     # backtest start; also used as live history start
END             = None             # backtest end date; None = latest data; live/paper always fetches to today
FEE             = 0.0005           # 0.05% per side (taker fee) — deducted on every BUY and SELL

_env = dotenv_values()

# --- Logging ---
os.makedirs('/root/.openclaw/workspace/logs', exist_ok=True)
logging.basicConfig(
    filename=f'/root/.openclaw/workspace/logs/{STRATEGY_NAME}.log',
    level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s'
)

# --- Helpers ---
def load_state(): ...
def save_state(state): ...
def send_telegram(msg): ...
def place_order(side): ...   # implement using exchange API — read skills/blave-quant/references/<exchange>-skill.md

def fetch_historical(symbol, start, end):
    from datetime import datetime, timedelta
    headers = {'api-key': _env.get('blave_api_key', ''), 'secret-key': _env.get('blave_secret_key', '')}
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.strptime(end, '%Y-%m-%d') if end else datetime.now()
    rows = []
    cursor = s
    while cursor < e:
        chunk_end = min(cursor + timedelta(days=365), e)
        resp = requests.get(
            'https://api.blave.org/kline',
            headers=headers,
            params={'symbol': symbol, 'period': INTERVAL,
                    'start_date': cursor.strftime('%Y-%m-%d'),
                    'end_date': chunk_end.strftime('%Y-%m-%d')},
            timeout=60,
        )
        resp.raise_for_status()
        rows.extend(resp.json())
        cursor = chunk_end
    return rows

def compute_signal(candle, state) -> str:
    # Return desired position state — "LONG" or "FLAT"
    # Pure function: no API calls, no I/O, identical in backtest and live
    ...

def compute_strat_returns(candles, trades_log):
    trades_sorted = sorted(trades_log, key=lambda t: t['time'])
    trade_idx = 0
    in_pos = False
    strat_returns = []
    for i, candle in enumerate(candles):
        bar_ret = (candle['close'] - candles[i-1]['close']) / candles[i-1]['close'] if i > 0 else 0.0
        pos = 1 if in_pos else 0
        while trade_idx < len(trades_sorted) and trades_sorted[trade_idx]['time'] == candle['time']:
            in_pos = trades_sorted[trade_idx]['action'] == 'BUY'
            trade_idx += 1
        new_pos = 1 if in_pos else 0
        fee = abs(new_pos - pos) * FEE
        strat_returns.append(pos * bar_ret - fee)
    return strat_returns

def plot_pnl(candles, state, symbol):
    closes = [c['close'] for c in candles]
    times  = [c['time']  for c in candles]

    strat_ret = np.array(compute_strat_returns(candles, state.get('trades_log', [])))
    cum  = np.cumprod(1 + strat_ret)
    peak = np.maximum.accumulate(cum)
    dd   = (cum - peak) / peak

    action_map = {t['time']: t['action'] for t in sorted(state.get('trades_log', []), key=lambda t: t['time'])}
    in_pos = False
    pos = []
    for c in candles:
        if c['time'] in action_map:
            in_pos = action_map[c['time']] == 'BUY'
        pos.append(1 if in_pos else 0)
    pos = np.array(pos)

    dates      = pd.to_datetime(times, unit='s', utc=True)
    indicators = state.get('indicators', [])
    n_panels   = 2 + (1 if indicators else 0)
    fig, axes  = plt.subplots(n_panels, 1, figsize=(14, 4 * n_panels), sharex=True,
                               gridspec_kw={'height_ratios': [3, 1] + [1] * (n_panels - 2)})

    # Panel 1: Price + cumulative PnL
    ax1 = axes[0]; ax2 = ax1.twinx()
    ax1.plot(dates, closes, color="#3498db", lw=1, alpha=0.7, label="Price")
    ax1.set_ylabel("Price (USD)", fontsize=11, color="#3498db")
    ax1.tick_params(axis='y', labelcolor="#3498db")
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax2.plot(dates, (cum - 1) * 100, color="#2ecc71", lw=1.5, label=f"{STRATEGY_NAME} (+fees)")
    ax2.axhline(0, color="#888", lw=0.5, ls="--")
    ax2.set_ylabel("Strategy Return (%)", fontsize=11, color="#2ecc71")
    ax2.tick_params(axis='y', labelcolor="#2ecc71")
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f}%"))
    prev = False
    for date, inp in zip(dates, pos > 0):
        if inp and not prev: entry_date = date
        if not inp and prev: ax1.axvspan(entry_date, date, alpha=0.08, color="#2ecc71")
        prev = inp
    if prev: ax1.axvspan(entry_date, dates[-1], alpha=0.08, color="#2ecc71")
    l1, lb1 = ax1.get_legend_handles_labels()
    l2, lb2 = ax2.get_legend_handles_labels()
    ax1.legend(l1 + l2, lb1 + lb2, fontsize=10, loc="upper left")
    ax1.set_title(f"{symbol} — {STRATEGY_NAME}", fontsize=13)

    # Panel 2: Drawdown
    axes[1].fill_between(dates, dd * 100, 0, color="#e74c3c", alpha=0.6)
    axes[1].set_ylabel("Drawdown (%)", fontsize=11)
    axes[1].yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f}%"))
    axes[1].axhline(0, color="#888", lw=0.5)

    # Panel 3: first indicator series (if any)
    if indicators:
        ind       = indicators[0]
        ind_dates = pd.to_datetime([d[0] for d in ind['data']], unit='s', utc=True)
        axes[2].plot(ind_dates, [d[1] for d in ind['data']], lw=0.8)
        axes[2].set_ylabel(ind['name'], fontsize=11)

    plt.tight_layout()
    fname = f"{symbol}_{STRATEGY_NAME.replace(' ', '_')}_pnl.png"
    plt.savefig(fname, dpi=150)
    plt.show()
    print(f"Saved: {fname}")

def upload_report(candles, state):
    klines    = [[c['time'], c['open'], c['high'], c['low'], c['close']] for c in candles]
    strat_ret = compute_strat_returns(candles, state.get('trades_log', []))
    body = json.dumps({
        'strategy_name': STRATEGY_NAME,
        'symbol':        SYMBOL,
        'interval':      INTERVAL,
        'mode':          MODE,
        'code':          open(__file__).read(),
        'trades':        state.get('trades_log', []),
        'klines':        klines,
        'indicators':    state.get('indicators', []),
        'returns':       [0.0 if r != r else r for r in strat_ret],
    }).encode()
    requests.post(
        'https://api.blave.org/openclaw/strategy/report',
        headers={
            'api-key':          _env.get('blave_api_key', ''),
            'secret-key':       _env.get('blave_secret_key', ''),
            'Content-Type':     'application/json',
            'Content-Encoding': 'gzip',
        },
        data=gzip.compress(body),
        timeout=60,
    ).raise_for_status()


def execute(candle, signal, state):
    # Compare desired signal state with current position, act on mismatch.
    want_long = signal == "LONG"
    if want_long and not state["in_position"]:
        state.update({"in_position": True, "entry": candle["close"]})
        state["trades_log"].append({"time": candle["time"], "action": "BUY", "price": candle["close"]})
        if MODE == "live":
            place_order("BUY")
        if MODE in ("live", "paper"):
            send_telegram(f"BUY @ {candle['close']}")
        else:
            logging.info(f"[BACKTEST] BUY @ {candle['close']}")

    elif not want_long and state["in_position"]:
        pnl = (candle["close"] - state["entry"]) / state["entry"] * 100
        state.update({"in_position": False, "entry": None,
                       "pnl": state["pnl"] + pnl, "trades": state["trades"] + 1})
        state["trades_log"].append({"time": candle["time"], "action": "SELL", "price": candle["close"]})
        if MODE == "live":
            place_order("SELL")
        if MODE in ("live", "paper"):
            send_telegram(f"SELL @ {candle['close']}  PnL={pnl:+.2f}%")
        else:
            logging.info(f"[BACKTEST] SELL @ {candle['close']}  PnL={pnl:+.2f}%")


def main():
    from datetime import datetime
    today   = datetime.now().strftime('%Y-%m-%d')
    end     = END if MODE == "backtest" else today
    candles = fetch_historical(SYMBOL, START, end)

    if MODE == "backtest":
        state = {"in_position": False, "entry": None, "pnl": 0.0, "trades": 0, "trades_log": [], "indicators": []}
    else:
        state = load_state()
        if state is None:
            # First live run — bootstrap state from history (no orders placed)
            state = {"in_position": False, "entry": None, "pnl": 0.0, "trades": 0, "trades_log": [], "indicators": []}
            for candle in candles[:-1]:
                sig = compute_signal(candle, state)
                if sig == "LONG" and not state["in_position"]:
                    state.update({"in_position": True, "entry": candle["close"]})
                    state["trades_log"].append({"time": candle["time"], "action": "BUY", "price": candle["close"]})
                elif sig == "FLAT" and state["in_position"]:
                    pnl = (candle["close"] - state["entry"]) / state["entry"] * 100
                    state.update({"in_position": False, "entry": None, "pnl": state["pnl"] + pnl, "trades": state["trades"] + 1})
                    state["trades_log"].append({"time": candle["time"], "action": "SELL", "price": candle["close"]})
            save_state(state)
            logging.info(f"Bootstrapped state: in_position={state['in_position']} entry={state['entry']}")

    bars = candles if MODE == "backtest" else [candles[-1]]

    for candle in bars:
        signal = compute_signal(candle, state)
        logging.info(f"signal={signal} close={candle['close']}")
        execute(candle, signal, state)

    if MODE == "backtest":
        print(f"Total PnL: {state['pnl']:+.2f}%  Trades: {state['trades']}")
        plot_pnl(candles, state, SYMBOL)

    upload_report(candles, state)

    if MODE != "backtest":
        save_state(state)


if __name__ == "__main__":
    main()
