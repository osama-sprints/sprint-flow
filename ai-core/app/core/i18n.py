"""The words a person sees, in their language.

Two languages are carried today, English and Arabic. The language of a turn
is decided from the person's message — Arabic script wins when it is the
larger share — with their Mattermost locale as the fallback for a message
that has no letters (an attachment on its own). It is bound to a ContextVar
before the turn runs, so the notices above a reply, the error and "Stopped"
replies, and the short answers to a typed cancel or retry all read it. Tool
results and anything else the model reads stay English: that is protocol, not
prose for a person.
"""

import re
from contextvars import ContextVar
from typing import (
    Any,
    Dict,
)

DEFAULT_LANGUAGE = "en"
SUPPORTED = ("en", "ar")

current_language: ContextVar[str] = ContextVar("current_language", default=DEFAULT_LANGUAGE)

_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ]")
_LATIN = re.compile(r"[A-Za-z]")

CATALOGUE: Dict[str, Dict[str, str]] = {
    "en": {
        "reply.fallback": "Sorry — I hit an error while working on that. Reply **retry** to try again.",
        "reply.stopped": "⏹ Stopped as you asked. Reply **retry** if you want me to run it again.",
        "reply.cut_short": "\n\n_(The reply was cut short by the length limit.)_",
        "reply.nothing_readable": "",
        "notice.message_truncated": (
            "Your message was {total:,} characters long; I read the first {limit:,}. "
            "Attach the rest as a file if you need me to read all of it."
        ),
        "notice.attachments_off": "Reading attachments is switched off on this assistant, so I answered the text only.",
        "notice.too_many_files": "Only the first {shown} of {total} attachments were read; send the others in a separate message.",
        "notice.unreadable": "I couldn't read **{name}**: {reason}.",
        "notice.not_saved": "I read the attachments but could not save them for later turns.",
        "notice.sheet_rows": "**{name}**, sheet {sheet}: only the first {cap} rows were read.",
        "reason.not_returned": "Mattermost did not return it",
        "reason.not_this_message": "it is not part of this message",
        "reason.too_large": "it is {size}; the limit per file is {limit}",
        "reason.total_too_large": "together the attachments exceed {limit}",
        "reason.download_failed": "it could not be downloaded",
        "reason.unreadable": "it could not be read",
        "reason.mismatch": "the content is {what}, not a .{ext} file",
        "reason.unsupported_type": "it is {what}, which is not a supported type",
        "reason.unsupported_ext": ".{ext} files are not supported",
        "reason.invalid_for_ext": "the content does not look like a valid .{ext} file",
        "reason.no_type": "the file has no recognisable type",
        "reason.binary": "the content is binary, not text",
        "reason.utf16": "the text is not valid UTF-16",
        "reason.utf8": "the text is not UTF-8; please save it as UTF-8 and send it again",
        "reason.encrypted": "it is password-protected",
        "reason.damaged_pdf": "it could not be opened as a PDF",
        "reason.no_pages": "it has no pages",
        "reason.docx": "it could not be opened as a Word document",
        "reason.xlsx": "it could not be opened as an Excel workbook",
        "reason.image_decode": "it could not be decoded as an image",
        "reason.image_too_big": "it is {width}×{height} pixels; the limit is {limit:,} pixels",
        "reason.kind_unsupported": "{kind} files are not supported",
        "what.pdf": "a PDF",
        "what.docx": "a Word document",
        "what.xlsx": "an Excel workbook",
        "what.png": "a PNG image",
        "what.jpeg": "a JPEG image",
        "what.webp": "a WebP image",
        "what.zip": "a zip archive",
        "what.other": "of type {mime}",
        "control.only_requester_stop": "Only the person who asked for that, or an admin, can stop it.",
        "control.only_requester_retry": "Only the person who asked for that, or an admin, can retry it.",
    },
    "ar": {
        "reply.fallback": "عذرًا — حدث خطأ أثناء العمل على طلبك. اكتب **أعد المحاولة** للمحاولة مجددًا.",
        "reply.stopped": "⏹ توقفت كما طلبت. اكتب **أعد المحاولة** إذا أردت تشغيله من جديد.",
        "reply.cut_short": "\n\n_(تم اختصار الرد بسبب حد الطول.)_",
        "reply.nothing_readable": "",
        "notice.message_truncated": (
            "كانت رسالتك بطول {total:,} حرفًا؛ قرأت أول {limit:,} حرفًا منها. أرفق الباقي كملف إذا أردت أن أقرأه كاملًا."
        ),
        "notice.attachments_off": "قراءة المرفقات متوقفة في هذا المساعد، لذلك أجبت عن النص فقط.",
        "notice.too_many_files": "قرأت أول {shown} من أصل {total} مرفقات فقط؛ أرسل الباقي في رسالة منفصلة.",
        "notice.unreadable": "لم أتمكن من قراءة **{name}**: {reason}.",
        "notice.not_saved": "قرأت المرفقات لكن لم أستطع حفظها للرسائل اللاحقة.",
        "notice.sheet_rows": "**{name}**، الورقة {sheet}: قُرئت أول {cap} صفًا فقط.",
        "reason.not_returned": "لم يُرجعه Mattermost",
        "reason.not_this_message": "ليس جزءًا من هذه الرسالة",
        "reason.too_large": "حجمه {size}؛ الحد الأقصى للملف الواحد {limit}",
        "reason.total_too_large": "مجموع المرفقات يتجاوز {limit}",
        "reason.download_failed": "تعذّر تنزيله",
        "reason.unreadable": "تعذّرت قراءته",
        "reason.mismatch": "المحتوى {what} وليس ملف .{ext}",
        "reason.unsupported_type": "هو {what}، وهذا نوع غير مدعوم",
        "reason.unsupported_ext": "ملفات .{ext} غير مدعومة",
        "reason.invalid_for_ext": "المحتوى لا يبدو ملف .{ext} صالحًا",
        "reason.no_type": "لا يمكن التعرف على نوع الملف",
        "reason.binary": "المحتوى ثنائي وليس نصًا",
        "reason.utf16": "النص ليس بترميز UTF-16 صالح",
        "reason.utf8": "النص ليس بترميز UTF-8؛ احفظه بترميز UTF-8 وأرسله مجددًا",
        "reason.encrypted": "محمي بكلمة مرور",
        "reason.damaged_pdf": "تعذّر فتحه كملف PDF",
        "reason.no_pages": "لا يحتوي على صفحات",
        "reason.docx": "تعذّر فتحه كمستند Word",
        "reason.xlsx": "تعذّر فتحه كمصنّف Excel",
        "reason.image_decode": "تعذّر فك ترميزه كصورة",
        "reason.image_too_big": "أبعاده {width}×{height} بكسل؛ الحد الأقصى {limit:,} بكسل",
        "reason.kind_unsupported": "ملفات {kind} غير مدعومة",
        "what.pdf": "ملف PDF",
        "what.docx": "مستند Word",
        "what.xlsx": "مصنّف Excel",
        "what.png": "صورة PNG",
        "what.jpeg": "صورة JPEG",
        "what.webp": "صورة WebP",
        "what.zip": "أرشيف مضغوط",
        "what.other": "من النوع {mime}",
        "control.only_requester_stop": "لا يمكن إيقاف ذلك إلا لمن طلبه أو لمسؤول.",
        "control.only_requester_retry": "لا يمكن إعادة المحاولة إلا لمن طلبها أو لمسؤول.",
    },
}


