"""Settings, read from the environment (or a .env file beside the app in development)."""

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = Field(default="development", pattern="^(development|test|production)$")
    database_url: str
    # Signs access tokens. 32+ random bytes; rotate by deploying a new value.
    jwt_secret: str = Field(min_length=32)
    # Fernet key (urlsafe base64, 32 bytes) that encrypts staff TOTP secrets at rest.
    totp_encryption_key: str
    # Keys the HMAC of supplier one-time codes (codes are never stored in clear).
    otp_secret: str = Field(min_length=32)
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    cors_origins: list[str] = []
    totp_issuer: str = "Sribees Supply"
    # Staff two-step sign-in (same rule as Finance: off only without real data).
    two_factor: bool = True
    allow_production_without_2fa: bool = False

    # ---- supplier app (phone + SMS one-time code) ----
    # A supplier signs in rarely on their own phone; SMS costs money, so the session is long.
    supplier_refresh_days: int = 90
    otp_minutes: int = 5
    otp_max_attempts: int = 5  # wrong codes before a code dies
    otp_resend_seconds: int = 60
    otp_max_per_phone_hour: int = 5
    otp_max_per_ip_hour: int = 20
    # "fake" keeps codes in memory (tests, development); "textlk" sends real SMS.
    sms_provider: str = Field(default="fake", pattern="^(fake|textlk)$")
    # Development only: this code always works for registered numbers, so nobody needs
    # an SMS on the dev server. Refused in production.
    dev_fixed_otp: str = ""

    @model_validator(mode="after")
    def _production_rules(self) -> "Settings":
        if self.environment == "production":
            if not self.two_factor and not self.allow_production_without_2fa:
                raise ValueError("TWO_FACTOR cannot be off in production "
                                 "(unless ALLOW_PRODUCTION_WITHOUT_2FA=true)")
            if self.sms_provider == "fake":
                raise ValueError("SMS_PROVIDER=fake is not allowed in production")
            if self.dev_fixed_otp:
                raise ValueError("DEV_FIXED_OTP is not allowed in production")
        if self.dev_fixed_otp and not (self.dev_fixed_otp.isdigit()
                                       and len(self.dev_fixed_otp) == 6):
            raise ValueError("DEV_FIXED_OTP must be 6 digits")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
