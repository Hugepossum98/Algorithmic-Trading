# Algorithmic Trading — AdHocMarkets bot

Python bot template for the class trading sessions, built on `fmclient`.

| File | What it's for |
| --- | --- |
| `bot.py` | The bot. Edit the CONFIG block and `_on_book_update()`. |
| `check_setup.py` | Run before class — confirms `fmclient` is installed and matches `bot.py`. |
| `credentials.example.json` | Copy to `credentials.json` (git-ignored) and fill in. |

## Setup

`fmclient` is **not on PyPI** — download the wheel from Canvas first, and note
where it saved (usually `Downloads`).

Use a virtual environment. Beyond being good practice, it guarantees `python`
and `pip` refer to the *same* interpreter — if you have several Pythons
installed, they often don't, and packages get installed where your script
can't see them.

fmclient is an older library, so prefer Python 3.11 or 3.12 if you have one.
Very new versions (3.13+) frequently have no compatible build.

### Windows (PowerShell)

```powershell
py -0                             # list your installed Pythons
py -3.12 -m venv venv             # or py -3.11, or just py
.\venv\Scripts\Activate.ps1       # prompt should now start with (venv)

# use the REAL filename -- type "fmc" and press Tab to autocomplete
pip install "$HOME\Downloads\fmclient-2.0.0-py3-none-any.whl"

python check_setup.py
```

If activation fails with an execution-policy error, allow it for that window
only: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

### macOS / Linux

```bash
python3 -m venv venv
source venv/bin/activate          # prompt should now start with (venv)

# use the REAL filename of the wheel you downloaded
pip install ~/Downloads/fmclient-2.0.0-py3-none-any.whl

python check_setup.py
```

Both blocks show `fmclient-2.0.0-py3-none-any.whl` as an example — substitute
whatever the Canvas file is actually called. Run `pip install` on a path that
exists, or pip will tell you the file isn't there.

Reactivate the venv (the activate line) in every new terminal you open.

`check_setup.py` prints the API of the version you installed. If it reports
`MISSING` for anything, the Canvas wheel differs from this template — adjust
`bot.py` to use the names it printed.

## Credentials

```powershell
Copy-Item credentials.example.json credentials.json   # PowerShell
```
```bash
cp credentials.example.json credentials.json          # macOS / Linux
```

Fill in your account name, email, password, and the marketplace id given in
class. `credentials.json` is git-ignored, so your password stays out of the
repo. Environment variables (`FM_ACCOUNT`, `FM_EMAIL`, `FM_PASSWORD`,
`FM_MARKETPLACE_ID`) override the file if you prefer those.

## Running

```bash
python bot.py
```

On startup the bot logs in, prints every market it can see, prints your
holdings, and then prints the order book on each update. **It places no
orders** until you set `ENABLE_EXAMPLE_STRATEGY = True` in `bot.py` or write
your own logic — so it's safe to run while you watch the callbacks fire.

## How fmclient calls your code

`Agent` is event-driven: you don't poll, you override callbacks.

| Callback | Fires when |
| --- | --- |
| `initialised()` | Once after login, when market metadata has arrived. |
| `pre_start_tasks()` | Once before the event loop — register periodic jobs here. |
| `received_session_info(session)` | Market opens or closes. |
| `received_holdings(holdings)` | Your cash/units change. |
| `received_orders(orders)` | The order book changes. |
| `order_accepted(order)` | Your order reached the book. |
| `order_rejected(info, order)` | Your order was refused — read `info`. |

## Things that cost people money last session

- **Prices are integer cents.** `250` is $2.50. Sending `2.50` will be rejected
  or mispriced.
- **`received_orders` gives you the whole book every time**, not a delta. Don't
  treat each callback as "new" orders.
- **One order in flight at a time.** Sending another before the first is
  accepted gets it rejected. `bot.py` tracks this with `_pending_ref`.
- **Check `cash_available` / `units_available`, not `cash` / `units`.** The
  difference is what's already committed to resting orders.
