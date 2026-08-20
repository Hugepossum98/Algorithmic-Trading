"""
NORMAL BOT -- trades the PRIVATE market only.

Its one job: when the manager posts a private order that is mispriced
against the public market, take it. It opens positions and never closes
them -- reactivebot.py does that in the public market.

    python bot.py

Run reactivebot.py alongside it in a second terminal.

Why it is safe for the two to run together:
  * this bot only ever sends orders to the PRIVATE market
  * reactivebot only ever sends orders to the PUBLIC market
  * both read position from the same server holdings feed, so neither
    opens while the other still has something to unwind

Prices are INTEGER CENTS. $2.50 is 250.
"""

import time

from fmclient import Agent, Holding, Order, OrderSide, OrderType, Session

import config

BOT_NAME = "NormalBot"


class MyBot(Agent):
    """Takes mispriced private orders. Private market only."""

    def __init__(self, account, email, password, marketplace_id):
        super().__init__(account, email, password, marketplace_id, name=BOT_NAME)
        self._public_market = None
        self._private_market = None

        self._net = 0            # units away from baseline
        self._cash = 0
        self._units_free = 0

        self._cycle_start = None
        self._cycle_index = 0
        self._seen_private = set()
        self._last_send = 0.0
        self._order_count = 0
        self._dumped = set()

    # -- lifecycle ---------------------------------------------------------

    def initialised(self):
        self._public_market, self._private_market = config.split_markets(
            self.markets, self)

    def pre_start_tasks(self):
        self.execute_periodically(self._tick, 5)

    def received_session_info(self, session: Session):
        self.inform(f"Session active={self.is_session_active()}")

    # -- data --------------------------------------------------------------

    def received_holdings(self, holdings: Holding):
        self._cash = getattr(holdings, "cash_available", 0)
        self._net = config.net_units(holdings)

        self._units_free = 0
        for _m, asset in (getattr(holdings, "assets", None) or {}).items():
            self._units_free += getattr(asset, "units_available", 0)

        self.inform(f"cash={self._cash} net={self._net:+d} "
                    f"(baseline {config.TARGET_UNITS})")

    def received_orders(self, orders):
        self._check_private_orders(orders)
        if not self._private_market or not self._public_market:
            return

        priv = [o for o in orders if o.market == self._private_market]
        pub = [o for o in orders if o.market == self._public_market]
        self._decide(priv, pub)

    def order_accepted(self, order: Order):
        self.inform(f"ACCEPTED {getattr(order, 'order_side', '?')} "
                    f"{getattr(order, 'units', '?')}@{getattr(order, 'price', '?')}")

    def order_rejected(self, info: dict, order: Order):
        self.error(f"REJECTED -- {info}")

    # -- the strategy ------------------------------------------------------

    def _decide(self, priv_book, pub_book):
        """Take a private order only when it beats the public market."""
        if not self.is_session_active():
            return

        # One order at a time, and give holdings a moment to catch up after
        # a send -- otherwise we open the same position twice.
        if self.pending_outgoing_orders_count(self._private_market) > 0:
            return
        if time.time() - self._last_send < 2.0:
            return

        # Not flat? reactivebot is still unwinding. Stay out of its way.
        if self._net != 0:
            return

        # Too late in the cycle to open something that must be closed.
        if self._seconds_left() <= config.UNWIND_BUFFER_SECONDS:
            return

        mine = {o.ref for o in Order.my_current().values()}
        priv_bid, priv_ask = self._best(
            [o for o in priv_book if o.ref not in mine])
        pub_bid, pub_ask = self._best(
            [o for o in pub_book if o.ref not in mine])

        # Manager sells cheap -> buy it, reactivebot sells it on the public
        # market at the higher public bid.
        if priv_ask is not None and pub_bid is not None:
            edge = pub_bid - priv_ask
            if edge >= config.MIN_EDGE and self._cash >= priv_ask * config.ORDER_UNITS:
                self.inform(f"BUY private @{priv_ask}, public bid {pub_bid}, "
                            f"edge {edge}c")
                self._send(OrderSide.BUY, priv_ask)
                return

        # Manager buys high -> sell to them, reactivebot buys it back cheaper
        # on the public market.
        if priv_bid is not None and pub_ask is not None:
            edge = priv_bid - pub_ask
            # Selling what we do not hold is a short. With TARGET_UNITS = 0
            # that is the only way this branch can ever fire, so requiring
            # units here would silently disable half the strategy.
            can_sell = (config.ALLOW_SHORT
                        or self._units_free >= config.ORDER_UNITS)
            if edge >= config.MIN_EDGE and can_sell:
                self.inform(f"SELL private @{priv_bid}, public ask {pub_ask}, "
                            f"edge {edge}c")
                self._send(OrderSide.SELL, priv_bid)
                return

    # -- cycle -------------------------------------------------------------

    def _seconds_left(self):
        if self._cycle_start is None:
            return float(config.CYCLE_SECONDS)
        return max(0.0, config.CYCLE_SECONDS - (time.time() - self._cycle_start))

    def _check_private_orders(self, orders):
        """A new private order starts a cycle and confirms the private market."""
        for order in orders:
            if not getattr(order, "is_private", False):
                continue
            key = getattr(order, "fm_id", None) or getattr(order, "ref", id(order))
            if key in self._seen_private:
                continue
            self._seen_private.add(key)

            # An is_private order can only exist in the private market.
            market = getattr(order, "market", None)
            if market is not None and market is not self._private_market:
                self._private_market = market
                others = [m for m in self.markets.values() if m is not market]
                self._public_market = others[0] if len(others) == 1 else None
                self.inform(f"PRIVATE confirmed: {getattr(market, 'item', '?')}")

            self._cycle_index += 1
            self._cycle_start = time.time()
            self.inform(f"--- Cycle {self._cycle_index}: private order "
                        f"{getattr(order, 'order_side', '?')} "
                        f"{getattr(order, 'units', '?')}@"
                        f"{getattr(order, 'price', '?')} ---")

    def _tick(self):
        if self._cycle_start is None:
            self._cycle_start = time.time()
            return
        if self._seconds_left() <= 0:
            self._cycle_index += 1
            self._cycle_start = time.time()
        self.inform(f"net={self._net:+d} {self._seconds_left():.0f}s left")

    # -- helpers -----------------------------------------------------------

    def _best(self, book):
        bids = [o.price for o in book if o.order_side == OrderSide.BUY]
        asks = [o.price for o in book if o.order_side == OrderSide.SELL]
        return (max(bids) if bids else None, min(asks) if asks else None)

    def _send(self, side, price):
        order = Order.create_new(self._private_market)
        order.price = price
        order.units = config.ORDER_UNITS
        order.order_type = OrderType.LIMIT
        order.order_side = side

        self._order_count += 1
        order.ref = f"{BOT_NAME}-{self._order_count}"

        self._last_send = time.time()
        self.inform(f"Sending {side} {config.ORDER_UNITS}@{price}")
        self.send_order(order)


if __name__ == "__main__":
    creds = config.load_credentials()
    bot = MyBot(creds["account"], creds["email"], creds["password"],
                creds["marketplace_id"])
    bot.run()
