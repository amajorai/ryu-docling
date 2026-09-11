"""Ryu Docling sidecar — HTTP front for the `document.parse` capability.

The contract Core drives (every path below is declared in `manifest.json`; an
undeclared path 404s at the ext-proxy before it ever reaches this process):

    GET    /health          -> { ok, backend, available, missing_dependencies }
    GET    /capability      -> { backend, formats, limits, system_dependencies }
    POST   /parse           -> 202 { job_id, status }        (never blocks)
    GET    /jobs            -> { jobs: [ JobSnapshot ] }      (no results)
    GET    /jobs/{job_id}   -> JobSnapshot                    (result when done)
    DELETE /jobs/{job_id}   -> JobSnapshot                    (cooperative cancel)

Submit-then-poll is not a style choice: the ext-proxy's activity guard drops when
response headers arrive, so a single long-lived parse request on a `lazy` +
`idle_stop_secs` sidecar can be reaped mid-flight. Polling re-arms the guard, and
Docling's first parse — model download plus layout inference — is exactly the kind
of multi-minute call that would otherwise be reaped every time.

Neither route on this module's hot paths (`/health`, `/capability`) constructs a
`DocumentConverter`; see `deps.py` for why that matters.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import BACKEND, __version__
from .deps import snapshot
from .formats import SUPPORTED_EXTENSIONS
from .jobs import STORE
from .limits import (
    MAX_INPUT_BYTES,
    MAX_JOBS,
    MAX_OUTPUT_BYTES,
    MAX_WORKERS,
    TIMEOUT_SECS,
)
from .paths import InputError, resolve_input

app = FastAPI(title="Ryu Docling Sidecar", version=__version__)

# Shared-secret bearer Core stamps on every proxied hop and injects at spawn
# (`RYU_EXT_TOKEN`). FAIL-CLOSED for every route except GET /health: no token
# configured => reject all. Without it, any local process (or any web page that
# can reach loopback) could hand this sidecar a path and read the file back as
# "parsed text" — an arbitrary-file-read primitive.
_EXPECTED_TOKEN = (os.environ.get("RYU_EXT_TOKEN") or "").strip()


@app.middleware("http")
async def _require_ext_token(request: Request, call_next):
    # GET only: a POST to /health must not become an unauthenticated hole if the
    # route ever grows a body.
    if request.url.path == "/health" and request.method == "GET":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    presented = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""
    if not (_EXPECTED_TOKEN and hmac.compare_digest(presented, _EXPECTED_TOKEN)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


class ParseRequest(BaseModel):
    path: Optional[str] = Field(
        None,
        description="Absolute path to the document, confined to RYU_DOCLING_ROOTS.",
    )
    blob_sha256: Optional[str] = Field(
        None,
        description="Content address of the blob, for integrity/caching. Never used "
        "to construct a path — Core resolves the address, this sidecar only opens "
        "what it is handed.",
    )
    content_base64: Optional[str] = Field(
        None, description="Inline document bytes, for callers with no shared filesystem."
    )
    filename: Optional[str] = Field(
        None, description="Name (extension matters) for `content_base64` input."
    )
    mime: Optional[str] = Field(None, description="Advisory; the extension wins.")
    size_bytes: Optional[int] = Field(None, description="Advisory; the file is re-stat'd.")
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Backend hints: ocr (bool), ocr_engine "
        "(easyocr|tesseract|tesserocr|rapidocr|ocrmac), ocr_languages, "
        "table_structure (bool). Unknown keys are ignored, never an error — a "
        "hint one backend understands must not fail on another.",
    )


def _limits() -> dict[str, Any]:
    return {
        "max_input_bytes": MAX_INPUT_BYTES,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "timeout_secs": TIMEOUT_SECS,
        "max_workers": MAX_WORKERS,
        "max_jobs": MAX_JOBS,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    probe = snapshot()
    return {
        "ok": True,
        "version": __version__,
        "backend": BACKEND,
        # `available` is the honest answer to "can this parse anything right
        # now": the library must be importable. Answered from package metadata,
        # never by building a converter — that would download models on the
        # liveness path and read as a dead sidecar.
        "available": bool(probe["library_available"]),
        "library_version": probe["library_version"],
        "missing_dependencies": probe["missing_system_dependencies"],
    }


@app.get("/capability")
def capability() -> dict[str, Any]:
    probe = snapshot()
    return {
        "capability": "document.parse",
        "backend": BACKEND,
        "version": __version__,
        "available": bool(probe["library_available"]),
        "library_version": probe["library_version"],
        "docling_core_version": probe["docling_core_version"],
        "formats": sorted(SUPPORTED_EXTENSIONS),
        "system_dependencies": probe["system_dependencies"],
        "missing_dependencies": probe["missing_system_dependencies"],
        "limits": _limits(),
    }


def _staging_dir() -> Path:
    """Where inline uploads land. Core points this at `${RYU_DIR}/cache/...`."""
    configured = (os.environ.get("RYU_DOCLING_WORKDIR") or "").strip()
    root = Path(configured).expanduser() if configured else Path(tempfile.gettempdir())
    staging = root / "uploads"
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def _materialise_inline(req: ParseRequest) -> Path:
    """Write `content_base64` to a scratch file we own, returning its path."""
    try:
        raw = base64.b64decode(req.content_base64 or "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InputError(f"`content_base64` is not valid base64: {exc}") from exc
    if not raw:
        raise InputError("`content_base64` decoded to zero bytes")
    if len(raw) > MAX_INPUT_BYTES:
        raise InputError(
            f"inline input is {len(raw)} bytes, over the {MAX_INPUT_BYTES}-byte limit"
        )
    # Only the extension is taken from the caller's filename — the rest of the
    # name is ours, so a crafted `filename` cannot steer the write anywhere.
    suffix = Path((req.filename or "document.txt").replace("\\", "/")).suffix[:16]
    handle, tmp_path = tempfile.mkstemp(suffix=suffix or ".txt", dir=str(_staging_dir()))
    with os.fdopen(handle, "wb") as dst:
        dst.write(raw)
    return Path(tmp_path)


@app.post("/parse")
def parse(req: ParseRequest) -> JSONResponse:
    if bool(req.path) == bool(req.content_base64):
        return JSONResponse(
            {"error": "provide exactly one of `path` or `content_base64`"},
            status_code=400,
        )
    try:
        target = _materialise_inline(req) if req.content_base64 else resolve_input(req.path or "")
    except InputError as exc:
        return JSONResponse(
            {"error": str(exc), "error_code": "input_rejected"}, status_code=400
        )

    job = STORE.submit(target, req.options or {})
    # 202: the parse has been accepted, not performed. The caller polls
    # /jobs/{job_id} — see the module docstring for why this may not be one
    # long request.
    return JSONResponse({"job_id": job.id, "status": job.status}, status_code=202)


@app.get("/jobs")
def list_jobs() -> dict[str, Any]:
    # Results are omitted here on purpose: a listing that inlined every parsed
    # document would be megabytes and would blow the proxy's body cap.
    return {"jobs": [job.snapshot(include_result=False) for job in STORE.list()]}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    job = STORE.get(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse(job.snapshot())


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str) -> JSONResponse:
    job = STORE.cancel(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse(job.snapshot(include_result=False))
