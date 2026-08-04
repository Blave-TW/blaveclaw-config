# Order library template — copy to lib/order_{exchange}.py and implement below.
# PERP (USDT-M swap) venues only — Taiwan brokers follow lib/order_sinopac.py's
# signed-diff contract instead, not this file.
#
# Before implementing: read skills/blave-quant/references/{exchange}-skill.md for
# endpoints, auth, broker attribution, and response schema, and read
# lib/order_bingx.py — it is the reference implementation of this contract
# (transport retries, guard integration, fill polling, hedge/one-way handling).
#
# THE FOUR NAMES BELOW ARE THE RECONCILER'S FIXED INTERFACE. manager/reconciler.py
# calls them by exactly these names; a lib missing any of them crashes at
# RECONCILE time (mid-trade), not at integration time — measured live 2026-08 when
# an OKX lib shipped without close_position_partial and every reduce order failed.
# Add richer strategy-facing helpers (open_position with SL/TP, limit orders,
# cancels — see order_bingx.py) on top, but never rename or omit these four.
#
# Design rules (same as order_bingx.py — non-negotiable):
#   FAIL LOUD        unexpected response shapes raise, with the exchange's own
#                    error code/message surfaced (top-level codes often hide the
#                    real reason in a nested field, e.g. OKX data[].sCode).
#   CONFIRMED        poll orders to a terminal state; return what the EXCHANGE
#                    says (avg price, executed qty, fees) — never the intent.
#   IDEMPOTENT       client_order_id unique per intent; resubmits must not
#                    double an order.
#   HALTABLE+AUDITED every order-mutating request goes through lib/guard:
#                    state/HALT blocks NEW-EXPOSURE orders before any network
#                    call, but closes/reduces/cancels MUST still pass (classify
#                    intent from the request BODY, not just the URL params).
#   ATTRIBUTION      the exchange's broker header/field on every order request —
#                    its omission is silent, so include it from the first order.


def get_contract_rules(env: dict, symbol: str) -> dict:
    """
    Instrument trading rules for a CANONICAL symbol (dashless uppercase,
    e.g. 'BTCUSDT') — convert to the venue's own format inside this lib; callers
    never pass venue-specific symbols.
    Returns at least {'step': str, 'min_qty': float, 'min_notional': float,
    'contract_value': float}. Cache per process — rules rarely change.
    """
    raise NotImplementedError


def format_qty(env: dict, symbol: str, qty: float, price: float = None) -> str:
    """
    Floor a BASE-currency qty to the instrument's step and return the venue's
    order-qty string (which may be CONTRACTS, not base units).

    Callers use this ONLY as the min-size gate (empty/zero result → skip the
    order). NEVER feed its return into place_market_order — that re-converts
    contracts→contracts and oversizes orders N× on ctVal≠1 instruments
    (measured live: 10× on ETH). See references/manager.md, units pitfall.
    """
    raise NotImplementedError


def place_market_order(env: dict, symbol: str, direction: str, qty: float,
                       client_order_id: str = None, reduce_only: bool = False):
    """
    Confirmed market order. qty is BASE currency (ETH, SOL…) — this lib converts
    to contracts internally. direction is the POSITION's direction
    ('long'|'short'); handle one-way AND hedge position modes (hedge: shrinking
    a side must be reduce-only, or it opens the OPPOSITE side — measured live).
    Poll to a terminal state; return exchange-reported fills
    {'avg_price', 'executed_qty', 'commission', 'order_id'}.
    Below min qty/notional → return False (intentional skip, no phantom-trade
    notification). Raise on rejection with the exchange's real error surfaced.
    """
    raise NotImplementedError


def close_position_partial(env: dict, symbol: str, direction: str, qty: float,
                           client_order_id: str = None):
    """
    Reduce an existing position by a BASE-currency qty, reduce-only semantics.
    direction is the position BEING CLOSED ('long'|'short'). Must work under
    state/HALT (closing is always allowed). If the venue's close call is already
    qty-capable, alias it — but this NAME must exist (see header).
    """
    raise NotImplementedError
