"""Outbound email.

Kept in one module so ``smtplib`` never appears in a request handler and tests
have a single seam to replace - the same shape as ``synth.build_communicate``.

Sending is blocking; callers wrap it in ``run_in_threadpool``.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Optional, Protocol

__all__ = [
    "Mailer",
    "SmtpMailer",
    "ConsoleMailer",
    "MailError",
    "build_verification_email",
    "mailer_from_settings",
]

log = logging.getLogger("hebrew_voice.mailer")


class MailError(Exception):
    """Raised when a message could not be handed to the mail server."""


class Mailer(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class SmtpMailer:
    """Sends over SMTP with STARTTLS - i.e. Gmail on port 587."""

    def __init__(
        self,
        host: str,
        port: int = 587,
        *,
        username: str = "",
        password: str = "",
        use_starttls: bool = True,
        timeout: int = 15,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_starttls = use_starttls
        self.timeout = timeout

    def send(self, message: EmailMessage) -> None:
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                smtp.ehlo()
                if self.use_starttls:
                    smtp.starttls()
                    smtp.ehlo()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            # Includes authentication failures, which on Gmail almost always
            # mean the app password is wrong or 2-Step Verification is off.
            raise MailError(f"could not send mail via {self.host}:{self.port}: {exc}") from exc
        log.info("sent %r to %s", message["Subject"], message["To"])


class ConsoleMailer:
    """Logs the message instead of sending it.

    Used when no SMTP host is configured outside production, so local
    development and the test suite work without a mail server. The verification
    link is logged in full, which is how you complete a signup locally.
    """

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)
        body = _plain_part(message)
        log.warning(
            "[ConsoleMailer] no SMTP configured; message not sent\n"
            "  To:      %s\n  Subject: %s\n%s",
            message["To"],
            message["Subject"],
            "\n".join(f"  {line}" for line in body.splitlines() if line.strip()),
        )


def _plain_part(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain",))
    return part.get_content() if part else ""


def build_verification_email(
    *,
    to: str,
    link: str,
    expires_hours: int,
    from_address: str,
    from_name: str = "",
    app_name: str = "מחולל קול עברי",
) -> EmailMessage:
    """The Hebrew "confirm your address" message.

    Carries a plain-text alternative as well as HTML: a message with no text
    part scores badly with spam filters, and some clients only render text.
    """
    message = EmailMessage()
    message["Subject"] = f"אימות כתובת האימייל · {app_name}"
    message["From"] = formataddr((from_name or app_name, from_address))
    message["To"] = to
    message["Message-ID"] = make_msgid()
    message["Auto-Submitted"] = "auto-generated"

    message.set_content(
        f"""שלום,

נרשמתם ל{app_name}. כדי להפעיל את החשבון, פתחו את הקישור הבא:

{link}

הקישור תקף למשך {expires_hours} שעות וניתן לשימוש פעם אחת בלבד.

אם לא נרשמתם, אפשר להתעלם מההודעה - לא נוצר חשבון פעיל.
""",
        subtype="plain",
        charset="utf-8",
    )

    message.add_alternative(
        f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
  <body style="margin:0;padding:24px;background:#f5f6f8;
               font-family:'Segoe UI',system-ui,-apple-system,sans-serif;color:#16181d;">
    <div style="max-width:520px;margin:0 auto;background:#ffffff;border:1px solid #dce0e6;
                border-radius:14px;padding:32px;">
      <h1 style="margin:0 0 8px;font-size:20px;">{app_name}</h1>
      <p style="margin:0 0 24px;color:#626873;font-size:14px;">אימות כתובת האימייל</p>
      <p style="margin:0 0 24px;font-size:15px;line-height:1.6;">
        שלום, נרשמתם לשירות. כדי להפעיל את החשבון לחצו על הכפתור:
      </p>
      <p style="margin:0 0 24px;">
        <a href="{link}"
           style="display:inline-block;padding:12px 24px;background:#2f5fd8;color:#ffffff;
                  text-decoration:none;border-radius:8px;font-weight:600;font-size:15px;">
          אימות כתובת האימייל
        </a>
      </p>
      <p style="margin:0 0 8px;color:#626873;font-size:13px;line-height:1.6;">
        הקישור תקף {expires_hours} שעות וניתן לשימוש פעם אחת בלבד.
        אם הכפתור אינו עובד, העתיקו את הכתובת הזו לדפדפן:
      </p>
      <p style="margin:0 0 24px;word-break:break-all;font-size:12px;color:#8b919c;"
         dir="ltr">{link}</p>
      <p style="margin:0;color:#8b919c;font-size:12px;line-height:1.6;">
        אם לא נרשמתם, אפשר להתעלם מההודעה - לא נוצר חשבון פעיל.
      </p>
    </div>
  </body>
</html>
""",
        subtype="html",
        charset="utf-8",
    )
    return message


def mailer_from_settings(settings) -> Mailer:
    """Pick a mailer from configuration.

    No SMTP host means the console mailer; :meth:`Settings.validate` already
    refuses that combination in production when verification is required, so
    this can only fall back during development and tests.
    """
    if not settings.smtp_host:
        return ConsoleMailer()
    return SmtpMailer(
        settings.smtp_host,
        settings.smtp_port,
        username=settings.smtp_user,
        password=settings.smtp_password,
        use_starttls=settings.smtp_starttls,
        timeout=settings.smtp_timeout,
    )
