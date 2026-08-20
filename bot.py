"""
Algorithmic Trading -- AdHocMarkets bot (fmclient).

Two-market arbitrage bot: snipe mispriced orders from the manager's private
market, unwind the resulting position in the public market.

Prices in fmclient are INTEGER CENTS, not dollars. $2.50 is 250.

INVENTORY DISCIPLINE
--------------------
The manager issues a private order roughly every CYCLE_SECONDS. You must be
back at your BASELINE unit count before the next one arrives, so every buy
has to be paired with a sell inside the same cycle.

Baseline is whatever you held when the bot connected -- NOT zero. A bot that
treats zero as flat will happily sell off the starting inventory the class
gave you.

Enforced in the plumbing rather than left to the strategy:

  * baseline units captured from the first holdings update
  * an open position must be closed before a new snipe is taken
  * inside the last UNWIND_BUFFER_SECONDS the bot stops opening and starts
    flattening, crossing the spread if that is what it takes
  * a one-second timer drives the unwind, so a quiet book cannot strand you
  * a cycle that ends off baseline is logged as an error
"""

import copy
import json
import os
import time
from pathlib import Path

from fmclient import Agent, Holding, Market, Order, OrderSide, OrderType, Session

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
# This repository is PUBLIC. Real values belong in credentials.json, which is
# git-ignored -- see README.md. These placeholders are only a fallback.
#
#   credentials.json:
#   {
#     "account": "your-account-name",
#     "email": "you@student.unimelb.edu.au",
#     "password": "your-password",
#     "marketplace_id": 3265
#   }

ACCOUNT = "<your account name>"
EMAIL = "<your email>"
PASSWORD = "<your password>"
MARKETPLACE_ID = 0

BOT_NAME = "MyBot"

# Item name (or fragment) of the manager's private market. Leave blank to let
# the bot work it out: it guesses from the name, then confirms definitively
# from the first order flagged is_private. Set this if the guess is wrong.
PRIVATE_MARKET_ITEM = ""

# Safety switch. False = watch the market, place no orders.
ENABLE_EXAMPLE_STRATEGY = True

# Price limits (cents). Used as fallbacks when a book side is empty.
MAX_BUY_PRICE = 400
MIN_SELL_PRICE = 600
ORDER_UNITS = 1

# -- cycle / inventory discipline ------------------------------------------
# How often the manager hands out a new private order. The bot also detects
# the private order itself and resyncs the clock to it, so this is a fallback
# for the first cycle and any the detector misses.
CYCLE_SECONDS = 60

# Stop opening new positions and start flattening this many seconds before
# the cycle ends. Long enough to actually get filled.
UNWIND_BUFFER_SECONDS = 15

# Baseline unit count to return to before each new private order.
# None = whatever you were holding when the bot first connected.
TARGET_UNITS = None

# When flattening at the end of a cycle, cross the spread rather than carry
# the position into the next cycle. Being flat matters more than the last cent.
ALLOW_LOSS_TO_FLATTEN = True

# Prints the real attribute names of the first Market / Holding / Order the
# server sends, once each.
DEBUG_DUMP_ATTRS = True


def load_credentials():
    """Env vars > credentials.json > the placeholders above."""
    creds = {
        "account": ACCOUNT,
        "email": EMAIL,
        "password": PASSWORD,
        "marketplace_id": MARKETPLACE_ID,
    }

    cred_file = Path(__file__).with_name("credentials.json")
    if cred_file.exists():
        creds.update(json.loads(cred_file.read_text()))

    for key, env_var in (
        ("account", "FM_ACCOUNT"),
        ("email", "FM_EMAIL"),
        ("password", "FM_PASSWORD"),
        ("marketplace_id", "FM_MARKETPLACE_ID"),
    ):
        if os.environ.get(env_var):
            creds[key] = os.environ[env_var]

    creds["marketplace_id"] = int(creds["marketplace_id"])

    unset = [key for key, value in creds.items()
             if str(value).startswith("<") or value in ("", 0)]
    if unset:
        raise SystemExit(
            f"Credentials missing or blank: {', '.join(sorted(unset))}.\n"
            f"Create {cred_file.name} next to bot.py:\n"
            '  {"account": "...", "email": "...", "password": "...", '
            '"marketplace_id": 3265}\n'
            "It is git-ignored, so your password stays out of this public repo."
        )
    return creds


