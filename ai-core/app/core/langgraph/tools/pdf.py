"""Reading PDFs the way a person does: find the page, read the page, cite it.

Three tools, all scoped to the conversation the turn runs in:

* ``inspect_pdf`` — how long it is, what it is called, its table of contents,
  which pages have a text layer, what has already been read;
* ``search_pdf`` — where a phrase occurs in the text that is available, with
  an honest account of pages that could not be searched;
* ``read_pdf_pages`` — a page or a range, native text where it is usable and a
  rendered-and-transcribed reading where it is not or where the agent asks.

The model supplies a document id and page numbers; identity, authorisation
and file access come from the turn context, never from the arguments.
"""

from typing import (
    Any,
    Dict,
    List,
    Optional,
)

from langchain_core.tools import tool

from app.core.langgraph.tools.results import (
    ResultCode,
    guarded_tool,
    tool_result,
)
from app.services.documents import service as documents
from app.services.documents.service import (
    AskResult,
    BudgetExhausted,
    DocumentUnavailable,
    InvalidRange,
    ReadResult,
    SearchResult,
)

_TOC_SHOWN = 25


def _inspect_text(facts: Dict[str, Any]) -> str:
    lines = [f"{facts['name']} (id {facts['id']}) — {facts['pages']} pages."]
    header = [f"{k}: {facts[k]}" for k in ("title", "author", "created") if facts.get(k)]
    if header:
        lines.append("; ".join(header) + ".")
    if facts["labels"]:
        lines.append("Pages carry printed labels that differ from their physical numbers; use physical numbers here.")
    toc: List[Dict[str, Any]] = facts["toc"]
    if toc:
        entries = [
            f"{'  ' * int(e.get('level', 0))}{e.get('title')}" + (f" — p.{e['page']}" if e.get("page") else "")
            for e in toc[:_TOC_SHOWN]
        ]
        lines.append("Contents:")
        lines.extend(f"  {entry}" for entry in entries)
        if len(toc) > _TOC_SHOWN:
            lines.append(f"  … {len(toc) - _TOC_SHOWN} more entries")
    else:
        lines.append("No table of contents in the file.")

    if facts["assessed_pages"] == 0:
        lines.append("Text layer not assessed yet.")
    else:
        parts = []
        if facts["text_page_count"]:
            parts.append(f"text layer on {facts['text_page_count']} pages ({facts['text_pages']})")
        if facts["no_text_page_count"]:
            parts.append(
                f"NO usable text on {facts['no_text_page_count']} pages ({facts['no_text_pages']}) — these need "
                'read_pdf_pages with mode="auto" or "vision" to be transcribed before they can be searched'
            )
        if facts["transcribed_pages"]:
            parts.append(f"already transcribed: {facts['transcribed_pages']}")
        if facts["unassessed"]:
            parts.append(f"{facts['unassessed']} pages not assessed yet (assessed on first search or read)")
        lines.append("Coverage: " + "; ".join(parts) + ".")
    if facts["read_count"]:
        lines.append(
            f"Read in this conversation so far: pages {facts['read_pages']} ({facts['read_count']} of {facts['pages']})."
        )
    else:
        lines.append("Nothing from this document has been read in this conversation yet.")
    if facts["next_unread_page"] is not None:
        lines.append(f"First unread page: {facts['next_unread_page']}.")
    lines.append(
        f"Transcription budget left this turn: {facts['budget_remaining']} pages (model {facts['ocr_model']})."
    )
    return "\n".join(lines)


def _read_text(result: ReadResult) -> str:
    doc = result.document
    start, end = result.requested
    methods = {"native": 0, "vision": 0, "none": 0}
    for page in result.pages:
        methods[page.method] = methods.get(page.method, 0) + 1
    how = ", ".join(
        f"{v} {('native text' if k == 'native' else 'transcribed' if k == 'vision' else 'unreadable')}"
        for k, v in methods.items()
        if v
    )
    head = [
        f"{doc.name} (id {doc.id}) — pages {start}–{end} of {result.total_pages}; served {len(result.pages)} page(s): {how}."
    ]
    for entry in result.not_processed:
        reason = entry["reason"]
        if reason == "range":
            head.append(f"Not served yet: pages {entry['pages']} (at most one batch per call).")
        elif reason == "budget":
            head.append(
                f"Not transcribed: pages {entry['pages']} — this turn's transcription budget is used up "
                "(cached and native pages still work). Tell the person which pages remain."
            )
        elif reason == "size":
            head.append(
                f"Not shown: pages {entry['pages']} — the result would be too large; read them in a smaller batch."
            )
    if result.next_page is not None:
        head.append(f'Continue with read_pdf_pages("{doc.id}", {result.next_page}, {end}).')
    elif any(e["reason"] == "budget" for e in result.not_processed):
        head.append("Do not call again for the skipped pages in this turn; the budget will not have changed.")
    head.append(f"Read in this conversation so far: {result.coverage or 'nothing'} of {result.total_pages} pages.")
    head.append(f"Transcription budget left this turn: {result.budget_remaining}.")

    body: List[str] = []
    for page in result.pages:
        label = f' (label "{page.label}")' if page.label else ""
        method = {
            "native": "native text",
            "vision": f"transcribed by {page.model}" + (" (cached)" if page.cached else ""),
            "none": "nothing readable",
        }[page.method]
        warnings = f" · {'; '.join(page.warnings)}" if page.warnings else ""
        body.append(f"--- page {page.page_no}{label} · {method}{warnings} ---")
        body.append(page.text if page.text else "(no text)")
    return "\n".join(head) + "\n\n" + "\n".join(body)


