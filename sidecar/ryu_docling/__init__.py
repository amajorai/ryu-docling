"""Ryu Docling sidecar — a Core-managed document-parsing runtime.

Wraps IBM's MIT-licensed `docling` library behind the swappable `document.parse`
capability contract: submit a parse, get a `job_id` back immediately, poll for the
result. Core owns lifecycle, storage, chunking and embedding; this process only
turns bytes on disk into markdown.

Why job-id + poll rather than one long request: the ext-proxy's activity guard
drops as soon as response headers arrive, so a `lazy` + `idle_stop_secs` sidecar
can be reaped *mid-request*. That is not a theoretical risk for this backend —
Docling's first parse downloads its layout and table-structure models and can run
for minutes — so a single long-lived HTTP call would be reaped almost every time
on a cold node. Each poll re-arms the guard.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Default HTTP port. Core pins the real (profile-shifted) port at spawn via
# RYU_DOCLING_PORT — under the dev profile every port is +1000 (so 9095), which is
# why this constant is only the standalone/bare-`python -m` fallback.
DEFAULT_PORT = 8095

# Backend id as it appears in `document.parse` provider binding + /capability.
BACKEND = "docling"
