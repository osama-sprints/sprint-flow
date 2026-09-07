"""The one place the document pipeline's thresholds live.

These are implementation details with defaults that suit a team chat, not
deployment knobs: they are documented here, changed here, and covered by the
tests here. Only two things about PDFs are environment settings, because only
those two are decisions a deployment genuinely makes — which model transcribes
pages (``PDF_OCR_MODEL``) and how many pages one turn may render and transcribe
(``PDF_PAGE_BUDGET_PER_TURN``).

Tests that need a different threshold replace the module attribute with
``dataclasses.replace(policy.PDF, ...)``; code reads ``policy.PDF`` at call
time for that reason.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PdfPolicy:
    """How PDFs are opened, read, rendered, transcribed and searched.

    Attributes:
        native_min_chars: A page whose text layer has fewer visible characters
            than this is treated as having none (a scan, a drawing, a cover).
        native_max_replacement_ratio: Above this share of U+FFFD or ``(cid:``
            markers a text layer is treated as unusable — broken font maps
            produce text that only looks like text.
        intake_native_pages: Pages whose native text is extracted and stored
            when the file arrives, so search works immediately. Beyond this the
            rest is extracted on first inspect/search.
        intake_inline_pages: Pages whose text is shown to the model in the
            arriving message, so short documents answer without a tool call.
        intake_inline_chars: Ceiling on that inline text.
        max_pages_per_read: Most pages one ``read_pdf_pages`` call returns;
            larger ranges continue with ``next_page``.
        vision_concurrency: Pages transcribed at the same time.
        render_max_edge: Longest edge, in pixels, of a rendered page.
        render_max_pixels: Ceiling on a rendered page's area, enforced before
            the bitmap is allocated; a page of unusual proportions cannot
            outgrow it whatever its edge.
        render_jpeg_quality: JPEG quality of the rendered page.
        visual_pages_per_call: Most page images one ``ask_pdf_pages`` question
            may cover; wider ranges are refused, not silently trimmed, because
            an answer about pages that were not shown would be wrong.
        search_transcribe_pages_per_call: Most untranscribed pages one
            progressive ``search_pdf`` call transcribes before handing back.
        result_chars_per_page: Ceiling on one page's text in a tool result.
        result_chars_total: Ceiling on a whole tool result.
        search_pages_per_call: Pages one ``search_pdf`` call scans before it
            hands back a cursor.
        search_max_hits: Most hits one search call returns.
        search_snippet_chars: Characters of context around a hit.
        toc_entries: Most table-of-contents entries ``inspect_pdf`` lists.
        ocr_timeout_seconds: Per-page transcription timeout.
        ocr_attempts: Transcription attempts before a page is reported unreadable.
        cache_bytes: Bytes of opened documents kept in memory across calls.
    """

    native_min_chars: int = 40
    native_max_replacement_ratio: float = 0.2
    intake_native_pages: int = 2000
    intake_inline_pages: int = 2
    intake_inline_chars: int = 4000
    max_pages_per_read: int = 12
    vision_concurrency: int = 3
    render_max_edge: int = 1600
    render_max_pixels: int = 4_000_000
    render_jpeg_quality: int = 85
    visual_pages_per_call: int = 4
    search_transcribe_pages_per_call: int = 12
    result_chars_per_page: int = 6000
    result_chars_total: int = 16000
    search_pages_per_call: int = 400
    search_max_hits: int = 20
    search_snippet_chars: int = 160
    toc_entries: int = 40
    ocr_timeout_seconds: int = 90
    ocr_attempts: int = 2
    cache_bytes: int = 64 * 1024 * 1024

    @property
    def render_key(self) -> str:
        """The cache key component for rendering settings.

        Returns:
            str: Changes whenever a rendering setting that affects OCR changes.
        """
        return f"edge{self.render_max_edge}-q{self.render_jpeg_quality}"


@dataclass(frozen=True)
class FileInputPolicy:
    """Ceilings for the files a message may carry, other than the upload size.

    Attributes:
        max_files: Files read per message; the rest are reported, not read.
        max_total_bytes: Combined size read per message.
        max_sheet_rows: Rows read per spreadsheet sheet.
        max_inline_chars: Extracted text shown inline per file.
        max_total_inline_chars: Extracted text shown inline per message.
        max_stored_chars: Extracted text kept for ``read_attachment``.
        max_image_pixels: Decompression-bomb ceiling for images.
        max_image_edge: Longest edge, in pixels, of an image sent to a model.
        vision_capable_prefixes: Model names that accept image input.
        retention_sweep_seconds: How often expired records are deleted.
    """

    max_files: int = 5
    max_total_bytes: int = 40 * 1024 * 1024
    max_sheet_rows: int = 500
    max_inline_chars: int = 12000
    max_total_inline_chars: int = 40000
    max_stored_chars: int = 400000
    max_image_pixels: int = 30_000_000
    max_image_edge: int = 2048
    vision_capable_prefixes: tuple[str, ...] = (
        "gemini/",
        "gemini-",
        "gpt-4o",
        "gpt-4.1",
        "gpt-5",
        "openai/gpt-4o",
        "openai/gpt-4.1",
        "openai/gpt-5",
    )
    retention_sweep_seconds: int = 6 * 3600


PDF = PdfPolicy()
FILE_INPUT = FileInputPolicy()
