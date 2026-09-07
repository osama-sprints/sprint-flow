"""Arabic: a wider reversed-layer corpus, and the words a person reads in their language."""

import io

import pytest
from PIL import Image

from app.core import i18n
from app.services.attachments.detect import (
    Unsupported,
    detect,
)
from app.services.documents.pdf import assess

ARABIC_PARAGRAPHS = [
    "تم الاتفاق بين المؤجر والمستأجر على تأجير الشقة في حي النرجس من أول أكتوبر إلى نهاية سبتمبر",
    "يجب على الموظف تقديم طلب الإجازة قبل أربعة عشر يومًا من الموعد المحدد وذلك عبر النظام الإلكتروني",
    "تُعقد اجتماعات المراجعة بعد نهاية كل سبرنت مع الفريق، ويتم توثيق القرارات في محضر يُرسل إلى الجميع",
    "في حال وجود خلاف بين الطرفين يُحال الأمر إلى لجنة التحكيم التي تصدر قرارها خلال ثلاثين يومًا",
    "هذه الوثيقة سرية ولا يجوز نسخها أو توزيعها دون إذن كتابي مسبق من الإدارة القانونية",
]


def _mirror(paragraph: str) -> str:
    return " ".join(word[::-1] for word in paragraph.split())


@pytest.mark.parametrize("paragraph", ARABIC_PARAGRAPHS)
def test_reversed_arabic_layers_are_caught_and_genuine_ones_are_not(paragraph):
    body = paragraph + " " + paragraph
    assert assess(body)[1] is True
    _, usable, warnings = assess(_mirror(body))
    assert usable is False and "visual order" in warnings[0]


def test_reversed_probe_survives_diacritics_numbers_and_latin_in_the_mix():
    genuine = "الحدُّ الأقصى للمصروفات في Riyadh هو 350 ريالًا لليلة، ومن ثمّ يُطلب إيصال بعد كل رحلة إلى London"
    assert assess(genuine + " " + genuine)[1] is True
    mirrored = " ".join(word[::-1] if any("؀" <= c <= "ۿ" for c in word) else word for word in genuine.split())
    assert assess(mirrored + " " + mirrored)[1] is False


def test_a_page_with_few_function_words_is_left_usable_rather_than_guessed():
    # A list-like page: no probe words either way, so no verdict is forced.
    listing = "الرياض 350 220 جدة 400 260 دبي 1100 480 لندن 2600 900 " * 3
    assert assess(listing)[1] is True and assess(_mirror(listing))[1] is True


def test_language_detection_and_locale_fallback():
    assert i18n.detect_language("اقرأ صفحة 7") == "ar"
    assert i18n.detect_language("Read page 7 please") == "en"
    assert i18n.detect_language("طيب read the table من صفحة 12") == "ar"
    assert i18n.detect_language("", fallback="ar") == "ar"
    assert i18n.detect_language("12345", fallback="en") == "en"
    assert i18n.language_from_locale("ar") == "ar" and i18n.language_from_locale("en-GB") == "en"
    assert i18n.language_from_locale(None) == "en"


def test_refusal_reasons_render_in_the_persons_language():
    token = i18n.current_language.set("ar")
    try:
        with pytest.raises(Unsupported) as refused:
            detect("invoice.pdf", _png())
        assert str(refused.value) == "the content is a PNG image, not a .pdf file"  # English for logs and tests
        assert refused.value.render() == "المحتوى صورة PNG وليس ملف .pdf"
        assert i18n.t("notice.unreadable", name="x.exe", reason=i18n.t("reason.unsupported_ext", ext="exe")) == (
            "لم أتمكن من قراءة **x.exe**: ملفات .exe غير مدعومة."
        )
        assert i18n.t("reply.stopped").startswith("⏹ توقفت")
    finally:
        i18n.current_language.reset(token)
    assert i18n.t("reply.stopped").startswith("⏹ Stopped")


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()
