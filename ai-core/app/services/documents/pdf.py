"""The PDF engine: open, inspect, extract native text, render pages.

Built on pypdfium2 (PDFium), which reads the text layer with correct order for
Arabic and Latin scripts alike, renders pages, and exposes the outline and
page labels. PDFium is not thread-safe, so every call into it here runs under
one lock; callers use ``asyncio.to_thread`` around these functions.

Page numbers are physical and 1-based everywhere outside this module; the
printed label ("iv", "A-3") is carried separately and never used to address
a page.
"""

import io
import re
import threading
from collections import OrderedDict
from dataclasses import (
    dataclass,
    field,
)
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
)

import pypdfium2 as pdfium

from app.services.documents import policy

_LOCK = threading.Lock()
_WHITESPACE = re.compile(r"\s+")
_REPLACEMENT = re.compile(r"�|\(cid:\d+\)")

# Some producers (Chrome/Skia among them) store Arabic runs so that PDFium
# returns the words back to front. Common function words make a cheap probe:
# when their mirrored forms outnumber the real ones, the layer reads backwards.
_ARABIC_FUNCTION_WORDS = ("في", "على", "من", "بين", "إلى", "عن", "هذا", "هذه", "التي", "الذي", "مع", "بعد", "قبل")


def _arabic_word_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(word) for word in words)
    return re.compile(rf"(?<![\u0600-\u06FF])(?:{alternatives})(?![\u0600-\u06FF])")


_FORWARD_ARABIC = _arabic_word_pattern(_ARABIC_FUNCTION_WORDS)
_REVERSED_ARABIC = _arabic_word_pattern(tuple(word[::-1] for word in _ARABIC_FUNCTION_WORDS))


class PdfError(Exception):
    """The document cannot be opened.

    Attributes:
        kind: encrypted or damaged.
    """

    def __init__(self, kind: str, message: str) -> None:
        """Create the error.

        Args:
            kind: encrypted or damaged.
            message: PDFium's explanation.
        """
        super().__init__(message)
        self.kind = kind


@dataclass
class PageText:
    """The text layer of one page and whether it can be answered from.

    Attributes:
        page_no: Physical page number, 1-based.
        text: The text as extracted.
        chars: Visible (non-whitespace) characters.
        usable: Whether the text layer is good enough to answer from.
        warnings: Why it is not, when it is not.
        label: Printed page label, when the document defines one.
    """

    page_no: int
    text: str
    chars: int
    usable: bool
    warnings: List[str] = field(default_factory=list)
    label: Optional[str] = None


def open_pdf(data: bytes) -> pdfium.PdfDocument:
    """Open a document from bytes.

    Args:
        data: The file.

    Returns:
        PdfDocument: The open document. Close it when done.

    Raises:
        PdfError: ``encrypted`` when a password is needed, ``damaged`` otherwise.
    """
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        text = str(e).lower()
        raise PdfError("encrypted" if "password" in text else "damaged", str(e)) from e


def assess(text: str) -> tuple[int, bool, List[str]]:
    """Judge a text layer.

    Args:
        text: Text extracted from a page.

    Returns:
        tuple: visible character count, whether usable, and warnings.
    """
    chars = len(_WHITESPACE.sub("", text))
    if chars < policy.PDF.native_min_chars:
        return chars, False, ["no usable text layer on this page"]
    # The share of visible characters that are replacement markers, so a page
    # of "(cid:12)(cid:13)…" counts as fully broken, not one marker per eight.
    bad = sum(len(marker) for marker in _REPLACEMENT.findall(text))
    if bad / max(chars, 1) > policy.PDF.native_max_replacement_ratio:
        return chars, False, ["text layer is unusable (broken font encoding)"]
    mirrored = len(_REVERSED_ARABIC.findall(text))
    if mirrored >= 2 and mirrored > len(_FORWARD_ARABIC.findall(text)):
        return (
            chars,
            False,
            ["Arabic text layer is stored in visual order (words reversed); transcribe the page instead"],
        )
    return chars, True, []


def _label(pdf: pdfium.PdfDocument, index: int) -> Optional[str]:
    getter = getattr(pdf, "get_page_label", None)
    if getter is None:
        return None
    try:
        label = getter(index)
    except Exception:
        return None
    if not label or label == str(index + 1):
        return None
    return str(label)[:64]


def page_count(pdf: pdfium.PdfDocument) -> int:
    """Number of pages.

    Args:
        pdf: An open document.

    Returns:
        int: Page count.
    """
    with _LOCK:
        return len(pdf)


def metadata(pdf: pdfium.PdfDocument) -> Dict[str, Any]:
    """Document information fields worth showing.

    Args:
        pdf: An open document.

    Returns:
        dict: title, author, subject, producer, created — only those present.
    """
    with _LOCK:
        try:
            raw = pdf.get_metadata_dict(skip_empty=True)
        except Exception:
            raw = {}
    wanted = {
        "Title": "title",
        "Author": "author",
        "Subject": "subject",
        "Producer": "producer",
        "CreationDate": "created",
    }
    return {wanted[k]: str(v)[:200] for k, v in raw.items() if k in wanted and v}


