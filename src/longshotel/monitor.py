"""Availability monitor – polls the API and fires notifications on changes."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone

from rich.console import Console

from longshotel.client import RateLimitedError, fetch_hotels_dual
from longshotel.config import NotifyMode, Settings
from longshotel.display import print_hotels
from longshotel.models import Hotel
from longshotel.notifications import (
    send_discord_general_notification,
    send_discord_interval_summary,
    send_discord_notification,
    send_discord_soldout_notification,
    send_discord_summary,
)

console = Console()
log = logging.getLogger(__name__)

# If the hotel count drops below this fraction of the previous count we
# treat the response as degraded and skip the diff to avoid corrupting
# baseline state.
_DEGRADED_RATIO = 0.5


def _available_ids(hotels: list[Hotel]) -> set[int]:
    return {h.hotel_id for h in hotels if h.is_available}


def _log_cycle_summary(
    now: str,
    label: str,
    hotels: list[Hotel],
    current_available: set[int],
) -> None:
    statuses: dict[str, int] = {}
    for h in hotels:
        statuses[h.status] = statuses.get(h.status, 0) + 1
    status_breakdown = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
    log.info(
        "[monitor] %s | %s | hotels=%d available=%d | %s",
        now, label, len(hotels), len(current_available), status_breakdown,
    )


def _is_degraded(hotels: list[Hotel], previous_count: int, now: str, label: str) -> bool:
    if not hotels:
        log.warning("[monitor] %s | %s: empty hotel list — skipping diff", now, label)
        console.print(
            f"[yellow][{now}] Empty {label} response — "
            f"skipping this cycle (baseline preserved)[/yellow]"
        )
        return True
    if previous_count > 0 and len(hotels) < previous_count * _DEGRADED_RATIO:
        log.warning(
            "[monitor] %s | %s: hotel count dropped from %d to %d — skipping diff",
            now, label, previous_count, len(hotels),
        )
        console.print(
            f"[yellow][{now}] Degraded {label} response "
            f"({len(hotels)}/{previous_count} hotels) — "
            f"skipping this cycle (baseline preserved)[/yellow]"
        )
        return True
    return False


def _in_quiet_hours(settings: Settings) -> bool:
    """Return True if the current local time falls within configured quiet hours."""
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    if start == end:
        return False
    hour = datetime.now().hour
    if start > end:  # midnight-crossing window e.g. 22–06
        return hour >= start or hour < end
    return start <= hour < end  # same-day window e.g. 02–07


def _compute_sleep_seconds(settings: Settings, consecutive_errors: int) -> float:
    """Return how long to sleep before the next poll cycle."""
    if consecutive_errors > 0:
        base = settings.poll_interval_seconds * (2 ** (consecutive_errors - 1))
        capped = min(base, settings.backoff_max_seconds)
        return capped + random.uniform(0, capped * 0.2)

    if _in_quiet_hours(settings):
        interval = settings.quiet_hours_interval_seconds
        return interval + random.uniform(0, interval * 0.2)

    interval = settings.poll_interval_seconds
    return interval + random.uniform(0, interval * 0.2)


async def run_monitor(settings: Settings | None = None) -> None:
    """Run the polling monitor loop.

    On each tick the monitor:

    1. Fetches both general and date-specific hotel availability.
    2. Compares each with their previous snapshots.
    3. Prints a summary of any changes.
    4. Fires optional notifications for changes in either set.
    """
    if settings is None:
        settings = Settings()

    # Resolve effective notify mode — require Discord to be configured
    notify_mode = settings.notify_mode
    if not settings.discord_configured and notify_mode != NotifyMode.off:
        console.print(
            "[yellow]⚠ notify_mode is "
            f"'{notify_mode.value}' but no Discord credentials are set "
            "— notifications disabled.[/yellow]\n"
        )
        notify_mode = NotifyMode.off

    prev_general_available: set[int] | None = None
    prev_dated_available: set[int] | None = None
    prev_general_count: int = 0
    prev_dated_count: int = 0
    consecutive_errors: int = 0

    interval_start_time: float | None = None
    interval_start_wall_str: str = ""
    interval_start_dated_available: set[int] | None = None
    interval_poll_count: int = 0
    interval_error_count: int = 0

    quiet_info = ""
    if settings.quiet_hours_start != settings.quiet_hours_end:
        quiet_info = (
            f"\n  Quiet hours: {settings.quiet_hours_start:02d}:00–"
            f"{settings.quiet_hours_end:02d}:00 local "
            f"(interval: {settings.quiet_hours_interval_seconds}s)"
        )
    console.print(
        f"[bold cyan]Starting monitor[/bold cyan] — polling every "
        f"{settings.poll_interval_seconds}s (+20% jitter){quiet_info}\n"
        f"  Tracking: general availability + "
        f"{settings.arrive} → {settings.depart}  (Ctrl+C to stop)\n"
    )

    while True:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            result = await fetch_hotels_dual(settings)
            consecutive_errors = 0
        except RateLimitedError as exc:
            consecutive_errors += 1
            interval_error_count += 1
            log.error(
                "[monitor] %s | rate limited (attempt %d): %s",
                now, consecutive_errors, exc,
            )
            console.print(
                f"[bold red][{now}] Rate limited / blocked "
                f"(consecutive: {consecutive_errors}) — backing off[/bold red]"
            )
            await asyncio.sleep(_compute_sleep_seconds(settings, consecutive_errors))
            continue
        except Exception as exc:
            consecutive_errors += 1
            interval_error_count += 1
            level = logging.ERROR if consecutive_errors >= 3 else logging.WARNING
            log.log(
                level,
                "[monitor] %s | fetch failed (attempt %d): %s",
                now, consecutive_errors, exc,
            )
            console.print(
                f"[red][{now}] Error fetching hotels "
                f"(consecutive failures: {consecutive_errors}): {exc}[/red]"
            )
            await asyncio.sleep(_compute_sleep_seconds(settings, consecutive_errors))
            continue

        general_hotels = result.general
        dated_hotels = result.dated

        # ── Guard: skip diff if either response looks degraded ───────
        if _is_degraded(general_hotels, prev_general_count, now, "general") or \
                _is_degraded(dated_hotels, prev_dated_count, now, "dated"):
            await asyncio.sleep(_compute_sleep_seconds(settings, consecutive_errors))
            continue

        general_by_id = {h.hotel_id: h for h in general_hotels}
        dated_by_id = {h.hotel_id: h for h in dated_hotels}
        cur_general_available = _available_ids(general_hotels)
        cur_dated_available = _available_ids(dated_hotels)

        # ── Structured per-cycle logs ────────────────────────────────
        _log_cycle_summary(now, "general", general_hotels, cur_general_available)
        _log_cycle_summary(now, f"{settings.arrive}–{settings.depart}", dated_hotels, cur_dated_available)

        if prev_general_available is None:
            # First run
            console.print(f"[dim][{now}] Initial fetch complete[/dim]")
            console.print(f"\n[bold]Date-specific ({settings.arrive} → {settings.depart}):[/bold]")
            print_hotels(dated_hotels, show_soldout=settings.show_soldout)

            # Show hotels with general availability but not for user's dates
            general_only = cur_general_available - cur_dated_available
            if general_only:
                console.print(
                    f"\n[bold cyan]ℹ {len(general_only)} hotel(s) have rooms for "
                    f"OTHER dates (not {settings.arrive}–{settings.depart}):[/bold cyan]"
                )
                for hid in general_only:
                    h = general_by_id[hid]
                    console.print(f"  [cyan]○ {h.name}[/cyan] ({h.hotel_chain})")

            # Initialise interval tracking after first successful cycle
            interval_start_dated_available = cur_dated_available.copy()
            interval_start_time = time.monotonic()
            interval_start_wall_str = now
        else:
            any_change = False

            # ── Date-specific changes ────────────────────────────────
            newly_available = cur_dated_available - prev_dated_available
            newly_soldout = prev_dated_available - cur_dated_available

            if newly_available or newly_soldout:
                any_change = True
                log.info(
                    "[monitor] %s | dated changes: +%d available, -%d sold out",
                    now, len(newly_available), len(newly_soldout),
                )
                console.print(
                    f"\n[bold yellow][{now}] Change detected "
                    f"({settings.arrive}–{settings.depart})![/bold yellow]"
                )
                for hid in newly_available:
                    h = dated_by_id[hid]
                    console.print(
                        f"  [green]+ AVAILABLE:[/green] {h.name} "
                        f"(${h.display_rate:,.2f}/night)"
                    )
                for hid in newly_soldout:
                    h = dated_by_id.get(hid)
                    name = h.name if h else f"Hotel #{hid}"
                    console.print(f"  [red]- SOLD OUT:[/red] {name}")

                if notify_mode == NotifyMode.changes and settings.discord_configured:
                    new_hotels = [dated_by_id[hid] for hid in newly_available]
                    soldout_hotels = [
                        dated_by_id[hid] for hid in newly_soldout if hid in dated_by_id
                    ]
                    try:
                        await send_discord_notification(settings, new_hotels)
                    except Exception as exc:
                        console.print(f"  [red]Discord notification failed: {exc}[/red]")
                    try:
                        await send_discord_soldout_notification(settings, soldout_hotels)
                    except Exception as exc:
                        console.print(f"  [red]Discord notification failed: {exc}[/red]")

            # ── General availability changes (any dates) ─────────────
            # Only report hotels that are newly available generally but NOT
            # already available for the user's specific dates (avoid duplicate alerts).
            gen_only_new = (cur_general_available - prev_general_available) - cur_dated_available

            if gen_only_new:
                any_change = True
                log.info(
                    "[monitor] %s | general changes: %d hotel(s) now have rooms for other dates",
                    now, len(gen_only_new),
                )
                console.print(
                    f"\n[bold cyan][{now}] {len(gen_only_new)} hotel(s) now have rooms "
                    f"for OTHER dates (not {settings.arrive}–{settings.depart}):[/bold cyan]"
                )
                for hid in gen_only_new:
                    h = general_by_id[hid]
                    console.print(f"  [cyan]○ {h.name}[/cyan] ({h.hotel_chain})")

                if notify_mode == NotifyMode.changes and settings.discord_configured:
                    try:
                        await send_discord_general_notification(
                            settings,
                            [general_by_id[hid] for hid in gen_only_new],
                            settings.arrive,
                            settings.depart,
                        )
                    except Exception as exc:
                        console.print(f"  [red]Discord notification failed: {exc}[/red]")

            if not any_change:
                console.print(
                    f"[dim][{now}] No changes "
                    f"({len(cur_dated_available)} dated, "
                    f"{len(cur_general_available)} general)[/dim]"
                )

        # Fire summary notification every poll (after first run)
        if notify_mode == NotifyMode.every and settings.discord_configured:
            try:
                await send_discord_summary(settings, dated_hotels)
            except Exception as exc:
                console.print(f"  [red]Discord summary failed: {exc}[/red]")

        interval_poll_count += 1

        # ── Interval summary ─────────────────────────────────────────────
        if (
            settings.interval_summary_notification_seconds > 0
            and interval_start_time is not None
            and (time.monotonic() - interval_start_time) >= settings.interval_summary_notification_seconds
            and notify_mode != NotifyMode.off
            and settings.discord_configured
        ):
            if _in_quiet_hours(settings):
                log.info("[monitor] %s | interval summary suppressed (quiet hours)", now)
                console.print(f"[dim][{now}] Interval summary suppressed (quiet hours)[/dim]")
            else:
                newly_available_net = cur_dated_available - interval_start_dated_available
                newly_soldout_net = interval_start_dated_available - cur_dated_available
                try:
                    await send_discord_interval_summary(
                        settings,
                        newly_available_net=[
                            dated_by_id[hid] for hid in newly_available_net if hid in dated_by_id
                        ],
                        newly_soldout_net=[
                            dated_by_id.get(hid) for hid in newly_soldout_net
                        ],
                        current_available=[
                            dated_by_id[hid] for hid in cur_dated_available if hid in dated_by_id
                        ],
                        poll_count=interval_poll_count,
                        error_count=interval_error_count,
                        period_start=interval_start_wall_str,
                        period_end=now,
                    )
                except Exception as exc:
                    console.print(f"  [red]Discord interval summary failed: {exc}[/red]")
            # Reset unconditionally — quiet hours suppression skips, does not defer
            interval_start_dated_available = cur_dated_available.copy()
            interval_start_time = time.monotonic()
            interval_start_wall_str = now
            interval_poll_count = 0
            interval_error_count = 0

        prev_general_available = cur_general_available
        prev_dated_available = cur_dated_available
        prev_general_count = len(general_hotels)
        prev_dated_count = len(dated_hotels)
        await asyncio.sleep(_compute_sleep_seconds(settings, consecutive_errors))
