"""Tell you (and requesters) about meeting requests: phone push, email, Discord.

Each channel can be switched on or off on its own in the settings panel. Sending
happens in a background thread so a slow mail server never holds up a page, and the
last result of each channel is kept for the panel to show.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import httpx

from .config import AppConfig, EmailCfg
from .sources import USER_AGENT

log = logging.getLogger("kalends.notify")
CHANNELS = ("ntfy", "email", "discord")


@dataclass
class Note:
    title: str
    body: str
    url: str | None = None  # where to act on it (the settings panel)
    tags: str = "calendar"  # ntfy emoji tags


@dataclass
class ChannelResult:
    ok: bool
    detail: str
    at: datetime


def invite_ics(
    uid: str,
    start: datetime,
    end: datetime,
    summary: str,
    description: str = "",
    method: str = "PUBLISH",
    sequence: int = 0,
    status: str = "CONFIRMED",
    location: str | None = None,
    url: str | None = None,
) -> str:
    """A one-event calendar file people can open to add the meeting."""

    def fmt(dt: datetime) -> str:
        return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    def esc(text: str) -> str:
        return (
            text.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\r", "")
            .replace("\n", "\\n")
        )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Kalends//Meeting request//EN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{fmt(datetime.now(UTC))}",
        f"DTSTART:{fmt(start)}",
        f"DTEND:{fmt(end)}",
        f"SEQUENCE:{sequence}",
        f"STATUS:{status}",
        f"SUMMARY:{esc(summary)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{esc(description)}")
    if location:
        lines.append(f"LOCATION:{esc(location)}")
    if url and "\n" not in url and "\r" not in url:
        lines.append(f"URL:{url}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(_fold(line) for line in lines)


def _fold(line: str) -> str:
    """Fold long iCalendar lines at 75 octets, as the format requires."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


class Notifier:
    def __init__(self, cfg_getter):
        self._cfg = cfg_getter  # a function, so saved settings apply without a restart
        self.last: dict[str, ChannelResult] = {}
        self._lock = threading.Lock()

    @property
    def cfg(self) -> AppConfig:
        return self._cfg()

    def enabled(self) -> list[str]:
        n = self.cfg.notify
        out = []
        if n.ntfy.enabled and n.ntfy.topic:
            out.append("ntfy")
        if n.email.enabled and n.email.smtp_host and n.email.owner_addr:
            out.append("email")
        if n.discord.enabled and n.discord.webhook_url:
            out.append("discord")
        return out

    def can_email_requesters(self) -> bool:
        e = self.cfg.notify.email
        return e.enabled and e.notify_requester and bool(e.smtp_host and e.sender)

    # -- sending ---------------------------------------------------------------------------

    def owner(self, note: Note, wait: bool = False) -> None:
        """Send to every channel that's switched on."""
        self._run(lambda: [self.send(ch, note) for ch in self.enabled()], wait)

    def requester(self, to: str, subject: str, body: str, ics: str | None = None, wait: bool = False) -> None:
        if not to or not self.can_email_requesters():
            return
        self._run(lambda: self._email(to, subject, body, ics, record=False), wait)

    def send(self, channel: str, note: Note) -> ChannelResult:
        try:
            if channel == "ntfy":
                detail = self._ntfy(note)
            elif channel == "discord":
                detail = self._discord(note)
            elif channel == "email":
                to = self.cfg.notify.email.owner_addr
                if not to:
                    raise ValueError("no address to send to")
                body = note.body + (f"\n\nOpen Kalends: {note.url}" if note.url else "")
                detail = self._email(to, note.title, body, None)
            else:
                raise ValueError(channel)
            res = ChannelResult(True, detail, datetime.now(UTC))
        except Exception as exc:
            res = ChannelResult(False, _safe_error(exc), datetime.now(UTC))
            log.warning("%s notification failed: %s", channel, res.detail)
        with self._lock:
            self.last[channel] = res
        return res

    def _run(self, fn, wait: bool) -> None:
        if wait:
            fn()
        else:
            threading.Thread(target=fn, daemon=True, name="kalends-notify").start()

    def _ntfy(self, note: Note) -> str:
        n = self.cfg.notify.ntfy
        headers = {"Title": _header(note.title), "Tags": note.tags, "User-Agent": USER_AGENT}
        if note.url:
            headers["Click"] = note.url
        if n.token:
            headers["Authorization"] = f"Bearer {n.token}"
        r = httpx.post(
            f"{n.server}/{n.topic}", content=note.body.encode("utf-8"), headers=headers, timeout=15
        )
        r.raise_for_status()
        return "Sent to your phone"

    def _discord(self, note: Note) -> str:
        url = self.cfg.notify.discord.webhook_url or ""
        text = f"**{note.title}**\n{note.body}"
        if note.url:
            text += f"\n<{note.url}>"
        payload = {"content": text[:1900], "allowed_mentions": {"parse": []}, "username": "Kalends"}
        r = httpx.post(url, json=payload, timeout=15, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        return "Posted to Discord"

    def _email(self, to: str, subject: str, body: str, ics: str | None, record: bool = True) -> str:
        e: EmailCfg = self.cfg.notify.email
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr(("Kalends", e.sender or ""))
        msg["To"] = to
        msg["Message-ID"] = make_msgid(domain=(e.sender or "kalends.local").split("@")[-1])
        msg.set_content(body)
        if ics:
            msg.add_attachment(
                ics.encode("utf-8"),
                maintype="text",
                subtype="calendar",
                filename="meeting.ics",
                params={"method": "PUBLISH"},
            )
        host, port = e.smtp_host or "", e.smtp_port
        ctx = ssl.create_default_context()
        if e.security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=ctx, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        try:
            if e.security == "starttls":
                server.starttls(context=ctx)
            if e.username and e.password:
                server.login(e.username, e.password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except smtplib.SMTPException:
                pass
        return f"Emailed {to}"


def _header(text: str) -> str:
    """HTTP headers must be plain ASCII-ish; ntfy also accepts RFC 2047 but keep it simple."""
    return text.encode("ascii", "replace").decode("ascii")[:200]


def _safe_error(exc: Exception) -> str:
    """A readable error without secrets (webhook links, topics, passwords)."""
    import re

    text = f"{type(exc).__name__}: {exc}"
    text = re.sub(r"https?://\S+", "<link>", text)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "The mail server refused the username or password (for Gmail, use an app password)."
    if isinstance(exc, httpx.HTTPStatusError):
        return f"The service answered HTTP {exc.response.status_code}."
    if isinstance(exc, httpx.HTTPError | OSError):
        return f"Couldn't reach it ({type(exc).__name__})."
    return text[:200]


def new_uid(domain: str = "glimpse") -> str:
    return f"{uuid.uuid4()}@{domain}"
