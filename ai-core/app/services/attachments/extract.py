"""Turn an accepted file into what the model can read.

Every reader here is synchronous and CPU-bound; the service runs them in a
worker thread. Each returns an ``Extracted`` describing text with its
provenance markers (sheet, table) or a content block that hands the bytes to
a multimodal model (images). A PDF is different: it is not read whole here.
Its native text is assessed per page and stored, a short opening excerpt is
shown to the model, and everything else — including rendering and
transcribing pages without a text layer — happens on demand through the PDF
tools, page by page.
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

from app.core.i18n import t
from app.services.attachments.detect import (
    Detection,
    Unsupported,
    decode_text,
)
from app.services.documents import policy
from app.services.documents.pdf import (
    PageText,
    PdfError,
    extract_text,
    has_labels,
    metadata,
    open_pdf,
    outline,
    page_count,
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
        pages: Per-page native text (PDF), persisted by the caller.
        metadata: Kind-specific facts (PDF title, contents, coverage).
    """

    text: str = ""
    extraction: str = "none"
    summary: str = ""
    page_count: Optional[int] = None
    block: Optional[Dict[str, Any]] = None
    visual: bool = False
    notes: List[str] = field(default_factory=list)
    pages: List[PageText] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


def _data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _pdf(name: str, data: bytes) -> Extracted:
    try:
        pdf = open_pdf(data)
    except PdfError as e:
        raise Unsupported("reason.encrypted" if e.kind == "encrypted" else "reason.damaged_pdf") from e
    try:
        total = page_count(pdf)
        if total == 0:
            raise Unsupported("reason.no_pages")
        facts: Dict[str, Any] = {
            "page_count": total,
            **metadata(pdf),
            "toc": outline(pdf, policy.PDF.toc_entries),
            "labels": has_labels(pdf),
        }
        pages = extract_text(pdf, range(1, min(total, policy.PDF.intake_native_pages) + 1))
    finally:
        pdf.close()

    with_text = [page for page in pages if page.usable]
    facts["text_pages"] = len(with_text)
    facts["assessed_pages"] = len(pages)

    # The opening pages, so a short document answers without a tool call.
    # Everything past this is reached page by page through the PDF tools.
    inline: List[str] = []
    budget = policy.PDF.intake_inline_chars
    for page in pages[: policy.PDF.intake_inline_pages]:
        if not page.usable or budget <= 0:
            continue
        piece = page.text[:budget]
        budget -= len(piece)
        inline.append(f"[page {page.page_no}]\n{piece}")

    if len(pages) == total:
        coverage = "no text layer" if not with_text else f"{len(with_text)} of {total} pages with a text layer"
    else:
        coverage = f"text layer assessed for the first {len(pages)} pages"
    summary = f"PDF, {total} page{'s' if total != 1 else ''}, {coverage}"
    return Extracted(
        text="\n\n".join(inline),
        extraction="pdf",
        summary=summary,
        page_count=total,
        pages=pages,
        metadata=facts,
    )


def _docx(data: bytes) -> Extracted:
    try:
        document = docx.Document(io.BytesIO(data))
    except (PackageNotFoundError, KeyError, ValueError) as e:
        raise Unsupported("reason.docx") from e

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
        raise Unsupported("reason.xlsx") from e

    cap = policy.FILE_INPUT.max_sheet_rows
    lines: List[str] = []
    notes: List[str] = []
    sheets = list(workbook.worksheets)
    for sheet in sheets:
        lines.append(f"[sheet {sheet.title}]")
        count = 0
        for row in sheet.iter_rows(values_only=True):
            count += 1
            if count > cap:
                notes.append(t("notice.sheet_rows", name=name, sheet=sheet.title, cap=cap))
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
        raise Unsupported("reason.image_decode") from e

    limit = policy.FILE_INPUT.max_image_pixels
    if width * height > limit:
        raise Unsupported("reason.image_too_big", width=width, height=height, limit=limit)

    try:
        image.load()
    except (OSError, ValueError) as e:
        raise Unsupported("reason.image_decode") from e

    source_format = (image.format or detection.extension).upper()
    edge = policy.FILE_INPUT.max_image_edge
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
    raise Unsupported("reason.kind_unsupported", kind=detection.kind)
