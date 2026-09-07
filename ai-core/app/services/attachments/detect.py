"""What an attached file actually is, decided from its bytes.

The name a file was uploaded under is a claim; the bytes are the fact. A PDF is
accepted because it starts like a PDF, a Word document because it is a zip
holding ``word/document.xml``, an image because its signature says so — and a
file whose name and content disagree is refused rather than parsed by whichever
reader the name suggests. Plain-text kinds have no signature, so for them the
check is the opposite: the bytes must decode as text and carry no binary.
"""

import io
import zipfile
from dataclasses import dataclass
from typing import (
    Dict,
    FrozenSet,
    Optional,
    Tuple,
)

import filetype

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Detected content type -> (kind, extensions the name may carry).
_BINARY_KINDS: Dict[str, Tuple[str, FrozenSet[str]]] = {
    "application/pdf": ("pdf", frozenset({"pdf"})),
    DOCX_MIME: ("docx", frozenset({"docx"})),
    XLSX_MIME: ("xlsx", frozenset({"xlsx"})),
    "image/png": ("image", frozenset({"png"})),
    "image/jpeg": ("image", frozenset({"jpg", "jpeg"})),
    "image/webp": ("image", frozenset({"webp"})),
}

# Text kinds have no signature: kind -> (content type, extensions).
_TEXT_KINDS: Dict[str, Tuple[str, FrozenSet[str]]] = {
    "csv": ("text/csv", frozenset({"csv"})),
    "markdown": ("text/markdown", frozenset({"md", "markdown"})),
    "text": ("text/plain", frozenset({"txt", "text", "log"})),
}

SUPPORTED_EXTENSIONS: FrozenSet[str] = frozenset().union(
    *(exts for _, exts in _BINARY_KINDS.values()), *(exts for _, exts in _TEXT_KINDS.values())
)

_DESCRIPTIONS = {
    "application/pdf": "a PDF",
    DOCX_MIME: "a Word document",
    XLSX_MIME: "an Excel workbook",
    "image/png": "a PNG image",
    "image/jpeg": "a JPEG image",
    "image/webp": "a WebP image",
    "application/zip": "a zip archive",
}

# Bytes of a text file that are never legitimate in text.
_BINARY_MARKERS = frozenset(range(0, 9)) | frozenset({11, 12}) | frozenset(range(14, 32)) | {127}


class Unsupported(ValueError):
    """The file cannot be read; the message says why, for the person."""


@dataclass(frozen=True)
class Detection:
    """What a file was found to be.

    Attributes:
        kind: pdf, docx, xlsx, csv, markdown, text or image.
        mime: The content type derived from the bytes (or, for text, the kind).
        extension: Lower-cased extension from the name, without the dot.
    """

    kind: str
    mime: str
    extension: str


def extension_of(name: str) -> str:
    """Return the lower-cased extension of a file name, without the dot.

    Args:
        name: The uploaded name.

    Returns:
        str: The extension, or "" when the name has none.
    """
    base = name.rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower().strip()


def describe(mime: str) -> str:
    """Human wording for a detected content type.

    Args:
        mime: The content type.

    Returns:
        str: A phrase such as "a PNG image".
    """
    return _DESCRIPTIONS.get(mime, f"a {mime} file")


def _office_kind(data: bytes) -> Optional[str]:
    """Tell a Word document from a workbook by the parts inside the zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    if "word/document.xml" in names:
        return DOCX_MIME
    if "xl/workbook.xml" in names:
        return XLSX_MIME
    return None


def sniff(data: bytes) -> Optional[str]:
    """Return the content type the bytes declare, or None for signature-less data.

    Args:
        data: The file's bytes (the head is enough, but the whole file is fine).

    Returns:
        str | None: A content type, or None when no known signature matched.
    """
    guess = filetype.guess(data)
    if guess is None:
        return None
    mime = str(guess.mime)
    if mime in (DOCX_MIME, XLSX_MIME):
        # filetype identifies these from the zip's first entries; confirm by
        # the parts that define the format, so a renamed archive cannot pass.
        return _office_kind(data) or "application/zip"
    if mime == "application/zip":
        return _office_kind(data) or mime
    return mime


def decode_text(data: bytes) -> str:
    """Decode bytes that are meant to be text.

    Args:
        data: The file's bytes.

    Returns:
        str: The decoded text.

    Raises:
        Unsupported: When the bytes carry binary markers or are not UTF-8/UTF-16.
    """
    if any(b in _BINARY_MARKERS for b in data[:65536]):
        raise Unsupported("the content is binary, not text")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as e:
            raise Unsupported("the text is not valid UTF-16") from e
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise Unsupported("the text is not UTF-8; please save it as UTF-8 and send it again") from e


def detect(name: str, data: bytes) -> Detection:
    """Decide a file's kind from its bytes, cross-checked against its name.

    Args:
        name: The uploaded file name.
        data: The file's bytes.

    Returns:
        Detection: The kind, content type and extension.

    Raises:
        Unsupported: For unsupported types, and for a name that contradicts
            the content.
    """
    extension = extension_of(name)
    mime = sniff(data)

    if mime in _BINARY_KINDS:
        kind, extensions = _BINARY_KINDS[mime]
        if extension and extension not in extensions:
            raise Unsupported(f"the content is {describe(mime)}, not a .{extension} file")
        return Detection(kind=kind, mime=mime, extension=extension or sorted(extensions)[0])

    if mime is not None:
        label = describe(mime) if mime in _DESCRIPTIONS else f"of type {mime}"
        raise Unsupported(f"it is {label}, which is not a supported type")

    for kind, (text_mime, extensions) in _TEXT_KINDS.items():
        if extension in extensions:
            decode_text(data)
            return Detection(kind=kind, mime=text_mime, extension=extension)

    if extension in SUPPORTED_EXTENSIONS:
        raise Unsupported(f"the content does not look like a valid .{extension} file")
    if extension:
        raise Unsupported(f".{extension} files are not supported")
    raise Unsupported("the file has no recognisable type")
