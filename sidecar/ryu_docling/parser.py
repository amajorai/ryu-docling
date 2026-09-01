"""The parse itself: a file path in, a `document.parse` result out.

Everything that can go wrong here is reported as a typed error the job carries,
never as an exception that kills the worker or an empty result that looks like a
blank document:

  * `library_missing`      — `docling` was never installed
  * `missing_dependency`   — a native binary the requested OCR engine needs is absent
  * `unsupported_format`   — the installed Docling has no pipeline for this extension
  * `parse_failed`         — the converter raised, or reported a failed conversion
  * `input_rejected`       — path confinement / archive safety refused the input

The converter is built **lazily, inside the worker thread, and cached**. Docling
resolves and downloads its layout and table-structure models when a converter is
constructed, so building one eagerly (at import, on `/health`, on `/capability`)
would stall the liveness probe for minutes on a cold node and read as a dead
sidecar. Caching means only the first parse pays; every later job reuses the
loaded weights.
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from . import BACKEND
from .deps import docling_version, missing_for_ocr_engine
from .limits import MAX_ELEMENT_CHARS, MAX_ELEMENTS, MAX_OUTPUT_BYTES
from .paths import InputError, is_archive, safe_extract

# OCR engines a caller may name. Anything else is ignored rather than rejected —
# `options` is a hint bag and an unknown key must never fail a parse (§3.4).
_OCR_ENGINES = ("easyocr", "tesseract", "tesserocr", "rapidocr", "ocrmac")


class ParseError(RuntimeError):
    """A parse failure with a machine-readable code and a human-readable fix."""

    def __init__(self, code: str, message: str, *, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.missing = missing or []


def _truncate(text: str, budget: int) -> tuple[str, bool]:
    """Clip to a byte budget on a character boundary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text, True
    if budget <= 0:
        return "", not encoded
    clipped = encoded[:budget].decode("utf-8", errors="ignore")
    return clipped, False


def _fit_elements(
    elements: list[dict[str, Any]], budget: int
) -> tuple[list[dict[str, Any]], bool]:
    """Keep as many element records as the remaining byte budget allows.

    Returns `(kept, whole)`. The size is measured on the serialised record because
    that is what actually rides the response — an element list that fits in memory
    but not through the ext-proxy's 10 MiB body cap would make the *whole* job
    snapshot unreadable, taking the markdown down with it.
    """
    if budget <= 0:
        return [], not elements
    kept: list[dict[str, Any]] = []
    used = 0
    for record in elements:
        used += len(json.dumps(record, default=str).encode("utf-8")) + 1
        if used > budget:
            return kept, False
        kept.append(record)
    return kept, True


# --------------------------------------------------------------------------- #
# Converter construction and cache
# --------------------------------------------------------------------------- #

_CONVERTERS: dict[str, Any] = {}
_CONVERTER_LOCK = threading.Lock()


def _normalise_options(options: dict[str, Any]) -> dict[str, Any]:
    """Take only the hints we understand, in a canonical form.

    Unknown keys are dropped silently: a hint one backend understands must not
    fail on another, and the five `document.parse` backends do not share an
    options vocabulary.
    """
    clean: dict[str, Any] = {}
    if isinstance(options.get("ocr"), bool):
        clean["ocr"] = options["ocr"]
    engine = options.get("ocr_engine")
    if isinstance(engine, str) and engine.lower() in _OCR_ENGINES:
        clean["ocr_engine"] = engine.lower()
    languages = options.get("ocr_languages")
    if isinstance(languages, list) and all(isinstance(lang, str) for lang in languages):
        clean["ocr_languages"] = [lang for lang in languages if lang]
    if isinstance(options.get("table_structure"), bool):
        clean["table_structure"] = options["table_structure"]
    return clean


