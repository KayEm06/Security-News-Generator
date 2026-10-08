"""Daily briefing email: build it from recent articles and send it to a subscriber list over SMTP."""

from __future__ import annotations

import html
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
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


def build_html(articles: list[Article], taxonomy: Taxonomy, title: str, date: datetime) -> str:
    e = html.escape
    color = {t.id: t.color or NAVY for t in taxonomy.topics}
    rows = []
    for a in articles:
        topic = a.primary_topic
        label = taxonomy.name_of(topic) if topic != "general" else "Security"
        also = f" &middot; also reported by {e(', '.join(a.also_reported_by))}" if a.also_reported_by else ""
        cves = f"<br><span style=\"color:#b91c1c;font-size:12px\">{e(', '.join(a.cves[:6]))}</span>" if a.cves else ""
        img = (f'<td width="120" valign="top" style="padding:0 14px 0 0"><img src="{e(a.image)}" width="120" alt="" '
               f'style="display:block;width:120px;height:auto;border:0"></td>') if a.image.startswith("https://") else ""
        rows.append(f"""
<tr><td style="padding:16px 0;border-bottom:1px solid #e5e5e5">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{img}<td valign="top">
    <div style="font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:{color.get(topic, NAVY)}">{e(label)}</div>
    <a href="{e(a.link)}" style="display:block;margin:4px 0;font-size:16px;font-weight:700;line-height:1.3;color:#000;text-decoration:none">{e(a.title)}</a>
    <div style="font-size:12px;color:{NAVY}">{e(a.source)}{also}</div>
    <div style="margin-top:6px;font-size:13px;line-height:1.45;color:#333">{e(a.summary[:320])}{'…' if len(a.summary) > 320 else ''}</div>{cves}
  </td></tr></table>
</td></tr>""")
    body = "".join(rows) or '<tr><td style="padding:24px 0;color:#666">No new stories in this period.</td></tr>'
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Inter,Segoe UI,Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4"><tr><td align="center" style="padding:20px 10px">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;background:#fff">
  <tr><td style="background:{MAROON};padding:18px 24px;color:#fff">
    <div style="font-family:Georgia,serif;font-size:26px">sanitised<span style="color:#9ab">.</span></div>
    <div style="font-size:13px;opacity:.9">{e(title)} &middot; {date.strftime('%A %d %B %Y')}</div>
  </td></tr>
  <tr><td style="background:{NAVY};height:6px;font-size:0;line-height:0">&nbsp;</td></tr>
  <tr><td style="padding:8px 24px 0">
    <div style="margin:14px 0 2px;font-size:14px;font-weight:700;font-style:italic;color:{MAROON_TEXT}">Today's top {len(articles)} security stories</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{body}</table>
  </td></tr>
  <tr><td style="padding:18px 24px;font-size:11px;color:#888">You're receiving this because you subscribed to the {e(title)}.
    Reply to this email to unsubscribe.</td></tr>
</table></td></tr></table></body></html>"""


def build_text(articles: list[Article], title: str, date: datetime) -> str:
    lines = [f"{title} - {date.strftime('%A %d %B %Y')}", ""]
    for a in articles:
        lines += [a.title, f"{a.source} - {a.link}", a.summary[:320], ""]
    lines.append("Reply to this email to unsubscribe.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- sending


class SmtpConfigError(RuntimeError):
    pass


def smtp_settings() -> dict:
    env = os.environ
    s = {
        "host": env.get("SECNEWS_SMTP_HOST", ""),
        "port": int(env.get("SECNEWS_SMTP_PORT", "587")),
        "user": env.get("SECNEWS_SMTP_USER", ""),
        "password": env.get("SECNEWS_SMTP_PASSWORD", ""),
        "sender": env.get("SECNEWS_SMTP_FROM") or env.get("SECNEWS_SMTP_USER", ""),
        "security": env.get("SECNEWS_SMTP_SECURITY", "starttls").lower(),  # starttls | ssl | none
    }
    missing = [k for k in ("host", "sender") if not s[k]]
    if missing:
        raise SmtpConfigError("set " + " and ".join(f"SECNEWS_SMTP_{'FROM' if k == 'sender' else k.upper()}" for k in missing))
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
