"""Tests for new Discord notification functions (flex and status report)."""

from __future__ import annotations

import httpx
import pytest
import respx

from longshotel.config import Settings
from longshotel.models import Hotel
from longshotel.notifications import (
    send_discord_flex_notification,
    send_discord_status_report,
)

WEBHOOK_URL = "https://discord.com/api/webhooks/test/fake"

_BASE = dict(
    event_code="TEST",
    block_index=1,
    base_url="https://compass.onpeak.com",
    arrive="2026-07-21",
    depart="2026-07-27",
)

AVAILABLE_HOTEL_DATA = {
    "hotelId": 1001,
    "name": "Hotel Alpha",
    "hotelChain": "Chain A",
    "latitude": 32.71,
    "longitude": -117.16,
    "distance": 0.3,
    "distanceUnits": "Miles",
    "starRatingDecimal": 4,
    "amenities": [],
    "promotions": [],
    "hasPromo": False,
    "type": "EVENT",
    "starRating": 0,
    "images": {},
    "avail": {
        "hotelId": 1001,
        "status": "AVAILABLE",
        "lowestAvgRateNumeric": 200,
        "inclusiveLowestAvgRateNumeric": 215,
        "showInclusiveLowestAvgRate": True,
        "totalAdditionalFees": 15,
        "additionalFeesMessage": "",
        "additionalFeesLong": "",
        "isServiceFeeIncluded": False,
        "roomsBooked": 5,
        "maxAllowed": 3,
        "groupMax": 3,
        "hotelGroupMax": 100,
        "maxOneBlockReservations": 0,
        "maxMultiBlockReservations": 0,
    },
}


def _webhook_settings() -> Settings:
    return Settings(**_BASE, discord_webhook_url=WEBHOOK_URL)


@respx.mock
@pytest.mark.asyncio
async def test_send_discord_flex_notification_posts_message() -> None:
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
    hotel = Hotel.model_validate(AVAILABLE_HOTEL_DATA)

    await send_discord_flex_notification(
        _webhook_settings(), [hotel], "2026-07-20", "2026-07-26"
    )

    assert route.called
    payload = route.calls[0].request.content
    assert b"Hotel Alpha" in payload
    assert b"2026-07-20" in payload
    assert b"2026-07-26" in payload


@respx.mock
@pytest.mark.asyncio
async def test_send_discord_flex_notification_skips_empty_list() -> None:
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))

    await send_discord_flex_notification(
        _webhook_settings(), [], "2026-07-20", "2026-07-26"
    )

    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_send_discord_status_report_posts_message() -> None:
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))

    report = "📊 **SDCC 2026 — Status Report**\nPeriod: …\nPolls: 10 successful / 0 failed"
    await send_discord_status_report(_webhook_settings(), report)

    assert route.called
    payload = route.calls[0].request.content
    assert b"Status Report" in payload
    assert b"Polls" in payload
