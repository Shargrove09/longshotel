# longshotel

Monitor hotel availability for **San Diego Comic-Con 2026** via the [OnPeak Compass](https://compass.onpeak.com) API.

> **No browser automation required.** This tool talks directly to the OnPeak JSON API using `httpx`, so it runs in ~1–2 seconds with zero heavyweight dependencies (no Playwright/Chromium).

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

# Custom poll interval
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
| `LONGSHOTEL_DISCORD_WEBHOOK_URL` | *(none)* | Discord webhook for notifications |
| `LONGSHOTEL_SHOW_SOLDOUT` | `false` | Show sold-out hotels |

## How It Works

The tool calls the OnPeak Compass availability API directly:

```
GET https://compass.onpeak.com/e/{event_code}/{block}/avail?arrive={date}&depart={date}
```

This returns a JSON object with a `hotels` property containing all participating hotels, their amenities, distances from the venue, nightly rates, and availability status.

### Commands

- **`check`** — Single fetch, prints a table of available hotels, exits.
- **`monitor`** — Polls repeatedly, detects changes, and optionally sends Discord notifications when rooms become available.

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
├── client.py         # httpx-based OnPeak API client
├── config.py         # pydantic-settings configuration
├── display.py        # Rich table display
├── models.py         # Pydantic models for API response
├── monitor.py        # Polling monitor with change detection
└── notifications.py  # Discord webhook notifications
```
