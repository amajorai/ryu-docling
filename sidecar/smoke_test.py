"""Smoke test: the contract holds with or without Docling installed.

Covers the acceptance criteria that need no model download:
  - an unauthenticated request is rejected (fail-closed bearer gate)
  - GET /health is open, POST /health is NOT
  - /health and /capability answer *fast* and without building a converter
  - POST /parse returns 202 + a job_id immediately, and the job reaches a
    terminal state (succeeded with docling installed, `library_missing`
    without it — either way, never a hang and never a crash)
  - path confinement rejects `..`, absolute paths outside the roots, and
    symlinks pointing out of the allowed roots
  - archive expansion rejects traversal and absolute members

With `docling` installed the parse is a real one, on a real fixture — but note
that the *first* such run downloads models and may take minutes; the timeout here
is generous for that reason. Without it, the same submission must still land on a
clean typed job error rather than an empty document. The test prints which mode it
ran in.
"""

from __future__ import annotations

import base64
import os
import tempfile
import time
import zipfile
from pathlib import Path

# The server reads RYU_EXT_TOKEN at import time for its fail-closed auth gate;
# set it before importing `app` and present it as the bearer on every request.
os.environ.setdefault("RYU_EXT_TOKEN", "smoke-token")

# Confine parse inputs to this run's scratch dir so the confinement test has a
# real boundary to cross. Must also be set before the modules read it.
_SCRATCH = Path(tempfile.mkdtemp(prefix="ryu-docling-smoke-"))
_ROOT = _SCRATCH / "root"
_ROOT.mkdir()
os.environ["RYU_DOCLING_ROOTS"] = str(_ROOT)
os.environ["RYU_DOCLING_WORKDIR"] = str(_SCRATCH / "work")

from fastapi.testclient import TestClient  # noqa: E402

from ryu_docling.paths import InputError, allowed_roots, safe_extract  # noqa: E402
from ryu_docling.server import app  # noqa: E402

TOKEN = os.environ["RYU_EXT_TOKEN"]
client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
anon = TestClient(app)

TERMINAL = {"succeeded", "failed", "cancelled"}

# Generous: a first real parse downloads Docling's layout + table models.
PARSE_TIMEOUT = float(os.environ.get("SMOKE_PARSE_TIMEOUT", "600"))


def _await_terminal(job_id: str, timeout: float = PARSE_TIMEOUT) -> dict:
    deadline = time.time() + timeout
    snap: dict = {}
    while time.time() < deadline:
        snap = client.get(f"/jobs/{job_id}").json()
        if snap["status"] in TERMINAL:
            return snap
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never reached a terminal state: {snap}")


def test_auth_is_fail_closed() -> None:
    assert anon.post("/parse", json={"path": "/x"}).status_code == 401
    assert anon.get("/jobs").status_code == 401
    assert anon.get("/capability").status_code == 401
    assert anon.delete("/jobs/parse_nope").status_code == 401
    # /health is exempt on GET only.
    assert anon.get("/health").status_code == 200
    assert anon.post("/health").status_code == 401
    print("auth: unauthenticated rejected, GET /health open, POST /health closed")


def test_probes_are_cheap() -> None:
    """The liveness and picker probes must not build a converter.

    Core gives /capability a 2-second budget on every composer mount. If either
    route ever instantiates `DocumentConverter`, this assertion fails on a cold
    node instead of the sidecar silently reading as dead.
    """
    started = time.time()
    assert anon.get("/health").status_code == 200
    assert client.get("/capability").status_code == 200
    elapsed = time.time() - started
    assert elapsed < 2.0, f"/health + /capability took {elapsed:.1f}s — did a converter load?"
    print(f"probes: /health + /capability answered in {elapsed * 1000:.0f}ms, no converter built")


