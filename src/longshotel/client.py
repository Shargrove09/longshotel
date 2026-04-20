"""Thin async HTTP client for the OnPeak Compass availability API."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from longshotel.config import Settings
from longshotel.models import Hotel

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """Holds both general and date-specific hotel availability."""

    general: list[Hotel] = field(default_factory=list)
    """Hotels from the initial /avail call (no date filter)."""

    dated: list[Hotel] = field(default_factory=list)
    """Hotels for the user's specific arrive/depart range."""

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

_BLOCK_RE = __import__("re").compile(r"/e/[^/]+/(\d+)")


async def _fetch_via_httpx(settings: Settings) -> FetchResult | None:
    """Try to fetch availability using httpx only (fast path, no browser).

    Strategy
    --------
    1. GET ``/e/{event_code}`` with ``follow_redirects=True``.  The server
       redirects directly to the active block (e.g. ``/e/EVENT/22``), so the
       block number is discovered automatically without needing ``category_id``.
    2. Extract the block number from the final URL (or any intermediate
       redirect URL that contains ``/e/EVENT/<digits>``).
    3. Call ``/<block>/avail`` with the established session cookies.
    4. Return ``None`` (signal browser fallback) if the response is missing
       the ``hotels`` key or any step raises an exception.
    """
    import re as _re

    event_url = f"{settings.base_url}/e/{settings.event_code}"

    async with httpx.AsyncClient(
        headers=_JSON_HEADERS,
        follow_redirects=True,
        timeout=20,
    ) as client:
        # Step 1: GET the base event URL — the server redirects to the active block.
        try:
            page_resp = await client.get(event_url)
            page_resp.raise_for_status()
        except Exception as exc:
            log.debug("[fetch] httpx event page failed: %s", exc)
            return None

        # Step 2: discover the block from the redirect chain or final URL.
        log.debug(
            "[fetch] httpx final URL: %s | redirect history: %s",
            page_resp.url,
            [str(r.url) for r in page_resp.history],
        )
        block: int | None = None
        candidates = [str(page_resp.url)] + [
            str(r.url) for r in page_resp.history
        ]
        for candidate in reversed(candidates):  # final URL first
            m = _re.search(r"/e/[^/]+/(\d+)", candidate)
            if m:
                block = int(m.group(1))
                log.debug("[fetch] httpx discovered block %d from %s", block, candidate)
                break

        if block is None:
            block = settings.block_index
            log.debug("[fetch] httpx could not discover block — using fallback %d", block)

        avail_url = _build_url(settings, block_index=block)

        # Step 3: general /avail (no date filter).
        try:
            general_resp = await client.get(
                avail_url,
                params={"_": str(int(time.time() * 1000))},
            )
            general_resp.raise_for_status()
            general_data: dict = general_resp.json()
        except Exception as exc:
            log.debug("[fetch] httpx general /avail failed: %s", exc)
            return None

        log.info(
            "[httpx] /avail response keys: %s",
            list(general_data.keys()) if isinstance(general_data, dict) else type(general_data).__name__,
        )

        if "hotels" not in general_data:
            import json as _json
            preview = _json.dumps(general_data)[:500]
            log.warning(
                "[httpx] general /avail missing 'hotels' key — response preview: %s",
                preview,
            )
            return None

        general_hotels = _parse_hotels_from_data(general_data)
        log.info(
            "[httpx] general availability: %d hotels, %d available",
            len(general_hotels),
            sum(1 for h in general_hotels if h.is_available),
        )

        # Step 4: dated /avail (with arrive/depart).
        if not (settings.arrive and settings.depart):
            return FetchResult(general=general_hotels, dated=general_hotels)

        try:
            dated_resp = await client.get(avail_url, params=_build_params(settings))
            dated_resp.raise_for_status()
            dated_data: dict = dated_resp.json()
        except Exception as exc:
            log.debug("[fetch] httpx dated /avail failed: %s — using general data", exc)
            return FetchResult(general=general_hotels, dated=general_hotels)

        if "hotels" not in dated_data:
            log.debug("[httpx] dated /avail missing 'hotels' key — using general data")
            return FetchResult(general=general_hotels, dated=general_hotels)

        dated_hotels = _parse_hotels_from_data(dated_data)
        log.info(
            "[httpx] dated /avail returned %d hotel entries",
            len(dated_hotels),
        )
        return FetchResult(general=general_hotels, dated=dated_hotels)


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


