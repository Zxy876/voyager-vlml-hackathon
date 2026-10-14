#!/usr/bin/env python3
"""Query 2025 tournaments for series IDs."""
import asyncio
import sys
from pathlib import Path

scripts_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(scripts_root))
sys.path.insert(0, str(scripts_root.parent.parent / "src"))

from vlml.client.grid_client import GRIDClient
from ingestion.file_download_client import FileDownloadClient

# Query to get all tournaments
GET_TOURNAMENTS = """
query GetTournaments($titleId: ID, $first: Int!) {
    tournaments(first: $first, filter: { titleId: $titleId }) {
        edges {
            node {
                id
                name
            }
        }
    }
}
"""

# Query to get series for a specific tournament
GET_SERIES_BY_TOURNAMENT = """
query GetSeriesByTournament($tournamentId: ID!, $first: Int!) {
    allSeries(first: $first, filter: { tournamentId: $tournamentId }) {
        edges {
            node {
                id
                startTimeScheduled
                title {
                    name
                }
                teams {
                    baseInfo {
                        name
                    }
                }
            }
        }
    }
}
"""


async def query_2025_tournaments():
    """Query 2025 tournaments and their series."""
    print("=" * 70)
    print("  Querying 2025 VCT Tournaments for Series IDs")
    print("=" * 70)
    print()

    central = GRIDClient()
    file_client = FileDownloadClient()

    try:
        # Get all Valorant tournaments
        print("📊 Fetching all Valorant tournaments...")
        result = await central.execute(
            GET_TOURNAMENTS,
            variables={"titleId": "6", "first": 50}
        )

        tournaments = result.get("tournaments", {}).get("edges", [])
        print(f"  Found {len(tournaments)} tournaments total\n")

        # Filter for 2025 tournaments (by name)
        tournaments_2025 = []
        for edge in tournaments:
            tournament = edge.get("node", {})
            name = tournament.get("name", "")

            if "2025" in name:
                tournaments_2025.append(tournament)

        print(f"🎯 Found {len(tournaments_2025)} tournaments with '2025' in name:\n")

        for tournament in tournaments_2025:
            print(f"  • {tournament.get('name')}")
            print(f"    ID: {tournament.get('id')}")
            print()

        # Query each 2025 tournament for series
        all_series = []

        for tournament in tournaments_2025:
            tournament_id = tournament.get("id")
            tournament_name = tournament.get("name")

            print(f"🔍 Checking tournament: {tournament_name} (ID: {tournament_id})")

            series_result = await central.execute(
                GET_SERIES_BY_TOURNAMENT,
                variables={"tournamentId": tournament_id, "first": 50}
            )

            series_list = series_result.get("allSeries", {}).get("edges", [])
            print(f"  Found {len(series_list)} series")

            if series_list:
                for edge in series_list:
                    series = edge.get("node", {})
                    series_id = series.get("id")
                    scheduled = series.get("startTimeScheduled", "Unknown")
                    teams = series.get("teams", [])
                    team_names = " vs ".join([
                        t.get("baseInfo", {}).get("name", "Unknown") for t in teams[:2]
                    ])

                    print(f"    - Series {series_id}: {team_names} ({scheduled[:10]})")
                    all_series.append(series)
            print()

        # Test File Download API for found series
        if all_series:
            print("=" * 70)
            print(f"  Testing File Download API for {len(all_series)} Series")
            print("=" * 70)
            print()

            available_count = 0

            for i, series in enumerate(all_series[:10], 1):  # Test first 10
                series_id = series.get("id")
                scheduled = series.get("startTimeScheduled", "Unknown")[:10]

                print(f"[{i}/{min(10, len(all_series))}] Testing series {series_id} ({scheduled})...", end=" ")

                try:
                    events = await file_client.download_events(series_id)
                    if events:
                        print(f"✅ {len(events):,} events available")
                        available_count += 1
                    else:
                        print("❌ No events")
                except Exception as e:
                    error_msg = str(e)
                    if "403" in error_msg:
                        print("❌ 403 Forbidden")
                    elif "404" in error_msg:
                        print("❌ 404 Not Found")
                    else:
                        print(f"❌ Error: {error_msg[:40]}")

            print()
            print("=" * 70)
            print(f"  Result: {available_count}/{min(10, len(all_series))} series have event files")
            print("=" * 70)

        else:
            print("=" * 70)
            print("  ❌ No Series Found in 2025 Tournaments")
            print("=" * 70)
            print()
            print("This means:")
            print("  • Tournaments exist in the system but have no series data")
            print("  • Matches haven't been recorded yet")
            print("  • Or data is stored differently for 2025")

    finally:
        await central.close()
        await file_client.close()


if __name__ == "__main__":
    asyncio.run(query_2025_tournaments())