def outline(pdf: pdfium.PdfDocument, limit: int) -> List[Dict[str, Any]]:
    """The table of contents (bookmarks), when the document has one.

    Args:
        pdf: An open document.
        limit: Most entries to return.

    Returns:
        list[dict]: ``title``, ``page`` (1-based, or None) and ``level``.
    """
    entries: List[Dict[str, Any]] = []
    with _LOCK:
        try:
            for item in pdf.get_toc(max_depth=4):
                # pypdfium2 5 yields PdfBookmark (get_title / get_dest);
                # older releases yielded a namedtuple with title / page_index.
                title = item.get_title() if hasattr(item, "get_title") else getattr(item, "title", "")
                index: Any = None
                if hasattr(item, "get_dest"):
                    dest = item.get_dest()
                    index = dest.get_index() if dest is not None else None
                else:
                    index = getattr(item, "page_index", None)
                page = index + 1 if isinstance(index, int) and index >= 0 else None
                entries.append(
                    {"title": str(title or "")[:120], "page": page, "level": int(getattr(item, "level", 0))}
                )
                if len(entries) >= limit:
                    break
        except Exception:
            return entries
    return entries


def has_labels(pdf: pdfium.PdfDocument, sample: int = 5) -> bool:
    """Whether the document defines printed page labels distinct from numbers.

    Args:
        pdf: An open document.
        sample: Pages to probe from the start.

    Returns:
        bool: True when any probed page has a distinct label.
    """
    with _LOCK:
        count = len(pdf)
        return any(_label(pdf, index) is not None for index in range(min(count, sample)))


def extract_text(pdf: pdfium.PdfDocument, page_numbers: Iterable[int]) -> List[PageText]:
    """Read the text layer of the given pages.

    Args:
        pdf: An open document.
        page_numbers: Physical, 1-based.

    Returns:
        list[PageText]: One entry per requested page, in the order given.
    """
    results: List[PageText] = []
    with _LOCK:
        count = len(pdf)
        for page_no in page_numbers:
            if page_no < 1 or page_no > count:
                continue
            warnings: List[str] = []
            try:
                page = pdf[page_no - 1]
                textpage = page.get_textpage()
                text = textpage.get_text_bounded() or ""
                textpage.close()
                page.close()
            except Exception as e:
                text = ""
                warnings.append(f"text layer could not be read ({type(e).__name__})")
            chars, usable, notes = assess(text)
            results.append(
                PageText(
                    page_no=page_no,
                    text=text.strip(),
                    chars=chars,
                    usable=usable,
                    warnings=warnings + notes,
                    label=_label(pdf, page_no - 1),
                )
            )
    return results


def render_page(pdf: pdfium.PdfDocument, page_no: int) -> bytes:
    """Render one page as a JPEG bounded by the policy's longest edge.

    Args:
        pdf: An open document.
        page_no: Physical, 1-based.

    Returns:
        bytes: JPEG data.

    Raises:
        IndexError: When the page does not exist.
    """
    with _LOCK:
        count = len(pdf)
        if page_no < 1 or page_no > count:
            raise IndexError(page_no)
        page = pdf[page_no - 1]
        try:
            width, height = page.get_size()
            longest = max(width, height) or 1.0
            scale = max(0.5, min(4.0, policy.PDF.render_max_edge / longest))
            # The stub types scale as int; PDFium takes the pixel size from
            # width × scale and a fractional scale is what bounds the edge.
            bitmap = page.render(scale=scale)  # pyright: ignore[reportArgumentType]
            image = bitmap.to_pil().convert("RGB")
        finally:
            page.close()
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=policy.PDF.render_jpeg_quality, optimize=True)
    return buffer.getvalue()


class DocumentCache:
    """Bytes of recently opened documents, bounded by total size."""

    def __init__(self) -> None:
        """Create an empty cache."""
        self._entries: "OrderedDict[str, bytes]" = OrderedDict()
        self._size = 0

    def get(self, key: str) -> Optional[bytes]:
        """Return cached bytes and mark them recently used.

        Args:
            key: Document id plus revision.

        Returns:
            bytes | None: The data, when cached.
        """
        data = self._entries.get(key)
        if data is not None:
            self._entries.move_to_end(key)
        return data

    def put(self, key: str, data: bytes) -> None:
        """Cache bytes, evicting the least recently used past the size ceiling.

        Args:
            key: Document id plus revision.
            data: The file.
        """
        limit = policy.PDF.cache_bytes
        if len(data) > limit:
            return
        if key in self._entries:
            self._size -= len(self._entries.pop(key))
        self._entries[key] = data
        self._size += len(data)
        while self._size > limit and self._entries:
            _, evicted = self._entries.popitem(last=False)
            self._size -= len(evicted)

    def clear(self) -> None:
        """Drop everything."""
        self._entries.clear()
        self._size = 0


def compress_ranges(pages: Iterable[int]) -> str:
    """Describe a set of pages compactly: "1–3, 7, 12–15".

    Args:
        pages: Page numbers, any order.

    Returns:
        str: Empty for no pages.
    """
    ordered = sorted(set(pages))
    if not ordered:
        return ""
    parts: List[str] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        parts.append(f"{start}–{previous}" if start != previous else str(start))
        start = previous = page
    parts.append(f"{start}–{previous}" if start != previous else str(start))
    return ", ".join(parts)