def test_capability_answers_without_the_library() -> dict:
    cap = client.get("/capability").json()
    assert cap["capability"] == "document.parse", cap
    assert cap["backend"] == "docling", cap
    assert ".pdf" in cap["formats"] and ".docx" in cap["formats"], cap
    # Media is deliberately absent — Core unions this into the picker's accept
    # list and nothing here parses audio or video.
    assert not any(ext in cap["formats"] for ext in (".mp3", ".wav", ".mp4")), cap
    # So is `.msg`: Docling reads `.eml` but has no Outlook reader, and an
    # advertised extension that bounces is a picker entry that can only fail.
    assert ".msg" not in cap["formats"], cap
    assert ".eml" in cap["formats"], cap
    assert cap["limits"]["timeout_secs"] > 0, cap
    # Must stay under the ext-proxy's 10 MiB forwarded-body cap, or a large
    # result is unreadable rather than truncated.
    assert cap["limits"]["max_output_bytes"] < 10 * 1024 * 1024, cap
    print(
        f"capability: available={cap['available']} "
        f"library={cap['library_version']} formats={len(cap['formats'])}"
    )
    return cap


def test_roots_prefer_ryu_dir_over_home() -> None:
    """RYU_DOCLING_ROOTS > RYU_DIR > ~/.ryu — the middle step is the dev profile."""
    saved_roots = os.environ.pop("RYU_DOCLING_ROOTS")
    os.environ["RYU_DIR"] = str(_SCRATCH / "profile")
    try:
        assert allowed_roots() == [(_SCRATCH / "profile").resolve()], allowed_roots()
    finally:
        os.environ.pop("RYU_DIR", None)
        os.environ["RYU_DOCLING_ROOTS"] = saved_roots
    assert allowed_roots() == [_ROOT.resolve()], allowed_roots()
    print("roots: RYU_DIR honoured before ~/.ryu (dev profile lands correctly)")


def test_parse_roundtrip(available: bool) -> None:
    fixture = _ROOT / "hello.md"
    fixture.write_text(
        "# Ryu Document Parsing\n\nDocling reconstructs layout, tables and reading "
        "order.\n\n| Region | Total |\n| --- | --- |\n| EU | 12 |\n",
        encoding="utf-8",
    )
    submitted = client.post("/parse", json={"path": str(fixture)})
    assert submitted.status_code == 202, submitted.text
    # `queued` is what the contract documents, but the worker thread starts
    # immediately and may already have flipped the job to `running` by the time
    # the response serialises. Both are members of the status vocabulary and the
    # caller polls either way; asserting the strict literal would make this test
    # fail on a fast machine and pass on a slow one.
    assert submitted.json()["status"] in ("queued", "running"), submitted.text
    job_id = submitted.json()["job_id"]
    assert job_id.startswith("parse_"), submitted.text

    snap = _await_terminal(job_id)
    if available:
        assert snap["status"] == "succeeded", snap
        result = snap["result"]
        assert "Docling" in result["markdown"], result["markdown"][:200]
        assert result["backend"] == "docling", result
        assert result["truncated"] is False, result
        print(
            f"parse: succeeded, {len(result['markdown'])} md chars, "
            f"{result['metadata']['element_count']} elements"
        )
    else:
        assert snap["status"] == "failed", snap
        assert snap["error_code"] == "library_missing", snap
        print(f"parse: library absent, clean job error -> {snap['error'][:80]}...")


def test_inline_parse_accepted() -> None:
    body = base64.b64encode(b"# inline\n\nbody text\n").decode("ascii")
    r = client.post("/parse", json={"content_base64": body, "filename": "note.md"})
    assert r.status_code == 202, r.text
    _await_terminal(r.json()["job_id"])
    print("inline: content_base64 accepted and reaches a terminal state")


def test_unknown_options_are_ignored() -> None:
    """A hint another backend understands must not fail this one."""
    fixture = _ROOT / "opts.md"
    fixture.write_text("# opts\n", encoding="utf-8")
    r = client.post(
        "/parse",
        json={"path": str(fixture), "options": {"strategy": "hi_res", "nonsense": 42}},
    )
    assert r.status_code == 202, r.text
    _await_terminal(r.json()["job_id"])
    print("options: unknown keys ignored rather than rejected")


