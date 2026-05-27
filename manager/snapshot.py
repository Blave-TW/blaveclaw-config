import sys, json, logging, os, importlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from datetime import datetime, timezone
from dotenv import dotenv_values


def _plot_equity(path='manager/snapshot_equity.png'):
    """Draw equity curve from snapshots.jsonl. Returns path if successful, None if < 2 points."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from collections import defaultdict

        records = defaultdict(list)
        with open('manager/snapshots.jsonl') as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r.get('equity') is not None:
                        records[f"{r['exchange']} ({r['currency']})"].append(
                            (datetime.fromisoformat(r['ts']), float(r['equity']))
                        )
                except Exception:
                    pass

        series = {k: sorted(v) for k, v in records.items() if len(v) >= 2}
        if not series:
            return None

        fig, ax = plt.subplots(figsize=(10, 4))
        for label, pts in series.items():
            xs, ys = zip(*pts)
            ax.plot(xs, ys, marker='o', markersize=3, label=label)

        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        fig.autofmt_xdate()
        ax.set_title('Account Equity')
        ax.set_ylabel('Equity')
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(path, dpi=130)
        plt.close(fig)
        return path
    except Exception as e:
        logging.warning(f'[snapshot] chart failed: {e}')
        return None


def get_account_equity(exchange: str, env: dict) -> dict:
    """Auto-dispatches to lib/account_{exchange}.py → get_equity(env).
    Create that file from lib/account_TEMPLATE.py when wiring a new exchange.
    Returns {'equity': float, 'currency': str}
    """
    try:
        mod = importlib.import_module(f'lib.account_{exchange}')
    except ImportError:
        raise NotImplementedError(
            f"lib/account_{exchange}.py not found — "
            f"create it from lib/account_TEMPLATE.py"
        )
    return mod.get_equity(env)


def get_positions(exchange: str, env: dict) -> list:
    """Auto-dispatches to lib/account_{exchange}.py → get_positions(env).
    Returns [{'symbol', 'side', 'size', 'mark_price'}, ...] or [] if flat.
    If get_positions is not yet implemented in the lib file, silently returns [].
    """
    try:
        mod = importlib.import_module(f'lib.account_{exchange}')
    except ImportError:
        raise NotImplementedError(
            f"lib/account_{exchange}.py not found — "
            f"create it from lib/account_TEMPLATE.py"
        )
    if not hasattr(mod, 'get_positions'):
        return []
    return mod.get_positions(env)


def _load_portfolio_config():
    path = 'manager/portfolio_config.json'
    if not os.path.exists(path):
        return {'exchanges': {}}
    with open(path) as f:
        return json.load(f)


def _load_last_snapshots(exchanges):
    """Return {exchange: last_record} from snapshots.jsonl, or {} if missing."""
    path = 'manager/snapshots.jsonl'
    if not os.path.exists(path):
        return {}
    last = {}
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
                last[rec['exchange']] = rec
            except Exception:
                pass
    return last


def _append_snapshot(record):
    os.makedirs('manager', exist_ok=True)
    with open('manager/snapshots.jsonl', 'a') as f:
        f.write(json.dumps(record) + '\n')


def _make_sender():
    try:
        from lib.notify import make_sender
        return make_sender()
    except Exception:
        return None


def run_snapshot():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    config    = _load_portfolio_config()
    env       = dotenv_values()
    exchanges = sorted(set(config.get('exchanges', {}).values()))

    if not exchanges:
        logging.warning('[snapshot] No exchanges configured in portfolio_config.json')
        return

    ts_now   = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    ts_label = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
    last     = _load_last_snapshots(exchanges)

    equity_lines   = []
    position_lines = []
    errors         = []
    any_snapshot   = False

    for exchange in exchanges:
        # ── Equity ────────────────────────────────────────────────────────────
        try:
            result   = get_account_equity(exchange, env)
            equity   = float(result['equity'])
            currency = result['currency']
        except NotImplementedError:
            logging.warning(f'[snapshot] {exchange}: get_account_equity not implemented — skipping')
            continue
        except Exception as e:
            logging.error(f'[snapshot] {exchange}: equity query failed — {e}')
            errors.append(f'{exchange.upper()} equity: {e}')
            continue

        prev      = last.get(exchange)
        daily_pnl = None
        daily_pct = None
        is_first  = prev is None or prev.get('equity') is None

        if not is_first:
            prev_equity = float(prev['equity'])
            if prev_equity != 0:
                daily_pnl = round(equity - prev_equity, 4)
                daily_pct = round(daily_pnl / prev_equity, 6)

        _append_snapshot({
            'ts': ts_now, 'exchange': exchange,
            'equity': equity, 'currency': currency,
            'daily_pnl': daily_pnl, 'daily_pnl_pct': daily_pct,
        })
        any_snapshot = True

        eq_str = f'{equity:,.2f} {currency}'
        if daily_pnl is not None:
            sign = '+' if daily_pnl >= 0 else ''
            equity_lines.append(
                f'  {exchange.upper():<8} {eq_str:<22} ({sign}{daily_pnl:,.2f} / {sign}{daily_pct*100:.2f}%)'
            )
        else:
            equity_lines.append(f'  {exchange.upper():<8} {eq_str}')

        logging.info(f'[snapshot] {exchange}: {equity} {currency}  daily_pnl={daily_pnl}')

        # ── Positions ─────────────────────────────────────────────────────────
        try:
            positions = get_positions(exchange, env)
            if positions:
                for p in positions:
                    side  = p.get('side', '').capitalize()
                    sym   = p.get('symbol', '')
                    size  = p.get('size', 0)
                    price = p.get('mark_price', 0)
                    position_lines.append(
                        f'  {exchange.upper():<8} {sym:<12} {side:<6} {size} @ {price:,.2f}'
                    )
            else:
                position_lines.append(f'  {exchange.upper():<8} (flat)')
        except NotImplementedError:
            pass  # positions not wired yet — silently skip
        except Exception as e:
            logging.warning(f'[snapshot] {exchange}: positions query failed — {e}')
            errors.append(f'{exchange.upper()} positions: {e}')

    # ── Build Telegram message ─────────────────────────────────────────────────
    msg_parts = [f'📊 Daily Snapshot — {ts_label}', '']

    if equity_lines:
        msg_parts += ['💰 Account Equity:'] + equity_lines

    if position_lines:
        msg_parts += ['', '📋 Positions:'] + position_lines

    if errors:
        msg_parts += ['', '⚠️ Errors:'] + [f'  {e}' for e in errors]

    if not any_snapshot and not errors:
        msg_parts += ['', '⚠️ No exchanges implemented yet — see snapshot.py get_account_equity()']

    msg = '\n'.join(msg_parts)
    print(msg)

    send = _make_sender()
    if send:
        chart = _plot_equity()
        if chart:
            try:
                from lib.notify import send_photo
                send_photo(chart)
            except Exception as e:
                logging.warning(f'[snapshot] send_photo failed: {e}')
        send(msg)


if __name__ == '__main__':
    run_snapshot()
