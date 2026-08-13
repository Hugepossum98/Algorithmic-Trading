"""
Follow-along file for in-class coding.

A bare skeleton with the seven callbacks fmclient requires. Type whatever
your teacher writes into the matching method -- no need to fight the fuller
template in bot.py while keeping up with a lecture.

Run with:  python class_bot.py

Reminders:
  - methods go INSIDE the class, indented 4 spaces
  - prices are integer cents ($2.50 is 250)
  - self.inform("...") prints to the log
"""

from fmclient import Agent, Holding, Order, OrderSide, OrderType, Session

# Fill these in, or copy the load_credentials() approach from bot.py.
ACCOUNT = "<your account name>"
EMAIL = "<your email>"
PASSWORD = "<your password>"
MARKETPLACE_ID = 0


class ClassBot(Agent):

    def __init__(self, account, email, password, marketplace_id):
        super().__init__(account, email, password, marketplace_id,
                         name="ClassBot")
        # Your own variables go here.

    def initialised(self):
        """Once after login, when market info has arrived."""
        for market_id, market in self.markets.items():
            self.inform(f"Market {market_id}: {market}")

    def pre_start_tasks(self):
        """Register repeating jobs before the event loop starts."""
        pass

    def received_session_info(self, session: Session):
        """Market opened / paused / closed."""
        self.inform(f"Session active: {self.is_session_active()}")

    def received_holdings(self, holdings: Holding):
        """Your cash and units changed."""
        self.inform(f"Holdings: {holdings}")

    def received_orders(self, orders):
        """The order book changed. `orders` is the WHOLE book, not a delta."""
        self.inform(f"{len(orders)} orders in the book")

    def order_accepted(self, order: Order):
        self.inform(f"Accepted: {order}")

    def order_rejected(self, info: dict, order: Order):
        self.error(f"Rejected: {info}")


if __name__ == "__main__":
    bot = ClassBot(ACCOUNT, EMAIL, PASSWORD, MARKETPLACE_ID)
    bot.run()
