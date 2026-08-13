"""
Algorithmic Trading -- AdHocMarkets bot template (fmclient).

Fill in the CONFIG block, then run:

    python bot.py

The bot connects, prints the markets/holdings it can see, and reacts to the
order book. It does NOT trade until you set ENABLE_EXAMPLE_STRATEGY = True
(or write your own logic in `_on_book_update`), so it is safe to run during a
practice session while you watch what the callbacks print.

Prices in fmclient are INTEGER CENTS, not dollars. $2.50 is 250.
"""

import copy
import json
import os
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

        # Latest holdings, refreshed by received_holdings().
        self._cash_available = 0
        self._units_available = 0

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

    def pre_start_tasks(self):
        """Register repeating jobs here, before the event loop starts.

        Example -- run _heartbeat every 10 seconds:
            self.execute_periodically(self._heartbeat, 10)
        """
        pass

    def received_session_info(self, session: Session):
        """Market opened or closed."""
        if self.is_session_active():
            self.inform("Session is ACTIVE -- trading allowed.")
        else:
            self.inform("Session is INACTIVE -- orders will be rejected.")

    # -- market data -------------------------------------------------------

    def received_holdings(self, holdings: Holding):
        """Your cash and units. 'available' excludes what is tied up in orders."""
        self._cash_available = getattr(holdings, "cash_available", 0)
        for market, asset in (getattr(holdings, "assets", None) or {}).items():
            if self._is_my_market(market):
                self._units_available = getattr(asset, "units_available", 0)

        self.inform(
            f"Holdings: cash={getattr(holdings, 'cash', '?')} "
            f"available={self._cash_available} "
            f"units_available={self._units_available}"
        )

    def received_orders(self, orders):
        """The current order book, resent on every change.

        `orders` is the whole book, not a delta. Use Order.my_current() to
        see just your own live orders.
        """
        if self._market is None:
            return

        book = [o for o in orders if self._is_my_market(getattr(o, "market", None))]
        best_bid, best_ask = self._best_prices(book)
        mine = len(Order.my_current())

        self.inform(
            f"Book: best_bid={best_bid} best_ask={best_ask} "
            f"({len(book)} orders, {mine} mine)"
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
        """
        if not ENABLE_EXAMPLE_STRATEGY:
            return

        # Don't stack orders: wait for anything in flight to be accepted or
        # rejected first, otherwise the marketplace rejects the extras.
        if self.pending_outgoing_orders_count(self._market) > 0:
            return

        # Example: cross the spread only when the price is clearly good for us.
        # Buy into a cheap ask.
        if best_ask is not None and best_ask <= MAX_BUY_PRICE:
            if self._cash_available >= best_ask * ORDER_UNITS:
                self._send_limit(OrderSide.BUY, best_ask, ORDER_UNITS)
                return

        # Sell into an expensive bid.
        if best_bid is not None and best_bid >= MIN_SELL_PRICE:
            if self._units_available >= ORDER_UNITS:
                self._send_limit(OrderSide.SELL, best_bid, ORDER_UNITS)
                return

    # -- helpers -----------------------------------------------------------

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
        order.ref = f"{BOT_NAME}-{self._order_count}"

        self.inform(f"Sending {side} {units}@{price} (ref={order.ref})")
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
        self.inform(f"Alive. cash_available={self._cash_available} "
                    f"units_available={self._units_available}")


if __name__ == "__main__":
    creds = load_credentials()
    bot = MyBot(
        creds["account"],
        creds["email"],
        creds["password"],
        creds["marketplace_id"],
    )
    bot.run()