def _build_pdf_pipeline_options(clean: dict[str, Any], warnings: list[str]) -> Optional[Any]:
    """Translate our hints into Docling's PDF pipeline options, or None.

    Returns None when there is nothing to configure. Every failure here is a
    *warning*, not an error: Docling's pipeline-options surface moves between
    minor versions, and a caller asking for OCR on a version whose option names
    have shifted should still get their document back from the default pipeline
    rather than a 500.
    """
    if not clean:
        return None
    try:
        from docling.datamodel.pipeline_options import PdfPipelineOptions
    except Exception as exc:  # noqa: BLE001 — version drift, not a parse failure
        warnings.append(
            f"parse options ignored: this Docling version does not expose "
            f"PdfPipelineOptions ({type(exc).__name__}: {exc})"
        )
        return None

    try:
        pipeline = PdfPipelineOptions()
        if "ocr" in clean:
            pipeline.do_ocr = clean["ocr"]
        if "table_structure" in clean:
            pipeline.do_table_structure = clean["table_structure"]
        languages = clean.get("ocr_languages")
        if languages and getattr(pipeline, "ocr_options", None) is not None:
            # Not every engine's options object carries `lang`; asking for a
            # language an engine cannot express is a warning, not a failure.
            if hasattr(pipeline.ocr_options, "lang"):
                pipeline.ocr_options.lang = languages
            else:
                warnings.append(
                    "ocr_languages ignored: the active OCR engine takes no language list"
                )
        return pipeline
    except Exception as exc:  # noqa: BLE001
        warnings.append(
            f"parse options ignored: {type(exc).__name__}: {exc} — falling back to "
            "Docling's default pipeline"
        )
        return None


def _new_converter(clean: dict[str, Any], warnings: list[str]) -> Any:
    """Construct a DocumentConverter. Slow on first call: models load here."""
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise ParseError(
            "library_missing",
            "the `docling` library is not installed in this sidecar's venv — "
            'install it with `pip install "docling"` (or reinstall this app, which '
            "installs the `parse` extra)",
        ) from exc

    pipeline = _build_pdf_pipeline_options(clean, warnings)
    if pipeline is None:
        # The plain form, verbatim from Docling's own README. Kept as the fast
        # path precisely because it is the shape least likely to break across
        # versions.
        return DocumentConverter()

    try:
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import PdfFormatOption

        return DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline)}
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(
            f"parse options ignored: could not attach them to the converter "
            f"({type(exc).__name__}: {exc})"
        )
        return DocumentConverter()


def _converter_for(clean: dict[str, Any], warnings: list[str]) -> Any:
    """Cached converter for one option set.

    The cache is keyed by the canonical option JSON so the common case (no
    options at all) loads the models exactly once for the life of the process.
    """
    key = json.dumps(clean, sort_keys=True)
    with _CONVERTER_LOCK:
        cached = _CONVERTERS.get(key)
        if cached is not None:
            return cached
    # Built outside the lock: construction can take minutes on a cold node and
    # holding the lock would serialise every worker behind the first download.
    built = _new_converter(clean, warnings)
    with _CONVERTER_LOCK:
        return _CONVERTERS.setdefault(key, built)


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #


def _preflight(path: Path, clean: dict[str, Any]) -> None:
    """Refuse a parse whose explicitly requested OCR engine has no binary."""
    missing = missing_for_ocr_engine(clean.get("ocr_engine"))
    if missing:
        raise ParseError(
            "missing_dependency",
            " ".join(dep.message() for dep in missing),
            missing=[dep.key for dep in missing],
        )


def _document_of(result: Any, path: Path) -> Any:
    """Pull the DoclingDocument out of a conversion result, or explain why not."""
    document = getattr(result, "document", None)
    if document is None:
        status = getattr(result, "status", None)
        raise ParseError(
            "parse_failed",
            f"Docling returned no document for `{path.name}` (status={status})",
        )
    status = getattr(result, "status", None)
    name = getattr(status, "name", None) or str(status or "")
    if name.upper() in ("FAILURE", "SKIPPED"):
        detail = ""
        errors = getattr(result, "errors", None) or []
        if errors:
            detail = f": {'; '.join(str(item) for item in errors[:4])}"
        raise ParseError("parse_failed", f"Docling failed to convert `{path.name}`{detail}")
    return document


