# Algorithmic Trading — AdHocMarkets bots

University of Melbourne algorithmic trading class. Two bots trade one
marketplace against the rest of the class.

Account `jocund-value`, marketplace **3266**. Password lives only in
`credentials.json`, which is git-ignored — **this repo is public**, so it
must never reach a tracked file.

## Setting up a fresh machine

`fmclient` is **not on PyPI**. It ships as a wheel on Canvas
(`fmclient-6.0.1b0-py3-none-any.whl`) and must be downloaded first.

```powershell
git clone https://github.com/Hugepossum98/Algorithmic-Trading.git
cd Algorithmic-Trading
py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install "$HOME\Downloads\fmclient-6.0.1b0-py3-none-any.whl"
.\trade.ps1 creds          # prompts; password hidden
.\trade.ps1 check          # must print "setup looks good"
```

Python 3.11 or 3.12. Do not use 3.13+ — the wheel is a beta and newer
Pythons often have no compatible build.

### Traps that cost real time on this project

1. **Bare `pip` and `python` can resolve to different interpreters.** On
   Windows the Microsoft Store Python installs execution aliases that shadow
   a venv. Symptom: `Defaulting to user installation because normal
   site-packages is not writeable` *while a venv is active*. Always use
   `.\venv\Scripts\python.exe -m pip`, never bare `pip`.
2. **VS Code's ▶ Run button picks the wrong interpreter** and fails with
   `ModuleNotFoundError: No module named 'fmclient'`. Either run through
   `trade.ps1`, which calls the venv python explicitly, or set the
   interpreter once: Ctrl+Shift+P → `Python: Select Interpreter` → the
   `('venv': venv)` entry.
3. **Keep the project out of OneDrive.** A venv is thousands of small files;
   OneDrive syncing them causes file-lock errors mid-session.
4. **One clone only.** Two clones drift and you lose track of which is real.
5. `source venv/bin/activate` is Unix. On PowerShell it is
   `.\venv\Scripts\Activate.ps1` — though `trade.ps1` means you never need it.

## Run

```powershell
.\trade.ps1 start      # both bots, background
.\trade.ps1 logs       # follow both logs (Ctrl+C stops watching, not the bots)
.\trade.ps1 status     # running? what is the position?
.\trade.ps1 stop       # stop both
.\trade.ps1 panic      # cancel every resting order NOW
```

`trade.ps1` calls `venv\Scripts\python.exe` directly, so there is no venv to
activate. To run a bot in the foreground instead:
`.\venv\Scripts\python.exe bot.py`.

**Stopping a process does not cancel its resting orders.** They stay live and
can still fill, which is how a "stopped" bot ends a cycle off baseline. After
any hard stop, run `.\trade.ps1 panic`. Panic cancels orders only — it does
not trade back to baseline, because that needs a price decision.

Full command list: `.\trade.ps1 help`. Settings are changed without opening
an editor via `.\trade.ps1 set NAME VALUE`, which validates before saving.
`Trade.bat` is a double-clickable entry point that opens PowerShell already
in the project folder.

## The files

| File | Role |
| --- | --- |
| `trade.ps1` | start/stop/restart/status/logs/panic/set/config/creds/edit/check/update |
| `Trade.bat` | double-click entry point, pinnable to the taskbar |
| `config.py` | every parameter, credential loading, market identification, `net_units()`, `urgency()` |
| `bot.py` | NORMAL bot — private market only, opens positions |
| `reactivebot.py` | REACTIVE bot — public market only, closes positions |
| `panic.py` | cancels every resting order, reports whether position is at baseline |
| `setparam.py` | backs `trade.ps1 set`; rewrites one assignment, compiles before saving |
| `check_setup.py` | verifies the fmclient install, dumps its real API on mismatch |
| `credentials.json` | password. Git-ignored. Never commit. |

## The setting

Every student has the same setup: a **private market** only they can see,
where a manager posts a buy or sell order roughly every 60 seconds in
**blocks of 5 units**, and a shared **public market** where the whole class
trades against each other.

The binding constraint: you must return to your **starting inventory** before
each new private order. Not zero — whatever you began with. Ending a cycle
holding cash instead of stock is a failure, not a profit.

## The strategy

The private order is not the edge — everyone gets one. The edge is *when* you
unwind it.

Because every student must settle inside the same minute, the end of each
cycle is a predictable liquidity crunch: a crowd of forced traders all
crossing the spread at once, at the worst prices of the cycle. The bots aim
to be **finished before that crowd arrives**.

- `bot.py` (normal) — private market **only**, opens positions. Takes a
  manager order when it beats the public book by `MIN_EDGE` per unit.
- `reactivebot.py` (reactive) — public market **only**, closes positions.
  Walks its limit price continuously from patient to aggressive via
  `config.urgency()`: one tick inside the spread at cycle start, conceding
  steadily, crossing with `UNWIND_BUFFER_SECONDS` still on the clock.

## Why two processes are safe together

They never send to the same book, so they cannot race on orders. Both derive
position from the same server holdings feed through `config.net_units()`, so
the normal bot stands down (`net != 0`) while the reactive bot still has
something to unwind. The server is the shared ledger; there is no state file.

## fmclient 6.0.1b0 — hard-won facts

The Canvas wheel is 6.x, several major versions past the v2-era API most
examples show. Verified against the real wheel with `check_setup.py`:

- **`Order.create_cancel()` does not exist.** Cancel = `copy.copy(order)`
  with `order_type = OrderType.CANCEL`.
- `OrderType` members are `LIMIT` and `CANCEL` only.
- Use `pending_outgoing_orders_count(market)` and `Order.my_current()`.
  There is **no `order.mine`** attribute.
