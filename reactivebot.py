"""
REACTIVE BOT -- trades the PUBLIC market only.

Its one job: whenever the position drifts off baseline, react by trading it
back. It never opens a position of its own -- bot.py does that in the
private market. This bot only ever closes.

    python reactivebot.py

Run bot.py alongside it in a second terminal.

Behaviour:
  * long  -> post a sell, undercutting the best public ask by one tick
  * short -> post a buy, one tick above the best public bid
  * reprice whenever the book moves and our order is no longer best
  * in the last UNWIND_BUFFER_SECONDS, stop being fussy and cross the
    spread, because being flat matters more than the last cent

Prices are INTEGER CENTS. $2.50 is 250.
"""

import copy
import time

from fmclient import Agent, Holding, Order, OrderSide, OrderType, Session

import config

BOT_NAME = "ReactiveBot"


class Reactive(Agent):
    """Returns the position to baseline. Public market only."""

    def __init__(self, account, email, password, marketplace_id):
        super().__init__(account, email, password, marketplace_id, name=BOT_NAME)
        self._public_market = None
        self._private_market = None

        self._net = 0
        self._cash = 0
        self._units_free = 0

        self._cycle_start = None
        self._seen_private = set()
        self._pub_book = []
        self._order_count = 0
        self._dumped = set()

    # -- lifecycle ---------------------------------------------------------

    def initialised(self):
        self._public_market, self._private_market = config.split_markets(
            self.markets, self)

    def pre_start_tasks(self):
        # Every second: the book can go quiet right when we most need to
        # exit, so the deadline has to be driven by a clock, not by events.
        self.execute_periodically(self._cycle_tick, 1)

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
        if not self._public_market:
            return

        self._pub_book = [o for o in orders if o.market == self._public_market]
        self._react()

    def order_accepted(self, order: Order):
        self.inform(f"ACCEPTED {getattr(order, 'order_side', '?')} "
                    f"{getattr(order, 'units', '?')}@{getattr(order, 'price', '?')}")

    def order_rejected(self, info: dict, order: Order):
        self.error(f"REJECTED -- {info}")

    # -- the reaction ------------------------------------------------------

    def _react(self):
        """Position off baseline? Trade it back."""
        if not self.is_session_active() or self._public_market is None:
            return
        if self.pending_outgoing_orders_count(self._public_market) > 0:
            return

        if self._net == 0:
            # Flat. Nothing to do -- but pull any leftover order so it
            # cannot fill later and push us off baseline again.
            for order in self._my_public_orders():
                self._cancel(order)
                return
            return

        u = config.urgency(self._seconds_left())
        urgent = u >= 1.0
        mine = {o.ref for o in Order.my_current().values()}
        bid, ask = self._best([o for o in self._pub_book if o.ref not in mine])

        side = OrderSide.SELL if self._net > 0 else OrderSide.BUY
        price = self._work_price(side, bid, ask, u)

        if price is None:
            self.warning(f"net {self._net:+d} but no price to work with.")
            return

        # Anything of ours that is not this exact order is in the way.
        # Checking only same-side orders here would deadlock: a leftover
        # buy while long can never reduce a long, but would still look
        # like "we already have an order out".
        for order in self._my_public_orders():
            if order.order_side != side or order.price != price:
                self._cancel(order)
                return          # let the cancel land first
            return              # right order, right price -- wait for the fill

        # Work in slices while there is time; take the whole thing once the
        # ramp is at full urgency, so we never end a cycle with a remainder.
        units = abs(self._net) if urgent else min(abs(self._net),
                                                  config.SLICE_UNITS)
        if side == OrderSide.SELL:
            units = min(units, self._units_free)
        else:
            units = min(units, self._cash // price if price else 0)

        if units <= 0:
            self.warning(f"net {self._net:+d} but cannot size an order "
                         f"(free={self._units_free} cash={self._cash}).")
            return

        self.inform(f"{'CROSSING' if urgent else 'working'} {side} "
                    f"{units}@{price} to close {self._net:+d} "
                    f"(urgency {u:.0%}, {self._seconds_left():.0f}s left)")
        self._send(side, price, units)

    def _work_price(self, side, bid, ask, u):
        """Walk the limit price from patient to aggressive as u goes 0 -> 1.

        u = 0.0  post where we earn the spread (best price, one tick better
                 than the current best on our side)
        u = 1.0  cross and take whatever the other side is showing

        Everything between is a linear walk. The point is to be finished
        before the end-of-cycle crowd forces its way out at the worst
        prices -- start ambitious, concede steadily, never get stuck.
        """
        tick = self._tick_size()

        if side == OrderSide.SELL:
            patient = (ask - tick) if ask is not None else bid
            aggressive = bid if bid is not None else patient
        else:
            patient = (bid + tick) if bid is not None else ask
            aggressive = ask if ask is not None else patient

        if patient is None:
            return None if aggressive is None else self._round(aggressive)
        if aggressive is None:
            return self._round(patient)

        price = patient + (aggressive - patient) * u
        return self._round(price)

    def _round(self, price):
        """Snap to the tick and keep inside the market's legal range."""
        tick = self._tick_size()
        price = int(round(price / tick) * tick)

        low = getattr(self._public_market, "min_price", None)
        high = getattr(self._public_market, "max_price", None)
        if low is not None:
            price = max(price, low)
        if high is not None:
            price = min(price, high)
        return price

    # -- cycle -------------------------------------------------------------

    def _seconds_left(self):
        if self._cycle_start is None:
            return float(config.CYCLE_SECONDS)
        return max(0.0, config.CYCLE_SECONDS - (time.time() - self._cycle_start))

    def _check_private_orders(self, orders):
        """Track the cycle clock, and learn which market is which."""
        for order in orders:
            if not getattr(order, "is_private", False):
                continue
            key = getattr(order, "fm_id", None) or getattr(order, "ref", id(order))
            if key in self._seen_private:
                continue
            self._seen_private.add(key)

            market = getattr(order, "market", None)
            if market is not None and market is not self._private_market:
                self._private_market = market
                others = [m for m in self.markets.values() if m is not market]
                self._public_market = others[0] if len(others) == 1 else None
                self.inform(f"PUBLIC confirmed: "
                            f"{getattr(self._public_market, 'item', '?')}")

            if self._net != 0:
                self.error(f"New cycle started while still {self._net:+d} "
                           f"off baseline.")
            self._cycle_start = time.time()

    def _cycle_tick(self):
        """Clock-driven: a silent book must not stop us exiting."""
        if self._cycle_start is None:
            self._cycle_start = time.time()
            return
        if self._seconds_left() <= 0:
            self._cycle_start = time.time()
        if self._net != 0 and self._seconds_left() <= config.UNWIND_BUFFER_SECONDS:
            self.warning(f"UNWIND {self._net:+d}, "
                         f"{self._seconds_left():.0f}s left")
            self._react()

    # -- helpers -----------------------------------------------------------

    def _tick_size(self):
        return getattr(self._public_market, "tick", None) or 1

    def _my_public_orders(self):
        return [o for o in Order.my_current().values()
                if o.market == self._public_market]

    def _best(self, book):
        bids = [o.price for o in book if o.order_side == OrderSide.BUY]
        asks = [o.price for o in book if o.order_side == OrderSide.SELL]
        return (max(bids) if bids else None, min(asks) if asks else None)

    def _send(self, side, price, units):
        order = Order.create_new(self._public_market)
        order.price = price
        order.units = units
        order.order_type = OrderType.LIMIT
        order.order_side = side

        self._order_count += 1
        order.ref = f"{BOT_NAME}-{self._order_count}"
        self.send_order(order)

    def _cancel(self, order: Order):
        cancel = copy.copy(order)
        cancel.order_type = OrderType.CANCEL
        cancel.ref = f"cancel-{getattr(order, 'ref', 'order')}"
        self.inform(f"Cancelling {getattr(order, 'ref', None)}")
        self.send_order(cancel)


if __name__ == "__main__":
    creds = config.load_credentials()
    bot = Reactive(creds["account"], creds["email"], creds["password"],
                   creds["marketplace_id"])
    bot.run()
