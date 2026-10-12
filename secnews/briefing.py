"""Daily briefing email: build it from recent articles and send it to a subscriber list over SMTP."""

from __future__ import annotations

import html
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from .config import Taxonomy
from .models import Article

MAROON, NAVY, MAROON_TEXT = "#5a0000", "#000d4c", "#6b0000"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --------------------------------------------------------------------------- subscribers


def subscribers_path() -> Path:
    return Path(os.environ.get("SECNEWS_SUBSCRIBERS") or Path.home() / ".secnews" / "subscribers.txt")


def load_subscribers(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        email = line.split("#", 1)[0].strip().lower()
        if email and email not in out:
            out.append(email)
    return out


def save_subscribers(path: Path, emails: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{e}\n" for e in sorted(set(emails))), encoding="utf-8")


# --------------------------------------------------------------------------- building


def pick_stories(articles: list[Article], limit: int) -> list[Article]:
    """Most-covered stories first (reported by several outlets), then newest."""
    ranked = sorted(articles, key=lambda a: (len(a.also_reported_by), a.published or datetime.min.replace(tzinfo=timezone.utc)),
                    reverse=True)
    return ranked[:limit]


@dataclass
class Section:
    heading: str
    stories: list[Article]
    note: str = ""
    catch_up: bool = False  # older stories: show their date instead of "today"
    label_topics: tuple[str, ...] = ()  # prefer these topics for each story's label (the briefing's focus)


def _ts(a: Article) -> datetime:
    return a.published or datetime.min.replace(tzinfo=timezone.utc)


def plan_briefing(articles: list[Article], now: datetime, *, hours: int = 24, focus: list[str] | None = None,
                  focus_names: str = "", min_focus: int = 5, max_focus: int = 12, catchup_days: int = 14,
                  general: int = 3, limit: int = 15, already_sent: set[str] | None = None) -> list[Section]:
    """Choose what goes in the briefing.

    Without ``focus``: the day's most widely reported stories. With ``focus`` (topic ids such as
    ``ics-ot``): the day's stories on those topics first; if there are fewer than ``min_focus``, older
    focus stories from the last ``catchup_days`` that haven't been sent before; then the ``general``
    biggest other stories of the day so nothing major is missed.
    """
    sent = already_sent or set()
    since = now - timedelta(hours=hours)
    fresh = [a for a in articles if a.published and a.published >= since and a.link not in sent]
    if not focus:
        return [Section("Today's top security stories", pick_stories(fresh, limit))]

    wanted = set(focus)
    is_focus = lambda a: bool(wanted & set(a.topics))  # noqa: E731
    # Primary-topic matches first, then most widely reported, then newest.
    rank = lambda a: (a.primary_topic in wanted, len(a.also_reported_by), _ts(a))  # noqa: E731
    today = sorted((a for a in fresh if is_focus(a)), key=rank, reverse=True)[:max_focus]
    sections = [Section(f"Today in {focus_names}", today,
                        "" if today else f"No new {focus_names} stories in the last {hours} hours.",
                        label_topics=tuple(focus))]

    if len(today) < min_focus:
        cutoff = now - timedelta(days=catchup_days)
        shown = {a.link for a in today}
        older = [a for a in articles if a.published and cutoff <= a.published < since and is_focus(a)
                 and a.link not in sent and a.link not in shown]
        older.sort(key=_ts, reverse=True)
        extra = older[: min_focus - len(today)]
        if extra:
            sections.append(Section("Catch up", extra, f"Recent {focus_names} stories you haven't had in a briefing yet.",
                                    catch_up=True, label_topics=tuple(focus)))
    if general:
        others = [a for a in fresh if not is_focus(a)]
        top = pick_stories(others, general)
        if top:
            sections.append(Section("Also in security today", top))
    return sections


# --------------------------------------------------------------------------- sent log


def load_sent(path: Path) -> dict:
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def record_sent(path: Path, links: list[str], day: str, keep_days: int = 60) -> None:
    """Remember what went out (so catch-up never repeats a story) and on which day."""
    import json
    log = load_sent(path)
    sent = log.get("links", {})
    for link in links:
        sent[link] = day
    cutoff = (datetime.fromisoformat(day) - timedelta(days=keep_days)).date().isoformat()
    log = {"last_sent_day": day, "links": {k: v for k, v in sent.items() if v >= cutoff}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- rendering


def _story_rows(stories: list[Article], taxonomy: Taxonomy, catch_up: bool, label_topics: tuple[str, ...] = ()) -> str:
    e = html.escape
    color = {t.id: t.color or NAVY for t in taxonomy.topics}
    rows = []
    for a in stories:
        topic = next((t for t in a.topics if t in label_topics), a.primary_topic)
        label = taxonomy.name_of(topic) if topic != "general" else "Security"
        when = f" &middot; {a.published.strftime('%d %b')}" if catch_up and a.published else ""
        also = f" &middot; also reported by {e(', '.join(a.also_reported_by))}" if a.also_reported_by else ""
        cves = f"<br><span style=\"color:#b91c1c;font-size:12px\">{e(', '.join(a.cves[:6]))}</span>" if a.cves else ""
        img = (f'<td width="120" valign="top" style="padding:0 14px 0 0"><img src="{e(a.image)}" width="120" alt="" '
               f'style="display:block;width:120px;height:auto;border:0"></td>') if a.image.startswith("https://") else ""
        rows.append(f"""
<tr><td style="padding:16px 0;border-bottom:1px solid #e5e5e5">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{img}<td valign="top">
    <div style="font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:{color.get(topic, NAVY)}">{e(label)}</div>
    <a href="{e(a.link)}" style="display:block;margin:4px 0;font-size:16px;font-weight:700;line-height:1.3;color:#000;text-decoration:none">{e(a.title)}</a>
    <div style="font-size:12px;color:{NAVY}">{e(a.source)}{when}{also}</div>
    <div style="margin-top:6px;font-size:13px;line-height:1.45;color:#333">{e(a.summary[:320])}{'…' if len(a.summary) > 320 else ''}</div>{cves}
  </td></tr></table>
</td></tr>""")
    return "".join(rows)


def _sections_html(sections: list[Section], taxonomy: Taxonomy) -> str:
    e = html.escape
    parts = []
    for sec in sections:
        if not sec.stories and not sec.note:
            continue
        parts.append(f'<div style="margin:22px 0 2px;padding-top:8px;border-top:3px solid {NAVY};font-size:17px;'
                     f'font-weight:800;color:#000">{e(sec.heading)}</div>')
        if sec.note:
            parts.append(f'<div style="font-size:13px;font-style:italic;color:{MAROON_TEXT};margin:2px 0 4px">{e(sec.note)}</div>')
        if sec.stories:
            parts.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                         f'{_story_rows(sec.stories, taxonomy, sec.catch_up, sec.label_topics)}</table>')
    return "\n".join(parts) or '<p style="color:#666">No stories in this period.</p>'


def build_html(sections: list[Section] | list[Article], taxonomy: Taxonomy, title: str, date: datetime,
               fragment: bool = False) -> str:
    """The briefing as a complete email, or (fragment=True) just the content for a newsletter service,
    which adds its own header, footer and unsubscribe link."""
    if sections and isinstance(sections[0], Article):
        sections = [Section("Today's top security stories", list(sections))]
    e = html.escape
    content = _sections_html(sections, taxonomy)
    dateline = date.strftime("%A %d %B %Y")
    if fragment:
        return (f'<p style="font-size:14px;font-weight:700;font-style:italic;color:{MAROON_TEXT}">{e(title)} &middot; {dateline}</p>\n'
                + content)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Inter,Segoe UI,Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4"><tr><td align="center" style="padding:20px 10px">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;background:#fff">
  <tr><td style="background:{MAROON};padding:18px 24px;color:#fff">
    <div style="font-family:Georgia,serif;font-size:26px">sanitised<span style="color:#9ab">.</span></div>
    <div style="font-size:13px;opacity:.9">{e(title)} &middot; {dateline}</div>
  </td></tr>
  <tr><td style="background:{NAVY};height:6px;font-size:0;line-height:0">&nbsp;</td></tr>
  <tr><td style="padding:0 24px 8px">{content}</td></tr>
  <tr><td style="padding:18px 24px;font-size:11px;color:#888">You're receiving this because you subscribed to the {e(title)}.
    Reply to this email to unsubscribe.</td></tr>
</table></td></tr></table></body></html>"""


def build_text(sections: list[Section] | list[Article], title: str, date: datetime) -> str:
    if sections and isinstance(sections[0], Article):
        sections = [Section("Today's top security stories", list(sections))]
    lines = [f"{title} - {date.strftime('%A %d %B %Y')}", ""]
    for sec in sections:
        if not sec.stories and not sec.note:
            continue
        lines += [sec.heading.upper(), "=" * len(sec.heading)]
        if sec.note:
            lines += [sec.note, ""]
        for a in sec.stories:
            when = f" ({a.published.strftime('%d %b')})" if sec.catch_up and a.published else ""
            lines += [a.title, f"{a.source}{when} - {a.link}", a.summary[:320], ""]
    lines.append("Reply to this email to unsubscribe.")
    return "\n".join(lines)


def all_stories(sections: list[Section]) -> list[Article]:
    return [a for sec in sections for a in sec.stories]


# --------------------------------------------------------------------------- Buttondown (newsletter service)

BUTTONDOWN_API = "https://api.buttondown.com/v1"


class ButtondownError(RuntimeError):
    pass


def buttondown_settings() -> dict | None:
    """{'api_key', 'username'} when the briefing should go out through Buttondown, else None."""
    cfg = load_email_config()
    key = os.environ.get("SECNEWS_BUTTONDOWN_API_KEY") or (cfg.get("buttondown_api_key") if cfg.get("method") == "buttondown" else "")
    if not key:
        return None
    return {"api_key": key, "username": os.environ.get("SECNEWS_BUTTONDOWN_USERNAME") or cfg.get("buttondown_username", "")}


def buttondown_subscribe_url(username: str) -> str:
    return f"https://buttondown.com/api/emails/embed-subscribe/{username}"


def _buttondown(method: str, path: str, api_key: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        f"{BUTTONDOWN_API}{path}", method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Token {api_key}", "Content-Type": "application/json",
                 "User-Agent": "secnews", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https API host
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code == 401:
            raise ButtondownError("Buttondown rejected the API key. Copy it again from Buttondown > Settings > API.") from None
        if exc.code in (402, 403):
            raise ButtondownError(
                "Buttondown won't let this account use the API (it may need a paid plan). You can still send the "
                "briefing for free: run 'secnews briefing --paste' and paste the result into a new Buttondown email. "
                f"Details: {detail}") from None
        raise ButtondownError(f"Buttondown returned HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise ButtondownError(f"couldn't reach Buttondown: {exc.reason}") from None
    return json.loads(raw or b"{}")


def buttondown_check(api_key: str) -> int:
    """Validate the key; returns the current subscriber count."""
    data = _buttondown("GET", "/subscribers?page_size=1", api_key)
    return int(data.get("count", len(data.get("results", []))))


def buttondown_send(api_key: str, subject: str, body_html: str, draft: bool = False,
                    send_at: datetime | None = None) -> dict:
    """Create the briefing in Buttondown and send it to every subscriber now, at ``send_at``
    (Buttondown holds it and sends it then), or leave it as a draft."""
    payload = {"subject": subject, "body": body_html, "status": "draft" if draft else "about_to_send"}
    if send_at and not draft:
        payload["status"] = "scheduled"
        payload["publish_date"] = send_at.astimezone(timezone.utc).isoformat()
    # Buttondown requires this explicit confirmation before an API call may email subscribers.
    headers = {} if draft else {"X-Buttondown-Live-Dangerously": "true"}
    return _buttondown("POST", "/emails", api_key, payload, headers)


# --------------------------------------------------------------------------- sending over SMTP


class SmtpConfigError(RuntimeError):
    pass


# Common providers, offered by `secnews email setup`.
PROVIDERS = {
    "office365": {"label": "Microsoft 365 / Office 365 work account", "host": "smtp.office365.com", "port": 587, "security": "starttls",
                  "tip": "Your IT team may need to enable 'Authenticated SMTP' for this mailbox."},
    "gmail": {"label": "Gmail / Google Workspace", "host": "smtp.gmail.com", "port": 587, "security": "starttls",
              "tip": "Use an App Password (Google Account > Security > App passwords), not your normal password."},
    "outlook": {"label": "Outlook.com / Hotmail", "host": "smtp-mail.outlook.com", "port": 587, "security": "starttls",
                "tip": "Microsoft is phasing out password sign-in for personal accounts; if it's refused, use another provider."},
    "other": {"label": "Other (enter the server details yourself)", "host": "", "port": 587, "security": "starttls", "tip": ""},
}


def email_config_path() -> Path:
    return Path(os.environ.get("SECNEWS_EMAIL_CONFIG") or Path.home() / ".secnews" / "email.json")


def load_email_config(path: Path | None = None) -> dict:
    import json
    try:
        return json.loads((path or email_config_path()).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_email_config(cfg: dict, path: Path | None = None) -> Path:
    import json
    path = path or email_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created owner-only (0600) because it may hold the mailbox password.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:  # e.g. Windows, where the user profile folder is already private
        pass
    return path


def smtp_settings() -> dict:
    """Email settings from ~/.secnews/email.json, overridden by SECNEWS_SMTP_* environment variables."""
    cfg, env = load_email_config(), os.environ
    def get(key: str, env_name: str, default="") -> str:
        if env.get(env_name):
            return env[env_name]
        value = cfg.get(key)
        return str(default) if value is None or value == "" else str(value)  # keeps a saved False/0
    s = {
        "host": get("host", "SECNEWS_SMTP_HOST"),
        "port": int(get("port", "SECNEWS_SMTP_PORT", 587)),
        "user": get("user", "SECNEWS_SMTP_USER"),
        "password": get("password", "SECNEWS_SMTP_PASSWORD"),
        "security": get("security", "SECNEWS_SMTP_SECURITY", "starttls").lower(),  # starttls | ssl | none
    }
    s["sender"] = get("sender", "SECNEWS_SMTP_FROM") or s["user"]
    missing = [k for k in ("host", "sender") if not s[k]]
    if missing:
        raise SmtpConfigError("no email server set up. Run 'secnews email setup' (or set SECNEWS_SMTP_HOST and SECNEWS_SMTP_FROM)")
    if s["user"] and not s["password"]:
        raise SmtpConfigError("no password. Run 'secnews email setup' again or set SECNEWS_SMTP_PASSWORD")
    return s


def send(recipients: list[str], subject: str, html_body: str, text_body: str, settings: dict) -> list[str]:
    """Send one message per recipient (so nobody sees the list). Returns addresses that failed."""
    ctx = ssl.create_default_context()
    if settings["security"] == "ssl":
        server = smtplib.SMTP_SSL(settings["host"], settings["port"], context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(settings["host"], settings["port"], timeout=30)
    failed = []
    with server as conn:
        if settings["security"] == "starttls":
            conn.starttls(context=ctx)
        if settings["user"]:
            conn.login(settings["user"], settings["password"])
        for rcpt in recipients:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = subject, settings["sender"], rcpt
            msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid()
            msg["List-Unsubscribe"] = f"<mailto:{settings['sender']}?subject=unsubscribe>"
            msg.set_content(text_body)
            msg.add_alternative(html_body, subtype="html")
            try:
                conn.send_message(msg)
            except smtplib.SMTPException:
                failed.append(rcpt)
    return failed
