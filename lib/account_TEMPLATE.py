# Account library template — copy to lib/account_{exchange}.py and implement below.
# Before implementing: read skills/blave-quant/references/{exchange}-skill.md for
# the correct balance and position endpoints, auth pattern, and response schema.
#
# API keys go in .env (same file as blave keys):
#   {exchange}_api_key, {exchange}_secret_key, {exchange}_passphrase (if required)
#
# snapshot.py auto-imports this file by name — no changes to snapshot.py needed.


def get_equity(env: dict) -> dict:
    """
    Query exchange for total account equity.
    Returns {'equity': float, 'currency': str}

    env: dict from dotenv_values() — read keys with env.get('{exchange}_api_key'), etc.
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
