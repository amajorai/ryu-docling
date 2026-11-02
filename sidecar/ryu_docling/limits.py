"""Resource limits for one parse, fixed in code.

These are the *contract* floors, not tuning knobs: Core sizes its own timeouts
against them, and the ext-proxy caps a forwarded body at 10 MiB independently, so
the output cap here must stay below that or a large result becomes unreadable
rather than truncated. They are not read from the environment: every node
runs the same bounds, and /capability reports them.
"""

from __future__ import annotations

_MIB = 1024 * 1024

# Largest input file (or archive) we will open. Inputs arrive as a path to a
# Core-owned blob, not as an upload, so this is a disk-read cap rather than a
# request-body cap. 200 MiB is the Space-file ceiling.
MAX_INPUT_BYTES = 200 * _MIB

# Largest payload a job result may carry, counted across `markdown` + `text` +
# `elements` together. Beyond this the result is truncated and flagged
# (`truncated: true`) rather than dropped: a clipped document is useful, a 500 is
# not.
#
# One shared budget rather than one per field, and that is the whole point: the
# ext-proxy caps a forwarded body at 10 MiB, so three fields each allowed 8 MiB
# would let a job snapshot exceed the cap and become *unreadable* — the exact
# failure the contract's output bound exists to prevent — while /capability
# cheerfully reported 8 MiB. The number this module publishes has to bound what
# the response can actually be. `markdown` claims the budget first because it is
# the field the contract is about.
MAX_OUTPUT_BYTES = 8 * _MIB

# Wall-clock ceiling for one parse. Held at the contract's 600s rather than raised
# for Docling's slower pipeline: the first parse on a cold node also downloads
# models and can legitimately blow through this, which is why the timeout error
# says to retry once the models are cached and the README says so out loud. Raising the default would
# hide a stalled download behind ten quiet minutes.
TIMEOUT_SECS = 600

# Concurrent parses. Docling runs layout and table-structure models per document
# and each converter holds its own weights in memory, so a small number is the
# honest default — two concurrent hi-res PDFs already saturate a laptop.
MAX_WORKERS = 2

# Members we will expand out of one archive, and total expanded bytes — a zip
# bomb is otherwise a trivial local DoS.
MAX_ARCHIVE_MEMBERS = 512
MAX_ARCHIVE_BYTES = 512 * _MIB

# Finished jobs kept in the table before the oldest are evicted. The result of a
# parse is large; an unbounded table is a slow memory leak in a process that is
# meant to idle-stop.
MAX_JOBS = 64

# Structural items echoed back in `result.elements`. These are *count* caps that
# keep the walk cheap; the bytes those items may occupy are governed by whatever
# is left of MAX_OUTPUT_BYTES after markdown and text have taken their share, so
# optional detail can never crowd the contract's payload out of the response.
MAX_ELEMENTS = 2000
MAX_ELEMENT_CHARS = 2048
