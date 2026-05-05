import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def reconstruct_arrays(df, stats, vol_lookback=720, hours_per_year=8760):
    """Reconstruct equity, position, realized vol, cumulative return arrays from backtesting.py stats."""
    equity    = stats['_equity_curve']['Equity'].reindex(df.index, method='ffill')
    strat_ret = equity.pct_change().fillna(0).values
    position  = np.zeros(len(df))
    for _, row in stats['_trades'].iterrows():
        i0 = df.index.searchsorted(row['EntryTime'])
        i1 = df.index.searchsorted(row['ExitTime'])
        position[i0:i1] = 1.0
    close        = df['Close'].values
    log_ret      = np.concatenate([[0.0], np.log(close[1:] / close[:-1])])
    realized_vol = pd.Series(log_ret).rolling(vol_lookback).std().values * np.sqrt(hours_per_year)
    cum          = equity.values / equity.values[0]
    return {'strat_ret': strat_ret, 'position': position, 'realized_vol': realized_vol, 'cum': cum}


def regime_analysis(df, result, vol_lookback=720, hours_per_year=8760):
    """Print text table of strategy performance by year / bull-bear / high-low vol regime."""
    strat_ret    = result['strat_ret']
    realized_vol = result['realized_vol']
    close        = df['Close'].values
    dates        = df.index

    ma_window  = vol_lookback * 200 // 30
    ma200      = pd.Series(close).rolling(ma_window).mean().values
    valid_ma   = ~np.isnan(ma200)
    vol_median = np.nanmedian(realized_vol)
    valid_vol  = ~np.isnan(realized_vol)
    bull       = close > ma200
    highvol    = realized_vol > vol_median

    def _stats(mask):
        r = strat_ret[mask]; r = r[~np.isnan(r)]
        if len(r) < 2: return None
        total_years = len(r) / hours_per_year
        cum_r   = np.prod(1 + r) - 1
        ann_r   = (1 + cum_r) ** (1 / total_years) - 1 if total_years > 0 else np.nan
        ann_vol = r.std() * np.sqrt(hours_per_year)
        sharpe  = (r.mean() / r.std()) * np.sqrt(hours_per_year) if r.std() > 0 else np.nan
        cc = np.cumprod(1 + r); pk = np.maximum.accumulate(cc)
        mdd = ((cc - pk) / pk).min()
        n_total = len(strat_ret[~np.isnan(strat_ret)])
        return dict(ann_ret=ann_r, ann_vol=ann_vol, sharpe=sharpe, max_dd=mdd, pct_time=len(r) / n_total)

    rows = []
    for yr in sorted(dates.year.unique()):
        s = _stats(dates.year == yr)
        if s: rows.append({'label': str(yr), **s})
    rows.append({'label': '─' * 20})
    for label, mask in [('Bull (price > MA200)', bull & valid_ma),
                         ('Bear (price < MA200)', ~bull & valid_ma)]:
        s = _stats(mask)
        if s: rows.append({'label': label, **s})
    rows.append({'label': '─' * 20})
    for label, mask in [('High Vol (>median)',  highvol & valid_vol),
                         ('Low  Vol (≤median)', ~highvol & valid_vol)]:
        s = _stats(mask)
        if s: rows.append({'label': label, **s})

    hdr = f"  {'Regime':<22} {'Ann Ret':>9} {'Ann Vol':>9} {'Sharpe':>8} {'MDD':>8} {'Time%':>7}"
    print(f"\n{'─' * len(hdr)}\n  Regime Analysis\n{'─' * len(hdr)}\n{hdr}\n{'─' * len(hdr)}")
    for row in rows:
        if 'ann_ret' not in row:
            print(f"  {row['label']}")
            continue
        print(f"  {row['label']:<22} {row['ann_ret']*100:>8.1f}% {row['ann_vol']*100:>8.1f}%"
              f" {row['sharpe']:>8.2f} {row['max_dd']*100:>7.1f}% {row['pct_time']*100:>6.1f}%")
    print('─' * len(hdr))


