"""
Pre-class sanity check. Run this BEFORE the session:

    python check_setup.py

It verifies fmclient imports, prints the API of the version you actually
installed from Canvas, and confirms bot.py lines up with it. If anything
below is marked MISSING, the Canvas wheel differs from the template and you
should adjust bot.py to match what is printed here.
"""

import inspect
import sys

print(f"Python {sys.version.split()[0]}  ({sys.executable})")

try:
    import fmclient
except ImportError:
    sys.exit(
        "\nFAIL: fmclient is not installed in this interpreter.\n"
        "  Activate your virtual environment, then install the wheel from\n"
        "  Canvas, e.g.  pip install ./fmclient-<version>-py3-none-any.whl\n"
    )

print(f"fmclient {getattr(fmclient, '__version__', 'version unknown')}")
print(f"  from {getattr(fmclient, '__file__', '?')}\n")

# --- names bot.py imports at module level ---------------------------------
EXPECTED_NAMES = ["Agent", "Order", "OrderSide", "OrderType", "Holding",
                  "Market", "Session"]

print("Top-level names:")
missing_names = []
for name in EXPECTED_NAMES:
    ok = hasattr(fmclient, name)
    print(f"  {'ok     ' if ok else 'MISSING'} fmclient.{name}")
    if not ok:
        missing_names.append(name)

# --- Agent callbacks bot.py overrides -------------------------------------
EXPECTED_METHODS = ["initialised", "pre_start_tasks", "received_session_info",
                    "received_holdings", "received_orders", "order_accepted",
                    "order_rejected", "send_order", "run", "inform", "error",
                    "warning", "is_session_active", "execute_periodically",
                    "pending_outgoing_orders_count"]

missing_methods = []
if hasattr(fmclient, "Agent"):
    print("\nAgent members:")
    for name in EXPECTED_METHODS:
        ok = hasattr(fmclient.Agent, name)
        print(f"  {'ok     ' if ok else 'MISSING'} Agent.{name}")
        if not ok:
            missing_methods.append(name)

    print("\n  Agent.__init__ signature:")
    print(f"    {inspect.signature(fmclient.Agent.__init__)}")

    abstract = sorted(getattr(fmclient.Agent, "__abstractmethods__", set()))
    if abstract:
        print("\n  Abstract methods you MUST implement:")
        for name in abstract:
            print(f"    - {name}")

# --- Order construction helpers -------------------------------------------
if hasattr(fmclient, "Order"):
    print("\nOrder helpers:")
    # No create_cancel in fmclient 6 -- cancels are a copy with
    # order_type = OrderType.CANCEL. See MyBot._cancel in bot.py.
    for name in ["create_new", "my_current", "current"]:
        ok = hasattr(fmclient.Order, name)
        print(f"  {'ok     ' if ok else 'MISSING'} Order.{name}")
        if not ok:
            missing_methods.append(f"Order.{name}")

for enum_name in ["OrderSide", "OrderType"]:
    enum = getattr(fmclient, enum_name, None)
    if enum is not None:
        members = [m for m in dir(enum) if m.isupper()]
        print(f"\n{enum_name} members: {', '.join(members)}")

# --- ORM attribute names bot.py reads -------------------------------------
# Instance attributes (market.tick, holdings.cash_available, ...) don't show
# up in dir() of the class, so print each __init__ signature too -- that is
# where the real attribute names are visible.
print("\n" + "-" * 70)
print("ORM classes -- check these names match what bot.py reads")
print("-" * 70)

for cls_name in ["Market", "Holding", "Asset", "Session", "Order"]:
    cls = getattr(fmclient, cls_name, None)
    if cls is None:
        print(f"\n{cls_name}: NOT EXPORTED")
        continue
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        sig = "(unavailable)"
    print(f"\n{cls_name}.__init__{sig}")
    props = sorted(
        n for n in dir(cls)
        if not n.startswith("_") and isinstance(getattr(cls, n, None), property)
    )
    if props:
        print(f"  properties: {', '.join(props)}")

# --- full surface, so a mismatch can be diagnosed from one run -------------
if missing_names or missing_methods:
    print("\n" + "=" * 70)
    print("Mismatch detected -- dumping the full API of this version.")
    print("=" * 70)

    public = [n for n in dir(fmclient) if not n.startswith("_")]
    print(f"\nAll public names in fmclient ({len(public)}):")
    for name in public:
        obj = getattr(fmclient, name)
        kind = type(obj).__name__
        print(f"  {name}  [{kind}]")

    agent_cls = getattr(fmclient, "Agent", None)
    if agent_cls is not None:
        print("\nEvery public Agent member:")
        for name in sorted(n for n in dir(agent_cls) if not n.startswith("_")):
            attr = getattr(agent_cls, name, None)
            try:
                sig = str(inspect.signature(attr)) if callable(attr) else ""
            except (TypeError, ValueError):
                sig = "(?)"
            print(f"  {name}{sig}")

    order_cls = getattr(fmclient, "Order", None)
    if order_cls is not None:
        print("\nEvery public Order member:")
        for name in sorted(n for n in dir(order_cls) if not n.startswith("_")):
            attr = getattr(order_cls, name, None)
            try:
                sig = str(inspect.signature(attr)) if callable(attr) else ""
            except (TypeError, ValueError):
                sig = ""
            print(f"  {name}{sig}")

# --- verdict ---------------------------------------------------------------
print()
if missing_names or missing_methods:
    print("RESULT: fmclient imports, but the API differs from bot.py.")
    print("        Send the dump above to reconcile bot.py with this version.")
    sys.exit(1)

print("RESULT: setup looks good. bot.py matches this fmclient version.")
print("        Next: put your credentials in credentials.json, then "
      "run  python bot.py")
