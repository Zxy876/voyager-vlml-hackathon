#!/usr/bin/env python3
"""Download raw event files with year/tournament partitioning."""
import asyncio
import argparse
import sys
from pathlib import Path
from datetime import datetime

scripts_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(scripts_root))
sys.path.insert(0, str(scripts_root.parent.parent / "src"))

from vlml.client.grid_client import GRIDClient
from ingestion.file_download_client import FileDownloadClient
from vlml.client.queries import GET_SERIES_BY_TOURNAMENT, GET_TOURNAMENTS


async def discover_tournaments_by_year(client: GRIDClient, year: str) -> list:
    """Discover VCT tournaments for a specific year.

    Args:
        client: GRID API client
        year: Year to search (e.g., "2025")

    Returns:
        List of tournament dicts with id and name
    """
    print(f"🔍 Discovering VCT tournaments from {year}...")

    # Get all Valorant tournaments
    result = await client.execute(
        GET_TOURNAMENTS,
        variables={"titleId": "6", "first": 50}
    )

    tournaments = result.get("tournaments", {}).get("edges", [])

    # Filter for specified year
    year_tournaments = []
    for edge in tournaments:
        tournament = edge.get("node", {})
        tournament_id = tournament.get("id")
        tournament_name = tournament.get("name", "")

        # Check if tournament name contains the year
        if year in tournament_name:
            year_tournaments.append({
                "id": tournament_id,
                "name": tournament_name
            })

    return year_tournaments


def select_tournaments(tournaments: list) -> list:
    """Interactively select which tournaments to download.

    Args:
        tournaments: List of tournament dicts with id and name

    Returns:
        List of selected tournament IDs
    """
    if not tournaments:
        return []

    print("\n" + "=" * 70)
    print("  📋 Available Tournaments")
    print("=" * 70)
    print()

    for i, tournament in enumerate(tournaments, 1):
        print(f"  [{i}] {tournament['name']}")

    print(f"  [0] All tournaments")
    print()

    while True:
        try:
            choice = input("Select tournaments (e.g., '1,3,5' or '0' for all): ").strip()

            if choice == "0":
                print("  ✅ Selected: All tournaments")
                return tournaments

            # Parse comma-separated numbers
            indices = [int(x.strip()) for x in choice.split(",")]

            # Validate indices
            if all(1 <= idx <= len(tournaments) for idx in indices):
                selected = [tournaments[idx - 1] for idx in indices]
                print(f"  ✅ Selected: {len(selected)} tournament(s)")
                for t in selected:
                    print(f"     - {t['name']}")
                return selected
            else:
                print(f"  ❌ Invalid selection. Please enter numbers between 1 and {len(tournaments)}")

        except (ValueError, IndexError):
            print("  ❌ Invalid input. Please enter comma-separated numbers (e.g., '1,3,5')")
        except KeyboardInterrupt:
            print("\n\n  ⚠️  Selection cancelled")
            sys.exit(1)


def normalize_keywords(raw: str) -> list[str]:
    if not raw:
        return []
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def apply_tournament_filters(tournaments: list, keywords: list[str]) -> list:
    if not keywords:
        return tournaments
    filtered = []
    for tournament in tournaments:
        name = tournament.get("name", "")
        name_lower = name.lower()
        if any(keyword in name_lower for keyword in keywords):
            filtered.append(tournament)
    return filtered


def preset_keywords(preset: str | None) -> list[str]:
    if not preset:
        return []
    presets = {
        "masters": ["masters"],
        "champions": ["champions"],
        "vct": ["vct"],
    }
    return presets.get(preset, [])