def test_path_confinement() -> None:
    outside = _SCRATCH / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    rejected = client.post("/parse", json={"path": str(outside)})
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error_code"] == "input_rejected", rejected.text

    traversal = client.post("/parse", json={"path": f"{_ROOT}/../outside.txt"})
    assert traversal.status_code == 400, traversal.text

    link = _ROOT / "link.txt"
    link.symlink_to(outside)
    escaped = client.post("/parse", json={"path": str(link)})
    assert escaped.status_code == 400, escaped.text

    neither = client.post("/parse", json={})
    assert neither.status_code == 400, neither.text

    both = client.post("/parse", json={"path": str(_ROOT / "hello.md"), "content_base64": "eA=="})
    assert both.status_code == 400, both.text
    print("confinement: outside-root, `..`, and escaping symlink all rejected")


def test_archive_traversal_rejected() -> None:
    bomb = _ROOT / "evil.zip"
    with zipfile.ZipFile(bomb, "w") as zf:
        zf.writestr("../escaped.txt", "pwned")
    try:
        safe_extract(bomb, _SCRATCH / "extract")
        raise AssertionError("traversal member was extracted")
    except InputError as exc:
        assert "parent-directory" in str(exc), exc

    absolute = _ROOT / "absolute.zip"
    with zipfile.ZipFile(absolute, "w") as zf:
        zf.writestr("/etc/passwd", "pwned")
    try:
        safe_extract(absolute, _SCRATCH / "extract2")
        raise AssertionError("absolute member was extracted")
    except InputError as exc:
        assert "absolute" in str(exc), exc

    assert not (_SCRATCH / "escaped.txt").exists(), "traversal member escaped to disk"
    print("archive: `..` and absolute members rejected")


def test_output_budget_is_shared() -> None:
    """markdown + text + elements together must fit `max_output_bytes`.

    Three fields each allowed the full budget would let one job snapshot exceed
    the proxy's 10 MiB body cap, which makes the whole response — markdown
    included — unreadable rather than truncated.
    """
    import json as _json

    from ryu_docling.limits import MAX_OUTPUT_BYTES
    from ryu_docling.parser import _fit_elements, _truncate

    md, whole_md = _truncate("x" * (MAX_OUTPUT_BYTES + 1024), MAX_OUTPUT_BYTES)
    assert whole_md is False
    remaining = MAX_OUTPUT_BYTES - len(md.encode("utf-8"))
    text, whole_text = _truncate("y" * 5000, remaining)
    assert (text, whole_text) == ("", False), (len(text), whole_text)
    elements, whole_elements = _fit_elements([{"text": "z" * 100}], remaining)
    assert (elements, whole_elements) == ([], False)

    fat = [{"id": str(n), "text": "z" * 512} for n in range(64)]
    kept, whole = _fit_elements(fat, 4096)
    assert whole is False and 0 < len(kept) < len(fat), (len(kept), whole)
    assert len(_json.dumps(kept).encode("utf-8")) <= 4096 + 256, len(_json.dumps(kept))
    print("budget: markdown/text/elements share one max_output_bytes")


def test_job_listing_and_cancel() -> None:
    listing = client.get("/jobs").json()
    assert isinstance(listing["jobs"], list) and listing["jobs"], listing
    assert all(job["result"] is None for job in listing["jobs"]), listing
    assert client.get("/jobs/parse_missing").status_code == 404
    assert client.delete("/jobs/parse_missing").status_code == 404
    print("jobs: listing omits results, unknown job ids 404")


def main() -> None:
    test_auth_is_fail_closed()
    test_probes_are_cheap()
    cap = test_capability_answers_without_the_library()
    test_roots_prefer_ryu_dir_over_home()
    test_parse_roundtrip(bool(cap["available"]))
    test_inline_parse_accepted()
    test_unknown_options_are_ignored()
    test_path_confinement()
    test_archive_traversal_rejected()
    test_output_budget_is_shared()
    test_job_listing_and_cancel()
    mode = "with docling installed" if cap["available"] else "without docling"
    print(f"\nSMOKE_OK ({mode})")


if __name__ == "__main__":
    main()
