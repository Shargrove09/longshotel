"""Notification sinks for availability changes."""

from __future__ import annotations

import httpx

from longshotel.models import Hotel


async def send_discord_notification(
    webhook_url: str,
    newly_available: list[Hotel],
) -> None:
    """Post a message to a Discord webhook about newly available hotels."""
    if not newly_available:
        return

    lines = ["**🏨 New Hotel Availability for SDCC 2026!**\n"]
    for hotel in newly_available:
        rate = hotel.display_rate
        rate_str = f"${rate:,.2f}/night" if rate and rate > 0 else "rate TBD"
        lines.append(
            f"• **{hotel.name}** ({hotel.hotel_chain}) — "
            f"{hotel.distance:.2f} mi — {rate_str}"
        )

    payload = {"content": "\n".join(lines)}

    async with httpx.AsyncClient() as client:
        resp = await client.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