def plot_regime(df, result, title='Regime Analysis', vol_lookback=720, hours_per_year=8760, output_path='/tmp/regime.png'):
    """Bar chart of ann_ret/sharpe/mdd by year, trend regime, volatility regime."""
    strat_ret    = result['strat_ret']
    realized_vol = result['realized_vol']
    close        = df['Close'].values
    dates        = df.index

    ma_window  = vol_lookback * 200 // 30
    ma200      = pd.Series(close).rolling(ma_window).mean().values
    valid_ma   = ~np.isnan(ma200)
    vol_median = np.nanmedian(realized_vol)
    valid_vol  = ~np.isnan(realized_vol)
    bull       = close > ma200
    highvol    = realized_vol > vol_median

    def _stats(mask):
        r = strat_ret[mask]; r = r[~np.isnan(r)]
        if len(r) < 2: return None
        total_years = len(r) / hours_per_year; cum_r = np.prod(1 + r) - 1
        ann_r   = (1 + cum_r) ** (1 / total_years) - 1 if total_years > 0 else np.nan
        ann_vol = r.std() * np.sqrt(hours_per_year)
        sharpe  = (r.mean() / r.std()) * np.sqrt(hours_per_year) if r.std() > 0 else np.nan
        cc = np.cumprod(1 + r); pk = np.maximum.accumulate(cc)
        return dict(ann_ret=ann_r, sharpe=sharpe, max_dd=((cc - pk) / pk).min())

    groups = {
        'By Year':           [(str(yr), _stats(dates.year == yr)) for yr in sorted(dates.year.unique())],
        'Trend Regime':      [('Bull\n(>MA200)', _stats(bull & valid_ma)),
                               ('Bear\n(<MA200)', _stats(~bull & valid_ma))],
        'Volatility Regime': [('High Vol\n(>median)', _stats(highvol & valid_vol)),
                               ('Low Vol\n(≤median)',  _stats(~highvol & valid_vol))],
    }
    groups = {k: [(lbl, s) for lbl, s in v if s is not None] for k, v in groups.items()}

    fig, axes = plt.subplots(1, 3, figsize=(16, 6))
    for ax, (group_name, items) in zip(axes, groups.items()):
        labels  = [lbl for lbl, _ in items]
        ann_ret = [s['ann_ret'] * 100 for _, s in items]
        sharpe  = [s['sharpe']        for _, s in items]
        mdd     = [s['max_dd'] * 100  for _, s in items]
        x = np.arange(len(labels)); w = 0.25
        b1 = ax.bar(x - w, ann_ret, w, label='Ann Ret (%)', color='#3498db', alpha=0.85)
        b2 = ax.bar(x,     sharpe,  w, label='Sharpe',      color='#2ecc71', alpha=0.85)
        b3 = ax.bar(x + w, mdd,     w, label='MDD (%)',     color='#e74c3c', alpha=0.85)
        for bars in [b1, b2, b3]:
            for bar in bars:
                h = bar.get_height()
                ax.text(bar.get_x() + bar.get_width() / 2,
                        h + (0.3 if h >= 0 else -1.5),
                        f'{h:.1f}', ha='center',
                        va='bottom' if h >= 0 else 'top', fontsize=8)
        ax.axhline(0, color='#555', lw=0.8)
        ax.set_title(group_name, fontsize=13, fontweight='bold')
        ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=10)
        ax.set_ylabel('Value', fontsize=10); ax.legend(fontsize=9)
        all_vals = ann_ret + sharpe + mdd
        ax.set_ylim(min(all_vals) - 5, max(all_vals) + 8)

    fig.suptitle(title, fontsize=13, fontweight='bold', y=1.01)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'Chart saved: {output_path}')
    return output_path


def plot_pnl(df, result, title='Strategy PnL', output_path='/tmp/pnl.png', extra_panels=None):
    """Generic 2-panel PnL chart: price+return / drawdown.
    extra_panels: list of dicts [{'data': array, 'label': str, 'color': str, 'hlines': [(y, color, label)]}]
    """
    close  = df['Close'].values
    dates  = df.index
    cum    = result['cum']
    pos    = result['position']
    peak   = np.maximum.accumulate(cum)
    dd     = (cum - peak) / peak

    n_panels = 2 + (len(extra_panels) if extra_panels else 0)
    height_ratios = [3, 1] + [1] * (n_panels - 2)
    fig, axes = plt.subplots(n_panels, 1, figsize=(14, 4 + 2 * n_panels),
                              sharex=True, gridspec_kw={'height_ratios': height_ratios})
    if n_panels == 1:
        axes = [axes]

    # Panel 1: price + strategy return
    ax1 = axes[0]
    ax2 = ax1.twinx()
    ax1.plot(dates, close, color='#3498db', lw=1, alpha=0.7, label='Price')
    ax1.set_ylabel('Price', fontsize=10, color='#3498db')
    ax1.tick_params(axis='y', labelcolor='#3498db')
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    ax2.plot(dates, (cum - 1) * 100, color='#2ecc71', lw=1.5, label='Strategy Return')
    ax2.axhline(0, color='#888', lw=0.5, ls='--')
    ax2.set_ylabel('Return (%)', fontsize=10, color='#2ecc71')
    ax2.tick_params(axis='y', labelcolor='#2ecc71')
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:.0f}%'))

    prev = False
    for i, (date, inp) in enumerate(zip(dates, pos > 0)):
        if inp and not prev: start = date
        if not inp and prev: ax1.axvspan(start, date, alpha=0.08, color='#2ecc71')
        prev = inp
    if prev: ax1.axvspan(start, dates[-1], alpha=0.08, color='#2ecc71')

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, fontsize=9, loc='upper left')
    ax1.set_title(title, fontsize=12)

    # Panel 2: drawdown
    axes[1].fill_between(dates, dd * 100, 0, color='#e74c3c', alpha=0.6)
    axes[1].set_ylabel('Drawdown (%)', fontsize=10)
    axes[1].yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:.0f}%'))
    axes[1].axhline(0, color='#888', lw=0.5)

    # Extra panels (strategy-specific indicators)
    for i, panel in enumerate(extra_panels or []):
        ax = axes[2 + i]
        ax.plot(dates, panel['data'], color=panel.get('color', '#9b59b6'), lw=0.8, alpha=0.8, label=panel.get('label', ''))
        for hline in panel.get('hlines', []):
            ax.axhline(hline[0], color=hline[1], lw=1, ls='--', label=hline[2] if len(hline) > 2 else None)
        ax.axhline(0, color='#888', lw=0.5)
        ax.set_ylabel(panel.get('label', ''), fontsize=10)
        ax.legend(fontsize=9, loc='upper right')

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    print(f'Chart saved: {output_path}')
    return output_path