def _search_text(result: SearchResult) -> str:
    doc = result.document
    first, last = result.searched
    lines = [
        f'{doc.name} (id {doc.id}) — searched pages {first}–{last} of {result.total_pages} for "{result.query}": '
        f"{len(result.hits)} hit(s)."
    ]
    if result.transcribed_now:
        lines.append(f"Transcribed in this call so they could be searched: pages {result.transcribed_now}.")
    if result.empty_pages:
        lines.append(
            f"Transcribed earlier and found blank or unreadable (nothing to search): pages {result.empty_pages}."
        )
    if result.unsearched:
        if result.budget_skipped:
            lines.append(
                f"NOT searched: pages {result.unsearched} have no readable text; pages {result.budget_skipped} were "
                "left untranscribed because this turn's transcription budget is used up. Tell the person which "
                "pages remain unsearched; they can ask again to continue."
            )
        else:
            lines.append(
                f"NOT searched: pages {result.unsearched} have no readable text yet. Call search_pdf again with "
                "transcribe_missing=true to transcribe them in order within the budget, or transcribe a chosen range "
                f'with read_pdf_pages("{doc.id}", start, end, mode="vision"). Never conclude the phrase is absent '
                "from pages that were not searched."
            )
    for hit in result.hits:
        label = f' (label "{hit.label}")' if hit.label else ""
        lines.append(
            f"- page {hit.page_no}{label} [{'transcribed' if hit.method == 'vision' else 'native'}]: {hit.snippet}"
        )
    if result.capped:
        lines.append(f"Hit limit reached on page {last}; later pages were not scanned.")
    if result.next_cursor is not None:
        lines.append(
            f'More pages remain: continue with search_pdf("{doc.id}", "{result.query}", cursor={result.next_cursor}).'
        )
    elif not result.unsearched:
        lines.append("Every page has been searched.")
    lines.append(f"Transcription budget left this turn: {result.budget_remaining}.")
    return "\n".join(lines)


def _ask_text(result: AskResult) -> str:
    doc = result.document
    pages = ", ".join(str(p) for p in result.pages)
    lines = [
        f"{doc.name} (id {doc.id}) — visual reading of page(s) {pages} of {result.total_pages} by {result.model}. "
        "This is an interpretation of the page images, not a transcription; for exact wording use read_pdf_pages.",
        "<<<",
        result.answer,
        ">>>",
        f"Read in this conversation so far: {result.coverage or 'nothing'} of {result.total_pages} pages. "
        f"Visual/transcription budget left this turn: {result.budget_remaining}.",
    ]
    return "\n".join(lines)


@tool
@guarded_tool
async def inspect_pdf(document_id: str) -> str:
    """Describe a PDF attached in this conversation before reading it.

    Start here for any question about a PDF: it gives the page count, title,
    table of contents with page numbers, which pages have a text layer and
    which do not (scans need transcribing), what has already been read in this
    conversation, the first unread page, and how many pages this turn may
    still transcribe.

    Args:
        document_id: The id shown with the file.

    Returns:
        Structure and coverage of the document.
    """
    try:
        facts = await documents.inspect(document_id)
    except DocumentUnavailable as e:
        return tool_result(ResultCode.PDF_NOT_FOUND, str(e))
    return tool_result(ResultCode.PDF_INSPECTED, _inspect_text(facts))


