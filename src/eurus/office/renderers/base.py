from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

SOFFICE_TIMEOUT_SECONDS = 180.0
PDF_CONVERSION_TIMEOUT_SECONDS = 240.0
PDF_PAGES_TIMEOUT_SECONDS = 180.0
ARTIFACT_MIN_BYTES = 64
ZIP_MAGIC = b"PK\x03\x04"


@dataclass(frozen=True, slots=True)
class RenderCapabilities:
    soffice: bool
    pdftoppm: bool
    playwright: bool
    chromium: bool

    @property
    def office_to_pdf(self) -> bool:
        return self.soffice

    @property
    def pdf_to_images(self) -> bool:
        return self.soffice and self.pdftoppm

    @property
    def html_raster(self) -> bool:
        return self.playwright and self.chromium

    def missing(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.soffice:
            missing.append("LibreOffice (soffice)")
        if not self.pdftoppm:
            missing.append("poppler-utils (pdftoppm)")
        if not self.playwright:
            missing.append("playwright")
        if not self.chromium:
            missing.append("Chromium browser for playwright")
        return tuple(missing)


def _which(binary: str) -> bool:
    return shutil.which(binary) is not None


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


def detect_capabilities(*, probe_chromium: bool = False) -> RenderCapabilities:
    capabilities = RenderCapabilities(
        soffice=_which("soffice"),
        pdftoppm=_which("pdftoppm"),
        playwright=_chromium_importable(),
        chromium=False,
    )
    if probe_chromium and capabilities.playwright:
        capabilities = RenderCapabilities(
            soffice=capabilities.soffice,
            pdftoppm=capabilities.pdftoppm,
            playwright=True,
            chromium=_chromium_available(),
        )
    return capabilities


def _chromium_importable() -> bool:
    try:
        from importlib.util import find_spec

        return find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


async def _run_doomed_process(
    process: asyncio.subprocess.Process, timeout: float
) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        _terminate_process(process)
        await process.wait()
        raise
    except TimeoutError:
        _terminate_process(process)
        await process.wait()
        raise


def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.kill()


async def convert_to_pdf(source: Path, output_dir: Path, *, timeout: float | None = None) -> Path:
    if not _which("soffice"):
        raise RuntimeError("LibreOffice (soffice) is not available on this system")
    limit = PDF_CONVERSION_TIMEOUT_SECONDS if timeout is None else float(timeout)
    if limit <= 0:
        raise RuntimeError(f"pdf conversion timeout must be positive, got {limit:g}s")
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "soffice",
        "--headless",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(source),
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await _run_doomed_process(process, limit)
    except TimeoutError:
        raise RuntimeError(f"LibreOffice conversion timed out after {limit:g}s") from None
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[:500]
        raise RuntimeError(f"LibreOffice conversion failed: {detail or process.returncode}")
    pdf_path = output_dir / f"{source.stem}.pdf"
    if not pdf_path.is_file():
        raise RuntimeError("LibreOffice reported success but the PDF file was not created")
    return pdf_path


async def pdf_to_images(
    pdf_path: Path,
    output_dir: Path,
    *,
    dpi: int = 90,
    limit: int = 40,
    timeout: float | None = None,
) -> list[Path]:
    if not _which("pdftoppm"):
        raise RuntimeError("pdftoppm is not available on this system")
    time_limit = PDF_PAGES_TIMEOUT_SECONDS if timeout is None else float(timeout)
    if time_limit <= 0:
        raise RuntimeError(f"pdf page rendering timeout must be positive, got {time_limit:g}s")
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = output_dir / "page"
    command = [
        "pdftoppm",
        "-r",
        str(dpi),
        "-jpeg",
        "-jpegopt",
        "quality=70",
        str(pdf_path),
        str(prefix),
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await _run_doomed_process(process, time_limit)
    except TimeoutError:
        raise RuntimeError(f"pdftoppm rendering timed out after {time_limit:g}s") from None
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[:500]
        raise RuntimeError(f"pdftoppm failed: {detail or process.returncode}")
    images = sorted(output_dir.glob("page-*.jpg"))
    if not images:
        images = sorted(output_dir.glob("page*.jpg"))
    if not images:
        raise RuntimeError("pdftoppm produced no page images")
    return images[:limit]


def rasterize_html_component(
    html_document: str,
    output_path: Path,
    *,
    width_px: int,
    height_px: int,
    scale: float = 2.0,
) -> Path:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("playwright is not installed; raster rendering is unavailable") from exc
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                viewport={"width": width_px, "height": height_px},
                device_scale_factor=scale,
            )
            page.set_content(html_document, wait_until="networkidle")
            page.screenshot(
                path=str(output_path), clip={"x": 0, "y": 0, "width": width_px, "height": height_px}
            )
        finally:
            browser.close()
    if not output_path.is_file():
        raise RuntimeError("component rasterization produced no image")
    return output_path


def safe_remove(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _opener_problem(fmt: str, path: Path) -> str | None:
    try:
        if fmt == "docx":
            Document = import_module("docx").Document
            Document(str(path))
        elif fmt == "xlsx":
            load_workbook = import_module("openpyxl").load_workbook
            workbook = load_workbook(str(path), read_only=True)
            workbook.close()
        elif fmt == "pptx":
            Presentation = import_module("pptx").Presentation
            Presentation(str(path))
        else:
            return f"unknown office format: {fmt}"
    except ImportError:
        return None
    except Exception as exc:
        return f"{fmt} archive failed to open: {type(exc).__name__}: {exc}"[:300]
    return None


def verify_artifact_file(path: Path, fmt: str) -> tuple[bool, str]:
    try:
        if not path.is_file():
            return False, "file does not exist"
        size = path.stat().st_size
        if size < ARTIFACT_MIN_BYTES:
            return False, f"file is only {size} bytes"
        with path.open("rb") as handle:
            magic = handle.read(4)
        if magic != ZIP_MAGIC:
            return False, "file is not a ZIP-based office document"
        problem = _opener_problem(fmt, path)
        if problem is not None:
            return False, problem
        return True, f"{fmt} archive verified ({size} bytes)"
    except OSError as exc:
        return False, f"file could not be read: {exc}"


def run_soffice_version() -> str:
    try:
        result = subprocess.run(
            ["soffice", "--version"],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()[:200]
