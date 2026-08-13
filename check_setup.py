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
                    "order_rejected", "send_order", "run", "inform", "error"]

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
    for name in ["create_new", "create_cancel"]:
        ok = hasattr(fmclient.Order, name)
        print(f"  {'ok     ' if ok else 'MISSING'} Order.{name}")
        if not ok:
            missing_methods.append(f"Order.{name}")

for enum_name in ["OrderSide", "OrderType"]:
    enum = getattr(fmclient, enum_name, None)
    if enum is not None:
        members = [m for m in dir(enum) if m.isupper()]
        print(f"\n{enum_name} members: {', '.join(members)}")

# --- verdict ---------------------------------------------------------------
print()
if missing_names or missing_methods:
    print("RESULT: fmclient imports, but the API differs from bot.py.")
    print("        Update bot.py to use the names printed above.")
    sys.exit(1)

print("RESULT: setup looks good. bot.py matches this fmclient version.")
print("        Next: put your credentials in credentials.json, then "
      "run  python bot.py")
