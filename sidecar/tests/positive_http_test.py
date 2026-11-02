"""Positive PDF regression against a separately started, real Docling sidecar.

No converter doubles, optional-library success branch, or fixture-generation
dependencies. Run one fixture per owned server process; see the app README.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

FIXTURES = {
    "layout": {
        "file": "layout.pdf",
        "sha256": "a6f5083ea7b43909a711d5da00a0a37a7e98b364f28d8c87897b25ecd61538fb",
        "options": {"ocr": False, "table_structure": True},
    },
    "scanned": {
        "file": "scanned.pdf",
        "sha256": "5861392c388d6877de15d20dbd8fa0e85e6d56efb9a45f2f707cfd176e4a3d56",
        "options": {
            "ocr": True,
            "ocr_engine": "rapidocr",
            "ocr_languages": ["en"],
            "table_structure": False,
        },
    },
}
PACKAGES = (
    "docling", "docling-core", "torch", "transformers", "rapidocr",
    "docling-ibm-models", "pypdfium2", "fastapi", "uvicorn",
)


def require(condition: bool, message: str) -> None:
    # Keep checks active even if somebody invokes Python with -O.
    if not condition:
        raise AssertionError(message)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A local fixture must not hand its admission token to a redirected host.
        return None


def run(args: argparse.Namespace) -> None:
    token = os.environ.get("RYU_EXT_TOKEN")
    require(bool(token), "RYU_EXT_TOKEN must match the separately started sidecar")
    base = urlsplit(args.base_url)
    require(base.scheme == "http" and base.hostname in {"127.0.0.1", "localhost", "::1"}
            and base.port is not None and base.username is None and base.password is None
            and base.path in {"", "/"} and not base.query and not base.fragment,
            "Positive suite requires a direct loopback HTTP origin with an explicit port")
    opener = build_opener(ProxyHandler({}), NoRedirects())
    output = args.output_dir.resolve()
    # Never replace a previously completed or interrupted proof.
    output.mkdir(parents=True, exist_ok=False)
    calls: list[dict] = []

    def save(name: str, value: object) -> None:
        (output / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def http(method: str, path: str, payload: dict | None = None,
             authenticated: bool = True) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {token}"
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(args.base_url.rstrip("/") + path, data=data,
                          headers=headers, method=method)
        call = {"method": method, "path": path, "authenticated": authenticated,
                "body_sha256": hashlib.sha256(data).hexdigest() if data is not None else None}
        calls.append(call)
        try:
            try:
                response = opener.open(request, timeout=10)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                call["http_status"] = status
                require(not 300 <= status < 400, "Sidecar fixture must not redirect requests")
                body = json.loads(response.read())
            require(isinstance(body, dict), f"{method} {path} returned a non-object envelope")
        except (URLError, OSError, ValueError, AssertionError) as error:
            call["failure"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            save("http-calls.json", calls)
        return status, body

    versions: dict[str, str | None] = {}
    for package in PACKAGES:
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = None
    save("client-environment-versions.json", versions)
    status, health = http("GET", "/health", authenticated=False)
    save("health.json", health)
    require(status == 200 and health.get("backend") == "docling", "Real Docling health required")
    status, capability = http("GET", "/capability")
    save("capability.json", capability)
    require(status == 200 and capability.get("backend") == "docling" and capability.get("available") is True,
            "Positive suite requires installed Docling; library_missing is not success")
    status, _ = http("GET", "/capability", authenticated=False)
    require(status == 401, "Anonymous capability must be refused")

    fixture = FIXTURES[args.fixture]
    source = Path(__file__).parent / "fixtures" / fixture["file"]
    document = source.read_bytes()
    require(hashlib.sha256(document).hexdigest() == fixture["sha256"], "Fixed PDF fixture changed")
    require(document.startswith(b"%PDF-"), "Fixture must be an actual PDF")
    save("fixture.json", {"name": fixture["file"], "sha256": fixture["sha256"],
                          "bytes": len(document), "options": fixture["options"]})
    payload = {"filename": fixture["file"],
               "content_base64": base64.b64encode(document).decode(),
               "options": fixture["options"]}
    status, submission = http("POST", "/parse", payload)
    save("submission.json", {"http_status": status, "body": submission})
    require(status == 202 and isinstance(submission.get("job_id"), str) and bool(submission["job_id"]),
            "PDF must be accepted as a real asynchronous job")
    job_id = submission["job_id"]
    job_path = "/jobs/" + quote(job_id, safe="")
    started = time.monotonic()
    polls: list[dict] = []
    job: dict = {}
    while time.monotonic() - started < args.timeout:
        status, job = http("GET", job_path)
        require(status == 200 and job.get("job_id") == job_id, "Job readback identity/status mismatch")
        polls.append({"elapsed": round(time.monotonic() - started, 3), "status": job.get("status")})
        save("polls.json", polls)
        save("terminal-or-latest-job.json", job)
        require(job.get("status") in {"queued", "running", "succeeded", "failed", "cancelled"},
                "Unknown job lifecycle state")
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(1)
    require(job.get("status") == "succeeded",
            f"Real PDF conversion did not succeed: {job.get('status')} / {job.get('error_code')} / {job.get('error')}")
    result = job.get("result")
    require(isinstance(result, dict), "Succeeded PDF must contain its result")
    save("result.json", result)
    require(result.get("backend") == "docling" and result.get("truncated") is False,
            "Genuine, untruncated Docling output required")
    require(result.get("metadata", {}).get("page_count") == 1, "One-page PDF metadata required")
    markdown = result.get("markdown")
    require(isinstance(markdown, str) and "Ryu Document Parsing" in markdown and "12" in markdown,
            "PDF content must survive conversion")
    (output / "output.md").write_text(markdown, encoding="utf-8")
    warnings = result.get("warnings")
    require(isinstance(warnings, list) and not warnings,
            f"Positive fixture must not silently fall back, skip content or ignore options: {warnings}")
    if args.fixture == "layout":
        table_rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
                      for line in markdown.splitlines() if line.strip().startswith("|")]
        require(["Region", "Total"] in table_rows and ["EU", "12"] in table_rows,
                "Text PDF must retain its actual Markdown table columns and associated row values")
    else:
        require("scanned local conversion proof" in " ".join(markdown.lower().split()),
                "Image-only PDF must contain the text recovered by requested RapidOCR")
    status, repeated = http("GET", job_path)
    save("repeated-readback.json", repeated)
    require(status == 200 and repeated.get("job_id") == job_id and repeated.get("status") == "succeeded" and repeated.get("result") == result,
            "Repeated terminal readback must retain the exact result")
    status, listing = http("GET", "/jobs")
    save("listing.json", listing)
    jobs = listing.get("jobs")
    require(status == 200 and isinstance(jobs, list), "Job listing envelope required")
    require(all(isinstance(row, dict) for row in jobs), "Job listing rows must be objects")
    require(any(row.get("job_id") == job_id for row in jobs), "Submitted job must remain listed")
    require(all("result" in row and row["result"] is None for row in jobs),
            "Listing must omit every document result")
    save("completed.json", {"fixture": args.fixture, "job_id": job_id, "status": "succeeded",
                            "assertions": "content, options, table/OCR, terminal readback, result-free listing"})
    print(f"DOCLING_POSITIVE_HTTP_OK fixture={args.fixture} job_id={job_id}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, choices=FIXTURES)
    parser.add_argument("--base-url", default="http://127.0.0.1:" + os.environ.get("RYU_DOCLING_PORT", "19495"))
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=620)
    options = parser.parse_args()
    require(options.timeout > 0, "Timeout must be positive")
    run(options)
