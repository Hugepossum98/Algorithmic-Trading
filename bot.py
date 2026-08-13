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

ACCOUNT = "jocund-value"
EMAIL = "jvandersteen@student.unimelb.edu.au"
PASSWORD = ""
MARKETPLACE_ID = 3265  # integer id given in class

BOT_NAME = "MyBot"

# Safety switch. Leave False until you have read what the example does and
# you actually want the bot placing live orders.
ENABLE_EXAMPLE_STRATEGY = True

# Example-strategy parameters (all in cents).
MAX_BUY_PRICE = 400  # never pay more than this
MIN_SELL_PRICE = 600  # never sell for less than this
ORDER_UNITS = 1

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
    """Robust Multi-Market Arbitrage Bot"""

    def __init__(self, account, email, password, marketplace_id):
        super().__init__(account, email, password, marketplace_id, name=BOT_NAME)

        # We now track TWO markets
        self._public_market = None
        self._private_market = None
        
        self._order_count = 0
        self._dumped = set()
        self._cash_available = 0
        self._units_available = 0

    # -- lifecycle ---------------------------------------------------------

    def initialised(self):
        """Called once after login. Identify Private vs Public markets."""
        for market_id, market in self.markets.items():
            item_name = getattr(market, 'item', '?')
            self.inform(f"Market {market_id} visible: item={item_name}")
            
            # Heuristic: The private market usually has your account name or 'private' in it.
            if ACCOUNT.lower() in item_name.lower() or 'private' in item_name.lower():
                self._private_market = market
            else:
                self._public_market = market

        # Fallback if the naming convention is completely different
        if not self._private_market and len(self.markets) >= 2:
            markets_list = list(self.markets.values())
            self._private_market = markets_list[0]
            self._public_market = markets_list[1]

        if not self._public_market or not self._private_market:
            self.error("CRITICAL: Could not find both a Public and Private market!")
        else:
            self.inform(f"Assigned PRIVATE market: {getattr(self._private_market, 'item', '?')}")
            self.inform(f"Assigned PUBLIC market: {getattr(self._public_market, 'item', '?')}")

    def pre_start_tasks(self):
        pass

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
        """Tracks total cash and aggregates units across all markets."""
        self._cash_available = getattr(holdings, "cash_available", 0)
        
        total_units = 0
        for market, asset in (getattr(holdings, "assets", None) or {}).items():
            total_units += getattr(asset, "units_available", 0)
        
        self._units_available = total_units

        self.inform(
            f"Holdings: cash_available={self._cash_available} "
            f"total_units_available={self._units_available}"
        )

    def received_orders(self, orders):
        """Split the master order book into Public and Private books."""
        if not self._public_market or not self._private_market:
            return

        if orders and "Order" not in self._dumped:
            self._dump_attrs("Order", orders[0])

        pub_book = [o for o in orders if getattr(o, "market", None) == self._public_market]
        priv_book = [o for o in orders if getattr(o, "market", None) == self._private_market]

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
        """CUTTHROAT ARBITRAGE STRATEGY - V2"""
        if not ENABLE_EXAMPLE_STRATEGY:
            return

        # 1. Speed Check
        if self.pending_outgoing_orders_count(self._public_market) > 0 or \
           self.pending_outgoing_orders_count(self._private_market) > 0:
            return

        my_orders = list(Order.my_current().values())
        my_pub_orders = [o for o in my_orders if o.market == self._public_market]
        my_priv_orders = [o for o in my_orders if o.market == self._private_market]

        other_pub = [o for o in pub_book if o.ref not in [m.ref for m in my_orders]]
        other_priv = [o for o in priv_book if o.ref not in [m.ref for m in my_orders]]

        pub_best_bid, pub_best_ask = self._best_prices(other_pub)
        priv_best_bid, priv_best_ask = self._best_prices(other_priv)

        tick = getattr(self._public_market, "tick", 1)
        max_pub_units = getattr(self._public_market, "unitMaximum", ORDER_UNITS)

        # ------------------------------------------------------------------
        # PRIORITY 1: CUTTHROAT INVENTORY DUMPING (With Dynamic Repricing)
        # ------------------------------------------------------------------
        if self._units_available > 0:
            # Determine our ideal undercut price
            if pub_best_ask:
                ideal_price = pub_best_ask - tick
                if pub_best_bid and ideal_price <= pub_best_bid:
                    ideal_price = pub_best_bid
            else:
                ideal_price = pub_best_bid if pub_best_bid else MIN_SELL_PRICE

            # STALE ORDER CHECK: If our active order isn't at the ideal price, cancel it
            if my_pub_orders:
                for order in my_pub_orders:
                    if order.order_side == OrderSide.SELL and order.price != ideal_price:
                        self._cancel(order)
                        return # Let the server process the cancel before placing a new one
                return # If our order is at the perfect price, wait for the fill

            # Slice and Send
            units_to_sell = min(self._units_available, max_pub_units)
            self._send_limit(self._public_market, OrderSide.SELL, ideal_price, units_to_sell)
            return

        if self._units_available < 0:
            # Determine our ideal penny-jump price
            if pub_best_bid:
                ideal_price = pub_best_bid + tick
                if pub_best_ask and ideal_price >= pub_best_ask:
                    ideal_price = pub_best_ask
            else:
                ideal_price = pub_best_ask if pub_best_ask else MAX_BUY_PRICE

            # STALE ORDER CHECK
            if my_pub_orders:
                for order in my_pub_orders:
                    if order.order_side == OrderSide.BUY and order.price != ideal_price:
                        self._cancel(order)
                        return
                return

            units_to_buy = min(abs(self._units_available), max_pub_units)
            self._send_limit(self._public_market, OrderSide.BUY, ideal_price, units_to_buy)
            return

        # ------------------------------------------------------------------
        # PRIORITY 2: THE ARBITRAGE SNIPE
        # ------------------------------------------------------------------
        if self._units_available == 0:
            # Manager is selling cheap -> We BUY from Private.
            # (Inventory goes >0, Priority 1 will dump to Public)
            if priv_best_ask and pub_best_bid and priv_best_ask < pub_best_bid:
                if self._cash_available >= priv_best_ask * max_pub_units and not my_priv_orders:
                    self.inform(f"SNIPING MANAGER ASK: Buying @ {priv_best_ask}")
                    self._send_limit(self._private_market, OrderSide.BUY, priv_best_ask, ORDER_UNITS)
                    return

            # Manager is buying high -> We SHORT SELL to Private.
            # (Inventory goes <0, Priority 1 will buy back from Public)
            if priv_best_bid and pub_best_ask and priv_best_bid > pub_best_ask:
                if not my_priv_orders:
                    self.inform(f"SNIPING MANAGER BID: Selling short to Manager @ {priv_best_bid}")
                    self._send_limit(self._private_market, OrderSide.SELL, priv_best_bid, ORDER_UNITS)
                    return

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
        order.ref = f"{BOT_NAME}-{self._order_count}"

        market_name = getattr(market, 'item', 'Unknown')
        self.inform(f"Sending to {market_name}: {side} {units}@{price} (ref={order.ref})")
        self.send_order(order)

    def _cancel(self, order: Order):
        cancel = copy.copy(order)
        cancel.order_type = OrderType.CANCEL
        cancel.ref = f"cancel-{getattr(order, 'ref', 'order')}"
        self.inform(f"Cancelling ref={getattr(order, 'ref', None)}")
        self.send_order(cancel)

    def _cancel_all_mine(self):
        for order in Order.my_current().values():
            self._cancel(order)

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
        self.inform(f"Alive. cash_available={self._cash_available} units_available={self._units_available}")


if __name__ == "__main__":
    creds = load_credentials()
    bot = MyBot(
        creds["account"],
        creds["email"],
        creds["password"],
        creds["marketplace_id"],
    )
    bot.run()