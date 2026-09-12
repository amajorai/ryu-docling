"""Formats this backend claims.

Kept as data rather than derived from the library at import time on purpose: the
list must be answerable by `/capability` even when `docling` is not installed yet,
which is exactly when a user is deciding whether to install it. Deriving it would
also mean importing docling on the `/capability` path — see `deps.py` for why that
is forbidden.

Three rules govern what may be on this list, and all three are honesty rules:

* **Core unions it into the composer's picker `accept`.** Advertising a format
  Docling cannot dispatch widens the file picker to files that can only fail.
* **Every entry was checked against Docling's own extension table**
  (`docling.datamodel.base_models.FormatToExtensions`) on 2.116.0 — which is why
  `.msg` and `.markdown` are absent despite reading like obvious members: Docling
  handles `.eml` but not Outlook `.msg`, and knows `.md` but not the long spelling.
  Guessing an extension into this list is how a picker starts offering files that
  bounce.
* **Audio and video are deliberately absent** even though Docling 2.x dispatches
  them. Its ASR pipeline needs a dependency set this sidecar does not install or
  wire up, and Core classifies `audio/*` / `video/*` as terminal `unsupported` at
  Spaces insert time anyway. Claiming them would put media in the picker and get
  nothing back.

The list is deliberately narrower than Docling's full table (no `.docm`/`.dotx`
family, no legacy binary `.doc`/`.ppt`/`.xls`): those dispatch on 2.116.0 but not
across the whole `>=2.0` range this package allows, and the Unstructured backend
is the one that owns legacy Office. Under-claiming costs a picker entry;
over-claiming costs a failed parse.

A format on this list that the *installed* Docling version does not know still
terminates in a typed `unsupported_format` job error naming the file — never a
silent empty document.
"""

from __future__ import annotations

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Portable documents — the flagship path: layout model + table structure.
        ".pdf",
        # Office (OOXML)
        ".docx",
        ".pptx",
        ".xlsx",
        # OpenDocument
        ".odt",
        ".odp",
        ".ods",
        # Markup and plain text
        ".html",
        ".htm",
        ".xhtml",
        ".md",
        ".adoc",
        ".asciidoc",
        ".txt",
        ".text",
        ".csv",
        ".xml",
        # Typesetting
        ".tex",
        # Ebooks
        ".epub",
        # Email — `.eml` only; Docling has no Outlook `.msg` reader.
        ".eml",
        # Images (OCR)
        ".png",
        ".jpg",
        ".jpeg",
        ".tiff",
        ".tif",
        ".bmp",
        ".webp",
        # Archives we expand and parse member-by-member
        ".zip",
        ".tar",
        ".tar.gz",
        ".tgz",
        ".tar.bz2",
        ".tbz2",
        ".tar.xz",
        ".txz",
    }
)
