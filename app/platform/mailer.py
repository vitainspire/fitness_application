"""Transactional email — one seam every part of the app sends through.

Configurable with SMTP provider or defaults to ayush.kumar@vitainspire.com.
In development/test without SMTP configured, it logs the full email and link.
In production with SMTP configured, it sends real emails via SMTP/STARTTLS/SSL.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

_log = logging.getLogger("mailer")

DEFAULT_SENDER_EMAIL = "ayush.kumar@vitainspire.com"
DEFAULT_SENDER_NAME = "ViTAinspire"


def build_password_reset_html(
    *,
    reset_url: str,
    token: str,
    sender_email: str = DEFAULT_SENDER_EMAIL,
    min_length: int = 10,
) -> str:
    """Build a modern, responsive HTML email for password reset."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Reset Your ViTAinspire Password</title>
</head>
<body style="margin: 0; padding: 0; background-color: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #f8fafc;">
  <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color: #0f172a; padding: 40px 15px;">
    <tr>
      <td align="center">
        <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 540px; background-color: #1e293b; border-radius: 20px; border: 1px solid rgba(255, 255, 255, 0.1); overflow: hidden; box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);">
          <!-- Header Banner -->
          <tr>
            <td style="padding: 36px 36px 20px; text-align: center; background: linear-gradient(135deg, rgba(82, 102, 0, 0.25) 0%, rgba(200, 243, 35, 0.15) 100%); border-bottom: 1px solid rgba(255, 255, 255, 0.08);">
              <h1 style="margin: 0; font-size: 26px; font-weight: 800; letter-spacing: -0.5px; color: #c8f323;">ViTAinspire</h1>
              <p style="margin: 6px 0 0; font-size: 13px; color: #94a3b8; font-weight: 500; text-transform: uppercase; letter-spacing: 1px;">Smart Rehab & Fitness Platform</p>
            </td>
          </tr>

          <!-- Content Body -->
          <tr>
            <td style="padding: 32px 36px 28px;">
              <h2 style="margin: 0 0 16px; font-size: 22px; font-weight: 700; color: #f8fafc; letter-spacing: -0.3px;">Reset Your Password</h2>
              <p style="margin: 0 0 24px; font-size: 15px; line-height: 1.6; color: #cbd5e1;">
                We received a request to reset the password for your ViTAinspire account. Click the button below to set a new password:
              </p>

              <!-- CTA Button -->
              <table role="presentation" border="0" cellpadding="0" cellspacing="0" style="margin: 0 auto 28px;">
                <tr>
                  <td align="center" style="border-radius: 9999px; background: #c8f323;">
                    <a href="{reset_url}" target="_blank" style="display: inline-block; padding: 15px 36px; font-size: 15px; font-weight: 700; color: #171e00; text-decoration: none; border-radius: 9999px; box-shadow: 0 6px 18px rgba(200, 243, 35, 0.35); text-align: center;">
                      Reset My Password
                    </a>
                  </td>
                </tr>
              </table>

              <!-- Password Requirements Box -->
              <div style="background-color: #0f172a; border-radius: 12px; padding: 18px 20px; border: 1px solid rgba(255, 255, 255, 0.07); margin-bottom: 24px;">
                <p style="margin: 0 0 10px; font-size: 13px; font-weight: 700; color: #e2e8f0;">Your new password must contain:</p>
                <ul style="margin: 0; padding-left: 20px; font-size: 13px; line-height: 1.7; color: #94a3b8;">
                  <li>At least <strong style="color: #f1f5f9;">1 capital (uppercase) letter</strong> (A&ndash;Z)</li>
                  <li>At least <strong style="color: #f1f5f9;">1 small (lowercase) letter</strong> (a&ndash;z)</li>
                  <li>At least <strong style="color: #f1f5f9;">1 number</strong> (0&ndash;9)</li>
                  <li>At least <strong style="color: #f1f5f9;">1 special character</strong> (!@#$%^&amp;*...)</li>
                  <li>Minimum <strong style="color: #f1f5f9;">{min_length} characters</strong></li>
                </ul>
              </div>

              <!-- Direct link fallback -->
              <p style="margin: 0 0 12px; font-size: 13px; color: #94a3b8; line-height: 1.5;">
                Button not working? Copy and paste this URL into your browser:
              </p>
              <p style="margin: 0 0 20px; font-size: 12px; word-break: break-all; background-color: #0f172a; padding: 10px 14px; border-radius: 8px; border: 1px solid rgba(255, 255, 255, 0.05);">
                <a href="{reset_url}" style="color: #38bdf8; text-decoration: underline;">{reset_url}</a>
              </p>

              <!-- One-time code fallback -->
              <p style="margin: 0 0 6px; font-size: 13px; color: #94a3b8;">Or enter this reset code manually:</p>
              <div style="background-color: #0f172a; border-radius: 8px; padding: 10px 14px; font-family: monospace; font-size: 14px; color: #c8f323; font-weight: bold; letter-spacing: 1px; word-break: break-all; border: 1px solid rgba(200, 243, 35, 0.2); margin-bottom: 24px;">
                {token}
              </div>

              <p style="margin: 0; font-size: 13px; color: #64748b; line-height: 1.5;">
                This reset link is valid for <strong>1 hour</strong>. If you did not request this password reset, please ignore this email; your account remains secure.
              </p>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding: 20px 36px 28px; background-color: #172033; border-top: 1px solid rgba(255, 255, 255, 0.06); text-align: center;">
              <p style="margin: 0 0 4px; font-size: 12px; color: #94a3b8;">Sent by ViTAinspire &bull; <a href="mailto:{sender_email}" style="color: #c8f323; text-decoration: none;">{sender_email}</a></p>
              <p style="margin: 0; font-size: 11px; color: #64748b;">ViTAinspire Health & Rehab Intelligence Platform</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def send_email(
    *,
    to: str,
    subject: str,
    body: str,
    html_body: str | None = None,
    from_email: str | None = None,
    from_name: str | None = None,
    is_production: bool = False,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_user: str | None = None,
    smtp_password: str | None = None,
    smtp_use_tls: bool | None = None,
    smtp_use_ssl: bool | None = None,
) -> bool:
    """Send transactional email.

    - Uses ayush.kumar@vitainspire.com by default.
    - If SMTP is configured (host & port in env or params), delivers via SMTP/STARTTLS/SSL.
    - If SMTP is not configured:
        * In dev/test: logs full message at INFO level (including token and link).
        * In production: logs at ERROR level without raising an uncaught exception.
    """
    sender = from_email or os.environ.get("MAIL_FROM") or os.environ.get("SMTP_FROM") or DEFAULT_SENDER_EMAIL
    sender_name = from_name or os.environ.get("MAIL_SENDER_NAME") or DEFAULT_SENDER_NAME

    host = (smtp_host if smtp_host is not None else os.environ.get("SMTP_HOST", "")).strip()
    port = smtp_port if smtp_port is not None else int(os.environ.get("SMTP_PORT", "587"))
    user = (smtp_user if smtp_user is not None else os.environ.get("SMTP_USER", "")).strip()
    password = (smtp_password if smtp_password is not None else os.environ.get("SMTP_PASSWORD", "")).strip()
    use_tls = smtp_use_tls if smtp_use_tls is not None else (os.environ.get("SMTP_USE_TLS", "true").lower() == "true")
    use_ssl = smtp_use_ssl if smtp_use_ssl is not None else (os.environ.get("SMTP_USE_SSL", "false").lower() == "true")

    if not host:
        if not is_production:
            _log.info(
                "MAILER (dev/test log) -> from=%s <%s> to=%s subject=%r body=%s",
                sender_name, sender, to, subject, body,
            )
            return True
        _log.error(
            "MAILER NOT CONFIGURED in production — email to %s was NOT sent: subject=%r",
            to, subject,
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    msg["To"] = to
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                if use_tls:
                    server.starttls()
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        _log.info("MAILER delivered -> from=%s to=%s subject=%r", sender, to, subject)
        return True
    except Exception as exc:
        _log.error("MAILER delivery failed to %s: %s", to, exc)
        if not is_production:
            _log.info(
                "MAILER fallback body -> from=%s <%s> to=%s subject=%r body=%s",
                sender_name, sender, to, subject, body,
            )
        return False
