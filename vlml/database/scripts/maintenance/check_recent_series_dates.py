#!/usr/bin/env python3
"""Check exact dates of recent series."""
import asyncio
import sys
from pathlib import Path
from datetime import datetime

scripts_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(scripts_root))
sys.path.insert(0, str(scripts_root.parent.parent / "src"))

from ingestion.file_download_client import FileDownloadClient


async def check_dates():
    """Check dates of most recent accessible series."""
    print("=" * 70)
    print("  Checking Exact Dates of Recent Series")
    print("=" * 70)
    print()

    file_client = FileDownloadClient()

    # Check series around the latest known range
    test_ids = list(range(2653980, 2654010)) + list(range(2654100, 2654120))

    latest_date = None
    latest_series = None

    print("Scanning for most recent series...\n")

    for series_id in test_ids:
        try:
            events = await file_client.download_events(str(series_id))
            if events and len(events) > 0:
                # Get first and last event timestamps
                first_event = events[0].get("occurredAt", "Unknown")
                last_event = events[-1].get("occurredAt", "Unknown")

                # Parse date
                try:
                    event_date = datetime.fromisoformat(first_event.replace("Z", "+00:00"))
                    date_str = event_date.strftime("%Y-%m-%d")

                    print(f"✅ Series {series_id}: {date_str} ({len(events):,} events)")

                    if latest_date is None or event_date > latest_date:
                        latest_date = event_date
                        latest_series = series_id

                except:
                    print(f"✅ Series {series_id}: Date unknown ({len(events):,} events)")

        except Exception as e:
            # Skip 403 and 404 silently
            pass

    print()
    print("=" * 70)
    if latest_series:
        print(f"  Most Recent Series Found: {latest_series}")
        print(f"  Date: {latest_date.strftime('%Y-%m-%d')}")
    else:
        print("  No series found in tested range")
    print("=" * 70)

    await file_client.close()


if __name__ == "__main__":
    asyncio.run(check_dates())
