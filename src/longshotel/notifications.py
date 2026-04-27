"""Notification sinks for availability changes."""

from __future__ import annotations

import httpx

from longshotel.config import Settings
from longshotel.models import Hotel

DISCORD_API = "https://discord.com/api/v10"
DISCORD_MAX_CONTENT = 2000


def _raise_with_discord_details(resp: httpx.Response, context: str) -> None:
    """Raise with Discord response body to make 4xx/5xx debugging easier."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = resp.text
        except Exception:
            detail = "<unavailable>"
        raise RuntimeError(
            f"Discord API error during {context}: {resp.status_code} {detail}"
        ) from exc


def _format_hotel_line(hotel: Hotel) -> str:
    """Format a single hotel into a bullet-point line."""
    rate = hotel.display_rate
    rate_str = f"${rate:,.2f}/night" if rate and rate > 0 else "rate TBD"
    return (
        f"• **{hotel.name}** ({hotel.hotel_chain}) — "
        f"{hotel.distance:.2f} mi — {rate_str}"
    )


def _split_discord_content(content: str) -> list[str]:
    """Split content into Discord-safe chunks (<=2000 chars each)."""
    if len(content) <= DISCORD_MAX_CONTENT:
        return [content]

    chunks: list[str] = []
    current = ""

    # Prefer line boundaries to keep formatting readable.
    for line in content.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= DISCORD_MAX_CONTENT:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        # If a single line is too long, force-split it.
        remaining = line
        while len(remaining) > DISCORD_MAX_CONTENT:
            chunks.append(remaining[:DISCORD_MAX_CONTENT])
            remaining = remaining[DISCORD_MAX_CONTENT:]
        current = remaining

    if current:
        chunks.append(current)

    return chunks


async def _post_webhook(webhook_url: str, content: str) -> None:
    """POST a message payload to a Discord webhook."""
    async with httpx.AsyncClient() as client:
        for chunk in _split_discord_content(content):
            resp = await client.post(
                webhook_url, json={"content": chunk}, timeout=10,
            )
            _raise_with_discord_details(resp, "sending webhook message")


async def _send_bot_dm(bot_token: str, user_id: str, content: str) -> None:
    """Send a DM to a user via the Discord bot API."""
    normalized_user_id = user_id.strip()
    if not normalized_user_id.isdigit():
        raise ValueError(
            "LONGSHOTEL_DISCORD_USER_ID must be a numeric Discord user ID "
            "(snowflake), e.g. 123456789012345678"
        )

    headers = {"Authorization": f"Bot {bot_token}"}
    async with httpx.AsyncClient() as client:
        # Open (or reuse) a DM channel with the user
        resp = await client.post(
            f"{DISCORD_API}/users/@me/channels",
            json={"recipient_id": normalized_user_id},
            headers=headers,
            timeout=10,
        )
        _raise_with_discord_details(resp, "creating DM channel")
        channel_id = resp.json()["id"]

        # Send one or more messages if content exceeds Discord's 2000-char limit.
        for chunk in _split_discord_content(content):
            resp = await client.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                json={"content": chunk},
                headers=headers,
                timeout=10,
            )
            _raise_with_discord_details(resp, "sending DM message")


async def _send_discord(settings: Settings, content: str) -> None:
    """Send a Discord message using the best available method.

    Bot DM takes priority over webhook when both are configured.
    """
    if settings.discord_bot_token and settings.discord_user_id:
        await _send_bot_dm(
            settings.discord_bot_token, settings.discord_user_id, content,
        )
    elif settings.discord_webhook_url:
        await _post_webhook(settings.discord_webhook_url, content)


async def send_discord_notification(
    settings: Settings,
    newly_available: list[Hotel],
) -> None:
    """Post a message about newly available hotels."""
    if not newly_available:
        return

    lines = ["**🏨 New Hotel Availability for SDCC 2026!**\n"]
    lines.extend(_format_hotel_line(h) for h in newly_available)
    await _send_discord(settings, "\n".join(lines))


async def send_discord_soldout_notification(
    settings: Settings,
    newly_soldout: list[Hotel],
) -> None:
    """Post a message about newly sold-out hotels."""
    if not newly_soldout:
        return

    lines = ["**⚠️ Hotels Sold Out — SDCC 2026**\n"]
    for hotel in newly_soldout:
        lines.append(f"• **{hotel.name}** ({hotel.hotel_chain}) — SOLD OUT")
    await _send_discord(settings, "\n".join(lines))


async def send_discord_summary(
    settings: Settings,
    hotels: list[Hotel],
) -> None:
    """Post a full availability summary."""
    available = [h for h in hotels if h.is_available]
    soldout = [h for h in hotels if not h.is_available]

    lines = [
        f"**📋 SDCC 2026 Hotel Summary** — "
        f"{len(available)} available, {len(soldout)} sold out\n",
    ]
    if available:
        lines.append("**Available:**")
        lines.extend(_format_hotel_line(h) for h in available)
    if soldout:
        lines.append("\n**Sold out:**")
        for h in soldout:
            lines.append(f"• ~~{h.name}~~ ({h.hotel_chain})")

    await _send_discord(settings, "\n".join(lines))


async def send_discord_general_notification(
    settings: Settings,
    hotels: list[Hotel],
    arrive: str,
    depart: str,
) -> None:
    """Post a message about hotels with new general availability (any dates)."""
    if not hotels:
        return

    lines = [
        f"**🔔 Hotels now available for OTHER dates (not {arrive}–{depart})**\n",
        "Check OnPeak for exact date ranges:\n",
    ]
    lines.extend(
        f"• **{h.name}** ({h.hotel_chain}) — {h.distance:.2f} mi"
        for h in hotels
    )
    await _send_discord(settings, "\n".join(lines))


async def send_discord_interval_summary(
    settings: Settings,
    newly_available_net: list[Hotel],
    newly_soldout_net: list[Hotel | None],
    current_available: list[Hotel],
    poll_count: int,
    error_count: int,
    period_start: str,
    period_end: str,
) -> None:
    """Post a periodic interval summary covering net changes since the last report."""
    arrive, depart = settings.arrive, settings.depart
    lines = [
        f"**⏱ Hourly Report — SDCC 2026 ({arrive}–{depart})**",
        f"Period: {period_start} → {period_end}",
        f"Polls: {poll_count} | Errors: {error_count}",
        f"Currently available: {len(current_available)} hotel(s)",
    ]
    if newly_available_net:
        lines.append(f"\n**➕ Became available ({len(newly_available_net)}):**")
        lines.extend(_format_hotel_line(h) for h in newly_available_net)
    if newly_soldout_net:
        lines.append(f"\n**➖ Sold out ({len(newly_soldout_net)}):**")
        for h in newly_soldout_net:
            name = h.name if h else "Unknown hotel"
            chain = f" ({h.hotel_chain})" if h else ""
            lines.append(f"• **{name}**{chain} — SOLD OUT")
    if not newly_available_net and not newly_soldout_net:
        lines.append("\nNo net changes this period.")
    await _send_discord(settings, "\n".join(lines))
