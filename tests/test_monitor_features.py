"""Tests for new monitor features: StatusReportAggregator, state persistence, flex dates."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from longshotel.client import FetchResult
from longshotel.config import NotifyMode, Settings
from longshotel.models import Hotel
from longshotel.monitor import (
    StatusReportAggregator,
    _flex_date_ranges,
    _load_state,
    _save_state,
    run_monitor,
)

WEBHOOK_URL = "https://discord.com/api/webhooks/test/fake"

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

SOLDOUT_HOTEL_DATA = {
    **AVAILABLE_HOTEL_DATA,
    "hotelId": 1002,
    "name": "Hotel Beta",
    "hotelChain": "Chain B",
    "distance": 1.1,
    "avail": {
        **AVAILABLE_HOTEL_DATA["avail"],
        "hotelId": 1002,
        "status": "SOLDOUT",
    },
}


def _hotel(data: dict) -> Hotel:
    return Hotel.model_validate(data)


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        event_code="TEST",
        block_index=1,
        base_url="https://compass.onpeak.com",
        arrive="2026-07-21",
        depart="2026-07-27",
        poll_interval_seconds=1,
        discord_webhook_url=WEBHOOK_URL,
        notify_mode=NotifyMode.changes,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class _StopMonitor(Exception):
    pass


# ---------------------------------------------------------------------------
# StatusReportAggregator
# ---------------------------------------------------------------------------

def test_aggregator_initial_counts_are_zero() -> None:
    agg = StatusReportAggregator()
    assert agg._polls_ok == 0
    assert agg._polls_failed == 0
    assert agg._changes == []


def test_aggregator_records_polls() -> None:
    agg = StatusReportAggregator()
    agg.record_poll_ok()
    agg.record_poll_ok()
    agg.record_poll_failed()
    assert agg._polls_ok == 2
    assert agg._polls_failed == 1


def test_aggregator_records_changes() -> None:
    agg = StatusReportAggregator()
    agg.record_available(1001, "Hotel Alpha")
    agg.record_soldout(1002, "Hotel Beta")
    assert len(agg._changes) == 2
    assert agg._changes[0].event_type == "available"
    assert agg._changes[1].event_type == "soldout"


def test_aggregator_generate_report_resets_counters() -> None:
    agg = StatusReportAggregator()
    agg.record_poll_ok()
    agg.record_available(1001, "Hotel Alpha")

    hotels = [_hotel(AVAILABLE_HOTEL_DATA), _hotel(SOLDOUT_HOTEL_DATA)]
    settings = _make_settings()
    next_time = datetime.now(timezone.utc) + timedelta(hours=1)

    report = agg.generate_report(hotels, hotels, settings, next_time)

    # Report content
    assert "Status Report" in report
    assert "Hotel Alpha" in report
    assert "1 hotel(s) became available" in report
    assert "1 successful" in report

    # Counters reset
    assert agg._polls_ok == 0
    assert agg._polls_failed == 0
    assert agg._changes == []


def test_aggregator_report_no_changes_says_none() -> None:
    agg = StatusReportAggregator()
    agg.record_poll_ok()

    hotels = [_hotel(SOLDOUT_HOTEL_DATA)]
    settings = _make_settings()
    next_time = datetime.now(timezone.utc) + timedelta(hours=1)

    report = agg.generate_report(hotels, hotels, settings, next_time)
    assert "Changes This Period:** None" in report


def test_aggregator_report_includes_next_report_time() -> None:
    agg = StatusReportAggregator()
    next_time = datetime(2026, 4, 20, 20, 0, 0, tzinfo=timezone.utc)
    hotels: list[Hotel] = []
    settings = _make_settings()

    report = agg.generate_report(hotels, hotels, settings, next_time)
    assert "2026-04-20 20:00" in report


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def test_save_and_load_state_roundtrip() -> None:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        state_path = f.name

    settings = _make_settings(state_file=state_path)
    general_ids = {1001, 1002}
    dated_ids = {1001}

    _save_state(settings, general_ids, dated_ids)

    loaded_general, loaded_dated = _load_state(settings)

    assert loaded_general == general_ids
    assert loaded_dated == dated_ids

    Path(state_path).unlink(missing_ok=True)


def test_load_state_returns_none_if_no_file() -> None:
    settings = _make_settings(state_file="/tmp/nonexistent_state_xyz.json")
    g, d = _load_state(settings)
    assert g is None
    assert d is None


def test_load_state_returns_none_if_no_state_file_configured() -> None:
    settings = _make_settings(state_file=None)
    g, d = _load_state(settings)
    assert g is None
    assert d is None


def test_save_state_skips_if_no_state_file_configured() -> None:
    settings = _make_settings(state_file=None)
    # Should not raise
    _save_state(settings, {1001}, {1001})


def test_load_state_handles_corrupt_file() -> None:
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        f.write("not-valid-json{{{")
        state_path = f.name

    settings = _make_settings(state_file=state_path)
    g, d = _load_state(settings)
    assert g is None
    assert d is None

    Path(state_path).unlink(missing_ok=True)


def test_state_file_content_is_valid_json() -> None:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        state_path = f.name

    settings = _make_settings(state_file=state_path)
    _save_state(settings, {1001, 1002}, {1001})

    raw = json.loads(Path(state_path).read_text())
    assert "general_ids" in raw
    assert "dated_ids" in raw
    assert "timestamp" in raw
    assert sorted(raw["general_ids"]) == [1001, 1002]
    assert raw["dated_ids"] == [1001]

    Path(state_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Flex date ranges
# ---------------------------------------------------------------------------

def test_flex_date_ranges_zero_returns_empty() -> None:
    settings = _make_settings(date_flex_days=0)
    assert _flex_date_ranges(settings) == []


def test_flex_date_ranges_one_day() -> None:
    settings = _make_settings(
        arrive="2026-07-21",
        depart="2026-07-27",
        date_flex_days=1,
    )
    ranges = _flex_date_ranges(settings)
    # ±1 day shift, same 6-night stay → 2 ranges
    assert len(ranges) == 2
    assert ("2026-07-20", "2026-07-26") in ranges
    assert ("2026-07-22", "2026-07-28") in ranges


def test_flex_date_ranges_two_days() -> None:
    settings = _make_settings(
        arrive="2026-07-21",
        depart="2026-07-27",
        date_flex_days=2,
    )
    ranges = _flex_date_ranges(settings)
    # ±1 and ±2 shifts → 4 ranges
    assert len(ranges) == 4
    assert ("2026-07-19", "2026-07-25") in ranges
    assert ("2026-07-20", "2026-07-26") in ranges
    assert ("2026-07-22", "2026-07-28") in ranges
    assert ("2026-07-23", "2026-07-29") in ranges


def test_flex_date_ranges_preserves_stay_length() -> None:
    settings = _make_settings(
        arrive="2026-07-21",
        depart="2026-07-27",
        date_flex_days=3,
    )
    ranges = _flex_date_ranges(settings)
    stay_days = 6  # 27 - 21
    from datetime import date
    for arrive_str, depart_str in ranges:
        d_arrive = date.fromisoformat(arrive_str)
        d_depart = date.fromisoformat(depart_str)
        assert (d_depart - d_arrive).days == stay_days


# ---------------------------------------------------------------------------
# Monitor integration: state loaded from file → detects first-run changes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_monitor_loads_saved_state_and_detects_changes() -> None:
    """When state is loaded from file, changes since last run are detected."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        state_path = f.name

    # Saved state: hotel 1001 was available, 1002 was not
    initial_settings = _make_settings(state_file=state_path)
    _save_state(initial_settings, general_ids={1001}, dated_ids={1001})

    # New state: hotel 1001 sold out, 1002 now available
    hotel_alpha_sold = _hotel({**AVAILABLE_HOTEL_DATA, "avail": {**AVAILABLE_HOTEL_DATA["avail"], "status": "SOLDOUT"}})
    hotel_beta_avail = _hotel({**SOLDOUT_HOTEL_DATA, "avail": {**SOLDOUT_HOTEL_DATA["avail"], "status": "AVAILABLE"}})
    new_hotels = [hotel_alpha_sold, hotel_beta_avail]

    fetch_mock = AsyncMock(return_value=FetchResult(general=new_hotels, dated=new_hotels))
    send_avail = AsyncMock()
    send_soldout = AsyncMock()

    settings = _make_settings(notify_mode=NotifyMode.changes, state_file=state_path)

    with (
        patch("longshotel.monitor.fetch_hotels_dual", fetch_mock),
        patch("longshotel.monitor.send_discord_notification", send_avail),
        patch("longshotel.monitor.send_discord_soldout_notification", send_soldout),
        patch("longshotel.monitor.asyncio.sleep", side_effect=_StopMonitor),
    ):
        with pytest.raises(_StopMonitor):
            await run_monitor(settings)

    # Beta newly available, Alpha newly sold out
    send_avail.assert_called_once()
    send_soldout.assert_called_once()

    Path(state_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Monitor integration: status report is sent at configured interval
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_report_sent_at_configured_interval() -> None:
    """A status report is sent when status_report_interval_seconds elapses."""
    from unittest.mock import MagicMock

    hotels = [_hotel(AVAILABLE_HOTEL_DATA)]
    fetch_mock = AsyncMock(return_value=FetchResult(general=hotels, dated=hotels))
    send_report = AsyncMock()

    settings = _make_settings(
        notify_mode=NotifyMode.changes,
        status_report_interval_seconds=3600,  # 1 hour
    )

    # First 2 calls establish period_start and next_report_time at base_time.
    # All later calls return a time 2 hours ahead so the check fires.
    base_time = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    after_interval = datetime(2026, 4, 20, 14, 0, 0, tzinfo=timezone.utc)
    call_count = 0

    def _fake_now(tz=None):
        nonlocal call_count
        call_count += 1
        return base_time if call_count <= 2 else after_interval

    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.side_effect = _fake_now

    with (
        patch("longshotel.monitor.fetch_hotels_dual", fetch_mock),
        patch("longshotel.monitor.send_discord_status_report", send_report),
        patch("longshotel.monitor.asyncio.sleep", side_effect=[None, _StopMonitor]),
        patch("longshotel.monitor.datetime", mock_dt),
    ):
        with pytest.raises(_StopMonitor):
            await run_monitor(settings)

    assert send_report.call_count >= 1


@pytest.mark.asyncio
async def test_status_report_not_sent_when_interval_is_zero() -> None:
    """No status report is sent when status_report_interval_seconds=0."""
    hotels = [_hotel(AVAILABLE_HOTEL_DATA)]
    fetch_mock = AsyncMock(return_value=FetchResult(general=hotels, dated=hotels))
    send_report = AsyncMock()

    settings = _make_settings(
        notify_mode=NotifyMode.changes,
        status_report_interval_seconds=0,
    )

    with (
        patch("longshotel.monitor.fetch_hotels_dual", fetch_mock),
        patch("longshotel.monitor.send_discord_status_report", send_report),
        patch("longshotel.monitor.asyncio.sleep", side_effect=_StopMonitor),
    ):
        with pytest.raises(_StopMonitor):
            await run_monitor(settings)

    send_report.assert_not_called()


# ---------------------------------------------------------------------------
# Monitor integration: flex date scanning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flex_date_notification_sent_for_new_availability() -> None:
    """A flex-date notification is sent when a hotel becomes available on a flex range."""
    main_hotels = [_hotel(SOLDOUT_HOTEL_DATA)]
    flex_hotel_avail = _hotel(AVAILABLE_HOTEL_DATA)

    # Main fetch always returns soldout; flex returns an available hotel on tick 2
    fetch_main = AsyncMock(return_value=FetchResult(general=main_hotels, dated=main_hotels))

    tick = 0

    async def _fetch_flex(_settings, arrive, depart):
        nonlocal tick
        tick += 1
        if tick >= 2:
            return [flex_hotel_avail]
        return []

    send_flex = AsyncMock()

    settings = _make_settings(
        notify_mode=NotifyMode.changes,
        date_flex_days=1,
    )

    with (
        patch("longshotel.monitor.fetch_hotels_dual", fetch_main),
        patch("longshotel.monitor.fetch_dated_hotels", side_effect=_fetch_flex),
        patch("longshotel.monitor.send_discord_flex_notification", send_flex),
        patch("longshotel.monitor.asyncio.sleep", side_effect=[None, None, _StopMonitor]),
    ):
        with pytest.raises(_StopMonitor):
            await run_monitor(settings)

    # Flex notification fired when the hotel appeared on the second tick
    assert send_flex.call_count >= 1