class MyBot(Agent):
    """Multi-market arbitrage bot with per-cycle inventory discipline."""

    def __init__(self, account, email, password, marketplace_id):
        super().__init__(account, email, password, marketplace_id, name=BOT_NAME)

        # We track TWO markets
        self._public_market = None
        self._private_market = None

        self._order_count = 0
        self._dumped = set()
        self._cash_available = 0
        self._units = 0            # total held -- the real position
        self._units_available = 0  # free to sell right now

        # Inventory discipline: the baseline we must return to each cycle.
        self._target_units = TARGET_UNITS
        self._cycle_index = 0
        self._cycle_start = None
        self._buys_sent = 0
        self._sells_sent = 0
        self._seen_private = set()

        # Last public book seen, so the cycle timer can act without waiting
        # for a book update that may never come.
        self._pub_book = []

    # -- lifecycle ---------------------------------------------------------

    def initialised(self):
        """Called once after login. Identify Private vs Public markets."""
        for market_id, market in self.markets.items():
            item_name = getattr(market, "item", "?")
            self.inform(f"Market {market_id} visible: item={item_name}")
            self._dump_attrs("Market", market)

            if PRIVATE_MARKET_ITEM:
                # Explicit config wins over any guessing.
                if PRIVATE_MARKET_ITEM.lower() in item_name.lower():
                    self._private_market = market
                else:
                    self._public_market = market
            elif ACCOUNT.lower() in item_name.lower() or "private" in item_name.lower():
                self._private_market = market
            else:
                self._public_market = market

        if self._public_market and self._private_market:
            self.inform(f"Assigned PRIVATE market: "
                        f"{getattr(self._private_market, 'item', '?')}")
            self.inform(f"Assigned PUBLIC market: "
                        f"{getattr(self._public_market, 'item', '?')}")
            return

        # The name heuristic did not resolve it. Do NOT guess by dict order:
        # getting this backwards means sniping the wrong book. Wait instead --
        # the first order flagged is_private identifies the private market
        # for certain, and _adopt_private_market() fixes the assignment then.
        self._private_market = None
        self._public_market = None
        self.warning(
            "Could not identify the private market by name. Holding off "
            "trading until an order flagged is_private arrives, which "
            "identifies it definitively. Set PRIVATE_MARKET_ITEM in the "
            "config to skip this wait."
        )

    def pre_start_tasks(self):
        """The cycle tick is what makes the unwind reliable.

        _on_books_update only runs when the book changes, so a quiet market
        near the end of a cycle would otherwise leave a position stranded.
        """
        self.execute_periodically(self._cycle_tick, 1)
        self.execute_periodically(self._heartbeat, 15)

    def received_session_info(self, session: Session):
        if getattr(session, "is_open", False):
            state = "OPEN"
        elif getattr(session, "is_paused", False):
            state = "PAUSED"
        elif getattr(session, "is_closed", False):
            state = "CLOSED"
        else:
            state = "UNKNOWN"
        self.inform(f"Session {state} (active={self.is_session_active()})")

    # -- market data -------------------------------------------------------

    def received_holdings(self, holdings: Holding):
        """Track cash, total units, and units free to trade."""
        self._dump_attrs("Holding", holdings)
        self._cash_available = getattr(holdings, "cash_available", 0)

        total_units = 0
        total_available = 0
        for market, asset in (getattr(holdings, "assets", None) or {}).items():
            self._dump_attrs("Asset", asset)
            total_units += getattr(asset, "units", 0)
            total_available += getattr(asset, "units_available", 0)

        self._units = total_units
        self._units_available = total_available

        # Baseline: whatever we held the first time we heard. NOT zero.
        if self._target_units is None:
            self._target_units = self._units
            self.inform(f"BASELINE set to {self._target_units} units. "
                        f"Every cycle must end here.")

        self.inform(
            f"Holdings: cash={self._cash_available} units={self._units} "
            f"(free {self._units_available}) net={self._net_units():+d} vs baseline"
        )

    def received_orders(self, orders):
        """Split the master order book into Public and Private books."""
        if orders:
            self._dump_attrs("Order", orders[0])

        # Must run BEFORE the guard below: when the markets are unresolved
        # this is the only thing that can resolve them.
        self._detect_private_orders(orders)

        if not self._public_market or not self._private_market:
            return

        pub_book = [o for o in orders
                    if getattr(o, "market", None) == self._public_market]
        priv_book = [o for o in orders
                     if getattr(o, "market", None) == self._private_market]
        self._pub_book = pub_book

        self._on_books_update(pub_book, priv_book)

    # -- order feedback ----------------------------------------------------

    def order_accepted(self, order: Order):
        self.inform(f"Order ACCEPTED: ref={getattr(order, 'ref', None)} "
                    f"{getattr(order, 'order_side', '?')} "
                    f"{getattr(order, 'units', '?')}@{getattr(order, 'price', '?')}")

    def order_rejected(self, info: dict, order: Order):
        self.error(f"Order REJECTED: ref={getattr(order, 'ref', None)} -- {info}")

    # -- strategy ----------------------------------------------------------

    def _on_books_update(self, pub_book, priv_book):
        """Arbitrage, gated by inventory discipline.

        Priority 1 returns to baseline. Priority 2 opens a new arbitrage, but
        only while flat and with enough time left to unwind it.
        """
        if not ENABLE_EXAMPLE_STRATEGY:
            return

        # 1. Speed check -- never stack orders.
        if self.pending_outgoing_orders_count(self._public_market) > 0 or \
           self.pending_outgoing_orders_count(self._private_market) > 0:
            return

        my_orders = list(Order.my_current().values())
        my_refs = {o.ref for o in my_orders}
        my_pub_orders = [o for o in my_orders if o.market == self._public_market]
        my_priv_orders = [o for o in my_orders if o.market == self._private_market]

        other_pub = [o for o in pub_book if o.ref not in my_refs]
        other_priv = [o for o in priv_book if o.ref not in my_refs]

        pub_best_bid, pub_best_ask = self._best_prices(other_pub)
        priv_best_bid, priv_best_ask = self._best_prices(other_priv)

        tick = getattr(self._public_market, "tick", 1) or 1
        max_pub_units = self._max_units(self._public_market)

        net = self._net_units()
        unwinding = self._seconds_left() <= UNWIND_BUFFER_SECONDS

        # ------------------------------------------------------------------
        # PRIORITY 1: RETURN TO BASELINE (with dynamic repricing)
        # ------------------------------------------------------------------
        if net > 0:
            # Long: sell back down to baseline in the public market.
            if unwinding and ALLOW_LOSS_TO_FLATTEN and pub_best_bid:
                ideal_price = pub_best_bid  # cross, take the fill
            elif pub_best_ask:
                ideal_price = pub_best_ask - tick  # undercut
                if pub_best_bid and ideal_price <= pub_best_bid:
                    ideal_price = pub_best_bid
            else:
                ideal_price = pub_best_bid if pub_best_bid else MIN_SELL_PRICE

            # Stale order check. Anything of ours that is not a sell at the
            # ideal price is in the way -- a resting BUY here would otherwise
            # block the unwind forever, since it can never reduce a long.
            if self._clear_blocking_orders(my_pub_orders, OrderSide.SELL,
                                           ideal_price):
                return

            units_to_sell = min(net, self._units_available, max_pub_units)
            if units_to_sell <= 0:
                self.warning(f"Owe {net} sell but only {self._units_available} free.")
                return
            self._send_limit(self._public_market, OrderSide.SELL,
                             ideal_price, units_to_sell)
            return

        if net < 0:
            # Short: buy back up to baseline in the public market.
            if unwinding and ALLOW_LOSS_TO_FLATTEN and pub_best_ask:
                ideal_price = pub_best_ask  # cross, take the fill
            elif pub_best_bid:
                ideal_price = pub_best_bid + tick  # penny-jump
                if pub_best_ask and ideal_price >= pub_best_ask:
                    ideal_price = pub_best_ask
            else:
                ideal_price = pub_best_ask if pub_best_ask else MAX_BUY_PRICE

            if self._clear_blocking_orders(my_pub_orders, OrderSide.BUY,
                                           ideal_price):
                return

            affordable = self._cash_available // ideal_price if ideal_price else 0
            units_to_buy = min(-net, max_pub_units, affordable)
            if units_to_buy <= 0:
                self.warning(f"Owe {-net} buy but cash covers {affordable}.")
                return
            self._send_limit(self._public_market, OrderSide.BUY,
                             ideal_price, units_to_buy)
            return

        # ------------------------------------------------------------------
        # PRIORITY 2: THE ARBITRAGE SNIPE (flat only, and not while unwinding)
        # ------------------------------------------------------------------
        if unwinding:
            return  # no time left to close a new position

        # Manager is selling cheap -> BUY from Private, dump to Public.
        if priv_best_ask and pub_best_bid and priv_best_ask < pub_best_bid:
            if self._cash_available >= priv_best_ask * ORDER_UNITS and not my_priv_orders:
                self.inform(f"SNIPING MANAGER ASK: Buying @ {priv_best_ask} "
                            f"(public bid {pub_best_bid})")
                self._send_limit(self._private_market, OrderSide.BUY,
                                 priv_best_ask, ORDER_UNITS)
                return

        # Manager is buying high -> SELL to Private, buy back from Public.
        if priv_best_bid and pub_best_ask and priv_best_bid > pub_best_ask:
            if not my_priv_orders and self._units_available >= ORDER_UNITS:
                self.inform(f"SNIPING MANAGER BID: Selling @ {priv_best_bid} "
                            f"(public ask {pub_best_ask})")
                self._send_limit(self._private_market, OrderSide.SELL,
                                 priv_best_bid, ORDER_UNITS)
                return

    # -- cycle tracking ----------------------------------------------------

    def _net_units(self):
        """Units held away from baseline. + means we owe a sell."""
        if self._target_units is None:
            return 0
        return self._units - self._target_units

    def _seconds_left(self):
        """Seconds remaining in the current cycle."""
        if self._cycle_start is None:
            return float(CYCLE_SECONDS)
        return max(0.0, CYCLE_SECONDS - (time.time() - self._cycle_start))

    def _detect_private_orders(self, orders):
        """A new private order from the manager marks a new cycle."""
        for order in orders:
            if not getattr(order, "is_private", False):
                continue
            key = self._order_key(order)
            if key in self._seen_private:
                continue
            self._seen_private.add(key)
            self._adopt_private_market(order)
            self.inform(
                f"PRIVATE ORDER: {getattr(order, 'order_side', '?')} "
                f"{getattr(order, 'units', '?')}@{getattr(order, 'price', '?')}"
            )
            self._start_cycle()

    def _adopt_private_market(self, private_order):
        """Let a genuinely private order settle which market is which.

        This is the authoritative signal -- the name heuristic is only a
        guess, and trading the two books the wrong way round is the most
        expensive mistake this bot can make.
        """
        market = getattr(private_order, "market", None)
        if market is None or market is self._private_market:
            return

        if self._private_market is not None:
            self.warning(
                f"Private market was "
                f"{getattr(self._private_market, 'item', '?')}, but an "
                f"is_private order arrived in "
                f"{getattr(market, 'item', '?')} -- correcting."
            )

        self._private_market = market
        others = [m for m in self.markets.values() if m is not market]
        self._public_market = others[0] if len(others) == 1 else None

        self.inform(f"PRIVATE market confirmed: {getattr(market, 'item', '?')}")
        if self._public_market is not None:
            self.inform(f"PUBLIC market: "
                        f"{getattr(self._public_market, 'item', '?')}")
        else:
            self.error(f"{len(others)} candidate public markets -- cannot "
                       f"pick one. Set PRIVATE_MARKET_ITEM in the config.")

    def _order_key(self, order):
        for attr in ("fm_id", "id", "ref"):
            value = getattr(order, attr, None)
            if value is not None:
                return (attr, value)
        return ("obj", id(order))

    def _start_cycle(self):
        """Close the books on the last cycle, start the clock on a new one."""
        if self._cycle_index:
            net = self._net_units()
            summary = (f"Cycle {self._cycle_index} done: "
                       f"{self._buys_sent} buys / {self._sells_sent} sells sent, "
                       f"ending net {net:+d}")
            if net == 0:
                self.inform(summary + " -- back at baseline.")
            else:
                self.error(summary + " -- OFF BASELINE, unpaired position.")

        self._cycle_index += 1
        self._cycle_start = time.time()
        self._buys_sent = 0
        self._sells_sent = 0
        self.inform(f"--- Cycle {self._cycle_index} ---")

    def _cycle_tick(self):
        """Runs every second. Drives the unwind and rolls the cycle over."""
        if self._cycle_start is None:
            if self._public_market is not None:
                self._start_cycle()
            return

        if self._seconds_left() <= 0:
            self._start_cycle()
            return

        if self._seconds_left() > UNWIND_BUFFER_SECONDS:
            return

        net = self._net_units()
        if net == 0 or not ENABLE_EXAMPLE_STRATEGY:
            return
        if not self.is_session_active():
            return
        if self.pending_outgoing_orders_count(self._public_market) > 0:
            return

        # A quiet book means _on_books_update is not firing. Push it along.
        self.warning(f"UNWIND: {net:+d} units, {self._seconds_left():.0f}s left")
        self._on_books_update(self._pub_book, [])

    # -- helpers -----------------------------------------------------------

    def _dump_attrs(self, label, obj):
        if not DEBUG_DUMP_ATTRS or obj is None or label in self._dumped:
            return
        self._dumped.add(label)

        try:
            attrs = dict(vars(obj))
        except TypeError:
            attrs = {
                name: getattr(obj, name, None)
                for name in dir(obj)
                if not name.startswith("_") and not callable(getattr(obj, name, None))
            }

        self.inform(f"[attrs] {label} ({type(obj).__name__}):")
        for name, value in sorted(attrs.items()):
            self.inform(f"[attrs]   {name} = {value!r}")

    def _max_units(self, market):
        """Largest order size this market accepts.

        The attribute name is not confirmed -- fmclient's ORM builds these at
        runtime -- so try the plausible spellings and say so once if none
        match, rather than silently capping every order at ORDER_UNITS.
        """
        for name in ("unitMaximum", "unit_maximum", "max_units", "maxUnits"):
            value = getattr(market, name, None)
            if isinstance(value, int) and value > 0:
                return value

        if "max_units" not in self._dumped:
            self._dumped.add("max_units")
            self.warning(
                f"No max-units attribute on this market; capping orders at "
                f"ORDER_UNITS={ORDER_UNITS}. Check the [attrs] dump for the "
                f"real name and add it to _max_units()."
            )
        return ORDER_UNITS

    def _clear_blocking_orders(self, my_orders, wanted_side, wanted_price):
        """Cancel anything of ours that is not the order we want resting.

        Returns True if the caller should stop for now -- either a cancel is
        in flight, or the right order is already sitting at the right price.

        Checking only same-side orders here was a real deadlock: a leftover
        BUY while we were long matched no cancel branch, yet still counted as
        "we have an order", so the sell was never sent and the cycle ended
        off baseline.
        """
        if not my_orders:
            return False

        for order in my_orders:
            if order.order_side != wanted_side or order.price != wanted_price:
                self._cancel(order)
                return True  # let the cancel land before doing anything else

        return True  # the order we want is already resting; wait for the fill

    def _best_prices(self, book):
        bids = [o.price for o in book if o.order_side == OrderSide.BUY]
        asks = [o.price for o in book if o.order_side == OrderSide.SELL]
        return (max(bids) if bids else None, min(asks) if asks else None)

    def _send_limit(self, market, side, price, units):
        """Build and send a limit order to a SPECIFIC market."""
        if not self.is_session_active():
            self.warning("Session not active -- not sending.")
            return

        price = self._clamp_to_tick(market, price)
        if price is None:
            return

        order = Order.create_new(market)
        order.price = price
        order.units = units
        order.order_type = OrderType.LIMIT
        order.order_side = side

        self._order_count += 1
        order.ref = f"{BOT_NAME}-c{self._cycle_index}-{self._order_count}"

        if side == OrderSide.BUY:
            self._buys_sent += units
            projected = self._net_units() + units
        else:
            self._sells_sent += units
            projected = self._net_units() - units

        market_name = getattr(market, "item", "Unknown")
        self.inform(f"Sending to {market_name}: {side} {units}@{price} "
                    f"(ref={order.ref}) net {self._net_units():+d} -> "
                    f"{projected:+d} if filled")
        self.send_order(order)

    def _cancel(self, order: Order):
        cancel = copy.copy(order)
        cancel.order_type = OrderType.CANCEL
        cancel.ref = f"cancel-{getattr(order, 'ref', 'order')}"
        self.inform(f"Cancelling ref={getattr(order, 'ref', None)}")
        self.send_order(cancel)

    def _clamp_to_tick(self, market, price):
        tick = getattr(market, "tick", None) or 1
        price = int(round(price / tick) * tick)

        low = getattr(market, "min_price", None)
        high = getattr(market, "max_price", None)
        if (low is not None and price < low) or (high is not None and price > high):
            self.warning(f"Price {price} outside [{low}, {high}] -- not sending.")
            return None
        return price

    def _heartbeat(self):
        self.inform(f"Cycle {self._cycle_index}, {self._seconds_left():.0f}s left. "
                    f"units={self._units} (baseline {self._target_units}, "
                    f"net {self._net_units():+d}) cash={self._cash_available}")


if __name__ == "__main__":
    creds = load_credentials()
    bot = MyBot(
        creds["account"],
        creds["email"],
        creds["password"],
        creds["marketplace_id"],
    )
    bot.run()
