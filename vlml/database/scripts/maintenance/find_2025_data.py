#!/usr/bin/env python3
"""Search for 2025 VCT data in GRID APIs."""
import asyncio
import sys
from pathlib import Path
from datetime import datetime

scripts_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(scripts_root))
sys.path.insert(0, str(scripts_root.parent.parent / "src"))

from vlml.client.grid_client import GRIDClient
from ingestion.file_download_client import FileDownloadClient

# Paginated query
GET_SERIES_PAGINATED = """
query GetSeriesPaginated($titleId: ID, $first: Int!, $after: String) {
    allSeries(first: $first, filter: { titleId: $titleId }, after: $after) {
        edges {
            cursor
            node {
                id
                title {
                    name
                }
                tournament {
                    id
                    name
                }
                startTimeScheduled
            }
        }
        pageInfo {
            hasNextPage
            endCursor
        }
    }
}
"""


async def find_2025_data():
    """Search through pages to find 2025 data."""
    print("=" * 70)
    print("  Searching for 2025 VCT Data")
    print("=" * 70)
    print()

    central = GRIDClient()
    file_client = FileDownloadClient()

    try:
        all_series = []
        cursor = None
        page = 1
        max_pages = 20  # Prevent infinite loop

        print(f"📄 Fetching pages of series data (50 per page)...")

        # Paginate through results
        while page <= max_pages:
            print(f"  Page {page}...", end=" ", flush=True)

            result = await central.execute(
                GET_SERIES_PAGINATED,
                variables={
                    "titleId": "6",
                    "first": 50,
                    "after": cursor
                }
            )

            edges = result.get("allSeries", {}).get("edges", [])
            page_info = result.get("allSeries", {}).get("pageInfo", {})

            all_series.extend([e["node"] for e in edges])
            print(f"{len(edges)} series")

            if not page_info.get("hasNextPage"):
                print(f"\n✓ Reached last page")
                break

            cursor = page_info.get("endCursor")
            page += 1

        print(f"\n📊 Total series fetched: {len(all_series)}")
        print()

        # Group by year
        by_year = {}
        for series in all_series:
            scheduled = series.get("startTimeScheduled", "")
            try:
                year = scheduled.split("-")[0] if scheduled else "Unknown"
                if year not in by_year:
                    by_year[year] = []
                by_year[year].append(series)
            except:
                pass

        print("📅 Series distribution by year:")
        for year in sorted(by_year.keys(), reverse=True):
            count = len(by_year[year])
            print(f"  {year}: {count:,} series")
        print()

        # Check for 2025 data
        if "2025" in by_year:
            print("=" * 70)
            print("  ✅ FOUND 2025 DATA!")
            print("=" * 70)
            print()

            series_2025 = by_year["2025"]
            print(f"Total 2025 series: {len(series_2025)}")
            print()

            # Test first 5 for event files
            print("🔍 Testing event file availability...\n")

            for i, series in enumerate(series_2025[:5], 1):
                series_id = series.get("id")
                tournament = series.get("tournament", {}).get("name", "Unknown")
                scheduled = series.get("startTimeScheduled")

                print(f"[{i}/5] Series {series_id}")
                print(f"  Tournament: {tournament}")
                print(f"  Date: {scheduled}")

                try:
                    events = await file_client.download_events(series_id)
                    if events:
                        print(f"  ✅ Event file available: {len(events):,} events")
                    else:
                        print(f"  ⚠️  No events found")
                except Exception as e:
                    print(f"  ❌ Error: {str(e)[:60]}")

                print()

            print("=" * 70)
            print("✅ 2025 data confirmed! Update your database:")
            print("   ./update.sh")
            print("=" * 70)

        else:
            print("=" * 70)
            print("  ❌ No 2025 Data Found")
            print("=" * 70)
            print()
            print("Available years:", ", ".join(sorted(by_year.keys(), reverse=True)))
            print()
            print("Possible reasons:")
            print("  • 2025 VCT season hasn't started yet")
            print("  • Data not yet available in API")
            print("  • Need different query parameters")

    finally:
        await central.close()
        await file_client.close()


if __name__ == "__main__":
    asyncio.run(find_2025_data())
