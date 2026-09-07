"""Turn an accepted file into what the model can read.

Every reader here is synchronous and CPU-bound; the service runs them in a
worker thread. Each returns an ``Extracted`` describing either text with its
provenance markers (page, sheet, table) or a content block that hands the
bytes to a multimodal model — images, and PDFs that have no text layer.
"""

import base64
import csv
import io
from dataclasses import (
    dataclass,
    field,
)
from typing import (
    Any,
    Dict,
    List,
    Optional,
)

import docx
from docx.opc.exceptions import PackageNotFoundError
from openpyxl import load_workbook
from PIL import (
    Image,
    UnidentifiedImageError,
)
from pypdf import (
    PdfReader,
    PdfWriter,
)
from pypdf.errors import PdfReadError

from app.core.config import settings
from app.services.attachments.detect import (
    Detection,
    Unsupported,
    decode_text,
)


@dataclass
class Extracted:
    """What was read from a file.

    Attributes:
        text: Extracted text with provenance markers; empty for visual content.
        extraction: How it was read: pypdf, pages, docx, openpyxl, csv, text or image.
        summary: Short descriptor for the person and the model, e.g. "PDF, 12 pages".
        page_count: Pages (PDF) or sheets (XLSX), when meaningful.
        block: OpenAI-style content block carrying the bytes, for visual content.
        visual: Whether ``block`` is set.
        notes: User-facing notes about caps that applied.
    """

    text: str = ""
    extraction: str = "none"
    summary: str = ""
    page_count: Optional[int] = None
    block: Optional[Dict[str, Any]] = None
    visual: bool = False
    notes: List[str] = field(default_factory=list)


def _data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _pdf(name: str, data: bytes) -> Extracted:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                unlocked = bool(reader.decrypt(""))
            except Exception:
                unlocked = False
            if not unlocked:
                raise Unsupported("it is password-protected")
        total = len(reader.pages)
    except Unsupported:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, RecursionError) as e:
        raise Unsupported("it could not be opened as a PDF") from e

    if total == 0:
        raise Unsupported("it has no pages")

    cap = settings.FILE_INPUT_MAX_PDF_PAGES
    read = min(total, cap)
    notes: List[str] = []
    if total > cap:
        notes.append(f"**{name}**: only the first {cap} of {total} pages were read.")

    parts: List[str] = []
    chars = 0
    for index in range(read):
        try:
            page_text = (reader.pages[index].extract_text() or "").strip()
        except Exception:
            page_text = ""
        chars += len(page_text)
        parts.append(f"[page {index + 1}]\n{page_text}")

    if chars / read < settings.FILE_INPUT_SCANNED_PDF_MIN_CHARS_PER_PAGE:
        # No usable text layer: a scan, or a drawing. Hand the pages to the
        # model as a document so it can read them itself.
        payload = data
        if total > cap:
            writer = PdfWriter()
            for index in range(read):
                writer.add_page(reader.pages[index])
            buffer = io.BytesIO()
            writer.write(buffer)
            payload = buffer.getvalue()
        return Extracted(
            extraction="pages",
            summary=f"PDF, {total} page{'s' if total != 1 else ''}, no text layer — read as scanned pages",
            page_count=total,
            block={"type": "file", "file": {"filename": name, "file_data": _data_uri("application/pdf", payload)}},
            visual=True,
            notes=notes,
        )

    return Extracted(
        text="\n\n".join(parts),
        extraction="pypdf",
        summary=f"PDF, {total} page{'s' if total != 1 else ''}",
        page_count=total,
        notes=notes,
    )


def _docx(data: bytes) -> Extracted:
    try:
        document = docx.Document(io.BytesIO(data))
    except (PackageNotFoundError, KeyError, ValueError) as e:
        raise Unsupported("it could not be opened as a Word document") from e

    lines: List[str] = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for number, table in enumerate(document.tables, start=1):
        lines.append(f"[table {number}]")
        for row in table.rows:
            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    text = "\n".join(lines)
    return Extracted(
        text=text,
        extraction="docx",
        summary=f"Word document, {len(document.paragraphs)} paragraphs, {len(document.tables)} tables",
    )


