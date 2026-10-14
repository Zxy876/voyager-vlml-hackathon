#!/usr/bin/env python3
"""Direct search for 2025 series IDs in File Download API."""
import asyncio
import sys
from pathlib import Path

scripts_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(scripts_root))
sys.path.insert(0, str(scripts_root.parent.parent / "src"))

from ingestion.file_download_client import FileDownloadClient


async def search_for_2025():
    """Try different series ID ranges to find 2025 data."""
    print("=" * 70)
    print("  Direct Search for 2025 Series in File Download API")
    print("=" * 70)
    print()

    file_client = FileDownloadClient()

    # Try different ID ranges that might contain 2025 data
    test_ranges = [
        (2700000, 2700100, "Early 2025 estimate"),
        (2800000, 2800100, "Mid 2025 estimate"),
        (2900000, 2900100, "Late 2025 estimate"),
        (2654000, 2654100, "Right after 2024 data"),
    ]

    found_series = []

    for start_id, end_id, description in test_ranges:
        print(f"\n🔍 Testing range {start_id}-{end_id} ({description})...")

        for series_id in range(start_id, min(start_id + 10, end_id)):  # Test first 10
            try:
                events = await file_client.download_events(str(series_id))
                if events:
                    print(f"  ✅ Found series {series_id}: {len(events):,} events")
                    found_series.append(series_id)

                    # Check timestamp of first event for year
                    if events[0].get("occurredAt"):
                        timestamp = events[0]["occurredAt"]
                        year = timestamp[:4]
                        print(f"     Year: {year}")

            except Exception as e:
                # Skip errors silently for missing series
                if "404" not in str(e):
                    print(f"  ⚠️  Series {series_id}: {str(e)[:50]}")

        if found_series:
            print(f"\n  Found {len(found_series)} series in this range!")
            break

    if not found_series:
        print("\n" + "=" * 70)
        print("  ❌ No 2025 Series Found")
        print("=" * 70)
        print()
        print("Possible reasons:")
        print("  • 2025 data not yet available in File Download API")
        print("  • Series IDs are in a different range")
        print("  • API access restrictions")
        print()
        print("Latest confirmed working series: 2653984 (April 2024)")
    else:
        print("\n" + "=" * 70)
        print(f"  ✅ Found {len(found_series)} Series!")
        print("=" * 70)
        print()
        print("Series IDs:", found_series)

    await file_client.close()


if __name__ == "__main__":
    asyncio.run(search_for_2025())
