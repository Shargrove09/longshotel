"""Thin async HTTP client for the OnPeak Compass availability API."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from longshotel.config import Settings
from longshotel.models import Hotel

log = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_JSON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": _BROWSER_UA,
}


def _build_url(settings: Settings, block_index: int | None = None) -> str:
    """Construct the availability endpoint URL."""
    block = block_index if block_index is not None else settings.block_index
    return (
        f"{settings.base_url}/e/{settings.event_code}"
        f"/{block}/avail"
    )


def _build_params(settings: Settings) -> dict[str, str]:
    """Query-string parameters expected by the OnPeak API."""
    return {
        "arrive": settings.arrive,
        "depart": settings.depart,
        "_": str(int(time.time() * 1000)),  # cache-buster
    }


# ---------------------------------------------------------------------------
# Playwright-based live fetcher
# ---------------------------------------------------------------------------

_NAVIGATE_TIMEOUT_MS = 30_000


async def _fetch_via_browser(settings: Settings) -> list[Hotel]:
    """Navigate the OnPeak category page and intercept the /avail XHR.

    Strategy
    --------
    1. Navigate to ``/e/{event}/in/category/{category_id}``.  This is the
       public entry point that works without Queue-it (even in incognito).
    2. The server redirects to e.g. ``/e/{event}/10#hotels`` and the page's
       own JavaScript fires an XHR to ``/{block}/avail?_=…``.
    3. We intercept that response, extract the block number, and parse the
       hotel JSON directly.
    4. If the user specified ``arrive``/``depart`` dates we make a follow-up
       ``/avail`` call with those parameters using the established session.
    """
    from playwright.async_api import async_playwright
    import asyncio
    import re as _re

    category_url = (
        f"{settings.base_url}/e/{settings.event_code}"
        f"/in/category/{settings.category_id}"
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome",
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=_BROWSER_UA,
            locale="en-US",
            timezone_id="America/Los_Angeles",
        )
        page = await context.new_page()

        # Hide navigator.webdriver flag that marks headless browsers.
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => false});
        """)

        # Future that resolves when we see the first /avail response.
        avail_future: asyncio.Future[tuple[int, dict]] = asyncio.get_event_loop().create_future()

        async def _on_response(response):
            """Capture the first /avail XHR response from the page."""
            if avail_future.done():
                return
            url = response.url
            if "/avail" not in url:
                return
            try:
                # Extract block number from the URL  (e.g. /e/CODE/9/avail)
                m = _re.search(r"/e/[^/]+/(\d+)/avail", url)
                block = int(m.group(1)) if m else settings.block_index
                body = await response.json()
                if isinstance(body, dict) and "hotels" in body:
                    log.info(
                        "[browser] intercepted /avail (block %d) — %d hotel entries",
                        block, len(body.get("hotels", [])),
                    )
                    if not avail_future.done():
                        avail_future.set_result((block, body))
            except Exception as exc:
                log.debug("[browser] failed to parse intercepted /avail: %s", exc)

        page.on("response", _on_response)

        # ---- navigate to the category page ----
        log.debug("[browser] navigating → %s", category_url)
        try:
            await page.goto(
                category_url,
                timeout=_NAVIGATE_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
        except Exception as exc:
            log.debug("[browser] goto exception: %s", exc)

        # Wait for the page's own XHR to fire and return hotel data.
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        # Give the interceptor a moment if it hasn't resolved yet.
        if not avail_future.done():
            try:
                await asyncio.wait_for(asyncio.shield(avail_future), timeout=10)
            except asyncio.TimeoutError:
                pass

        if avail_future.done():
            discovered_block, initial_data = avail_future.result()
            log.debug("[browser] discovered block index: %d", discovered_block)

            # If dates were specified, make a follow-up /avail call with them
            # (the page's initial XHR may not include arrive/depart).
            if settings.arrive and settings.depart:
                avail_url = _build_url(settings, block_index=discovered_block)
                params = _build_params(settings)
                query = "&".join(f"{k}={v}" for k, v in params.items())
                full_url = f"{avail_url}?{query}"
                log.debug("[browser] fetching /avail with dates → %s", full_url)
                try:
                    resp = await context.request.get(
                        full_url,
                        headers={
                            "Accept": "application/json, text/plain, */*",
                            "X-Requested-With": "XMLHttpRequest",
                            "Referer": page.url,
                        },
                    )
                    body = await resp.json()
                    if isinstance(body, dict) and "hotels" in body:
                        log.info(
                            "[browser] /avail with dates returned %d hotel entries",
                            len(body.get("hotels", [])),
                        )
                        await browser.close()
                        return _parse_hotels_from_data(body)
                except Exception as exc:
                    log.debug("[browser] follow-up /avail failed: %s — using initial data", exc)

            # Fall back to the initial intercepted data.
            await browser.close()
            return _parse_hotels_from_data(initial_data)

        # No /avail response was intercepted — try a manual call using the
        # fallback block_index from settings.
        log.warning("[browser] no /avail XHR intercepted — trying fallback block %d", settings.block_index)
        avail_url = _build_url(settings)
        params = _build_params(settings)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        full_url = f"{avail_url}?{query}"
        try:
            resp = await context.request.get(
                full_url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": page.url,
                },
            )
            body = await resp.json()
            if isinstance(body, dict) and "hotels" in body:
                await browser.close()
                return _parse_hotels_from_data(body)
        except Exception as exc:
            log.debug("[browser] fallback /avail failed: %s", exc)

        await browser.close()

    log.info(
        "Could not retrieve hotel data — the sale may not be active yet "
        "for this event / date range."
    )
    return []


