"""Alert delivery: Telegram bot + email (SMTP over SSL). Failures never crash the caller."""
import smtplib
import ssl
from email.message import EmailMessage

import requests

from . import config


def telegram(text):
    if not (config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID):
        return "skipped (not configured)"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
            timeout=10)
        return "ok" if r.ok else f"error {r.status_code}: {r.text[:120]}"
    except requests.RequestException as exc:
        return f"error: {exc}"


def email(subject, body):
    if not (config.SMTP_USER and config.SMTP_PASS and config.ALERT_EMAIL_TO):
        return "skipped (not configured)"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_USER
    msg["To"] = config.ALERT_EMAIL_TO
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                              context=ssl.create_default_context(), timeout=15) as s:
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.send_message(msg)
        return "ok"
    except Exception as exc:  # noqa: BLE001 - report any SMTP problem
        return f"error: {exc}"


def alert(subject, body):
    """Send to every configured channel. Returns a short delivery summary."""
    text = f"{subject}\n{body}"
    result = {"telegram": telegram(text), "email": email(f"[Smart Campus] {subject}", body)}
    print(f"[alert] {subject} -> {result}", flush=True)
    return result
