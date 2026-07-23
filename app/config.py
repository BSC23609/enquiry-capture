"""Configuration, loaded from environment.

Reads a local .env file if one exists, so `python -m app.sync` works from a
plain shell and from cron. In GitHub Actions there is no .env — the values
arrive as real environment variables from repo secrets — and load_dotenv()
simply does nothing. Same code, both places.
"""
import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional; CI supplies env vars directly
    pass


def _csv(name: str, default: str) -> list[str]:
    return [x.strip().lower() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    # --- Microsoft Graph (Azure app registration, client-credentials flow) ---
    tenant_id: str = os.getenv("MS_TENANT_ID", "")
    client_id: str = os.getenv("MS_CLIENT_ID", "")
    client_secret: str = os.getenv("MS_CLIENT_SECRET", "")

    # The mailbox to watch, e.g. "sales@bharatsteels.in"
    mailbox: str = os.getenv("MAILBOX", "")
    folder: str = os.getenv("MAILBOX_FOLDER", "inbox")

    # --- Anthropic ---
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    # --- Neon / Postgres ---
    database_url: str = os.getenv("DATABASE_URL", "")

    # --- Behaviour ---
    max_messages_per_run: int = int(os.getenv("MAX_MESSAGES_PER_RUN", "100"))
    lookback_days_on_first_run: int = int(os.getenv("LOOKBACK_DAYS", "7"))
    dry_run: bool = os.getenv("DRY_RUN", "false").lower() == "true"

    internal_domains: list[str] = field(
        default_factory=lambda: _csv(
            "INTERNAL_DOMAINS",
            "bharatsteels.in,metfraa.com,crayonroofings.com,vestrics.in",
        )
    )

    def validate(self) -> None:
        missing = [
            n for n, v in {
                "MS_TENANT_ID": self.tenant_id,
                "MS_CLIENT_ID": self.client_id,
                "MS_CLIENT_SECRET": self.client_secret,
                "MAILBOX": self.mailbox,
                "ANTHROPIC_API_KEY": self.anthropic_api_key,
                "DATABASE_URL": self.database_url,
            }.items() if not v
        ]
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


settings = Settings()
