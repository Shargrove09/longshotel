"""Tests for the OnPeak API client."""

import httpx
import pytest
import respx

from longshotel.client import fetch_available_hotels, fetch_hotels
from longshotel.config import Settings

MOCK_RESPONSE = {
    "hotels": [
        {
            "hotelId": 1001,
            "name": "Hotel Alpha",
            "hotelChain": "Chain A",
            "latitude": 32.71,
            "longitude": -117.16,
            "distance": 0.3,
            "distanceUnits": "Miles",
            "starRatingDecimal": 4,
            "amenities": [{"type": "Pool"}],
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
        },
        {
            "hotelId": 1002,
            "name": "Hotel Beta",
            "hotelChain": "Chain B",
            "latitude": 32.72,
            "longitude": -117.17,
            "distance": 1.1,
            "distanceUnits": "Miles",
            "starRatingDecimal": 3,
            "amenities": [],
            "promotions": [],
            "hasPromo": False,
            "type": "EVENT",
            "starRating": 0,
            "images": {},
            "avail": {
                "hotelId": 1002,
                "status": "SOLDOUT",
                "lowestAvgRateNumeric": 150,
                "inclusiveLowestAvgRateNumeric": 160,
                "showInclusiveLowestAvgRate": False,
                "totalAdditionalFees": 0,
                "additionalFeesMessage": "",
                "additionalFeesLong": "",
                "isServiceFeeIncluded": False,
                "roomsBooked": 0,
                "maxAllowed": 0,
                "groupMax": 0,
                "hotelGroupMax": 0,
                "maxOneBlockReservations": 0,
                "maxMultiBlockReservations": 0,
            },
        },
    ]
}

SETTINGS = Settings(
    event_code="TEST",
    block_index=1,
    base_url="https://compass.onpeak.com",
    arrive="2026-07-21",
    depart="2026-07-27",
)


@respx.mock
@pytest.mark.asyncio
async def test_fetch_hotels_returns_all() -> None:
    respx.get("https://compass.onpeak.com/e/TEST/1/avail").mock(
        return_value=httpx.Response(200, json=MOCK_RESPONSE)
    )

    async with httpx.AsyncClient() as client:
        hotels = await fetch_hotels(SETTINGS, client=client)

    assert len(hotels) == 2
    # Sorted by distance
    assert hotels[0].name == "Hotel Alpha"
    assert hotels[1].name == "Hotel Beta"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_available_hotels_filters_soldout() -> None:
    respx.get("https://compass.onpeak.com/e/TEST/1/avail").mock(
        return_value=httpx.Response(200, json=MOCK_RESPONSE)
    )

    async with httpx.AsyncClient() as client:
        hotels = await fetch_available_hotels(SETTINGS, client=client)

    assert len(hotels) == 1
    assert hotels[0].name == "Hotel Alpha"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_hotels_empty_response() -> None:
    respx.get("https://compass.onpeak.com/e/TEST/1/avail").mock(
        return_value=httpx.Response(200, json={"hotels": []})
    )

    async with httpx.AsyncClient() as client:
        hotels = await fetch_hotels(SETTINGS, client=client)

    assert hotels == []


@respx.mock
@pytest.mark.asyncio
async def test_fetch_hotels_malformed_entry_skipped() -> None:
    bad_response = {
        "hotels": [
            {"bad": "data"},  # Missing required fields
            MOCK_RESPONSE["hotels"][0],
        ]
    }
    respx.get("https://compass.onpeak.com/e/TEST/1/avail").mock(
        return_value=httpx.Response(200, json=bad_response)
    )

    async with httpx.AsyncClient() as client:
        hotels = await fetch_hotels(SETTINGS, client=client)

    assert len(hotels) == 1
    assert hotels[0].name == "Hotel Alpha"
