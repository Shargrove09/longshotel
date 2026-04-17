"""Availability monitor – polls the API and fires notifications on changes."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone

from rich.console import Console

from longshotel.client import fetch_hotels
from longshotel.config import NotifyMode, Settings
from longshotel.display import print_hotels
from longshotel.models import Hotel
from longshotel.notifications import (
    send_discord_notification,
    send_discord_soldout_notification,
    send_discord_summary,
)

console = Console()


def _available_ids(hotels: list[Hotel]) -> set[int]:
    return {h.hotel_id for h in hotels if h.is_available}


async def run_monitor(settings: Settings | None = None) -> None:
    """Run the polling monitor loop.

    On each tick the monitor:

    1. Fetches the latest hotel list.
    2. Compares with the previous snapshot.
    3. Prints a summary of any changes.
    4. Fires optional notifications for newly-available hotels.
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

    previous_available: set[int] | None = None
    hotels_by_id: dict[int, Hotel] = {}

    jitter_label = (
        f" (±{settings.poll_jitter_seconds}s jitter)"
        if settings.poll_jitter_seconds
        else ""
    )
    console.print(
        f"[bold cyan]Starting monitor[/bold cyan] — polling every "
        f"{settings.poll_interval_seconds}s{jitter_label}  (Ctrl+C to stop)\n"
    )

    while True:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            hotels = await fetch_hotels(settings)
        except Exception as exc:
            console.print(f"[red][{now}] Error fetching hotels: {exc}[/red]")
            jitter = random.uniform(0, settings.poll_jitter_seconds)
            await asyncio.sleep(settings.poll_interval_seconds + jitter)
            continue

        hotels_by_id = {h.hotel_id: h for h in hotels}
        current_available = _available_ids(hotels)

        if previous_available is None:
            # First run – just display the current state
            console.print(f"[dim][{now}] Initial fetch complete[/dim]")
            print_hotels(hotels, show_soldout=settings.show_soldout)
        else:
            newly_available = current_available - previous_available
            newly_soldout = previous_available - current_available

            if newly_available or newly_soldout:
                console.print(f"\n[bold yellow][{now}] Change detected![/bold yellow]")
                for hid in newly_available:
                    h = hotels_by_id[hid]
                    console.print(
                        f"  [green]+ AVAILABLE:[/green] {h.name} "
                        f"(${h.display_rate:,.2f}/night)"
                    )
                for hid in newly_soldout:
                    h = hotels_by_id.get(hid)
                    name = h.name if h else f"Hotel #{hid}"
                    console.print(f"  [red]- SOLD OUT:[/red] {name}")

                # Fire change notifications
                if notify_mode == NotifyMode.changes and settings.discord_configured:
                    new_hotels = [hotels_by_id[hid] for hid in newly_available]
                    soldout_hotels = [
                        hotels_by_id[hid]
                        for hid in newly_soldout
                        if hid in hotels_by_id
                    ]
                    try:
                        await send_discord_notification(
                            settings, new_hotels,
                        )
                    except Exception as exc:
                        console.print(
                            f"  [red]Discord notification failed: {exc}[/red]"
                        )
                    try:
                        await send_discord_soldout_notification(
                            settings, soldout_hotels,
                        )
                    except Exception as exc:
                        console.print(
                            f"  [red]Discord notification failed: {exc}[/red]"
                        )
            else:
                console.print(
                    f"[dim][{now}] No changes "
                    f"({len(current_available)} available)[/dim]"
                )

        # Fire summary notification every poll (after first run)
        if notify_mode == NotifyMode.every and settings.discord_configured:
            try:
                await send_discord_summary(
                    settings, hotels,
                )
            except Exception as exc:
                console.print(
                    f"  [red]Discord summary failed: {exc}[/red]"
                )

        previous_available = current_available
        jitter = random.uniform(0, settings.poll_jitter_seconds)
        await asyncio.sleep(settings.poll_interval_seconds + jitter)
