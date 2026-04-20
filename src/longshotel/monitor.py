"""Availability monitor – polls the API and fires notifications on changes."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console

from longshotel.client import FetchResult, fetch_dated_hotels, fetch_hotels_dual
from longshotel.config import NotifyMode, Settings
from longshotel.display import print_hotels
from longshotel.models import Hotel
from longshotel.notifications import (
    send_discord_flex_notification,
    send_discord_general_notification,
    send_discord_notification,
    send_discord_soldout_notification,
    send_discord_status_report,
    send_discord_summary,
)

console = Console()
log = logging.getLogger(__name__)

# If the hotel count drops below this fraction of the previous count we
# treat the response as degraded and skip the diff to avoid corrupting
# baseline state.
_DEGRADED_RATIO = 0.5


# ---------------------------------------------------------------------------
# Status report aggregator
# ---------------------------------------------------------------------------

@dataclass
class _ChangeEvent:
    hotel_id: int
    hotel_name: str
    timestamp: datetime
    event_type: str  # "available" or "soldout"


class StatusReportAggregator:
    """Accumulates monitor events between periodic status reports.

    Call :meth:`record_poll_ok` / :meth:`record_poll_failed` and the
    ``record_*`` change methods on every cycle.  When the configured interval
    elapses call :meth:`generate_report` which formats the aggregated data,
    sends it to Discord, and resets the counters for the next period.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._period_start: datetime = datetime.now(timezone.utc)
        self._polls_ok: int = 0
        self._polls_failed: int = 0
        self._changes: list[_ChangeEvent] = []

    def record_poll_ok(self) -> None:
        self._polls_ok += 1

    def record_poll_failed(self) -> None:
        self._polls_failed += 1

    def record_available(self, hotel_id: int, hotel_name: str) -> None:
        self._changes.append(
            _ChangeEvent(hotel_id, hotel_name, datetime.now(timezone.utc), "available")
        )

    def record_soldout(self, hotel_id: int, hotel_name: str) -> None:
        self._changes.append(
            _ChangeEvent(hotel_id, hotel_name, datetime.now(timezone.utc), "soldout")
        )

    def generate_report(
        self,
        dated_hotels: list[Hotel],
        general_hotels: list[Hotel],
        settings: Settings,
        next_report_time: datetime,
    ) -> str:
        """Format an aggregated status report string and reset counters."""
        now = datetime.now(timezone.utc)
        start_str = self._period_start.strftime("%Y-%m-%d %H:%M")
        end_str = now.strftime("%Y-%m-%d %H:%M")
        total_mins = int((now - self._period_start).total_seconds() / 60)
        if total_mins >= 60:
            duration_str = f"{total_mins // 60}h {total_mins % 60}m"
        else:
            duration_str = f"{total_mins}m"

        lines: list[str] = [
            "📊 **SDCC 2026 — Status Report**",
            f"Period: {start_str} → {end_str} UTC ({duration_str})",
            f"Polls: {self._polls_ok} successful / {self._polls_failed} failed",
            "",
        ]

        newly_available = [c for c in self._changes if c.event_type == "available"]
        newly_soldout = [c for c in self._changes if c.event_type == "soldout"]

        if newly_available or newly_soldout:
            lines.append("**Changes This Period:**")
            if newly_available:
                names = ", ".join(c.hotel_name for c in newly_available)
                lines.append(f"🟢 +{len(newly_available)} hotel(s) became available: {names}")
            if newly_soldout:
                names = ", ".join(c.hotel_name for c in newly_soldout)
                lines.append(f"🔴 -{len(newly_soldout)} hotel(s) sold out: {names}")
        else:
            lines.append("**Changes This Period:** None")

        lines.append("")

        available_dated = [h for h in dated_hotels if h.is_available]
        soldout_dated = [h for h in dated_hotels if not h.is_available]
        lines.append(f"**Current Availability (for {settings.arrive}–{settings.depart}):**")
        if available_dated:
            for h in available_dated:
                rate = h.display_rate
                rate_str = f"${rate:,.2f}/night" if rate and rate > 0 else "rate TBD"
                lines.append(f"• {h.name} — {rate_str} — {h.distance:.2f} mi")
        else:
            lines.append("• (none available)")
        lines.append(f"({len(available_dated)} available, {len(soldout_dated)} sold out)")
        lines.append("")

        gen_avail_ids = {h.hotel_id for h in general_hotels if h.is_available}
        dated_avail_ids = {h.hotel_id for h in dated_hotels if h.is_available}
        gen_only_count = len(gen_avail_ids - dated_avail_ids)
        if gen_only_count:
            lines.append(f"**General Availability (other dates):** {gen_only_count} hotel(s) have rooms")
        else:
            lines.append("**General Availability (other dates):** none")
        lines.append("")

        next_str = next_report_time.strftime("%Y-%m-%d %H:%M")
        lines.append(f"Next report: {next_str} UTC")

        self.reset()
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state(settings: Settings) -> tuple[set[int] | None, set[int] | None]:
    """Load previous availability baseline from the state file.

    Returns ``(general_ids, dated_ids)`` or ``(None, None)`` if no valid
    state exists.  A loaded baseline means the very first poll cycle will
    detect changes that occurred since the last run.
    """
    if not settings.state_file:
        return None, None

    path = Path(settings.state_file)
    if not path.exists():
        return None, None

    try:
        raw = json.loads(path.read_text())
        general_ids: set[int] = set(raw.get("general_ids", []))
        dated_ids: set[int] = set(raw.get("dated_ids", []))
        timestamp = raw.get("timestamp", "unknown")
        log.info(
            "[state] loaded previous baseline from %s (saved %s, "
            "%d general / %d dated available)",
            settings.state_file, timestamp, len(general_ids), len(dated_ids),
        )
        return general_ids, dated_ids
    except Exception as exc:
        log.warning("[state] failed to load %s: %s", settings.state_file, exc)
        return None, None


