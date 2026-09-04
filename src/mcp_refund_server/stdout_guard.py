"""STDIO isolation.

A stray ``print()`` — in this code, in a dependency, or in a future edit — is
the classic way to corrupt a stdio MCP server's JSON-RPC stream. There are two
halves to preventing it here, and only one of them is ours.

**The transport's half (fd 1).** ``mcp.server.stdio.stdio_server()`` claims the
standard descriptors for the duration of the session: it duplicates fd 1 onto a
private descriptor, serves the wire from that, and points fd 1 itself at stderr
(fd 0 goes to ``/dev/null``). So while the server is running, *anything* that
writes to "stdout" — ``print``, ``sys.stdout.write``, a C extension writing to
fd 1 directly, a subprocess inheriting the descriptor — lands harmlessly on
stderr. That is enforced by the kernel, not by convention, so it holds even for
code that never heard of this server.

The critical corollary, and the reason this module exists as documentation:
**passing an explicit ``stdout=`` to ``stdio_server()`` opts out of the claim.**
``server.serve()`` therefore deliberately passes nothing, and no code in this
package may rebind ``sys.stdout`` before the transport starts — the claim
inspects ``sys.stdout`` to find fd 1, and would otherwise serve JSON-RPC onto
whatever it had been redirected to.

**Our half (everything else).** Diagnostics must not merely *miss* stdout, they
must go somewhere useful. ``logging_setup`` sends the root logger to stderr, so
library logging is captured rather than silently discarded.
"""

from __future__ import annotations

import os


def stdout_is_diverted() -> bool:
    """True when fd 1 and fd 2 refer to the same open file.

    That is the observable signature of the transport's claim being in effect.
    Used as a startup self-check and by the isolation tests; the claim is
    best-effort in exotic environments (a non-fd-backed ``sys.stdout``, a
    platform where ``dup2`` fails), so this reports rather than asserts.
    """
    try:
        one, two = os.fstat(1), os.fstat(2)
    except OSError:
        return False
    return (one.st_dev, one.st_ino, one.st_rdev) == (two.st_dev, two.st_ino, two.st_rdev)
