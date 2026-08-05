# Account library template — copy to lib/account_{exchange}.py and implement below.
# Before implementing: read skills/blave-quant/references/{exchange}-skill.md for
# the correct balance and position endpoints, auth pattern, and response schema.
#
# API keys are already in .env — read the EXACT names that exist there (grep .env
# first). Web-connected exchanges arrive uppercase ({EXCHANGE}_API_KEY /
# {EXCHANGE}_SECRET_KEY / {EXCHANGE}_PASSPHRASE); some older integrations are
# lowercase (sinopac_api_key). Never invent or re-case names.
#
# Platform readers auto-import this file by name — keep the exact filename convention.


def get_equity(env: dict) -> dict:
    """
    Query exchange for account equity.
    Returns {'equity': float, 'currency': str}
    Optionally add 'accounts': {name: float} — per-wallet breakdown (lowercase
    names like 'swap'/'spot'/'fund', USDT-denominated, best-effort), shown on
    the web so funds outside the trading account don't look like they vanished.

    Multi-account venues: equity = the account orders are placed on (it is the
    position-sizing base) — never the sum across spot/futures/funding accounts.

    env: dict from dotenv_values() — read the exact key names present in .env
    (web-connected exchanges: env.get('{EXCHANGE}_API_KEY'), uppercase).
    """
    raise NotImplementedError


def get_positions(env: dict) -> list:
    """
    Query exchange for open positions.
    Returns [{'symbol': str, 'side': str, 'size': float, 'mark_price': float}, ...]
    Return [] if flat.

    OKX pitfall: use the notionalUsd field directly — ctVal may be None for some
    instrument types, so computing pos * markPx * ctVal will silently return 0.
    """
    raise NotImplementedError


# OPTIONAL — implement when the venue has wallets holding coins outside the
# derivatives account (spot/funding). DISPLAY ONLY: the platform shows these
# on the web; they are NEVER positions and never reach the reconciler (a spot
# coin listed as a position would net futures orders against spot inventory).
#
# def get_holdings(env: dict) -> list:
#     """[{'asset': str, 'amount': float, 'usdt_value': float|None,
#     'wallet': str}, ...] sorted by value desc. usdt_value None when no live
#     price (delisted coin) — still LIST it (hiding it = money vanishing).
#     Drop dust under 0.1 USDT. See lib/account_binance.py for the reference
#     implementation."""
