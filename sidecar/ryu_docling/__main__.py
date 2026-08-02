"""Entry point: `python -m ryu_docling` starts the uvicorn server.

Host/port come from the environment so Core can pin them at spawn:
  RYU_DOCLING_HOST (default 127.0.0.1) · RYU_DOCLING_PORT

The port env is the manifest's `port_env`, which Core sets to the
**profile-shifted** port (the dev profile adds 1000 to every port, so 8095 becomes
9095), meaning the DEFAULT_PORT constant is only reached on a bare standalone run.
`DOCLING_PORT` is accepted as a plain-name fallback for running the package
outside Core.
"""

from __future__ import annotations

import os

import uvicorn

from . import DEFAULT_PORT


def _port() -> int:
    for name in ("RYU_DOCLING_PORT", "DOCLING_PORT"):
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return DEFAULT_PORT


def main() -> None:
    # Loopback only. This process reads local files by path; it must never be
    # reachable off-box, and Core proxies it from the same machine.
    host = os.environ.get("RYU_DOCLING_HOST", "127.0.0.1")
    # Single worker: the job table is in-process, so a second worker would answer
    # polls for jobs it has never heard of — and each worker would load its own
    # copy of the Docling models.
    uvicorn.run("ryu_docling.server:app", host=host, port=_port(), log_level="info")


if __name__ == "__main__":
    main()
