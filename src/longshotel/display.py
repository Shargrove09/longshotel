"""Rich-powered console display for hotel results."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from longshotel.models import Hotel

console = Console()


def print_hotels(hotels: list[Hotel], *, show_soldout: bool = False) -> None:
    """Print a Rich table summarising the hotels."""
    table = Table(
        title="🏨 SDCC 2026 Hotel Availability",
        show_lines=True,
        title_style="bold cyan",
    )

    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Hotel", style="bold", max_width=45)
    table.add_column("Chain", max_width=15)
    table.add_column("Dist", justify="right", width=8)
    table.add_column("Stars", justify="center", width=5)
    table.add_column("Rate/Night", justify="right", width=12)
    table.add_column("Status", justify="center", width=12)
    table.add_column("Amenities", max_width=30)

    idx = 0
    for hotel in hotels:
        if not show_soldout and not hotel.is_available:
            continue
        idx += 1

        status = hotel.status
        if hotel.is_available:
            status_str = f"[bold green]{status}[/bold green]"
        else:
            status_str = f"[dim red]{status}[/dim red]"

        rate = hotel.display_rate
        rate_str = f"${rate:,.2f}" if rate and rate > 0 else "—"

        dist_str = f"{hotel.distance:.2f} {hotel.distance_units}"
        stars_str = "⭐" * int(hotel.star_rating_decimal) if hotel.star_rating_decimal else "—"

        # Show top 3 amenities to keep table readable
        top_amenities = ", ".join(hotel.amenity_list[:3])
        if len(hotel.amenity_list) > 3:
            top_amenities += f" (+{len(hotel.amenity_list) - 3})"

        table.add_row(
            str(idx),
            hotel.name,
            hotel.hotel_chain,
            dist_str,
            stars_str,
            rate_str,
            status_str,
            top_amenities,
        )

    if idx == 0:
        console.print(
            "\n[bold red]No hotels with availability found.[/bold red]\n"
        )
    else:
        console.print()
        console.print(table)
        console.print(
            f"\n[dim]{idx} hotel(s) shown • {len(hotels)} total[/dim]\n"
        )