def _save_state(
    settings: Settings,
    general_ids: set[int],
    dated_ids: set[int],
) -> None:
    """Persist the current availability baseline to the state file."""
    if not settings.state_file:
        return

    path = Path(settings.state_file)
    try:
        data = {
            "general_ids": sorted(general_ids),
            "dated_ids": sorted(dated_ids),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(data, indent=2))
        log.debug("[state] saved baseline to %s", settings.state_file)
    except Exception as exc:
        log.warning("[state] failed to save %s: %s", settings.state_file, exc)


# ---------------------------------------------------------------------------
# Flex date helpers
# ---------------------------------------------------------------------------

def _flex_date_ranges(settings: Settings) -> list[tuple[str, str]]:
    """Return alternative (arrive, depart) pairs for the flex date scan.

    Each pair shifts both dates by the same delta (±1 … ±N days) so the
    stay length is preserved.
    """
    if not settings.date_flex_days:
        return []

    arrive = date.fromisoformat(settings.arrive)
    depart = date.fromisoformat(settings.depart)
    stay_length = (depart - arrive).days

    result: list[tuple[str, str]] = []
    for delta in range(-settings.date_flex_days, settings.date_flex_days + 1):
        if delta == 0:
            continue
        flex_arrive = arrive + timedelta(days=delta)
        flex_depart = flex_arrive + timedelta(days=stay_length)
        result.append((flex_arrive.isoformat(), flex_depart.isoformat()))
    return result


# ---------------------------------------------------------------------------
# Internal helpers (unchanged from original)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------