def _status_warnings(result: Any) -> list[str]:
    """Non-fatal degradations Docling reported alongside a usable document."""
    warnings: list[str] = []
    status = getattr(result, "status", None)
    name = (getattr(status, "name", None) or "").upper()
    if name == "PARTIAL_SUCCESS":
        warnings.append(
            "Docling reported a partial conversion — some pages or elements were skipped"
        )
    for item in (getattr(result, "errors", None) or [])[:4]:
        warnings.append(f"docling: {item}")
    return warnings


def _export_markdown(document: Any, path: Path) -> str:
    try:
        return document.export_to_markdown() or ""
    except Exception as exc:  # noqa: BLE001
        raise ParseError(
            "parse_failed",
            f"Docling parsed `{path.name}` but could not export markdown: "
            f"{type(exc).__name__}: {exc}",
        ) from exc


def _export_text(document: Any, markdown: str) -> str:
    """Markup-free rendering, falling back to the markdown when unavailable."""
    exporter = getattr(document, "export_to_text", None)
    if callable(exporter):
        try:
            text = exporter() or ""
        except Exception:  # noqa: BLE001 — a missing plain-text export is not fatal
            return markdown
        return text or markdown
    return markdown


def _elements(document: Any) -> list[dict[str, Any]]:
    """A bounded, JSON-safe view of the document's structural items.

    Optional per the contract (`result.elements`), and bounded independently of
    the markdown budget: the item stream is a second full copy of the text, and
    the markdown is the payload callers actually consume.
    """
    if MAX_ELEMENTS <= 0:
        return []
    iterate = getattr(document, "iterate_items", None)
    if not callable(iterate):
        return []
    records: list[dict[str, Any]] = []
    try:
        for entry in iterate():
            item = entry[0] if isinstance(entry, tuple) and entry else entry
            level = entry[1] if isinstance(entry, tuple) and len(entry) > 1 else None
            text = str(getattr(item, "text", "") or "")
            if not text.strip():
                continue
            label = getattr(item, "label", None)
            page: Any = None
            provenance = getattr(item, "prov", None) or []
            if provenance:
                page = getattr(provenance[0], "page_no", None)
            records.append(
                {
                    "id": str(getattr(item, "self_ref", "") or ""),
                    "category": str(getattr(label, "value", None) or label or ""),
                    "text": text[:MAX_ELEMENT_CHARS],
                    "page_number": page if isinstance(page, int) else None,
                    "level": level if isinstance(level, int) else None,
                }
            )
            if len(records) >= MAX_ELEMENTS:
                break
    except Exception:  # noqa: BLE001 — detail is optional, the markdown is not
        return records
    return records


def _page_count(document: Any) -> Optional[int]:
    pages = getattr(document, "pages", None)
    try:
        count = len(pages) if pages is not None else 0
    except TypeError:
        return None
    return count or None


def _convert_one(path: Path, clean: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]], list[str], Optional[int]]:
    """Convert one file. Returns (markdown, text, elements, warnings, page_count)."""
    warnings: list[str] = []
    _preflight(path, clean)
    converter = _converter_for(clean, warnings)
    try:
        result = converter.convert(str(path))
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        # Docling raises ConversionError/ValueError for a format it has no
        # pipeline for; that is a different action for the caller (pick another
        # backend) than a genuine parse crash, so it gets its own code.
        lowered = f"{name}: {exc}".lower()
        if any(marker in lowered for marker in ("unsupported", "not supported", "no pipeline")):
            raise ParseError(
                "unsupported_format",
                f"this Docling build has no pipeline for `{path.suffix or path.name}`: "
                f"{name}: {exc}",
            ) from exc
        raise ParseError(
            "parse_failed", f"parsing `{path.name}` failed: {name}: {exc}"
        ) from exc

    document = _document_of(result, path)
    warnings.extend(_status_warnings(result))
    markdown = _export_markdown(document, path)
    text = _export_text(document, markdown)
    return markdown, text, _elements(document), warnings, _page_count(document)


