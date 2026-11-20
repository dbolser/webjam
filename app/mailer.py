"""Plain-text email over SMTP (Gmail by default), for access-request notices.

Configure with:

    WEBJAM_SMTP_USER       e.g. webjam.bot@gmail.com (turns mail on)
    WEBJAM_SMTP_PASSWORD   for Gmail, an app password (Google account > Security > App passwords)
    WEBJAM_SMTP_HOST       default smtp.gmail.com
    WEBJAM_SMTP_PORT       default 587 (STARTTLS); 465 uses implicit TLS
    WEBJAM_MAIL_FROM       default WEBJAM_SMTP_USER

Without a user and password nothing is sent and send_mail() returns 'not configured'.
send_mail() blocks (up to the timeout), so call it from a thread or a background task. It never
raises and never logs the password.
"""

import logging
import os
import smtplib
import ssl
import threading
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger("webjam.mail")

SMTP_TIMEOUT = 10
SENT, FAILED, NOT_CONFIGURED = "sent", "failed", "not configured"

_warned_unconfigured = threading.Event()


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    mail_from: str


def header_safe(value: str) -> str:
    """No CR/LF (or other control characters) may reach a header."""
    return "".join(ch for ch in str(value or "") if ch >= " " and ch != "\x7f").strip()


def smtp_config() -> Optional[SmtpConfig]:
    user = header_safe(os.environ.get("WEBJAM_SMTP_USER", ""))
    password = os.environ.get("WEBJAM_SMTP_PASSWORD", "").strip()
    if not user or not password:
        return None
    try:
        port = int(os.environ.get("WEBJAM_SMTP_PORT", "").strip() or 587)
    except ValueError:
        port = 587
    return SmtpConfig(
        host=header_safe(os.environ.get("WEBJAM_SMTP_HOST", "")) or "smtp.gmail.com",
        port=port,
        user=user,
        password=password,
        mail_from=header_safe(os.environ.get("WEBJAM_MAIL_FROM", "")) or user,
    )


def send_mail(to: str, subject: str, body: str) -> str:
    """Send one plain-text message. Returns 'sent', 'failed' or 'not configured'.

    `subject` must be a fixed string: user-supplied text (names, notes) belongs in the body.
    """
    cfg = smtp_config()
    if cfg is None:
        if not _warned_unconfigured.is_set():
            _warned_unconfigured.set()
            logger.warning("Email is not configured (WEBJAM_SMTP_USER/WEBJAM_SMTP_PASSWORD); not sending.")
        return NOT_CONFIGURED
    msg = EmailMessage()
    msg["From"] = cfg.mail_from
    msg["To"] = header_safe(to)
    msg["Subject"] = header_safe(subject)
    msg.set_content(body)
    try:
        ctx = ssl.create_default_context()
        if cfg.port == 465:
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=SMTP_TIMEOUT, context=ctx) as smtp:
                smtp.login(cfg.user, cfg.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=SMTP_TIMEOUT) as smtp:
                smtp.starttls(context=ctx)
                smtp.login(cfg.user, cfg.password)
                smtp.send_message(msg)
    except Exception as exc:  # never raise into a request; the type is enough to diagnose
        logger.warning("Sending email via %s:%s failed: %s", cfg.host, cfg.port, type(exc).__name__)
        return FAILED
    return SENT
