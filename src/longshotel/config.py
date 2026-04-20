"""Application configuration via environment variables / .env file."""

from __future__ import annotations

from enum import Enum

from pydantic_settings import BaseSettings


class NotifyMode(str, Enum):
    """Controls when Discord notifications are sent."""

    off = "off"
    """Never send Discord messages."""

    changes = "changes"
    """Post only when hotel availability status changes."""

    every = "every"
    """Post a full summary after every poll cycle."""


class Settings(BaseSettings):
    """All tuneable knobs live here.

    Override any value with an environment variable prefixed ``LONGSHOTEL_``,
    e.g. ``LONGSHOTEL_ARRIVE=2026-07-20``.
    """

    # ── OnPeak event parameters ──────────────────────────────────────────
    event_code: str = "43CCI2026HIR"
    """The event slug that appears in the Compass URL path."""

    block_index: int = 3
    """Fallback block index for ``/avail``.  Normally discovered automatically
    from the category-page redirect."""

    category_id: int = 42031
    """OnPeak category ID.  The browser navigates to
    ``/e/{event_code}/in/category/{category_id}`` to establish a session."""

    base_url: str = "https://compass.onpeak.com"
    """OnPeak Compass base URL."""

    arrive: str = "2026-07-21"
    """Check-in date in YYYY-MM-DD format."""

    depart: str = "2026-07-27"
    """Check-out date in YYYY-MM-DD format."""

    # ── Monitoring ───────────────────────────────────────────────────────
    poll_interval_seconds: int = 60
    """How often to poll the API when running in monitor mode."""

    poll_jitter_seconds: int = 6
    """Random jitter added to each poll interval (0 to this value, ~10% of default interval)."""

    state_file: str | None = None
    """Path to a JSON file for persisting availability state across restarts.
    On startup the previous baseline is loaded so changes since the last run
    are detected immediately on the first poll."""

    date_flex_days: int = 0
    """If >0, also scan ±N day shifts of the primary stay (same length).
    Alerts are fired when hotels become available for any of the flex ranges."""

    status_report_interval_seconds: int = 0
    """Send a periodic aggregated status report every N seconds. 0 = disabled.
    The report is always sent (regardless of notify_mode) as long as Discord
    is configured and this value is >0."""

    # ── Notifications (optional) ─────────────────────────────────────────
    discord_webhook_url: str | None = None
    """If set, availability changes are posted to this Discord webhook."""

    discord_bot_token: str | None = None
    """Discord bot token for sending DMs. Takes priority over webhook."""

    discord_user_id: str | None = None
    """Your Discord user ID. Required when using bot DMs."""

    notify_mode: NotifyMode = NotifyMode.changes
    """When to send Discord notifications: off, changes, or every."""

    @property
    def discord_configured(self) -> bool:
        """Return True if any Discord delivery method is configured."""
        return bool(self.discord_bot_token and self.discord_user_id) or bool(
            self.discord_webhook_url
        )

    # ── Display ──────────────────────────────────────────────────────────
    show_soldout: bool = False
    """Whether to display sold-out hotels in the results table."""

    # ── Debug ────────────────────────────────────────────────────────────
    verbose: bool = False
    """Enable debug logging. Also set via LONGSHOTEL_VERBOSE=1."""

    dump_html: bool = False
    """Write raw HTML response bodies to files (response_attempt_N.html) for browser inspection. Also set via LONGSHOTEL_DUMP_HTML=1."""

    model_config = {"env_prefix": "LONGSHOTEL_", "env_file": ".env"}