def detect_language(text: str, fallback: str = DEFAULT_LANGUAGE) -> str:
    """Decide a turn's language from the person's words.

    Args:
        text: The message text.
        fallback: Language to use when the text has no letters at all.

    Returns:
        str: "ar" when at least three letters in ten are Arabic, else "en";
        the fallback for a message with no letters.
    """
    arabic = len(_ARABIC.findall(text or ""))
    latin = len(_LATIN.findall(text or ""))
    if arabic == 0 and latin == 0:
        return fallback if fallback in SUPPORTED else DEFAULT_LANGUAGE
    # Arabic speakers mix in English terms freely; the reverse is rare. A
    # message with a real share of Arabic letters is an Arabic message.
    return "ar" if arabic / (arabic + latin) >= 0.3 else "en"


def language_from_locale(locale: str | None) -> str:
    """Map a Mattermost locale to a supported language.

    Args:
        locale: e.g. "ar", "en-GB".

    Returns:
        str: "ar" for Arabic locales, otherwise "en".
    """
    return "ar" if (locale or "").lower().startswith("ar") else DEFAULT_LANGUAGE


def set_language(language: str) -> None:
    """Bind the language for the current turn.

    Args:
        language: "en" or "ar"; anything else falls back to English.
    """
    current_language.set(language if language in SUPPORTED else DEFAULT_LANGUAGE)


def language() -> str:
    """The current turn's language.

    Returns:
        str: "en" or "ar".
    """
    return current_language.get()


def t(key: str, **values: Any) -> str:
    """Render a catalogue entry in the current language.

    Args:
        key: Catalogue key.
        **values: Format arguments.

    Returns:
        str: The rendered text; English when the key is missing in the language.
    """
    entry = CATALOGUE.get(current_language.get(), {}).get(key) or CATALOGUE["en"][key]
    return entry.format(**values)


def t_in(language_code: str, key: str, **values: Any) -> str:
    """Render a catalogue entry in an explicit language.

    Args:
        language_code: "en" or "ar".
        key: Catalogue key.
        **values: Format arguments.

    Returns:
        str: The rendered text.
    """
    entry = CATALOGUE.get(language_code, {}).get(key) or CATALOGUE["en"][key]
    return entry.format(**values)