@tool
@guarded_tool
async def read_pdf_pages(
    document_id: str, start_page: int, end_page: Optional[int] = None, mode: str = "auto", char_offset: int = 0
) -> str:
    """Read one page or an inclusive range of a PDF, with provenance.

    Page numbers are physical and 1-based (page 1 is the first page in the
    file, whatever is printed on it). Modes: "auto" reads the text layer and
    transcribes only pages that have none; "text" reads the text layer only;
    "vision" renders and transcribes the pages even when text exists — use it
    for scanned pages, tables whose layout matters, screenshots and diagrams.
    Large ranges come back in batches with a continuation; call again with the
    given next page. Cite the page numbers you read.

    Args:
        document_id: The id shown with the file.
        start_page: First page, 1-based.
        end_page: Last page, inclusive. Omit for a single page.
        mode: "auto", "text" or "vision".
        char_offset: For a single dense page whose text was cut, the character to continue from
            (the previous result gives the number).

    Returns:
        The pages' text, how each was obtained, what was not served, and coverage so far.
    """
    try:
        result = await documents.read_pages(
            document_id,
            int(start_page),
            None if end_page is None else int(end_page),
            mode,
            char_offset=max(0, int(char_offset or 0)),
        )
    except DocumentUnavailable as e:
        return tool_result(ResultCode.PDF_NOT_FOUND, str(e))
    except InvalidRange as e:
        return tool_result(ResultCode.PDF_INVALID_RANGE, str(e))
    except documents.PdfError as e:
        return tool_result(
            ResultCode.PDF_UNREADABLE,
            "the file is password-protected" if e.kind == "encrypted" else "the file could not be opened as a PDF",
        )
    code = (
        ResultCode.PDF_BUDGET_EXHAUSTED
        if any(e["reason"] == "budget" for e in result.not_processed)
        else ResultCode.PDF_PAGES
    )
    return tool_result(code, _read_text(result))


@tool
@guarded_tool
async def search_pdf(
    document_id: str, query: str, cursor: Optional[int] = None, transcribe_missing: bool = False
) -> str:
    """Find which pages of a PDF mention a word or phrase.

    Matching is case-insensitive and ignores Arabic diacritics and common
    letter variants. Only text that is available is searched: the text layer,
    plus pages transcribed earlier. The result says exactly which pages were
    searched and which could not be — never treat an unsearched page as a
    page without the phrase. For a scanned document, first learn its
    structure (inspect_pdf; transcribe the contents page if there is one),
    then search with transcribe_missing=true: the call transcribes the
    untranscribed pages of the window in order, within this turn's budget,
    reports what it covered, and hands back a cursor to continue. Long
    documents are scanned in windows; continue with the returned cursor.

    Args:
        document_id: The id shown with the file.
        query: The word or phrase.
        cursor: Page to continue from, taken from a previous result.
        transcribe_missing: Transcribe pages without readable text before searching them.

    Returns:
        Hits with page numbers and snippets, the pages searched, transcribed and not searchable, and a cursor.
    """
    try:
        result = await documents.search(document_id, query, cursor, transcribe_missing=bool(transcribe_missing))
    except DocumentUnavailable as e:
        return tool_result(ResultCode.PDF_NOT_FOUND, str(e))
    except InvalidRange as e:
        return tool_result(ResultCode.PDF_INVALID_RANGE, str(e))
    except documents.PdfError as e:
        return tool_result(
            ResultCode.PDF_UNREADABLE,
            "the file is password-protected" if e.kind == "encrypted" else "the file could not be opened as a PDF",
        )
    code = ResultCode.PDF_BUDGET_EXHAUSTED if result.budget_skipped else ResultCode.PDF_SEARCH
    return tool_result(code, _search_text(result))


@tool
@guarded_tool
async def ask_pdf_pages(document_id: str, question: str, start_page: int, end_page: Optional[int] = None) -> str:
    """Ask a question about what one to four PDF pages look like.

    Use this when the question is about a diagram, chart, screenshot, photo,
    stamp, signature, handwriting, form layout or how a table is arranged —
    anything read_pdf_pages' text cannot carry. The pages are shown as images
    to a vision model that answers only from what is visible and names the
    pages it relies on. It is an interpretation, not a transcription: for the
    exact wording of a page use read_pdf_pages. At most four pages per
    question; split wider ranges.

    Args:
        document_id: The id shown with the file.
        question: What to find out from the page images.
        start_page: First page, 1-based.
        end_page: Last page, inclusive. Omit for a single page.

    Returns:
        The answer with the pages shown and the model that answered.
    """
    try:
        result = await documents.ask_pages(
            document_id, question, int(start_page), None if end_page is None else int(end_page)
        )
    except DocumentUnavailable as e:
        return tool_result(ResultCode.PDF_NOT_FOUND, str(e))
    except InvalidRange as e:
        return tool_result(ResultCode.PDF_INVALID_RANGE, str(e))
    except BudgetExhausted as e:
        return tool_result(
            ResultCode.PDF_BUDGET_EXHAUSTED,
            f"this turn's page budget is used up; pages {', '.join(str(p) for p in e.pages)} were not looked at. "
            "Tell the person, who can ask again.",
        )
    except documents.PdfError as e:
        return tool_result(
            ResultCode.PDF_UNREADABLE,
            "the file is password-protected" if e.kind == "encrypted" else "the file could not be opened as a PDF",
        )
    except Exception as e:  # the vision call itself failed after retries
        return tool_result(ResultCode.PDF_UNREADABLE, f"the pages could not be examined ({type(e).__name__}).")
    return tool_result(ResultCode.PDF_VISUAL_ANSWER, _ask_text(result))


PDF_TOOLS = [inspect_pdf, search_pdf, read_pdf_pages, ask_pdf_pages]
