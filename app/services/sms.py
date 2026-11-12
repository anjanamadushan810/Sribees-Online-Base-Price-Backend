"""Sending SMS. Owner rule: never send a test SMS. Tests and development use FakeSms, which
only keeps messages in memory; production refuses SMS_PROVIDER=fake (core/config.py)."""

from dataclasses import dataclass, field

from app.core.config import get_settings


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


def get_sender():
    provider = get_settings().sms_provider
    if provider == "fake":
        return _fake
    # Text.lk is wired when the supplier app goes to production (PLAN.md phase 7),
    # through the same account SRIBEESonline uses. Until then a real send is impossible.
    raise RuntimeError(f"SMS provider {provider!r} is not set up yet")


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
