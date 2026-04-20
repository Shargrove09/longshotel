"""Tests for the direct httpx fetch path, block-index discovery, and retry logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from longshotel.client import (
    FetchResult,
    _block_cache,
    _discover_block,
    _fetch_via_httpx,
    _invalidate_block_cache,
    fetch_hotels_dual,
)
from longshotel.config import Settings

SETTINGS = Settings(
    event_code="TEST",
    block_index=3,
    category_id=42031,
    base_url="https://compass.onpeak.com",
    arrive="2026-07-21",
    depart="2026-07-27",
)

AVAIL_RESPONSE = {
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
    ]
}


def _clean_cache() -> None:
    """Remove TEST entry from the block cache between tests."""
    _block_cache.pop("TEST", None)


# ---------------------------------------------------------------------------
# Block discovery
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_discover_block_uses_redirect_url() -> None:
    """Block index is parsed from the final URL after redirect."""
    _clean_cache()
    category_url = "https://compass.onpeak.com/e/TEST/in/category/42031"
    # Simulate redirect to block 10
    respx.get(category_url).mock(
        return_value=httpx.Response(
            302,
            headers={"location": "https://compass.onpeak.com/e/TEST/10"},
        )
    )
    respx.get("https://compass.onpeak.com/e/TEST/10").mock(
        return_value=httpx.Response(200, text="<html/>")
    )

    async with httpx.AsyncClient(follow_redirects=True) as client:
        block = await _discover_block(SETTINGS, client)

    assert block == 10
    assert _block_cache.get("TEST") == 10
    _clean_cache()


@respx.mock
@pytest.mark.asyncio
async def test_discover_block_falls_back_on_failure() -> None:
    """Block discovery falls back to settings.block_index on HTTP error."""
    _clean_cache()
    respx.get("https://compass.onpeak.com/e/TEST/in/category/42031").mock(
        return_value=httpx.Response(500)
    )

    async with httpx.AsyncClient(follow_redirects=True) as client:
        block = await _discover_block(SETTINGS, client)

    assert block == SETTINGS.block_index  # fallback
    assert "TEST" not in _block_cache
    _clean_cache()


@respx.mock
@pytest.mark.asyncio
async def test_discover_block_uses_cache() -> None:
    """Cached block index is returned without making an HTTP request."""
    _block_cache["TEST"] = 7

    async with httpx.AsyncClient(follow_redirects=True) as client:
        block = await _discover_block(SETTINGS, client)

    assert block == 7
    _clean_cache()


def test_invalidate_block_cache() -> None:
    """_invalidate_block_cache removes the cached entry."""
    _block_cache["TEST"] = 5
    _invalidate_block_cache("TEST")
    assert "TEST" not in _block_cache


# ---------------------------------------------------------------------------
# _fetch_via_httpx
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_fetch_via_httpx_returns_result() -> None:
    """_fetch_via_httpx returns general and dated hotel lists."""
    _clean_cache()
    _block_cache["TEST"] = 3  # skip discovery

    respx.get("https://compass.onpeak.com/e/TEST/3/avail").mock(
        return_value=httpx.Response(200, json=AVAIL_RESPONSE)
    )

    result = await _fetch_via_httpx(SETTINGS)

    assert result is not None
    assert len(result.general) == 1
    assert len(result.dated) == 1
    assert result.dated[0].name == "Hotel Alpha"
    _clean_cache()


@respx.mock
@pytest.mark.asyncio
async def test_fetch_via_httpx_returns_none_on_http_error() -> None:
    """_fetch_via_httpx returns None on HTTP error (caller falls back to browser)."""
    _clean_cache()
    _block_cache["TEST"] = 3

    respx.get("https://compass.onpeak.com/e/TEST/3/avail").mock(
        return_value=httpx.Response(503)
    )

    result = await _fetch_via_httpx(SETTINGS)

    assert result is None
    # Block cache should be cleared so it is rediscovered next time
    assert "TEST" not in _block_cache
    _clean_cache()


@respx.mock
@pytest.mark.asyncio
async def test_fetch_via_httpx_returns_none_on_missing_hotels_key() -> None:
    """_fetch_via_httpx returns None when the JSON has no 'hotels' key."""
    _clean_cache()
    _block_cache["TEST"] = 3

    respx.get("https://compass.onpeak.com/e/TEST/3/avail").mock(
        return_value=httpx.Response(200, json={"unexpected": "data"})
    )

    result = await _fetch_via_httpx(SETTINGS)

    assert result is None
    _clean_cache()


# ---------------------------------------------------------------------------
# fetch_hotels_dual retry logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_hotels_dual_retries_on_failure() -> None:
    """fetch_hotels_dual retries up to _MAX_RETRIES times before raising."""
    from longshotel.client import _MAX_RETRIES

    call_count = 0

    async def _failing_httpx(_s):
        nonlocal call_count
        call_count += 1
        return None  # signals "nothing retrieved"

    async def _failing_browser(_s):
        raise RuntimeError("browser also failed")

    with (
        patch("longshotel.client._fetch_via_httpx", side_effect=_failing_httpx),
        patch("longshotel.client._fetch_via_browser", side_effect=_failing_browser),
        patch("longshotel.client.asyncio.sleep", new_callable=AsyncMock),
    ):
        with pytest.raises(RuntimeError, match="browser also failed"):
            await fetch_hotels_dual(SETTINGS)

    # Should have tried _MAX_RETRIES + 1 times total
    assert call_count == _MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_fetch_hotels_dual_succeeds_on_retry() -> None:
    """fetch_hotels_dual succeeds if a later attempt returns data."""
    attempts = 0
    good_result = FetchResult(general=[], dated=[])

    async def _httpx_side_effect(_s):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return None  # fail first attempt
        return good_result  # succeed on retry

    with (
        patch("longshotel.client._fetch_via_httpx", side_effect=_httpx_side_effect),
        patch("longshotel.client.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await fetch_hotels_dual(SETTINGS)

    assert result is good_result
    assert attempts == 2


@pytest.mark.asyncio
async def test_fetch_hotels_dual_httpx_preferred_over_browser() -> None:
    """If httpx returns valid data the browser is never invoked."""
    good_result = FetchResult(general=[], dated=[])
    httpx_mock = AsyncMock(return_value=good_result)
    browser_mock = AsyncMock()

    with (
        patch("longshotel.client._fetch_via_httpx", httpx_mock),
        patch("longshotel.client._fetch_via_browser", browser_mock),
    ):
        result = await fetch_hotels_dual(SETTINGS)

    assert result is good_result
    browser_mock.assert_not_called()
