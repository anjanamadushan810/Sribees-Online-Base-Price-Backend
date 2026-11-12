"""Sending SMS. Owner rule: never send a test SMS. Tests and development use FakeSms, which
only keeps messages in memory; production refuses SMS_PROVIDER=fake (core/config.py)."""

from dataclasses import dataclass, field

from app.core.config import get_settings
from app.services.errors import SupplyError


class SmsUnavailable(SupplyError):
    status_code = 503


@dataclass
class SentSms:
    phone: str
    text: str


@dataclass
class FakeSms:
    outbox: list[SentSms] = field(default_factory=list)

    async def send(self, phone: str, text: str) -> None:
        self.outbox.append(SentSms(phone, text))
        del self.outbox[:-50]  # keep memory bounded on a long-running dev server


_fake = FakeSms()


class SmsSendError(Exception):
    pass


@dataclass
class TextLkSms:
    """Text.lk API v3, same account and sender ID as SRIBEESonline (owner, 2026-11-12).
    The token travels in the Authorization header, never in a URL."""

    api_token: str
    sender_id: str
    base_url: str = "https://app.text.lk/api/v3"

    async def send(self, phone: str, text: str) -> None:
        import httpx

        payload = {"recipient": phone.lstrip("+"), "sender_id": self.sender_id,
                   "type": "plain" if text.isascii() else "unicode", "message": text}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"{self.base_url.rstrip('/')}/sms/send", json=payload,
                                      headers={"Authorization": f"Bearer {self.api_token}",
                                               "Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise SmsSendError(f"Text.lk unreachable: {type(exc).__name__}") from exc
        try:
            body = r.json()
        except ValueError:
            body = None
        # Text.lk answers some failures with HTTP 200 and status "error".
        if r.status_code != 200 or not isinstance(body, dict) or body.get("status") not in (
                True, "success"):
            raise SmsSendError(f"Text.lk refused (HTTP {r.status_code})")


def get_sender():
    s = get_settings()
    if s.sms_provider == "fake":
        return _fake
    if not s.textlk_api_token or not s.textlk_sender_id:
        raise SmsUnavailable("SMS is not set up")
    return TextLkSms(api_token=s.textlk_api_token, sender_id=s.textlk_sender_id,
                     base_url=s.textlk_base_url)


def fake_outbox() -> FakeSms:
    """Tests read the codes from here."""
    return _fake


def otp_text(code: str, language: str, minutes: int) -> str:
    if language == "en":
        return f"Sribees Supply sign-in code: {code}. Valid for {minutes} minutes. Do not share it."
    return (
        f"Sribees Supply ඇතුල් වීමේ කේතය: {code}. මිනිත්තු {minutes}ක් වලංගුයි. "
        "කිසිවෙකුට නොදෙන්න."
    )
