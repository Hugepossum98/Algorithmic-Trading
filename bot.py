"""
Algorithmic Trading -- AdHocMarkets bot template (fmclient).

Fill in the CONFIG block, then run:

    python bot.py

The bot connects, prints the markets/holdings it can see, and reacts to the
order book. It does NOT trade until you set ENABLE_EXAMPLE_STRATEGY = True
(or write your own logic in `_on_book_update`), so it is safe to run during a
practice session while you watch what the callbacks print.

Prices in fmclient are INTEGER CENTS, not dollars. $2.50 is 250.

INVENTORY DISCIPLINE
--------------------
The manager issues a private order roughly every CYCLE_SECONDS. You must be
back at your baseline unit count before the next one arrives, so every buy
has to be paired with a sell inside the same cycle.

The bot enforces this rather than trusting the strategy to behave:

  * baseline units are captured from your first holdings update
  * exposure is capped at MAX_OPEN_UNITS away from that baseline
  * an open position must be closed before a new one is opened
  * in the last UNWIND_BUFFER_SECONDS of a cycle it stops opening and
    starts flattening, crossing the spread if that is what it takes
  * a timer drives the unwind, so a quiet book cannot strand you long

If a cycle still ends off baseline the bot logs it loudly -- that is the
error you care about most in this setting.
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
# Credentials are read from (in order): environment variables, a local
# credentials.json file, then the placeholders below. credentials.json is
# git-ignored -- prefer it so you never commit your password.
#
#   credentials.json:
#   {
#     "account": "your-account-name",
#     "email": "you@student.unimelb.edu.au",
#     "password": "your-password",
#     "marketplace_id": 1234
#   }

ACCOUNT = "<your account name>"
EMAIL = "<your email>"
PASSWORD = "<your password>"
MARKETPLACE_ID = 0  # integer id given in class

BOT_NAME = "MyBot"

# Safety switch. Leave False until you have read what the example does and
# you actually want the bot placing live orders.
ENABLE_EXAMPLE_STRATEGY = False

# Example-strategy parameters (all in cents).
MAX_BUY_PRICE = 400  # never pay more than this
MIN_SELL_PRICE = 600  # never sell for less than this
ORDER_UNITS = 1

# -- cycle / inventory discipline ------------------------------------------
# How often the manager hands out a new private order. The bot also detects
# the private order itself and resyncs the clock to it, so this is a fallback
# for the first cycle and for any the detector misses.
CYCLE_SECONDS = 60

# Stop opening new positions and start flattening this many seconds before
# the cycle ends. Needs to be long enough to actually get filled.
UNWIND_BUFFER_SECONDS = 15

# Units you may hold away from baseline at any moment. 1 means: buy one,
# sell it back, and only then buy again.
MAX_OPEN_UNITS = 1

# Baseline unit count to return to before each new private order.
# None = whatever you were holding when the bot first connected.
TARGET_UNITS = None

# When flattening at the end of a cycle, ignore MAX_BUY_PRICE / MIN_SELL_PRICE
# and take the best price available. Being flat matters more than the last
# cent. Set False to keep the limits and risk ending the cycle exposed.
ALLOW_LOSS_TO_FLATTEN = True

# Prints the real attribute names of the first Market / Holding / Order the
# server sends, once each. Leave True for your first live run -- fmclient's
# ORM builds attributes dynamically, so this is the only way to see them.
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

    if str(creds["email"]).startswith("<"):
        raise SystemExit(
            "Credentials not set. Edit the CONFIG block in bot.py, or create "
            "credentials.json (see README.md)."
        )
    return creds


class MyBot(Agent):
    """One callback per event from the marketplace. Override what you need."""

    def __init__(self, account, email, password, marketplace_id):
        super().__init__(account, email, password, marketplace_id, name=BOT_NAME)

        # The market we trade. Set in initialised() once we see what exists.
        self._market = None
        self._order_count = 0
        self._dumped = set()

        # Latest holdings, refreshed by received_holdings().
        self._cash_available = 0
        self._units = 0
        self._units_available = 0

        # Inventory discipline. _target_units is the baseline we must be
        # back at before each new private order.
        self._target_units = TARGET_UNITS
        self._cycle_index = 0
        self._cycle_start = None
        self._buys_sent = 0
        self._sells_sent = 0
        self._seen_private = set()

        # Last book seen, so the cycle timer can act without waiting for
        # a book update that may never come.
        self._book = []

    # -- lifecycle ---------------------------------------------------------

    def initialised(self):
        """Called once after login, when market metadata has arrived."""
        for market_id, market in self.markets.items():
            self.inform(
                f"Market {market_id}: item={getattr(market, 'item', '?')} "
                f"min={getattr(market, 'min_price', '?')} "
                f"max={getattr(market, 'max_price', '?')} "
                f"tick={getattr(market, 'tick', '?')}"
            )

        # Single-market sessions: just take the one we were given.
        self._market = next(iter(self.markets.values()), None)
        if self._market is None:
            self.error("No markets visible for this marketplace id.")
        else:
            self.inform(f"Trading market: {getattr(self._market, 'item', '?')}")
            self._dump_attrs("Market", self._market)

    def pre_start_tasks(self):
        """Register repeating jobs here, before the event loop starts.

        The cycle tick is what makes the unwind reliable: _on_book_update
        only runs when the book changes, so a quiet market near the end of
        a cycle would otherwise leave us holding a position.
        """
        self.execute_periodically(self._cycle_tick, 1)

    def received_session_info(self, session: Session):
        """Market opened, paused, or closed."""
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
        """Your cash and units. 'available' excludes what is tied up in orders."""
        self._dump_attrs("Holding", holdings)

        self._cash_available = getattr(holdings, "cash_available", 0)
        for market, asset in (getattr(holdings, "assets", None) or {}).items():
            self._dump_attrs("Asset", asset)
            if self._is_my_market(market):
                self._units = getattr(asset, "units", 0)
                self._units_available = getattr(asset, "units_available", 0)

        # Baseline: whatever we were holding the first time we heard.
        if self._target_units is None:
            self._target_units = self._units
            self.inform(f"Baseline inventory set to {self._target_units} units. "
                        f"Every cycle must end here.")

        self.inform(
            f"Holdings: cash={getattr(holdings, 'cash', '?')} "
            f"available={self._cash_available} "
            f"units={self._units} (available {self._units_available}) "
            f"net={self._net_units():+d} vs baseline"
        )

    def received_orders(self, orders):
        """The current order book, resent on every change.

        `orders` is the whole book, not a delta. Use Order.my_current() to
        see just your own live orders.
        """
        if self._market is None:
            return

        if orders:
            self._dump_attrs("Order", orders[0])

        self._detect_private_orders(orders)

        book = [o for o in orders if self._is_my_market(getattr(o, "market", None))]
        self._book = book
        best_bid, best_ask = self._best_prices(book)
        mine = len(Order.my_current())

        self.inform(
            f"Book: best_bid={best_bid} best_ask={best_ask} "
            f"({len(book)} orders, {mine} mine) "
            f"net={self._net_units():+d} {self._seconds_left():.0f}s left"
        )

        self._on_book_update(book, best_bid, best_ask)

    # -- order feedback ----------------------------------------------------

    def order_accepted(self, order: Order):
        self.inform(f"Order ACCEPTED: ref={getattr(order, 'ref', None)} "
                    f"{getattr(order, 'order_side', '?')} "
                    f"{getattr(order, 'units', '?')}@{getattr(order, 'price', '?')}")

    def order_rejected(self, info: dict, order: Order):
        """`info` is a dict explaining why -- always read it."""
        self.error(f"Order REJECTED: ref={getattr(order, 'ref', None)} -- {info}")

    # -- strategy ----------------------------------------------------------

    def _on_book_update(self, book, best_bid, best_ask):
        """YOUR STRATEGY GOES HERE.

        Called on every book update with:
          book      -- list of Order objects currently resting in this market
          best_bid  -- highest buy price in cents, or None
          best_ask  -- lowest sell price in cents, or None

        The inventory rules are enforced before your logic runs: if you are
        holding anything away from baseline, closing that comes first, and
        nothing new is opened once the unwind window starts.
        """
        if not ENABLE_EXAMPLE_STRATEGY:
            return

        # Don't stack orders: wait for anything in flight to be accepted or
        # rejected first, otherwise the marketplace rejects the extras.
        if self.pending_outgoing_orders_count(self._market) > 0:
            return

        net = self._net_units()
        unwinding = self._seconds_left() <= UNWIND_BUFFER_SECONDS

        # 1. An open position is a debt. Pay it before taking on more.
        if net != 0:
            self._reduce_exposure(net, best_bid, best_ask, forced=unwinding)
            return

        # 2. Flat, and too late to safely round-trip. Sit out the rest.
        if unwinding:
            return

        # 3. Flat with time to spare -- open a position worth closing.
        units = min(ORDER_UNITS, MAX_OPEN_UNITS)

        # Buy into a cheap ask.
        if best_ask is not None and best_ask <= MAX_BUY_PRICE:
            if self._cash_available >= best_ask * units:
                self._send_limit(OrderSide.BUY, best_ask, units)
                return

        # Sell into an expensive bid.
        if best_bid is not None and best_bid >= MIN_SELL_PRICE:
            if self._units_available >= units:
                self._send_limit(OrderSide.SELL, best_bid, units)
                return

    def _reduce_exposure(self, net, best_bid, best_ask, forced):
        """Move `net` units back toward baseline.

        net > 0 means we are long and owe a sell; net < 0 means short and
        owe a buy. `forced` drops the price limits -- late in a cycle, being
        flat beats holding out for a better fill.
        """
        take_any_price = forced and ALLOW_LOSS_TO_FLATTEN

        if net > 0:
            if best_bid is None:
                self.warning("Long but no bid to sell into.")
                return
            if not take_any_price and best_bid < MIN_SELL_PRICE:
                return
            units = min(net, self._units_available)
            if units <= 0:
                self.warning(f"Owe {net} sell but only {self._units_available} "
                             f"units free -- cancelling resting orders.")
                self._cancel_all_mine()
                return
            self._send_limit(OrderSide.SELL, best_bid, units)
            return

        need = -net
        if best_ask is None:
            self.warning("Short but no ask to buy from.")
            return
        if not take_any_price and best_ask > MAX_BUY_PRICE:
            return
        affordable = self._cash_available // best_ask if best_ask else 0
        units = min(need, affordable)
        if units <= 0:
            self.warning(f"Owe {need} buy but cash only covers {affordable} "
                         f"-- cancelling resting orders.")
            self._cancel_all_mine()
            return
        self._send_limit(OrderSide.BUY, best_ask, units)

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
            self.inform(
                f"PRIVATE ORDER: {getattr(order, 'order_side', '?')} "
                f"{getattr(order, 'units', '?')}@{getattr(order, 'price', '?')}"
            )
            self._start_cycle()

    def _order_key(self, order):
        for attr in ("fm_id", "id", "ref"):
            value = getattr(order, attr, None)
            if value is not None:
                return (attr, value)
        return ("obj", id(order))

    def _start_cycle(self):
        """Close the books on the last cycle and start the clock on a new one."""
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
            if self._market is not None:
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
        if self.pending_outgoing_orders_count(self._market) > 0:
            return

        # Resting orders tie up the units and cash we need to get flat.
        if Order.my_current():
            self._cancel_all_mine()
            return

        best_bid, best_ask = self._best_prices(self._book)
        self.warning(f"UNWIND: {net:+d} units, {self._seconds_left():.0f}s left")
        self._reduce_exposure(net, best_bid, best_ask, forced=True)

    # -- helpers -----------------------------------------------------------

    def _dump_attrs(self, label, obj):
        """Print an object's real attributes, once per label.

        fmclient's ORM classes build their attributes at runtime, so this is
        the only reliable way to learn the exact names. Set
        DEBUG_DUMP_ATTRS = False once you've seen them.
        """
        if not DEBUG_DUMP_ATTRS or obj is None or label in self._dumped:
            return
        self._dumped.add(label)

        try:
            attrs = dict(vars(obj))
        except TypeError:  # __slots__ or a C type
            attrs = {
                name: getattr(obj, name, None)
                for name in dir(obj)
                if not name.startswith("_") and not callable(getattr(obj, name, None))
            }

        self.inform(f"[attrs] {label} ({type(obj).__name__}):")
        for name, value in sorted(attrs.items()):
            self.inform(f"[attrs]   {name} = {value!r}")

    def _is_my_market(self, market):
        """True if `market` is the one we trade."""
        if market is None or self._market is None:
            return False
        if market is self._market:
            return True
        mine = getattr(self._market, "fm_id", None)
        return mine is not None and getattr(market, "fm_id", None) == mine

    def _best_prices(self, book):
        bids = [o.price for o in book if o.order_side == OrderSide.BUY]
        asks = [o.price for o in book if o.order_side == OrderSide.SELL]
        return (max(bids) if bids else None, min(asks) if asks else None)

    def _send_limit(self, side, price, units):
        """Build and send a limit order, with basic sanity checks."""
        if not self.is_session_active():
            self.warning("Session not active -- not sending.")
            return

        price = self._clamp_to_tick(price)
        if price is None:
            return

        order = Order.create_new(self._market)
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

        self.inform(f"Sending {side} {units}@{price} (ref={order.ref}) "
                    f"net {self._net_units():+d} -> {projected:+d} if filled")
        self.send_order(order)

    def _cancel(self, order: Order):
        """Cancel a live order of yours.

        fmclient 6 has no Order.create_cancel(): you copy the order you want
        gone and flip its type to OrderType.CANCEL.
        """
        cancel = copy.copy(order)
        cancel.order_type = OrderType.CANCEL
        cancel.ref = f"cancel-{getattr(order, 'ref', 'order')}"
        self.inform(f"Cancelling ref={getattr(order, 'ref', None)}")
        self.send_order(cancel)

    def _cancel_all_mine(self):
        """Pull all of your resting orders. Handy at session end."""
        for order in Order.my_current().values():
            self._cancel(order)

    def _clamp_to_tick(self, price):
        """Round to the market tick and reject prices outside the legal range."""
        market = self._market
        tick = getattr(market, "tick", None) or 1
        price = int(round(price / tick) * tick)

        low = getattr(market, "min_price", None)
        high = getattr(market, "max_price", None)
        if (low is not None and price < low) or (high is not None and price > high):
            self.warning(f"Price {price} outside [{low}, {high}] -- not sending.")
            return None
        return price

    def _heartbeat(self):
        """Sample periodic task -- see pre_start_tasks()."""
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