- **No `execution_delay`** on `Agent`; setting it silently does nothing.
- `Session` has `is_open` / `is_paused` / `is_closed` properties.
- `Agent.__init__(account, email, password, marketplace_id, name=None,
  enable_ws=False)`.
- ORM classes (`Market`, `Holding`, `Asset`, `Order`) build attributes at
  runtime and introspect as `(self, /, *args, **kwargs)`. Attribute names
  like `market.tick` and `asset.units` **cannot be confirmed statically** —
  every read goes through a `getattr` default, and `DEBUG_DUMP_ATTRS` prints
  the real names on first connect.

Run `python check_setup.py` after any wheel change.

## Bugs already fixed — do not reintroduce

1. **Baseline is not zero.** Treating flat as zero units sells off the
   starting inventory on the first book update. Position is measured against
   `config.TARGET_UNITS`.
2. **`units_available` is never negative.** A short unwind keyed off it can
   never fire. Use total `units` minus baseline. `units_available` is for
   sizing only — it excludes units held by your own resting orders, so it
   reads flat while an order is live.
3. **Wrong-side resting order deadlocks the unwind.** Cancelling only
   same-side orders, then falling through to "our order is already resting",
   never sends the closing order when the resting one is on the wrong side.
   `reactivebot._react()` cancels anything that is not the exact wanted
   side and price.
4. **Never guess which market is private.** Assigning by dict order is a coin
   flip on the most expensive mistake available. Resolve from an order
   flagged `is_private` — that can only exist in the private market — and
   refuse to trade while ambiguous. `_check_private_orders` must run *before*
   the resolved-markets guard, or it can never self-correct.
5. **Timer-driven unwind.** `received_orders` fires only when the book moves.
   A quiet market near the deadline would strand the position, so a
   1-second periodic task drives the exit.
6. **`net_units(holdings, target=TARGET_UNITS)` freezes the baseline** at
   import. Read module globals at call time.
7. **`ALLOW_SHORT`.** With `TARGET_UNITS = 0`, requiring units on hand to
   sell disables the entire sell-side arbitrage silently.

## Parameters (`config.py`)

| Name | Notes |
| --- | --- |
| `TARGET_UNITS` | **Set to real starting inventory before trading.** Wrong value = bot sells your stock off. |
| `MIN_EDGE` | Cents per unit. Must exceed roughly half the public spread or the round trip loses. |
| `ORDER_UNITS` / `MAX_POSITION` | 5 — the manager's block size. |
| `SLICE_UNITS` | Lower to work a block in pieces in a thin book. |
| `CYCLE_SECONDS` / `UNWIND_BUFFER_SECONDS` | 60 / 15. |
| `ALLOW_SHORT` | Sell to the manager holding nothing. |
| `PRIVATE_MARKET_ITEM` | Override if auto-detection picks wrong. |

## Unverified — resolve these on the first live run

Nothing here has touched a live marketplace. Logic was tested against a mock
built from introspection, which encodes the same assumptions as the code and
therefore cannot falsify them.

1. **ORM attribute names** — read the `[attrs]` dump.
2. **Two logins on one account** may be refused. If `reactivebot.py` cannot
   connect, merge both roles into one process rather than fighting it.
3. **Short selling** may be rejected — watch for rejections, then set
   `ALLOW_SHORT = False`.
4. **Market split** — confirm the `PRIVATE confirmed:` log line names the
   market you expect.

Recommended first run: set `MIN_EDGE` very high (e.g. 500) so nothing trades,
and check the market assignment, `TARGET_UNITS`, and the `[attrs]` names.

## Conventions

- Prices are **integer cents**. $2.50 is 250.
- `received_orders` delivers the **whole book** every time, never a delta.
- Read `order_rejected`'s `info` dict — a silent bot usually logged why there.
- Test changes against the mock pattern in the git history rather than the
  live market.

## How this was built, and how to change it safely

No live marketplace has ever been touched. Every behaviour was verified
against a hand-written mock shaped like the introspected fmclient 6.x API —
a stub module defining `Agent`, `Order`, `Market`, `OrderSide`, `OrderType`
with the same signatures, letting the callbacks be driven directly
(`bot.received_orders([...])`) and the sent orders inspected.

That mock encodes the same assumptions as the code, so **it cannot falsify
them**. It catches logic errors, not wrong beliefs about fmclient.

Scenarios currently covered: baseline preserved when flat, arbitrage snipe in
both directions, `MIN_EDGE` respected, normal bot standing down while the
reactive bot works, no position opened late in a cycle, the urgency ramp
walking monotonically from patient to crossing, timer-driven unwind on a
silent book, wrong-side order cancelled instead of deadlocking, leftover
order pulled when flat, cycle rollover without double-counting, and panic's
cancel-all with its two-sweep wait.

Two bugs were found *by* that testing rather than by reading: a name
collision where `_tick` was both the periodic task and a tick-size alias, and
the frozen default argument in `net_units`. Both were silent failures. Keep
driving the callbacks directly when changing strategy logic.

## Session history

Built in one session, in this order:

1. Initial template against the v2-era fmclient API most examples show.
2. `check_setup.py` run against the real Canvas wheel; API reconciled to 6.x.
3. Reviewed and hardened: the unwind deadlock, market identification, the
   credential guard, the silent size cap.
4. Split into two bots by market — normal owns private, reactive owns public
   — after the class setting was clarified.
5. Blocks of 5 and the urgency ramp, once it was clear every student settles
   inside the same minute.
6. `trade.ps1`, `panic.py`, `setparam.py`, `Trade.bat` for live operation.

PR #1 (merged) covers steps 1–3. `main` is behind the working branch
`claude/python-algo-trading-setup-vr77v9`, which has everything.