- **Respect `market.tick`, `min_price`, `max_price`.** `_clamp_to_tick()`
  handles this.
- **Read `order_rejected` output.** A silent bot that placed nothing usually
  logged the reason there. `info` is a dict with the reason in it.
- **There is no `Order.create_cancel()` in fmclient 6.** To cancel, copy the
  order and set `order_type = OrderType.CANCEL` — `_cancel()` does this.

## Version note

Written against **fmclient 6.0.1b0** (the Canvas wheel). The useful bits of
that API:

| Call | Use |
| --- | --- |
| `Order.create_new(market)` | Build a new order |
| `Order.my_current()` | Dict of your own live orders |
| `Order.current()` | Dict of every live order |
| `self.pending_outgoing_orders_count(market)` | Orders you've sent that haven't landed yet |
| `self.is_session_active()` | Whether trading is open right now |
| `self.execute_periodically(fn, secs)` | Run something on a timer |

Re-run `check_setup.py` if the wheel is ever updated.

fmclient's ORM classes (`Market`, `Holding`, `Asset`) build their attributes
at runtime, so `check_setup.py` can't confirm names like `market.tick` or
`holdings.cash_available` — only a live connection can. `bot.py` therefore
ships with `DEBUG_DUMP_ATTRS = True`, which prints the real attributes of the
first market, holding, and order the server sends, once each, tagged
`[attrs]`. Read those on your first run, then set it to `False` to quiet the
log. Every ORM read goes through a `getattr` default, so a name that doesn't
match logs `?` rather than crashing the bot mid-session.

## Inventory discipline (the cycle rule)

The manager hands out a private order roughly every 60 seconds, and you must
be back at your baseline unit count before the next one lands. Every buy has
to be paired with a sell inside the same cycle.

`bot.py` enforces this rather than trusting the strategy to remember:

| Setting | Default | Meaning |
| --- | --- | --- |
| `CYCLE_SECONDS` | `60` | Expected gap between private orders |
| `UNWIND_BUFFER_SECONDS` | `15` | Stop opening, start flattening, this far from the end |
| `MAX_OPEN_UNITS` | `1` | How far from baseline you may ever be |
| `TARGET_UNITS` | `None` | Baseline; `None` captures whatever you hold at connect |
| `ALLOW_LOSS_TO_FLATTEN` | `True` | Cross the spread if that's what being flat costs |

How it behaves:

1. **Baseline** is captured from your first holdings update and logged.
2. **An open position is a debt.** While `net != 0` the bot only trades in the
   direction that closes it — a cheap ask is ignored when you're already long.
3. **Late in a cycle it stops opening.** Inside the unwind window, a flat bot
   sits still rather than starting a round trip it can't finish.
4. **The unwind is timer-driven**, not book-driven. `_on_book_update` only
   fires when the book moves, so a quiet market would otherwise strand you
   holding units. `_cycle_tick` runs every second regardless.
5. **Resting orders get cancelled first** when flattening — they tie up the
   very units and cash needed to get flat.
6. **A cycle ending off baseline logs an error**, with the buy/sell counts. In
   this setting that's the failure that matters most.

Cycle boundaries are detected from the manager's order itself via
`order.is_private`, with `CYCLE_SECONDS` as the fallback clock.

`ALLOW_LOSS_TO_FLATTEN = True` means the bot will deliberately sell into a bad
bid rather than carry a position across the boundary. That is usually the
right trade in this setting, but it is a real cost — set it `False` if your
lecturer's rules say otherwise.

## Writing your strategy

Everything lives in one method:

```python
def _on_book_update(self, book, best_bid, best_ask):
    ...
```

`book` is the list of resting `Order` objects (yours have `order.mine == True`),
`best_bid` / `best_ask` are cents or `None`. Send orders with
`self._send_limit(OrderSide.BUY, price, units)` and cancel with
`self._cancel(order)`.

The included example only crosses the spread when the price clears a fixed
threshold (`MAX_BUY_PRICE` / `MIN_SELL_PRICE`) — it's a starting point for the
week 3 setting, not a strategy that will win the graded task.
