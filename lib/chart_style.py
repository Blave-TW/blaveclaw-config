"""Blave chart style for matplotlib — call apply() before plotting.

Colors come from the Blave design system (design-system.md tokens).
Category series use the --color-data-* scale; up/down use the trading colors.
"""

import matplotlib
from cycler import cycler

# Category series (--color-data-*): use in order for multi-series charts.
DATA_COLORS = ["#ff9960", "#5b7a9e", "#c79a3e", "#9a6f8e"]

# Trading colors — direction only (up/long/win vs down/short/loss).
# Never use them as generic category colors.
GREEN = "#31a86a"       # lines & fills
RED = "#d94f5c"
GREEN_TEXT = "#247b4d"  # text annotations (AA on white; GREEN/RED are not)
RED_TEXT = "#b8434e"

# Neutrals
TEXT = "#1a222c"        # titles, axis labels
TEXT_SOFT = "#647082"   # tick labels, captions
GRID = "#d5dde4"        # gridlines, hairlines


def apply():
    """Set Blave rcParams. Call once, before creating any figure."""
    matplotlib.rcParams.update({
        "figure.facecolor": "#ffffff",
        "axes.facecolor": "#ffffff",
        "savefig.facecolor": "#ffffff",
        "savefig.dpi": 150,
        "savefig.bbox": "tight",
        "axes.prop_cycle": cycler(color=DATA_COLORS),
        "axes.edgecolor": GRID,
        "axes.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "axes.axisbelow": True,
        "text.color": TEXT,
        "axes.labelcolor": TEXT,
        "axes.titlecolor": TEXT,
        "axes.titlesize": 13,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "axes.labelsize": 10,
        "xtick.color": TEXT_SOFT,
        "ytick.color": TEXT_SOFT,
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
        "xtick.direction": "out",
        "ytick.direction": "out",
        "legend.frameon": False,
        "legend.fontsize": 9,
        "legend.labelcolor": TEXT_SOFT,
        "lines.linewidth": 1.4,
    })
