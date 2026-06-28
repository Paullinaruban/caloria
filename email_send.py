"""Transactional email via Resend (stdlib HTTP only).

Sends verification & password-reset emails. If RESEND_API_KEY is not configured
the email is logged to the server console instead of sent (safe for local dev) —
the rest of the flow still works so you can copy the link from the log.

Templates are intentionally plain, branded, and free of tracking. We never put
the raw token anywhere except the one-time link.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import config

_RESEND_URL = "https://api.resend.com/emails"


class EmailError(RuntimeError):
    pass


def _send(to: str, subject: str, html: str, text: str) -> str | None:
    """Send one email via Resend. Returns the Resend message id on success (and
    logs it for a delivery audit trail), or None in the dev/console fallback."""
    if not config.email_ready():
        # Dev fallback — surface the message so the flow is testable without a key.
        print(f"[caloria][EMAIL:dev] to={to} subject={subject!r}\n{text}\n")
        return None
    payload = {
        "from": config.EMAIL_FROM,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }
    req = urllib.request.Request(
        _RESEND_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.RESEND_API_KEY}",
            "Content-Type": "application/json",
            # Resend is behind Cloudflare, which 403s the default Python-urllib
            # User-Agent (error 1010). A normal UA is required for delivery.
            "User-Agent": "Caloria/1.0 (+https://caloria.app)",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=config.EMAIL_TIMEOUT)
        body = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        raise EmailError(f"Email provider error {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise EmailError(f"Could not reach email provider: {e.reason}") from e
    try:
        mid = json.loads(body).get("id")
    except (ValueError, AttributeError):
        mid = None
    print(f"[caloria][EMAIL:sent] to={to} id={mid} subject={subject!r}")
    return mid


def _shell(title: str, body_html: str) -> str:
    return (
        '<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:480px;'
        'margin:0 auto;padding:28px;color:#3a2937">'
        f'<h1 style="font-size:22px;color:#f24d8c;margin:0 0 4px">Caloria</h1>'
        f'<h2 style="font-size:18px;margin:18px 0 10px">{title}</h2>'
        f'{body_html}'
        '<p style="color:#8a7886;font-size:12px;margin-top:28px">'
        "If you didn't request this, you can safely ignore this email.</p></div>"
    )


def _button(url: str, label: str) -> str:
    return (
        f'<p style="margin:22px 0"><a href="{url}" '
        'style="background:#f24d8c;color:#fff;text-decoration:none;padding:12px 22px;'
        'border-radius:100px;font-weight:600;display:inline-block">'
        f'{label}</a></p><p style="color:#8a7886;font-size:13px">Or paste this link:<br>'
        f'<a href="{url}" style="color:#2bb5c9;word-break:break-all">{url}</a></p>'
    )


def send_verification(to: str, link: str) -> None:
    html = _shell(
        "Confirm your email",
        "<p>Welcome to Caloria — your private wellness club. Confirm your email to "
        "activate your account.</p>" + _button(link, "Verify my email")
        + f"<p style='color:#8a7886;font-size:12px'>This link expires in "
          f"{config.VERIFY_TOKEN_TTL_HOURS} hours.</p>",
    )
    text = (
        "Welcome to Caloria! Confirm your email to activate your account:\n"
        f"{link}\nThis link expires in {config.VERIFY_TOKEN_TTL_HOURS} hours."
    )
    _send(to, "Confirm your Caloria email", html, text)


def send_verification_code(to: str, code: str) -> None:
    code_html = (
        '<div style="font-family:-apple-system,Segoe UI,sans-serif;font-size:34px;'
        'font-weight:700;letter-spacing:10px;color:#2a2230;background:#fff;'
        'border:1px solid #efe6ec;border-radius:14px;padding:18px 0;text-align:center;'
        f'margin:18px 0">{code}</div>'
    )
    html = _shell(
        "Your verification code",
        "<p>Welcome to Caloria — your private wellness club. Enter this code to "
        "verify your email and activate your account:</p>" + code_html
        + f"<p style='color:#8a7886;font-size:12px'>This code expires in "
          f"{config.VERIFY_CODE_TTL_MINUTES} minutes. Don't share it with anyone.</p>",
    )
    text = (
        f"Welcome to Caloria! Your email verification code is: {code}\n"
        f"It expires in {config.VERIFY_CODE_TTL_MINUTES} minutes. Don't share it with anyone."
    )
    _send(to, "Your Caloria verification code", html, text)


def _open(label="Open Caloria"):
    return _button(config.APP_BASE_URL, label)


def send_sunday_reset(to: str, name: str, data: dict) -> None:
    rows = "".join(
        f'<tr><td style="padding:6px 0;color:#8a7886">{k}</td>'
        f'<td style="padding:6px 0;text-align:right;font-weight:600">{v}</td></tr>'
        for k, v in data["stats"]
    )
    wins = "".join(f"<li>{w}</li>" for w in data["wins"])
    html = _shell(
        f"Your Sunday Reset 🌙",
        f"<p>Hi {name or 'there'}, here's your week in review.</p>"
        f'<table style="width:100%;border-collapse:collapse;margin:8px 0 16px">{rows}</table>'
        f"<p style='font-weight:600;margin:0 0 4px'>This week's wins</p><ul>{wins}</ul>"
        f'<div style="background:#fff4fa;border-radius:12px;padding:14px;margin:16px 0">'
        f'<b style="color:#f24d8c">✨ Future You</b><p style="margin:6px 0 0">{data["future_you"]}</p></div>'
        f"<p style='color:#8a7886'>Next week's focus: {data['focus']}</p>" + _open("See my full reset"))
    text = f"Your Sunday Reset\n" + "\n".join(f"{k}: {v}" for k, v in data["stats"]) + \
        f"\n\nFuture You: {data['future_you']}\n{config.APP_BASE_URL}"
    _send(to, "Your Sunday Reset is ready 🌙", html, text)


def send_midweek(to: str, name: str, body: str) -> None:
    html = _shell("A midweek note 💫",
                  f"<p>Hi {name or 'there'},</p><p>{body}</p>" + _open("Continue my week"))
    _send(to, "A little midweek encouragement 💫", html, f"{body}\n{config.APP_BASE_URL}")


def send_reengagement(to: str, name: str, body: str) -> None:
    html = _shell("Future You is waiting 💗",
                  f"<p>Hi {name or 'there'},</p><p>{body}</p>" + _open("Come back to Caloria"))
    _send(to, "Your journey is still here 💗", html, f"{body}\n{config.APP_BASE_URL}")


def send_milestone(to: str, name: str, headline: str, body: str) -> None:
    html = _shell(f"{headline} 🎉",
                  f"<p>Hi {name or 'there'},</p><p>{body}</p>"
                  f'<div style="background:linear-gradient(135deg,#ff9ec4,#7fe3da);color:#fff;'
                  f'border-radius:14px;padding:26px;text-align:center;margin:16px 0">'
                  f'<div style="font-size:24px;font-weight:700">{headline}</div></div>' + _open("Celebrate in the app"))
    _send(to, f"{headline} 🎉", html, f"{headline}\n{body}\n{config.APP_BASE_URL}")


def send_reset(to: str, link: str) -> None:
    html = _shell(
        "Reset your password",
        "<p>We received a request to reset your Caloria password. Tap below to "
        "choose a new one.</p>" + _button(link, "Reset password")
        + f"<p style='color:#8a7886;font-size:12px'>This link expires in "
          f"{config.RESET_TOKEN_TTL_HOURS} hour(s). If you didn't ask for this, "
          "your password is unchanged.</p>",
    )
    text = (
        "Reset your Caloria password:\n"
        f"{link}\nThis link expires in {config.RESET_TOKEN_TTL_HOURS} hour(s). "
        "If you didn't request it, ignore this email."
    )
    _send(to, "Reset your Caloria password", html, text)
