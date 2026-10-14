#!/usr/bin/env python3
"""
Check what data is available in GRID APIs.

This script helps verify:
1. What series are available in Central Data API
2. Which series have event files in File Download API
3. Date range of available data
"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime

scripts_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(scripts_root))
sys.path.insert(0, str(scripts_root.parent.parent / "src"))

from vlml.client.grid_client import GRIDClient
from ingestion.file_download_client import FileDownloadClient
from vlml.client.queries_updated import GET_RECENT_SERIES


async def check_availability():
    """Check data availability across GRID APIs."""
    print("=" * 70)
    print("  GRID API Data Availability Check")
    print("=" * 70)
    print()

    central_client = GRIDClient()
    file_client = FileDownloadClient()

    try:
        # Get recent series from Central Data API
        print("📊 Checking Central Data API...")
        result = await central_client.execute(
            GET_RECENT_SERIES,
            variables={"titleId": "6", "first": 50}
        )

        series_list = result.get("allSeries", {}).get("edges", [])
        print(f"  Found {len(series_list)} recent Valorant series")
        print()

        # Analyze date range
        dates = []
        for edge in series_list:
            scheduled = edge.get("node", {}).get("startTimeScheduled")
            if scheduled:
                try:
                    dt = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
                    dates.append(dt)
                except:
                    pass

        if dates:
            dates.sort()
            print(f"📅 Date Range in Central Data API:")
            print(f"  Earliest: {dates[0].strftime('%Y-%m-%d')}")
            print(f"  Latest:   {dates[-1].strftime('%Y-%m-%d')}")
            print()

        # Check File Download API availability
        print("📥 Checking File Download API (event files)...")
        print(f"  Testing first 10 series for event file availability...")
        print()

        available_count = 0
        unavailable_count = 0
        years_found = set()

        for i, edge in enumerate(series_list[:10], 1):
            series_data = edge.get("node", {})
            series_id = series_data.get("id")
            tournament = series_data.get("tournament", {}).get("name", "Unknown")
            scheduled = series_data.get("startTimeScheduled", "Unknown")

            # Extract year
            try:
                year = scheduled.split("-")[0]
                years_found.add(year)
            except:
                year = "Unknown"

            print(f"[{i}/10] Series {series_id} ({year})")
            print(f"  Tournament: {tournament[:60]}")

            # Check if event file exists
            try:
                events = await file_client.download_events(series_id)
                if events:
                    print(f"  ✅ Event file available ({len(events):,} events)")
                    available_count += 1
                else:
                    print(f"  ❌ No event file")
                    unavailable_count += 1
            except Exception as e:
                print(f"  ❌ Error: {str(e)[:50]}")
                unavailable_count += 1

            print()

        # Summary
        print("=" * 70)
        print("  Summary")
        print("=" * 70)
        print()
        print(f"Central Data API:")
        print(f"  ✓ {len(series_list)} series available")
        if dates:
            print(f"  ✓ Date range: {dates[0].year} - {dates[-1].year}")
        print()
        print(f"File Download API (sample of 10):")
        print(f"  ✓ {available_count} series with event files")
        print(f"  ✗ {unavailable_count} series without event files")
        if years_found:
            print(f"  📅 Years found: {', '.join(sorted(years_found))}")
        print()

        if available_count > 0:
            print("✅ Event data IS available! You can use:")
            print("   • update.sh to build database")
            print("   • Coaching tools for event-level analysis")
        else:
            print("⚠️  No event files found in sample")
            print("   Event-level coaching tools may have limited data")

        print()
        print("=" * 70)

    finally:
        await central_client.close()
        await file_client.close()


if __name__ == "__main__":
    asyncio.run(check_availability())