def parse_file(path: Path, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse one file (or one archive of files) into a `document.parse` result.

    The shape is the contract's: `markdown` is the primary payload, `text` the
    markup-free fallback, `elements` optional backend-specific detail, and
    `truncated` says whether the byte budget clipped the output.
    """
    clean = _normalise_options(options or {})
    if is_archive(path):
        markdown, text, elements, warnings, pages, sources = _parse_archive(path, clean)
    else:
        markdown, text, elements, warnings, pages = _convert_one(path, clean)
        sources = [path.name]

    # One shared budget, spent in contract order: `markdown` is the field Core
    # reads, `text` is the markup-free fallback, `elements` is optional detail.
    # Whatever is left after each takes its share is what the next may use, so the
    # whole snapshot stays under the `max_output_bytes` /capability advertises —
    # and therefore under the proxy's 10 MiB body cap.
    markdown, whole_md = _truncate(markdown, MAX_OUTPUT_BYTES)
    remaining = MAX_OUTPUT_BYTES - len(markdown.encode("utf-8"))
    text, whole_text = _truncate(text, remaining)
    remaining -= len(text.encode("utf-8"))
    elements, whole_elements = _fit_elements(elements, remaining)
    return {
        "backend": BACKEND,
        "backend_version": docling_version(),
        "markdown": markdown,
        "text": text,
        "elements": elements,
        "warnings": warnings,
        "truncated": not (whole_md and whole_text and whole_elements),
        "metadata": {
            "filename": path.name,
            "element_count": len(elements),
            "page_count": pages,
            "sources": sources,
        },
    }


def _parse_archive(
    path: Path, clean: dict[str, Any]
) -> tuple[str, str, list[dict[str, Any]], list[str], Optional[int], list[str]]:
    """Expand an archive into a scratch dir and parse every member we can read.

    One unreadable member must not sink the whole archive, so per-member failures
    become warnings and the rest of the documents still come back. Each member's
    markdown is nested under a heading carrying its path so an archive does not
    render as a flat run of `#` headings with no document boundaries.
    """
    md_blocks: list[str] = []
    text_blocks: list[str] = []
    elements: list[dict[str, Any]] = []
    warnings: list[str] = []
    sources: list[str] = []
    pages = 0
    with tempfile.TemporaryDirectory(prefix="ryu-docling-") as scratch:
        root = Path(scratch).resolve()
        try:
            members = safe_extract(path, root)
        except InputError as exc:
            raise ParseError("input_rejected", str(exc)) from exc
        for member in sorted(members):
            relative = str(member.relative_to(root))
            try:
                md, text, member_elements, member_warnings, member_pages = _convert_one(
                    member, clean
                )
            except ParseError as exc:
                warnings.append(f"{relative}: {exc}")
                continue
            if not md.strip():
                continue
            sources.append(relative)
            warnings.extend(f"{relative}: {warning}" for warning in member_warnings)
            pages += member_pages or 0
            md_blocks.append(f"# {relative}\n\n{md}")
            text_blocks.append(f"{relative}\n\n{text}")
            for record in member_elements:
                record["source"] = relative
            # The element budget is global across the archive, not per member.
            remaining = MAX_ELEMENTS - len(elements)
            if remaining > 0:
                elements.extend(member_elements[:remaining])
    return (
        "\n\n".join(md_blocks),
        "\n\n".join(text_blocks),
        elements,
        warnings,
        pages or None,
        sources,
    )