def _parse_hotels_from_data(data: dict[str, Any]) -> list[Hotel]:
    """Parse a pre-decoded /avail JSON dict into sorted Hotel list."""
    raw_hotels = data.get("hotels", [])
    log.debug("[parse] hotel entries: %d", len(raw_hotels))
    # The API returns hotels as a list; support dict (legacy) as fallback.
    items = raw_hotels if isinstance(raw_hotels, list) else raw_hotels.values()
    hotels: list[Hotel] = []
    for hotel_data in items:
        try:
            hotels.append(Hotel.model_validate(hotel_data))
        except Exception:
            continue
    hotels.sort(key=lambda h: h.distance)
    return hotels


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_hotels(
    settings: Settings | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[Hotel]:
    """Fetch the current hotel availability list.

    When *client* is provided (unit tests), a single direct ``/avail`` call
    is made via ``httpx``.  For live requests the function delegates to a
    headless Chromium browser via Playwright so that the full Queue-it /
    cookie-check session dance is handled natively by the browser engine.
    """
    if settings is None:
        settings = Settings()

    if client is not None:
        # Test path — direct httpx call, no session dance.
        url = _build_url(settings)
        params = _build_params(settings)
        resp = await client.get(url, headers=_JSON_HEADERS, params=params, timeout=15)
        resp.raise_for_status()
        return _parse_hotels(resp)

    return await _fetch_via_browser(settings)


def _parse_hotels(resp: httpx.Response) -> list[Hotel]:
    """Parse the raw ``/avail`` JSON response into a sorted list of Hotels.

    Used only for the httpx test path.
    """
    try:
        data: dict[str, Any] = resp.json()
    except Exception as exc:
        preview = resp.text[:500] if resp.text else "<empty body>"
        raise ValueError(
            f"Failed to parse JSON (HTTP {resp.status_code}, "
            f"Content-Type: {resp.headers.get('content-type', '?')}):\n{preview}"
        ) from exc

    log.debug("[parse] top-level JSON keys: %s", list(data.keys()))

    raw_hotels = data.get("hotels", [])
    log.debug("[parse] hotel entries: %d", len(raw_hotels))
    # The API returns hotels as a list; support dict (legacy) as fallback.
    items = raw_hotels if isinstance(raw_hotels, list) else raw_hotels.values()

    hotels: list[Hotel] = []
    for hotel_data in items:
        try:
            hotels.append(Hotel.model_validate(hotel_data))
        except Exception:
            continue

    hotels.sort(key=lambda h: h.distance)
    return hotels


async def fetch_available_hotels(
    settings: Settings | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[Hotel]:
    """Convenience wrapper that returns only hotels with rooms available."""
    hotels = await fetch_hotels(settings, client=client)
    return [h for h in hotels if h.is_available]
