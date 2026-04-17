"""Command-line interface for longshotel."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from rich.console import Console

from longshotel.client import fetch_hotels
from longshotel.config import Settings
from longshotel.display import print_hotels
from longshotel.monitor import run_monitor

console = Console()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="longshotel",
        description="Monitor SDCC 2026 hotel availability via OnPeak Compass.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── check ────────────────────────────────────────────────────────────
    check_p = sub.add_parser("check", help="One-shot availability check")
    check_p.add_argument(
        "--arrive", default=None, help="Check-in date (YYYY-MM-DD)"
    )
    check_p.add_argument(
        "--depart", default=None, help="Check-out date (YYYY-MM-DD)"
    )
    check_p.add_argument(
        "--show-soldout",
        action="store_true",
        default=False,
        help="Include sold-out hotels in the output",
    )
    check_p.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Enable debug logging",
    )

    # ── monitor ──────────────────────────────────────────────────────────
    mon_p = sub.add_parser(
        "monitor", help="Continuously poll for availability changes"
    )
    mon_p.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Polling interval in seconds",
    )
    mon_p.add_argument(
        "--arrive", default=None, help="Check-in date (YYYY-MM-DD)"
    )
    mon_p.add_argument(
        "--depart", default=None, help="Check-out date (YYYY-MM-DD)"
    )
    mon_p.add_argument(
        "--show-soldout",
        action="store_true",
        default=False,
        help="Include sold-out hotels in the output",
    )
    mon_p.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Enable debug logging",
    )

    return parser.parse_args(argv)


def _settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, object] = {}
    if getattr(args, "arrive", None):
        overrides["arrive"] = args.arrive
    if getattr(args, "depart", None):
        overrides["depart"] = args.depart
    if getattr(args, "show_soldout", False):
        overrides["show_soldout"] = True
    if getattr(args, "interval", None):
        overrides["poll_interval_seconds"] = args.interval
    if getattr(args, "verbose", False):
        overrides["verbose"] = True
    return Settings(**overrides)  # type: ignore[arg-type]


async def _check(settings: Settings) -> None:
    hotels = await fetch_hotels(settings)
    print_hotels(hotels, show_soldout=settings.show_soldout)

    available = [h for h in hotels if h.is_available]
    if not available:
        console.print("[bold red]⚠ No rooms available right now.[/bold red]")
        sys.exit(1)


async def _monitor(settings: Settings) -> None:
    try:
        await run_monitor(settings)
    except KeyboardInterrupt:
        console.print("\n[bold cyan]Monitor stopped.[/bold cyan]")


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = _settings_from_args(args)

    if settings.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    if args.command == "check":
        asyncio.run(_check(settings))
    elif args.command == "monitor":
        asyncio.run(_monitor(settings))


if __name__ == "__main__":
    main()
