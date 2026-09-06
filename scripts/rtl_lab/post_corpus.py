"""Post the bidi test corpus into a dedicated channel, as the bot.

Every case in the acceptance list gets its own post so a screenshot shows them
side by side: Arabic prose, Arabic opening with a Latin product name, mixed
prose, ordered and unordered lists, inline code and links, English prose and a
fenced code block.
"""

import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:8065/api/v4"

CORPUS = [
    ("arabic-only", "المساعد يقوم بتنظيم السبرنت الحالي ويتابع المهام المفتوحة مع كل عضو في الفريق."),
    ("latin-first", "Gemini هو النموذج الذي نستخدمه الآن في كل عمليات التوليد داخل المنصة."),
    ("latin-first-2", "SprintFlow يساعد الفريق على متابعة الاجتماعات والمهام بشكل يومي."),
    ("mixed", "قمنا بتحديث الـ pipeline الخاص بالـ deployment ثم أعدنا تشغيل الخدمة."),
    ("ordered-list", "خطوات تسجيل طالب جديد:\n\n1. إنشاء الحساب على المنصة\n2. إضافة الطالب إلى الفريق\n3. إرسال رسالة الترحيب"),
    ("unordered-list", "المطلوب هذا الأسبوع:\n\n- مراجعة الكود\n- تحديث الوثائق\n- إغلاق التذاكر المفتوحة"),
    ("inline-rich", "راجع الملف `app/services/mattermost.py` على [الرابط](https://example.com) مع @admin (رقم 42) قبل الغد."),
    ("english-only", "The deployment pipeline was updated and the service restarted cleanly."),
    ("code-block", "مثال على الكود:\n\n```python\ndef send_diagram(definition: str) -> None:\n    publish(definition)\n```"),
    ("mixed-paragraphs", "هذه فقرة عربية كاملة تشرح الميزة الجديدة.\n\nThis paragraph is entirely English and must stay left to right.\n\nوهذه فقرة عربية أخرى بعدها."),
]


def call(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    """Make an authenticated Mattermost API call."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def main() -> int:
    """Create the lab channel if needed and post every case."""
    env = {}
    for line in open("/home/o/poc/skill-sync/.env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key] = value.strip().strip('"')

    token = env["MATTERMOST_BOT_TOKEN"]
    team = call("GET", "/teams/name/sprints-community", token)

    try:
        channel = call("GET", f"/teams/{team['id']}/channels/name/rtl-lab", token)
    except urllib.error.HTTPError:
        channel = call(
            "POST",
            "/channels",
            token,
            {"team_id": team["id"], "name": "rtl-lab", "display_name": "RTL Lab", "type": "O"},
        )

    posted = []
    for name, message in CORPUS:
        post = call("POST", "/posts", token, {"channel_id": channel["id"], "message": message})
        posted.append({"case": name, "post_id": post["id"]})

    print(json.dumps({"channel_id": channel["id"], "channel": "rtl-lab", "posts": posted}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