def _xlsx(name: str, data: bytes) -> Extracted:
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise Unsupported("it could not be opened as an Excel workbook") from e

    cap = settings.FILE_INPUT_MAX_SHEET_ROWS
    lines: List[str] = []
    notes: List[str] = []
    sheets = list(workbook.worksheets)
    for sheet in sheets:
        lines.append(f"[sheet {sheet.title}]")
        count = 0
        for row in sheet.iter_rows(values_only=True):
            count += 1
            if count > cap:
                notes.append(f"**{name}**, sheet {sheet.title}: only the first {cap} rows were read.")
                break
            lines.append(", ".join("" if value is None else str(value) for value in row))
    workbook.close()
    return Extracted(
        text="\n".join(lines),
        extraction="openpyxl",
        summary=f"Excel workbook, {len(sheets)} sheet{'s' if len(sheets) != 1 else ''}",
        page_count=len(sheets),
        notes=notes,
    )


def _csv(data: bytes) -> Extracted:
    text = decode_text(data)
    try:
        rows = sum(1 for _ in csv.reader(io.StringIO(text)))
    except csv.Error:
        rows = text.count("\n") + 1
    return Extracted(text=text, extraction="csv", summary=f"CSV, {rows} rows")


def _text(kind: str, data: bytes) -> Extracted:
    text = decode_text(data)
    label = "Markdown" if kind == "markdown" else "text"
    return Extracted(text=text, extraction="text", summary=f"{label}, {len(text):,} characters")


def _image(detection: Detection, data: bytes) -> Extracted:
    # The pixel ceiling is enforced below from the header, before decoding,
    # so Pillow's own bomb heuristic is not what protects us.
    Image.MAX_IMAGE_PIXELS = None
    try:
        image = Image.open(io.BytesIO(data))
        width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise Unsupported("it could not be decoded as an image") from e

    limit = settings.FILE_INPUT_MAX_IMAGE_PIXELS
    if width * height > limit:
        raise Unsupported(f"it is {width}×{height} pixels; the limit is {limit:,} pixels")

    try:
        image.load()
    except (OSError, ValueError) as e:
        raise Unsupported("it could not be decoded as an image") from e

    source_format = (image.format or detection.extension).upper()
    edge = settings.FILE_INPUT_MAX_IMAGE_EDGE
    if max(width, height) <= edge:
        payload, mime = data, detection.mime
    else:
        # Scale down before encoding: a phone photo is far larger than any
        # model needs, and every byte here is sent on every call this turn.
        image.thumbnail((edge, edge))
        buffer = io.BytesIO()
        if source_format == "JPEG" and image.mode == "RGB":
            image.save(buffer, format="JPEG", quality=85)
            mime = "image/jpeg"
        else:
            if image.mode not in ("RGB", "RGBA", "L", "LA"):
                image = image.convert("RGBA")
            image.save(buffer, format="PNG", optimize=True)
            mime = "image/png"
        payload = buffer.getvalue()

    return Extracted(
        extraction="image",
        summary=f"{source_format} image, {width}×{height}",
        block={"type": "image_url", "image_url": {"url": _data_uri(mime, payload)}},
        visual=True,
    )


def extract(detection: Detection, name: str, data: bytes) -> Extracted:
    """Read a file of a known kind.

    Args:
        detection: What the file is.
        name: The uploaded name, used in provenance and notes.
        data: The file's bytes.

    Returns:
        Extracted: Text with provenance, or a content block for visual content.

    Raises:
        Unsupported: When the file cannot be read after all (corrupt, encrypted, oversize image).
    """
    if detection.kind == "pdf":
        return _pdf(name, data)
    if detection.kind == "docx":
        return _docx(data)
    if detection.kind == "xlsx":
        return _xlsx(name, data)
    if detection.kind == "csv":
        return _csv(data)
    if detection.kind in ("text", "markdown"):
        return _text(detection.kind, data)
    if detection.kind == "image":
        return _image(detection, data)
    raise Unsupported(f"{detection.kind} files are not supported")
