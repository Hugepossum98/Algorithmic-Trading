"""
Shared settings for both bots.

bot.py (NORMAL)   trades ONLY the private market -- it opens positions.
reactivebot.py    trades ONLY the public market  -- it closes them.

Both read this file, so they agree on the baseline and the cycle clock.
Change a number here and both bots pick it up.

Prices are INTEGER CENTS. $2.50 is 250.
"""

import json
import os
from pathlib import Path

# -- account ---------------------------------------------------------------
# Password is NOT here: this repo is public. Put it in credentials.json
# (git-ignored) or the FM_PASSWORD environment variable.
ACCOUNT = "jocund-value"
EMAIL = "jvanderstee@student.unimelb.edu.au"
MARKETPLACE_ID = 3266

# -- the baseline ----------------------------------------------------------
# Units you must be holding when each new private order arrives. BOTH bots
# measure their position against this, which is what keeps them in step.
# Set it to whatever the class starts you with.
TARGET_UNITS = 0

# -- sizing ----------------------------------------------------------------
# The manager deals in blocks of 5, so a private order moves you 5 units and
# the reactive bot has 5 to work back.
ORDER_UNITS = 5
MAX_POSITION = 5       # never go further than this from TARGET_UNITS

# Unwind in slices rather than dumping all 5 at once. Smaller slices get
# better average prices in a thin book but risk not finishing; the urgency
# ramp below compensates by getting more aggressive as time runs out.
# Set equal to ORDER_UNITS to always work the whole position at once.
SLICE_UNITS = 5

# -- entry rule (normal bot) -----------------------------------------------
# Minimum profit, in cents, before the normal bot takes a private order.
# Higher = fussier, fewer but better trades. Must cover the cost of
# unwinding into the public spread.
MIN_EDGE = 10

# Sell to the manager even when holding nothing, going short and buying back
# on the public market. With TARGET_UNITS = 0 this is the ONLY way the
# sell-side arbitrage can ever fire -- leaving it False throws away half the
# strategy. Set False only if the marketplace rejects short sells (you will
# see the rejections in the log).
ALLOW_SHORT = True

# -- cycle clock (both bots) -----------------------------------------------
CYCLE_SECONDS = 60           # how often the manager issues a private order
UNWIND_BUFFER_SECONDS = 15   # last N seconds: stop opening, force the exit


def urgency(seconds_left):
    """How hard to chase the fill: 0.0 = patient, 1.0 = cross the spread.

    Everyone in the class must settle inside the same minute, so the end of
    a cycle is a predictable liquidity crunch -- a crowd of forced sellers
    all crossing at once. Ramping continuously means we are done trading
    before that crowd arrives, instead of being part of it.

    Reaches 1.0 with UNWIND_BUFFER_SECONDS still on the clock, leaving that
    window as margin to actually get filled.
    """
    span = max(1.0, CYCLE_SECONDS - UNWIND_BUFFER_SECONDS)
    elapsed = CYCLE_SECONDS - seconds_left
    return max(0.0, min(1.0, elapsed / span))

# -- market identification -------------------------------------------------
# Fragment of the private market's item name. Leave "" to auto-detect.
PRIVATE_MARKET_ITEM = ""

DEBUG_DUMP_ATTRS = True


def load_credentials():
    """Account details, with the password from credentials.json or env."""
    creds = {
        "account": ACCOUNT,
        "email": EMAIL,
        "password": "",
        "marketplace_id": MARKETPLACE_ID,
    }

    cred_file = Path(__file__).with_name("credentials.json")
    if cred_file.exists():
        creds.update(json.loads(cred_file.read_text()))

    for key, env_var in (("account", "FM_ACCOUNT"), ("email", "FM_EMAIL"),
                         ("password", "FM_PASSWORD"),
                         ("marketplace_id", "FM_MARKETPLACE_ID")):
        if os.environ.get(env_var):
            creds[key] = os.environ[env_var]

    creds["marketplace_id"] = int(creds["marketplace_id"])

    if not creds["password"]:
        raise SystemExit(
            "No password. Create credentials.json next to this file:\n"
            '  {"password": "your-password"}\n'
            "It is git-ignored, so it stays out of this public repo."
        )
    return creds


def split_markets(markets, agent):
    """Work out which market is private and which is public.

    Returns (public, private). Either may be None when it cannot be told
    apart -- callers must not trade until both are set. A bot that guesses
    wrong here trades the wrong book, so guessing is not allowed.
    """
    public = private = None

    for market_id, market in markets.items():
        name = getattr(market, "item", "?")
        agent.inform(f"Market {market_id}: {name}")

        if PRIVATE_MARKET_ITEM:
            is_private = PRIVATE_MARKET_ITEM.lower() in name.lower()
        else:
            is_private = (ACCOUNT.lower() in name.lower()
                          or "private" in name.lower())

        if is_private:
            private = market
        else:
            public = market

    if public and private:
        agent.inform(f"PRIVATE={getattr(private, 'item', '?')} "
                     f"PUBLIC={getattr(public, 'item', '?')}")
    else:
        agent.warning("Cannot tell the markets apart by name. Waiting for an "
                      "order flagged is_private to settle it.")
        public = private = None

    return public, private


def net_units(holdings, target=None):
    """Units held away from baseline. Positive means we owe a sell.

    This is the shared ledger. Both bots compute it from the same server
    holdings, which is what stops them fighting each other.

    `target` is read at call time, not bound as a default -- a default
    argument would freeze TARGET_UNITS at import and silently ignore any
    later change to it.
    """
    if target is None:
        target = TARGET_UNITS

    total = 0
    for _market, asset in (getattr(holdings, "assets", None) or {}).items():
        total += getattr(asset, "units", 0)
    return total - target
