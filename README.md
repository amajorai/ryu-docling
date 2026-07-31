# ryu-docling

Docling for Ryu — document parsing via IBM's MIT-licensed Docling: the highest-fidelity `document.parse` backend, running real layout analysis rather than handing a model a flat character stream.

> **The public home of `ryu-docling`.** Source, builds, and releases live here —
> binaries for every platform are attached to each release.
>
> This tree is generated from the Ryu monorepo, so commits pushed here
> directly are replaced on the next sync. **Pull requests are welcome** —
> open them here and they are ported into the monorepo, then flow back out.
> Ryu as a whole: https://github.com/amajorai/ryu

## Source & build

The **source of record** for the universal Ryu TTS sidecar — a self-contained
Python HTTP front over several text-to-speech engines. Install its
dependencies (`pip install -r sidecar/requirements.txt`) and run
`python -m ryu_tts` from `sidecar/`; Core manages it as a sidecar in a
full Ryu install.

## License

Apache-2.0 — see [LICENSE](./LICENSE).

---

# Docling — `document.parse` backend

Turns documents into markdown using IBM's MIT-licensed
[`docling`](https://github.com/docling-project/docling). This is one of several
interchangeable backends behind the swappable `document.parse` capability: enable
it, pick it in the provider selector, and everything that ingests a document
(Spaces, RAG, chat attachments) routes through it. Nothing in Core is bound to it
— the swap is manifest data.

## What it is good at

**Layout, not just characters.** A PDF has no reading order; it has glyphs at
coordinates. Every other approach to "extract the text" guesses at the order from
those coordinates, and a two-column paper, a sidebar, or a footnote block is where
the guess fails — the extracted text interleaves the columns and every chunk built
from it is nonsense that still looks like prose. Docling runs an actual
page-segmentation model, recovers the reading order, and exports from a structured
document model. That is the single reason to pay its install cost.

**Tables that survive chunking.** A dedicated table-structure model (TableFormer)
reconstructs merged cells and spanning headers, and the markdown export emits a
real markdown table. A financial statement or a results table stays queryable
instead of collapsing into a column of loose numbers.

**Fully local, and air-gappable.** Every model runs on your machine; no document
or page image leaves the node. Upstream explicitly supports offline operation once
the weights are cached — see [Air-gapped nodes](#air-gapped-nodes).

## What it costs

Be honest with yourself about this before picking it over `markitdown`.

| | |
| --- | --- |
| **Install size** | multi-GB. `docling` pulls `torch` plus the docling model wrappers. |
| **Model weights** | *not* in the wheel. The **first parse downloads them** (hundreds of MB) and can take minutes. |
| **Warm parse speed** | seconds to minutes per document — this is model inference, not a string copy. Slower than every other backend here. |
| **Memory** | expect ~2–4 GB resident while a PDF is converting; two concurrent parses is the default cap for that reason. |
| **CPU/GPU** | CPU works. Apple Silicon and CUDA are used when `torch` finds them. A scanned 300-page PDF on CPU is a genuinely long job. |
| **Python** | **3.10 or newer.** Core does not install Python; it builds a venv from whatever `python3` the host already has. |
| **Native tools** | **none required.** Unlike the Unstructured backend there is no poppler / libreoffice / pandoc to install by hand. |

**Pick something else when:** you mostly attach `.docx`, `.md` and `.html`
(`markitdown` is a small pure-Python install and gets the same answer in
milliseconds); you need legacy binary Office formats `.doc` / `.ppt` / `.xls` or
`.msg` email (`unstructured`); or the node is a small VPS where a multi-GB
dependency tree and a few GB of RAM per parse are not available.

**Pick this one when:** the corpus is PDFs that matter — papers, filings,
manuals, contracts — and getting the reading order and the tables right is worth
minutes per document.

## System dependencies

None for the default pipeline. `/capability` reports `tesseract` because a caller
may select it as an alternative OCR engine, but it is **not** counted as a missing
dependency: Docling's default OCR engine needs no binary on `PATH`, so reporting
tesseract as "missing" on a machine where every document parses fine would be a
lie the UI would repeat.

If you do want Tesseract OCR:

```sh
brew install tesseract tesseract-lang          # macOS
apt-get install -y tesseract-ocr               # Debian/Ubuntu
pip install -e ".[tesseract]"                  # in the sidecar venv
```

then submit `{"options": {"ocr_engine": "tesseract"}}`. Asking for an engine whose
binary is absent fails the job with `error_code: "missing_dependency"` and names
the install command, rather than returning an empty document.

## Where its models and caches go

Core points the sidecar at profile-scoped directories so nothing lands in your
home cache and everything is removed with the app:

| Env (set by `manifest.json`) | Holds |
| --- | --- |
| `HF_HOME` = `${RYU_DIR}/models/hf` | layout + TableFormer weights, pulled from the Hugging Face hub |
| `TORCH_HOME` = `${RYU_DIR}/models/torch` | torch's own model cache |
| `DOCLING_CACHE_DIR` = `${RYU_DIR}/cache/docling/docling` | Docling's working cache |
| `RYU_DOCLING_WORKDIR` = `${RYU_DIR}/cache/docling` | inline uploads and expanded archives |

`${RYU_DIR}` is the only token the manifest interpolates, and Core resolves it from
its profile-aware data dir — so a `bun dev` node writes to `~/.ryu-dev`, not to the
release node's directory.

One caveat worth knowing: some optional OCR engines (RapidOCR in particular)
download their own weights into the **venv's `site-packages`**, not into any of the
directories above. Those go away when the app's venv is removed, but they are not
shared with anything else on the machine.

### Air-gapped nodes

Docling honours `DOCLING_ARTIFACTS_PATH`, which makes it load models from a local
directory *only* — no hub access at all. It is deliberately **not** set in the
manifest, because pointing it at an empty directory makes the first parse fail
instead of downloading. To use it, pre-fetch the weights on a connected machine
and then set it yourself:

```sh
docling-tools models download --output-dir /path/to/models
export DOCLING_ARTIFACTS_PATH=/path/to/models
```

## The contract it implements

Everything in `docs/document-parsing.md` §3, unchanged. Summary:

| Path | Method | Auth | Meaning |
| --- | --- | --- | --- |
| `/health` | GET | **exempt** | liveness + backend identity |
| `/capability` | GET | required | formats, limits, dependency state |
| `/parse` | POST | required | submit → `202 { job_id, status }`, immediately |
| `/jobs` | GET | required | listing, results omitted |
| `/jobs/{job_id}` | GET | required | poll |
| `/jobs/{job_id}` | DELETE | required | cooperative cancel |

Submit-then-poll is not optional here. The ext-proxy's activity guard drops when
response headers arrive, so a `lazy` sidecar is killable mid-request — and this
backend's cold first parse is exactly the multi-minute call that would be reaped.
Each poll re-arms the guard.

Two consequences of the contract you can see in the code:

- **`/health` and `/capability` never build a `DocumentConverter`.** Constructing
  one resolves and downloads models. Core probes `/capability` on every composer
  mount with a 2-second budget and reads `/health` as liveness, so a converter on
  either path would make a cold node look like a dead sidecar for minutes.
  Availability is answered from package metadata alone (`deps.py`), and the
  converter is built lazily inside the worker thread and cached, so only the first
  parse pays.
- **`available: false` is a real, supported state.** `docling` is an optional extra
  (`pip install -e ".[parse]"`), so the process boots and answers honestly on a
  machine where the install has not finished. Core's builtin floor handles `.txt` /
  `.md` / `.csv` without any provider, which is why a half-finished multi-GB
  install must not make plain text unreadable.

### Submit

```jsonc
// The primary form — Core resolves a blob to an absolute path; this sidecar opens
// it directly. A 200 MiB Space file cannot be inlined: the proxy caps a forwarded
// body at 10 MiB and base64 costs a third more on top.
{ "path": "/Users/x/.ryu/blobs/ab/abcd…", "blob_sha256": "abcd…",
  "filename": "Q3 report.pdf", "mime": "application/pdf", "options": {} }

// The inline form, for chat attachments Core already holds in memory.
{ "content_base64": "JVBERi0…", "filename": "Q3 report.pdf", "options": {} }
```

`options` this backend understands — every other key is **ignored, never an
error**, because a hint one backend takes must not fail on another:

| Key | Type | Effect |
| --- | --- | --- |
| `ocr` | bool | run OCR over page images |
| `ocr_engine` | `easyocr` \| `tesseract` \| `tesserocr` \| `rapidocr` \| `ocrmac` | which engine |
| `ocr_languages` | `string[]` | language hints, when the engine takes them |
| `table_structure` | bool | run the table-structure model |

Docling's pipeline-options surface moves between minor versions, so a request whose
options cannot be attached falls back to the **default** pipeline and reports a
`warnings` entry — you get your document, plus a note that the hint was dropped.
Losing a hint is recoverable; losing the document is not.

## Limits

Every one is env-overridable, and `/capability` reports the live values.

| Bound | Default | Env |
| --- | --- | --- |
| max input bytes | 200 MiB | `RYU_DOCLING_MAX_INPUT_BYTES` |
| max output bytes | 8 MiB | `RYU_DOCLING_MAX_OUTPUT_BYTES` |
| elements echoed in a result | 2000 | `RYU_DOCLING_MAX_ELEMENTS` |
| per-parse timeout | 600 s | `RYU_DOCLING_TIMEOUT_SECS` |
| concurrent parses | 2 | `RYU_DOCLING_MAX_WORKERS` |
| retained jobs | 64 | `RYU_DOCLING_MAX_JOBS` |
| archive members | 512 | `RYU_DOCLING_MAX_ARCHIVE_MEMBERS` |
| archive expanded bytes | 512 MiB | `RYU_DOCLING_MAX_ARCHIVE_BYTES` |

**`max_output_bytes` is one shared budget**, spent in contract order across
`markdown`, then `text`, then `elements` — not 8 MiB each. Three independent
8 MiB fields would let a job snapshot exceed the ext-proxy's 10 MiB body cap and
become *unreadable*, which is the exact failure the output bound exists to
prevent, while `/capability` reported 8 MiB. A result clipped by the budget is
flagged `truncated: true`, never dropped.

The 600 s timeout is the contract's, deliberately not raised for this backend. A
first parse that includes a model download can exceed it; when that happens the job
fails with `error_code: "timeout"` and an error naming both the variable and the
download, so a stalled fetch shows up as a stalled fetch instead of hiding inside a
longer silence. Retry once the models are cached and the same document is usually
seconds.

Timeout honesty: CPython cannot kill a running thread. The watchdog marks the job
`failed` at the deadline and stops waiting; the worker may run on and its result is
discarded. The job never hangs.

## Security

The floor from `docs/document-parsing.md` §5, in full.

- **Fail-closed `RYU_EXT_TOKEN`**, read once at module level and compared with
  `hmac.compare_digest`. No token configured means every request is rejected.
- **`/health` is exempt on `GET` only** — the predicate is `path == "/health" and
  method == "GET"`, so the route cannot become an unauthenticated hole if it ever
  grows a body.
- **Resolve, then contain.** A submitted `path` is resolved through symlinks
  *first*, then required to sit under a root from `RYU_DOCLING_ROOTS` (Core sets it
  to `${RYU_DIR}`; the fallback chain is `RYU_DOCLING_ROOTS` → `RYU_DIR` → `~/.ryu`,
  in that order, so a dev profile lands correctly). Without the post-resolution
  check a symlink planted in the blob directory would read `/etc/shadow` and return
  it as "document text". An empty allow-list means *nothing*.
- **Archive members are refused, not sanitised.** Absolute names, `..` segments,
  and symlink/hardlink/device members are rejected; the concrete destination is
  re-checked after joining; member count and total expanded bytes are bounded.
- **Only the extension is taken from a caller-supplied `filename`.** Inline uploads
  are written with `mkstemp` into a directory this sidecar owns.
- **No URL is ever fetched.** This backend reads local paths and inline bytes.

## Layout

```
manifest.json                     id, provides block, sidecar port 8095
sidecar/pyproject.toml            base server deps; `parse` extra pulls docling
sidecar/ryu_docling/__main__.py   uvicorn on RYU_DOCLING_PORT, 127.0.0.1
sidecar/ryu_docling/server.py     routes + fail-closed bearer middleware
sidecar/ryu_docling/jobs.py       in-process job table, worker pool, watchdog
sidecar/ryu_docling/parser.py     lazy cached converter, markdown export
sidecar/ryu_docling/paths.py      path containment + safe archive expansion
sidecar/ryu_docling/deps.py       availability from metadata — never builds a converter
sidecar/ryu_docling/formats.py    what /capability advertises
sidecar/ryu_docling/limits.py     the bounds above
sidecar/smoke_test.py             the contract, with or without docling installed
```

## Running it standalone

```sh
cd sidecar
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[parse]"          # omit [parse] to exercise the unavailable path
RYU_EXT_TOKEN=dev RYU_DOCLING_ROOTS="$HOME/.ryu" python -m ryu_docling
```

Smoke test (needs `httpx` for `fastapi.testclient`):

```sh
cd sidecar && python smoke_test.py
```

It passes in both modes and prints which one it ran in — with `docling` installed
it performs a real parse; without it, it asserts the submission still lands on a
clean `library_missing` job error rather than an empty document.