async def _fetch_via_browser(settings: Settings) -> FetchResult:
    """Navigate the OnPeak event page and intercept the /avail XHR.

    Returns a ``FetchResult`` containing:
    * **general** — availability from the initial page XHR (no date filter).
    * **dated** — availability for the user's arrive/depart range.

    Strategy
    --------
    1. Navigate to ``/e/{event_code}``.  The server redirects directly to the
       active block (e.g. ``/e/{event}/22#hotels``).
    2. The page's
       own JavaScript fires an XHR to ``/{block}/avail?_=…``.
    3. We intercept that response, extract the block number, and parse the
       hotel JSON directly.
    4. If the user specified ``arrive``/``depart`` dates we make a follow-up
       ``/avail`` call with those parameters using the established session.
    """
    from playwright.async_api import async_playwright
    import asyncio
    import re as _re

    event_url = f"{settings.base_url}/e/{settings.event_code}"

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

        # ---- navigate to the base event URL (server redirects to active block) ----
        log.debug("[browser] navigating → %s", event_url)
        try:
            await page.goto(
                event_url,
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

            general_hotels = _parse_hotels_from_data(initial_data)
            log.info(
                "[browser] general availability: %d hotels, %d available",
                len(general_hotels),
                sum(1 for h in general_hotels if h.is_available),
            )

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
                        dated_hotels = _parse_hotels_from_data(body)
                        await browser.close()
                        return FetchResult(general=general_hotels, dated=dated_hotels)
                except Exception as exc:
                    log.debug("[browser] follow-up /avail failed: %s — using initial data for both", exc)

            # Fall back to the initial intercepted data for both.
            await browser.close()
            return FetchResult(general=general_hotels, dated=general_hotels)

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
                dated = _parse_hotels_from_data(body)
                return FetchResult(general=dated, dated=dated)
        except Exception as exc:
            log.debug("[browser] fallback /avail failed: %s", exc)

        await browser.close()

    log.info(
        "Could not retrieve hotel data — the sale may not be active yet "
        "for this event / date range."
    )
    return FetchResult()


def _parse_hotels_from_data(data: dict[str, Any]) -> list[Hotel]:
    """Parse a pre-decoded /avail JSON dict into sorted Hotel list."""
    raw_hotels = data.get("hotels", [])
    log.debug("[parse] hotel entries: %d", len(raw_hotels))
    # The API returns hotels as a list; support dict (legacy) as fallback.
    items = raw_hotels if isinstance(raw_hotels, list) else raw_hotels.values()
    hotels: list[Hotel] = []
    skipped = 0
    for hotel_data in items:
        try:
            hotels.append(Hotel.model_validate(hotel_data))
        except Exception as exc:
            skipped += 1
            hotel_name = hotel_data.get("name", "?") if isinstance(hotel_data, dict) else "?"
            log.warning("[parse] skipped hotel %r: %s", hotel_name, exc)
            continue
    if skipped:
        log.warning("[parse] %d/%d hotel entries failed to parse", skipped, len(raw_hotels))
    hotels.sort(key=lambda h: h.distance)
    for h in hotels:
        log.debug("[parse] hotel %d %-40s status=%-12s rate=%s", h.hotel_id, h.name, h.status, h.display_rate)
    return hotels


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_hotels(
    settings: Settings | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[Hotel]:
    """Fetch date-specific hotel availability.

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

    result = await _fetch_via_browser(settings)
    return result.dated


async def fetch_hotels_dual(
    settings: Settings | None = None,
) -> FetchResult:
    """Fetch both general and date-specific availability via Playwright."""
    if settings is None:
        settings = Settings()

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
    skipped = 0
    for hotel_data in items:
        try:
            hotels.append(Hotel.model_validate(hotel_data))
        except Exception as exc:
            skipped += 1
            hotel_name = hotel_data.get("name", "?") if isinstance(hotel_data, dict) else "?"
            log.warning("[parse] skipped hotel %r: %s", hotel_name, exc)
            continue
    if skipped:
        log.warning("[parse] %d/%d hotel entries failed to parse", skipped, len(raw_hotels))

    hotels.sort(key=lambda h: h.distance)
    for h in hotels:
        log.debug("[parse] hotel %d %-40s status=%-12s rate=%s", h.hotel_id, h.name, h.status, h.display_rate)
    return hotels


async def fetch_available_hotels(
    settings: Settings | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[Hotel]:
    """Convenience wrapper that returns only hotels with rooms available."""
    hotels = await fetch_hotels(settings, client=client)
    return [h for h in hotels if h.is_available]
