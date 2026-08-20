"""
PANIC BUTTON -- cancel every resting order, then quit.

    python panic.py         (or:  .\trade.ps1 panic)

Killing a bot process does NOT cancel the orders it left on the market.
They stay live and can still fill, which is how a "stopped" bot ends a
cycle off baseline. Run this after any hard stop.

It cancels orders only. It does NOT trade you back to baseline -- closing
a position needs a price decision, and that is reactivebot's job. This
reports your position so you can decide.
"""

import copy
import sys

from fmclient import Agent, Holding, Order, OrderType, Session

import config

BOT_NAME = "Panic"


class Panic(Agent):
    """Connect, cancel everything resting, report position, exit."""

    def __init__(self, account, email, password, marketplace_id):
        super().__init__(account, email, password, marketplace_id, name=BOT_NAME)
        self._cancelled = set()
        self._sweeps = 0
        self._net = None

    def initialised(self):
        self.inform("Connected. Sweeping for resting orders...")

    def pre_start_tasks(self):
        self.execute_periodically(self._sweep, 2)

    def received_session_info(self, session: Session):
        pass

    def received_holdings(self, holdings: Holding):
        self._net = config.net_units(holdings)

    def received_orders(self, orders):
        self._sweep()

    def order_accepted(self, order: Order):
        self.inform(f"cancel accepted: {getattr(order, 'ref', None)}")

    def order_rejected(self, info: dict, order: Order):
        self.error(f"cancel REJECTED: {info}")

    def _sweep(self):
        """Cancel anything still resting; quit once two sweeps find nothing."""
        resting = list(Order.my_current().values())

        if not resting:
            self._sweeps += 1
            # Two clean passes, because a cancel sent on the previous sweep
            # may not have been acknowledged yet.
            if self._sweeps >= 2:
                self._report_and_quit()
            return

        self._sweeps = 0
        for order in resting:
            key = getattr(order, "fm_id", None) or getattr(order, "ref", id(order))
            if key in self._cancelled:
                continue
            self._cancelled.add(key)

            cancel = copy.copy(order)
            cancel.order_type = OrderType.CANCEL
            cancel.ref = f"panic-{getattr(order, 'ref', 'order')}"
            self.inform(f"cancelling {getattr(order, 'order_side', '?')} "
                        f"{getattr(order, 'units', '?')}@"
                        f"{getattr(order, 'price', '?')}")
            self.send_order(cancel)

    def _report_and_quit(self):
        self.inform(f"All clear. {len(self._cancelled)} order(s) cancelled.")

        if self._net is None:
            self.warning("No holdings received -- check your position manually.")
        elif self._net == 0:
            self.inform(f"Position is AT BASELINE ({config.TARGET_UNITS} units). "
                        f"Nothing further to do.")
        else:
            self.error(
                f"Position is {self._net:+d} units off baseline "
                f"({config.TARGET_UNITS}). Cancelling did not fix this -- you "
                f"must trade back to flat. Restart reactivebot.py, or do it "
                f"by hand in the web client."
            )

        self.stop_after_wait(1)


if __name__ == "__main__":
    creds = config.load_credentials()
    bot = Panic(creds["account"], creds["email"], creds["password"],
                creds["marketplace_id"])
    try:
        bot.run()
    except KeyboardInterrupt:
        sys.exit(0)
