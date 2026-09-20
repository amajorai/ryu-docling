"""Library and system-dependency detection.

The hard rule in this module: **nothing here may construct a
`DocumentConverter`.** `/health` gates the sidecar's liveness and `/capability` is
probed on every composer mount with a 2-second budget; instantiating a converter
resolves (and on a cold node downloads) the layout and table-structure models, so
a converter built on the health path would make first boot look like a dead
sidecar for several minutes. Availability is answered from package metadata only.

The second job is honesty about native tools. Docling's *default* pipeline needs
none — its OCR engine (EasyOCR) is pure pip and its models come from the hub — so
unlike the Unstructured backend there is nothing here that must be installed by
hand for the common case. `tesseract` is reported because a caller can select it
as an alternative OCR engine, but it is only counted as *missing* when it is
actually required, i.e. when that engine is requested. Reporting an optional tool
as a missing dependency would push a warning into Core's chip for a document that
parses perfectly.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class SystemDep:
    """One native dependency: how to detect it and how to install it."""

    key: str
    # Any one of these on PATH satisfies the dependency.
    binaries: tuple[str, ...]
    purpose: str
    brew: str
    apt: str
    # False when the default pipeline works without it. Optional deps never land
    # in `missing_dependencies` unless a request explicitly asks for them.
    required_by_default: bool = False

    def present(self) -> bool:
        return any(shutil.which(binary) for binary in self.binaries)

    def describe(self) -> dict[str, object]:
        return {
            "key": self.key,
            "present": self.present(),
            "optional": not self.required_by_default,
            "purpose": self.purpose,
            "install": {"brew": self.brew, "apt": self.apt},
        }

    def message(self) -> str:
        return (
            f"{self.key} is not installed — {self.purpose}. "
            f"Install it with `{self.brew}` (macOS) or `{self.apt}` (Debian/Ubuntu)."
        )


TESSERACT = SystemDep(
    key="tesseract",
    binaries=("tesseract",),
    purpose=(
        "the optional `tesseract`/`tesserocr` OCR engines; Docling's default OCR "
        "engine (EasyOCR) needs no native binary"
    ),
    brew="brew install tesseract tesseract-lang",
    apt="apt-get install -y tesseract-ocr",
)

ALL_DEPS: tuple[SystemDep, ...] = (TESSERACT,)

# OCR engines a caller may name in `options.ocr_engine`, and the native tool each
# one needs. `easyocr`, `rapidocr` and `ocrmac` require nothing off PATH.
OCR_ENGINE_DEPS: dict[str, tuple[SystemDep, ...]] = {
    "tesseract": (TESSERACT,),
    "tesserocr": (TESSERACT,),
    "easyocr": (),
    "rapidocr": (),
    "ocrmac": (),
}


def missing_for_ocr_engine(engine: str | None) -> list[SystemDep]:
    """Native tools absent for an explicitly requested OCR engine."""
    if not engine:
        return []
    return [dep for dep in OCR_ENGINE_DEPS.get(engine.lower(), ()) if not dep.present()]


def docling_version() -> str | None:
    """Installed `docling` version, or None when the library is absent.

    Metadata only — `importlib.metadata` reads the dist-info on disk and never
    executes the package, so this stays cheap enough for `/health`.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version as pkg_version
    except Exception:
        return None
    try:
        return pkg_version("docling")
    except PackageNotFoundError:
        return None
    except Exception:
        return None


def docling_core_version() -> str | None:
    """Installed `docling-core` version — the document model the export comes from."""
    try:
        from importlib.metadata import PackageNotFoundError, version as pkg_version
    except Exception:
        return None
    try:
        return pkg_version("docling-core")
    except PackageNotFoundError:
        return None
    except Exception:
        return None


def snapshot() -> dict[str, object]:
    """Everything a caller needs to explain why a parse will or will not work."""
    version = docling_version()
    deps = [dep.describe() for dep in ALL_DEPS]
    return {
        "backend": "docling",
        "library_available": version is not None,
        "library_version": version,
        "docling_core_version": docling_core_version(),
        "system_dependencies": deps,
        # Only genuinely required tools. Docling's default pipeline requires none,
        # so this is normally empty even on a bare machine — an optional tool
        # reported as missing would read as a broken install.
        "missing_system_dependencies": [
            dep.key for dep in ALL_DEPS if dep.required_by_default and not dep.present()
        ],
    }
