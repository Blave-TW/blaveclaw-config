'''
Strategy: [strategy name]
Symbol: BTCUSDT
Interval: 1h
Logic: [entry/exit rules]
'''

import logging, os, json, requests

# --- Config ---
MODE            = "backtest"       # "backtest" | "live"
STRATEGY_NAME   = "[strategy_name]"
SYMBOL          = "BTCUSDT"
INTERVAL        = "1h"
START           = "2024-01-01"     # backtest start; also used as live history start
END             = "2024-12-31"     # backtest only — live/paper ignores this and always fetches to today

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
def place_order(side): ...   # implement using exchange API — read skills/blave-quant/references/<exchange>.md

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
        strat_returns.append((1 if in_pos else 0) * bar_ret)
        while trade_idx < len(trades_sorted) and trades_sorted[trade_idx]['time'] == candle['time']:
            in_pos = trades_sorted[trade_idx]['action'] == 'BUY'
            trade_idx += 1
    return strat_returns

def upload_report(candles, state):
    env = dict(line.strip().split('=', 1) for line in open('/root/.openclaw/workspace/.env') if '=' in line)
    klines = [[c['time'], c['open'], c['high'], c['low'], c['close']] for c in candles]
    requests.post(
        'https://api.blave.org/openclaw/strategy/report',
        headers={'api-key': env.get('blave_api_key',''), 'secret-key': env.get('blave_secret_key','')},
        json={
            'strategy_name': STRATEGY_NAME,
            'symbol':        SYMBOL,
            'interval':      INTERVAL,
            'mode':          MODE,
            'code':          open(__file__).read(),
            'trades':        state.get('trades_log', []),
            'klines':        klines,
            'indicators':    state.get('indicators', []),
            'returns':       compute_strat_returns(candles, state.get('trades_log', [])),
        },
        timeout=30,
    )


def execute(candle, signal, state):
    # Compare desired signal state with current position, act on mismatch.
    want_long = signal == "LONG"
    if want_long and not state["in_position"]:
        state.update({"in_position": True, "entry": candle["close"]})
        state["trades_log"].append({"time": candle["time"], "action": "BUY", "price": candle["close"]})
        if MODE == "live":
            place_order("BUY")
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
            send_telegram(f"SELL @ {candle['close']}  PnL={pnl:+.2f}%")
        else:
            logging.info(f"[BACKTEST] SELL @ {candle['close']}  PnL={pnl:+.2f}%")


def main():
    from datetime import datetime, timezone
    today   = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    end     = END if MODE == "backtest" else today
    candles = fetch_historical(SYMBOL, START, end)

    if MODE == "backtest":
        state = {"in_position": False, "entry": None, "pnl": 0.0, "trades": 0, "trades_log": []}
    else:
        state = load_state()
        if state is None:
            # First live run — bootstrap state from history (no orders placed)
            state = {"in_position": False, "entry": None, "pnl": 0.0, "trades": 0, "trades_log": []}
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

    upload_report(candles, state)

    if MODE == "backtest":
        print(f"Total PnL: {state['pnl']:+.2f}%  Trades: {state['trades']}")
    else:
        save_state(state)


if __name__ == "__main__":
    main()
