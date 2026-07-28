"""Entrypoint kept at the repository root.

The OpenOps API spawns this server as `<path>/.venv/bin/python <path>/main.py`, so this
path is part of that contract. The implementation lives in the `openops_mcp` package.
"""

from openops_mcp.__main__ import main

if __name__ == "__main__":
    main()
