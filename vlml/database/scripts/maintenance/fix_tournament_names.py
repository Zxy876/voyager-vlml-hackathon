#!/usr/bin/env python3
"""Fix missing tournament names for 2025 series."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from vlml.client.grid_client import GRIDClient
from vlml.client.queries_updated import GET_SERIES_BY_ID
from vlml.db.manager import EventDatabase


# Define query for getting series details
GET_SERIES_DETAILS = """
query GetSeriesDetails($seriesId: ID!) {
    series(id: $seriesId) {
        id
        startTimeScheduled
        tournament {
            id
            name
        }
    }
}
"""


async def fix_tournament_names():
    """Update NULL tournament names in database."""

    print("=" * 70)
    print("  Fix Missing Tournament Names")
    print("=" * 70)
    print()

    # Connect to database
    db = EventDatabase()
    client = GRIDClient()

    try:
        # Get all series with NULL tournament names
        result = db.query("SELECT series_id FROM series WHERE tournament_name IS NULL")
        null_series = [row[0] for row in result]

        print(f"Found {len(null_series)} series with missing tournament names")
        print()

        if not null_series:
            print("✅ All series have tournament names!")
            return

        updated_count = 0
        error_count = 0

        for i, series_id in enumerate(null_series, 1):
            print(f"[{i}/{len(null_series)}] Series {series_id}...", end=" ", flush=True)

            try:
                # Query GRID API for series details
                result = await client.execute(
                    GET_SERIES_DETAILS,
                    variables={"seriesId": series_id}
                )

                series_data = result.get("series")
                if not series_data:
                    print("❌ Not found in API")
                    error_count += 1
                    continue

                tournament_name = series_data.get("tournament", {}).get("name")
                start_time = series_data.get("startTimeScheduled")

                if not tournament_name:
                    print("⚠️  No tournament name in API")
                    error_count += 1
                    continue

                # Update database
                db.conn.execute(
                    "UPDATE series SET tournament_name = ?, start_time = ? WHERE series_id = ?",
                    [tournament_name, start_time, series_id]
                )

                # Truncate name for display
                display_name = tournament_name[:50] + "..." if len(tournament_name) > 50 else tournament_name
                print(f"✅ {display_name}")
                updated_count += 1

            except Exception as e:
                print(f"❌ Error: {str(e)[:40]}")
                error_count += 1

        print()
        print("=" * 70)
        print("  Summary")
        print("=" * 70)
        print(f"  ✅ Updated: {updated_count}")
        print(f"  ❌ Errors:  {error_count}")
        print("=" * 70)

    finally:
        await client.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(fix_tournament_names())