async def download_raw_events(year: str, keywords: list[str] | None = None):
    """Download raw event files with partitioning."""

    print("=" * 70)
    print(f"  Download {year} Raw Event Files")
    print(f"  Structure: data/raw_events/{year}/{{tournament}}/{{series_id}}.jsonl")
    print("=" * 70)
    print()

    client = GRIDClient()
    file_client = FileDownloadClient()

    try:
        # Discover tournaments for the year
        tournaments = await discover_tournaments_by_year(client, year)

        if not tournaments:
            print(f"  ⚠️  No tournaments found for {year}")
            return

        print(f"  ✅ Found {len(tournaments)} tournaments\n")

        keywords = keywords or []
        if keywords:
            filtered = apply_tournament_filters(tournaments, keywords)
            print(f"  🔎 Filtered tournaments: {len(filtered)} match keyword(s)")
            tournaments = filtered

        if not tournaments:
            print("  ⚠️  No tournaments match the filters")
            return

        # Let user select which tournaments to download
        selected_tournaments = select_tournaments(tournaments)

        if not selected_tournaments:
            print("  ⚠️  No tournaments selected")
            return

        # Collect all series from selected tournaments
        all_series = []

        print(f"\n🔍 Fetching series from {len(selected_tournaments)} tournament(s)...")
        for tournament in selected_tournaments:
            result = await client.execute(
                GET_SERIES_BY_TOURNAMENT,
                variables={"tournamentId": tournament["id"], "first": 50}
            )

            series_list = result.get("allSeries", {}).get("edges", [])
            all_series.extend(series_list)

            if series_list:
                print(f"  {tournament['name']}: {len(series_list)} series")

        print(f"\n✅ Found {len(all_series)} total series from {year}\n")

        # Download raw files
        print("=" * 70)
        print("  Downloading Raw Event Files")
        print("=" * 70)
        print()

        downloaded_count = 0
        skipped_count = 0
        error_count = 0

        for i, edge in enumerate(all_series, 1):
            series_data = edge.get("node", {})
            series_id = series_data.get("id")
            tournament_name = series_data.get("tournament", {}).get("name", "Unknown")

            # Truncate for display
            display_name = tournament_name[:50] + "..." if len(tournament_name) > 50 else tournament_name

            print(f"[{i}/{len(all_series)}] {series_id}: {display_name}")

            try:
                # Download and save raw file (will check if exists first)
                events = await file_client.download_events(
                    series_id=series_id,
                    tournament_name=tournament_name,
                    year=year,
                    save_raw=True
                )

                print(f"  ✅ Downloaded {len(events):,} events")
                downloaded_count += 1

            except FileExistsError:
                print(f"  ⏭️  Already exists")
                skipped_count += 1
            except Exception as e:
                error_msg = str(e)
                if "403" in error_msg or "Forbidden" in error_msg:
                    print(f"  ❌ 403 Forbidden")
                elif "404" in error_msg or "Not Found" in error_msg:
                    print(f"  ❌ 404 Not Found")
                elif "rate limit" in error_msg.lower():
                    print(f"  ❌ Rate limit exceeded")
                    print(f"\n⚠️  Hit rate limit. Downloaded {downloaded_count} so far.")
                    print(f"   Wait ~1 hour and re-run to continue.\n")
                    break
                else:
                    print(f"  ❌ Error: {error_msg[:60]}")
                error_count += 1

        # Summary
        print()
        print("=" * 70)
        print("  Download Complete")
        print("=" * 70)
        print(f"  ✅ Downloaded: {downloaded_count}")
        print(f"  ⏭️  Skipped:    {skipped_count} (already exists)")
        print(f"  ❌ Errors:     {error_count}")
        print()
        print(f"📁 Raw files saved to: data/raw_events/{year}/")
        print("=" * 70)

    finally:
        await client.close()
        await file_client.close()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Download raw event files with year/tournament partitioning",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python download_raw_events.py --year 2025    # Download 2025 tournaments
  python download_raw_events.py --year 2024    # Download 2024 tournaments
  python download_raw_events.py                # Defaults to current year
  python download_raw_events.py --year 2025 --preset masters
  python download_raw_events.py --year 2025 --tournament-keywords "Masters,Champions"

The script will:
1. Discover tournaments for the specified year
2. Let you select which tournaments to download
3. Save raw JSONL files to: data/raw_events/{year}/{tournament}/{series_id}.jsonl
        """
    )

    parser.add_argument(
        "--year",
        type=str,
        default=str(datetime.now().year),
        help="Year to download (default: current year)"
    )
    parser.add_argument(
        "--tournament-keywords",
        type=str,
        help="Comma-separated keywords to filter tournaments (e.g., Masters,Champions)"
    )
    parser.add_argument(
        "--preset",
        type=str,
        choices=["masters", "champions", "vct"],
        help="Preset tournament filter (masters, champions, vct)"
    )

    args = parser.parse_args()

    keywords = normalize_keywords(args.tournament_keywords)
    keywords.extend(preset_keywords(args.preset))
    keywords = sorted(set([k for k in keywords if k]))

    try:
        asyncio.run(download_raw_events(args.year, keywords=keywords))
    except KeyboardInterrupt:
        print("\n\n⚠️  Download cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
