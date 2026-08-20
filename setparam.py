"""
Change a setting in config.py without opening an editor.

    python setparam.py                    list every setting and its value
    python setparam.py TARGET_UNITS       show one setting
    python setparam.py TARGET_UNITS 5     change it

Used by  .\trade.ps1 set TARGET_UNITS 5

Rewrites only the assignment line, leaving comments and layout alone, and
refuses to save if the result would not import -- so a bad value cannot
break the bots mid-session.
"""

import ast
import re
import sys
from pathlib import Path

CONFIG = Path(__file__).with_name("config.py")


def literal(text):
    """Turn a command-line word into the Python literal to write.

    Numbers, True/False/None and lists stay bare; anything else is quoted,
    so `set PRIVATE_MARKET_ITEM Widget` writes "Widget" and not a NameError.
    """
    try:
        ast.literal_eval(text)
        return text
    except (ValueError, SyntaxError):
        return '"' + text.replace('"', '\\"') + '"'


def settings(source):
    """Every module-level CONSTANT = value in config.py, in file order."""
    found = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id.isupper():
                found[target.id] = ast.get_source_segment(source, node.value)
    return found


def main(argv):
    source = CONFIG.read_text()
    current = settings(source)

    if not argv:
        width = max(len(name) for name in current)
        for name, value in current.items():
            print(f"  {name:<{width}} = {value}")
        return 0

    name = argv[0].upper()
    if name not in current:
        print(f"No setting called {name}. Known settings:", file=sys.stderr)
        for known in current:
            print(f"  {known}", file=sys.stderr)
        return 1

    if len(argv) == 1:
        print(f"{name} = {current[name]}")
        return 0

    new_value = literal(" ".join(argv[1:]))
    if new_value == current[name]:
        print(f"{name} is already {new_value}")
        return 0

    # Replace only the assignment, and only at the start of a line, so a
    # mention of the name inside a comment or docstring is left alone.
    pattern = re.compile(rf"^{re.escape(name)}\s*=[^\n]*$", re.MULTILINE)
    updated, count = pattern.subn(f"{name} = {new_value}", source, count=1)
    if count != 1:
        print(f"Could not find the line assigning {name}.", file=sys.stderr)
        return 1

    try:
        compile(updated, str(CONFIG), "exec")
    except SyntaxError as exc:
        print(f"Refusing to save -- that value breaks config.py: {exc}",
              file=sys.stderr)
        return 1

    CONFIG.write_text(updated)
    print(f"{name}: {current[name]} -> {new_value}")
    print("Restart the bots for this to take effect:  .\\trade.ps1 restart")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