async def run_monitor(settings: Settings | None = None) -> None:
    """Run the polling monitor loop.

    On each tick the monitor:

    1. Fetches both general and date-specific hotel availability (direct
       httpx path, Playwright fallback, up to 3 retries with back-off).
    2. Compares each with their previous snapshots (loaded from state file
       on startup if configured).
    3. Prints a summary of any changes and fires optional Discord notifications.
    4. Optionally scans flex date ranges (±N days) and alerts on new availability.
    5. Sends a periodic aggregated status report at the configured interval.
    6. Saves the current baseline to the state file.
    """
    if settings is None:
        settings = Settings()

    # ── Resolve effective notify mode ────────────────────────────────────
    notify_mode = settings.notify_mode
    if not settings.discord_configured and notify_mode != NotifyMode.off:
        console.print(
            "[yellow]⚠ notify_mode is "
            f"'{notify_mode.value}' but no Discord credentials are set "
            "— notifications disabled.[/yellow]\n"
        )
        notify_mode = NotifyMode.off

    # ── Load persisted state ─────────────────────────────────────────────
    saved_general_ids, saved_dated_ids = _load_state(settings)
    prev_general_available: set[int] | None = saved_general_ids
    prev_dated_available: set[int] | None = saved_dated_ids
    if saved_general_ids is not None:
        console.print(
            f"[dim]Loaded previous state: "
            f"{len(saved_dated_ids or set())} dated / "
            f"{len(saved_general_ids or set())} general available[/dim]\n"
        )

    prev_general_count: int = 0
    prev_dated_count: int = 0
    consecutive_errors: int = 0

    # ── Flex date ranges ─────────────────────────────────────────────────
    flex_ranges = _flex_date_ranges(settings)
    prev_flex_available: dict[str, set[int]] = {}

    # ── Status report setup ──────────────────────────────────────────────
    aggregator = StatusReportAggregator()
    report_interval = settings.status_report_interval_seconds
    report_enabled = report_interval > 0 and settings.discord_configured
    next_report_time: datetime | None = (
        datetime.now(timezone.utc) + timedelta(seconds=report_interval)
        if report_enabled else None
    )

    jitter_label = (
        f" (±{settings.poll_jitter_seconds}s jitter)"
        if settings.poll_jitter_seconds
        else ""
    )
    status_report_label = (
        f", status report every {report_interval}s"
        if report_enabled else ""
    )
    flex_label = (
        f", flex ±{settings.date_flex_days}d"
        if settings.date_flex_days else ""
    )
    console.print(
        f"[bold cyan]Starting monitor[/bold cyan] — polling every "
        f"{settings.poll_interval_seconds}s{jitter_label}{status_report_label}{flex_label}\n"
        f"  Tracking: general availability + "
        f"{settings.arrive} → {settings.depart}  (Ctrl+C to stop)\n"
    )

    while True:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            result = await fetch_hotels_dual(settings)
            consecutive_errors = 0
            aggregator.record_poll_ok()
        except Exception as exc:
            consecutive_errors += 1
            aggregator.record_poll_failed()
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
            jitter = random.uniform(0, settings.poll_jitter_seconds)
            await asyncio.sleep(settings.poll_interval_seconds + jitter)
            continue

        general_hotels = result.general
        dated_hotels = result.dated

        # ── Guard: skip diff if either response looks degraded ───────
        if _is_degraded(general_hotels, prev_general_count, now, "general") or \
                _is_degraded(dated_hotels, prev_dated_count, now, "dated"):
            jitter = random.uniform(0, settings.poll_jitter_seconds)
            await asyncio.sleep(settings.poll_interval_seconds + jitter)
            continue

        general_by_id = {h.hotel_id: h for h in general_hotels}
        dated_by_id = {h.hotel_id: h for h in dated_hotels}
        cur_general_available = _available_ids(general_hotels)
        cur_dated_available = _available_ids(dated_hotels)

        # ── Structured per-cycle logs ────────────────────────────────
        _log_cycle_summary(now, "general", general_hotels, cur_general_available)
        _log_cycle_summary(now, f"{settings.arrive}–{settings.depart}", dated_hotels, cur_dated_available)

        if prev_general_available is None:
            # First run (no saved state)
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
                    aggregator.record_available(h.hotel_id, h.name)
                for hid in newly_soldout:
                    h = dated_by_id.get(hid)
                    name = h.name if h else f"Hotel #{hid}"
                    console.print(f"  [red]- SOLD OUT:[/red] {name}")
                    aggregator.record_soldout(hid, name)

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

        # ── Flex date scanning ───────────────────────────────────────
        for flex_arrive, flex_depart in flex_ranges:
            flex_key = f"{flex_arrive}→{flex_depart}"
            try:
                flex_hotels = await fetch_dated_hotels(settings, flex_arrive, flex_depart)
            except Exception as exc:
                log.warning("[monitor] flex fetch failed for %s: %s", flex_key, exc)
                continue

            flex_avail_ids = _available_ids(flex_hotels)
            flex_by_id = {h.hotel_id: h for h in flex_hotels}

            if flex_key in prev_flex_available:
                new_flex = flex_avail_ids - prev_flex_available[flex_key]
                if new_flex:
                    log.info(
                        "[monitor] %s | flex %s: %d hotel(s) newly available",
                        now, flex_key, len(new_flex),
                    )
                    console.print(
                        f"\n[bold magenta][{now}] {len(new_flex)} hotel(s) available for "
                        f"alternate dates {flex_arrive}–{flex_depart}![/bold magenta]"
                    )
                    for hid in new_flex:
                        h = flex_by_id[hid]
                        rate = h.display_rate
                        rate_str = f"${rate:,.2f}/night" if rate and rate > 0 else "rate TBD"
                        console.print(f"  [magenta]+ {h.name}[/magenta] ({rate_str})")

                    if notify_mode == NotifyMode.changes and settings.discord_configured:
                        try:
                            await send_discord_flex_notification(
                                settings,
                                [flex_by_id[hid] for hid in new_flex if hid in flex_by_id],
                                flex_arrive,
                                flex_depart,
                            )
                        except Exception as exc:
                            console.print(f"  [red]Discord flex notification failed: {exc}[/red]")
            else:
                # First time seeing this flex range — just log current state
                if flex_avail_ids:
                    console.print(
                        f"[dim][{now}] Flex {flex_arrive}–{flex_depart}: "
                        f"{len(flex_avail_ids)} available[/dim]"
                    )

            prev_flex_available[flex_key] = flex_avail_ids

        # ── 'every' mode summary ─────────────────────────────────────
        if notify_mode == NotifyMode.every and settings.discord_configured:
            try:
                await send_discord_summary(settings, dated_hotels)
            except Exception as exc:
                console.print(f"  [red]Discord summary failed: {exc}[/red]")

        # ── Periodic status report ───────────────────────────────────
        if (
            report_enabled
            and next_report_time is not None
            and datetime.now(timezone.utc) >= next_report_time
        ):
            next_report_time = datetime.now(timezone.utc) + timedelta(seconds=report_interval)
            report_text = aggregator.generate_report(
                dated_hotels, general_hotels, settings, next_report_time
            )
            log.info("[monitor] sending periodic status report")
            try:
                await send_discord_status_report(settings, report_text)
            except Exception as exc:
                console.print(f"  [red]Status report failed: {exc}[/red]")

        # ── Save state ───────────────────────────────────────────────
        _save_state(settings, cur_general_available, cur_dated_available)

        prev_general_available = cur_general_available
        prev_dated_available = cur_dated_available
        prev_general_count = len(general_hotels)
        prev_dated_count = len(dated_hotels)
        jitter = random.uniform(0, settings.poll_jitter_seconds)
        await asyncio.sleep(settings.poll_interval_seconds + jitter)

