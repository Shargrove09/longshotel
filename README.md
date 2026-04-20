# longshotel

Monitor hotel availability for **San Diego Comic-Con 2026** via the [OnPeak Compass](https://compass.onpeak.com) API.

> **No browser automation required for normal operation.** The tool talks directly to the OnPeak JSON API using `httpx` (~1–2 s per check), with a Playwright/Chromium fallback available if bot-detection challenges are ever re-introduced.

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# One-shot availability check
longshotel check

# Show sold-out hotels too
longshotel check --show-soldout

# Custom dates
longshotel check --arrive 2026-07-20 --depart 2026-07-26

# Monitor for changes (polls every 60s by default)
longshotel monitor

# Custom poll interval + hourly status report to Discord
longshotel monitor --interval 30 --report-interval 3600

# Also scan ±1 day flex dates (same stay length, shifted by 1 day)
longshotel monitor --interval 30
```

## Configuration

All settings can be overridden via environment variables (prefix `LONGSHOTEL_`) or a `.env` file:

| Variable | Default | Description |
|---|---|---|
| `LONGSHOTEL_EVENT_CODE` | `43CCI2026HIR` | OnPeak event slug |
| `LONGSHOTEL_ARRIVE` | `2026-07-21` | Check-in date |
| `LONGSHOTEL_DEPART` | `2026-07-27` | Check-out date |
| `LONGSHOTEL_POLL_INTERVAL_SECONDS` | `60` | Monitor polling interval |
| `LONGSHOTEL_POLL_JITTER_SECONDS` | `6` | Random jitter added to each poll (±~10%) |
| `LONGSHOTEL_DISCORD_WEBHOOK_URL` | *(none)* | Discord webhook for notifications |
| `LONGSHOTEL_SHOW_SOLDOUT` | `false` | Show sold-out hotels |
| `LONGSHOTEL_STATE_FILE` | *(none)* | JSON file to persist availability state across restarts |
| `LONGSHOTEL_DATE_FLEX_DAYS` | `0` | Also scan ±N day shifts of the primary stay (same length) |
| `LONGSHOTEL_STATUS_REPORT_INTERVAL_SECONDS` | `0` | Send aggregated status report every N seconds (0 = disabled) |

## How It Works

The tool calls the OnPeak Compass availability API directly:

```
GET https://compass.onpeak.com/e/{event_code}/{block}/avail?arrive={date}&depart={date}
```

### Fast httpx Path (Primary)

1. GET `/e/{event}/in/category/{category_id}` — follows the HTTP redirect to discover the active **block index** (e.g. `/e/43CCI2026HIR/10`).  The block index is cached for the process lifetime.
2. GET `/{block}/avail` — fetches general availability (all dates).
3. GET `/{block}/avail?arrive=…&depart=…` — fetches date-specific availability.

The entire round-trip takes ~1–2 seconds.  If the server ever returns an unexpected response the block cache is cleared and a **Playwright browser fallback** is attempted automatically.

### Retry Logic

Each `fetch_hotels_dual` call retries up to **3 times** with exponential back-off (2 s → 4 s → 8 s) before propagating an error to the monitor loop.  `consecutive_errors` in the monitor only increments after all retries are exhausted, keeping short blips invisible to the change-detection logic.

### Commands

- **`check`** — Single fetch, prints a table of available hotels, exits.
- **`monitor`** — Polls repeatedly, detects changes, and optionally sends Discord notifications when rooms become available.

## Gap Mitigations

| Gap | Mitigation |
|---|---|
| Missed hotels (slow browser poll) | Direct httpx path: ~1–2 s vs ~30 s, enabling 60 s polling by default |
| Transient network errors | 3-retry exponential back-off inside each fetch cycle |
| Stale block index | Auto-discovery via HTTP redirect; cache cleared on HTTP errors |
| Missed changes across restarts | `LONGSHOTEL_STATE_FILE` persists baseline; changes since last run detected on first poll |
| Adjacent-date availability | `LONGSHOTEL_DATE_FLEX_DAYS` scans ±N-day shifts of the primary stay |

## Periodic Status Reports

Set `LONGSHOTEL_STATUS_REPORT_INTERVAL_SECONDS` (or `--report-interval` CLI flag) to receive an aggregated Discord report at the configured interval.  The report is **always sent** (regardless of whether any changes occurred) and includes:

- Period covered and duration
- Poll success / failure counts
- Hotels that became available or sold out during the period
- Current availability snapshot for your dates
- General availability (other dates)
- Time of the next scheduled report

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run a quick check against the live API
longshotel check --show-soldout
```

## Project Structure

```
src/longshotel/
├── __init__.py       # Package metadata
├── cli.py            # argparse CLI entry point
├── client.py         # httpx-based OnPeak API client (Playwright fallback)
├── config.py         # pydantic-settings configuration
├── display.py        # Rich table display
├── models.py         # Pydantic models for API response
├── monitor.py        # Polling monitor with change detection, state persistence,
│                     #   flex-date scanning, and status report aggregation
└── notifications.py  # Discord webhook / bot DM notifications
```
