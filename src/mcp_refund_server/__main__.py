"""Entry point: ``python -m mcp_refund_server``."""

from __future__ import annotations

import logging
import sys

import anyio


def main() -> int:
    # Imported lazily so that stdout is reserved inside serve() before any
    # module-level side effect has a chance to write to it.
    from .server import serve

    try:
        anyio.run(serve)
    except KeyboardInterrupt:
        logging.getLogger("mcp_refund_server").info("shutting down on SIGINT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
