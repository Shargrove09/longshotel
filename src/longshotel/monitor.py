"""Availability monitor – polls the API and fires notifications on changes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from rich.console import Console

from longshotel.client import fetch_hotels
from longshotel.config import Settings
from longshotel.display import print_hotels
from longshotel.models import Hotel
from longshotel.notifications import send_discord_notification

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

    previous_available: set[int] | None = None
    hotels_by_id: dict[int, Hotel] = {}

    console.print(
        f"[bold cyan]Starting monitor[/bold cyan] — polling every "
        f"{settings.poll_interval_seconds}s  (Ctrl+C to stop)\n"
    )

    while True:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            hotels = await fetch_hotels(settings)
        except Exception as exc:
            console.print(f"[red][{now}] Error fetching hotels: {exc}[/red]")
            await asyncio.sleep(settings.poll_interval_seconds)
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

                # Fire notifications
                new_hotels = [hotels_by_id[hid] for hid in newly_available]
                if settings.discord_webhook_url and new_hotels:
                    try:
                        await send_discord_notification(
                            settings.discord_webhook_url, new_hotels
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

        previous_available = current_available
        await asyncio.sleep(settings.poll_interval_seconds)
