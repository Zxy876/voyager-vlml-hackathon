"""File Download API client for GRID event data."""
import asyncio
import json
import zipfile
import io
from pathlib import Path
from typing import Dict, List, Any, Optional
import httpx
from vlml.config import Config


class FileDownloadClient:
    """Client for GRID File Download API.

    Downloads and parses event files (JSONL format) from completed Valorant matches.
    """

    def __init__(self, api_key: Optional[str] = None, api_url: Optional[str] = None, cache_dir: Optional[str] = None):
        """Initialize File Download client.

        Args:
            api_key: GRID API key (defaults to Config.GRID_API_KEY)
            api_url: File Download API base URL (defaults to Config.FILE_DOWNLOAD_API_URL)
            cache_dir: Directory for caching raw event files (defaults to data/raw_events/)
        """
        self.api_key = api_key or Config.GRID_API_KEY
        self.api_url = api_url or Config.FILE_DOWNLOAD_API_URL
        self._memory_cache: Dict[str, List[dict]] = {}  # In-memory cache

        # Setup file cache directory
        if cache_dir is None:
            project_root = Path(__file__).parent.parent.parent.parent
            cache_dir = project_root / "data" / "raw_events"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def list_files(self, series_id: str) -> Dict[str, Any]:
        """List available files for a series.

        Args:
            series_id: GRID series ID

        Returns:
            Dictionary with file list:
            {
                "files": [
                    {
                        "id": "events-grid-compressed",
                        "description": "Grid Series Events (.zip)",
                        "status": "ready",
                        "fileName": "events_2629390_grid.jsonl.zip",
                        "fullURL": "https://..."
                    }
                ]
            }
        """
        url = f"{self.api_url}/list/{series_id}"
        headers = {"x-api-key": self.api_key}

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
            except Exception as e:
                return {"error": f"Failed to list files: {str(e)}"}

    def _slugify(self, text: str) -> str:
        """Convert text to filesystem-safe slug."""
        import re
        # Convert to lowercase
        slug = text.lower()
        # Replace spaces and special chars with hyphens
        slug = re.sub(r'[^\w\s-]', '', slug)
        slug = re.sub(r'[-\s]+', '-', slug)
        return slug.strip('-')

    def _parse_jsonl_file(self, file_path: Path) -> List[dict]:
        """Parse a JSONL file from disk.

        Args:
            file_path: Path to JSONL file

        Returns:
            List of parsed event dictionaries
        """
        events = []
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    wrapper = json.loads(line)
                    # Extract actual events from wrapper's "events" array
                    wrapper_events = wrapper.get("events", [])
                    # Add wrapper timestamp to each event if not present
                    wrapper_timestamp = wrapper.get("occurredAt")
                    for event in wrapper_events:
                        if "occurredAt" not in event and wrapper_timestamp:
                            event["occurredAt"] = wrapper_timestamp
                        events.append(event)
                except json.JSONDecodeError:
                    continue  # Skip malformed lines
        return events

    async def download_events(
        self,
        series_id: str,
        tournament_name: str = None,
        year: str = None,
        save_raw: bool = True
    ) -> List[dict]:
        """Download and parse event JSONL file for a series.

        Args:
            series_id: GRID series ID
            tournament_name: Tournament name for partitioning (optional)
            year: Year for partitioning (optional, extracted from tournament_name if not provided)
            save_raw: Whether to save raw JSONL to disk (default: True)

        Returns:
            List of event dictionaries. Each event has:
            - id: Event ID
            - type: Event type (e.g., "player-killed-player")
            - occurredAt: ISO timestamp
            - actor: Entity that performed action
            - action: Action performed
            - target: Entity affected by action

        Raises:
            Exception: If download or parsing fails
        """
        # Check memory cache first
        if series_id in self._memory_cache:
            return self._memory_cache[series_id]

        # Check if raw file exists on disk
        if save_raw and tournament_name:
            # Extract year from tournament name if not provided
            if not year and tournament_name:
                import re
                year_match = re.search(r'20\d{2}', tournament_name)
                if year_match:
                    year = year_match.group()

            if year:
                # Create partition path: data/raw_events/{year}/{tournament_slug}/{series_id}.jsonl
                tournament_slug = self._slugify(tournament_name)
                raw_file_path = self.cache_dir / year / tournament_slug / f"{series_id}.jsonl"

                # If file exists, read from disk
                if raw_file_path.exists():
                    return self._parse_jsonl_file(raw_file_path)

        # Get file list
        files_response = await self.list_files(series_id)

        if "error" in files_response:
            raise Exception(files_response["error"])

        files = files_response.get("files", [])

        # Find events file
        events_file = next(
            (f for f in files if "events" in f.get("id", "").lower()),
            None
        )

        if not events_file:
            raise Exception(f"No events file found for series {series_id}")

        # Download events file
        download_url = events_file["fullURL"]
        headers = {"x-api-key": self.api_key}

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(download_url, headers=headers)
            response.raise_for_status()

            # Extract ZIP
            zip_data = io.BytesIO(response.content)

            with zipfile.ZipFile(zip_data) as zf:
                files_in_zip = zf.namelist()

                if not files_in_zip:
                    raise Exception("ZIP file is empty")

                # Read JSONL file
                jsonl_file = files_in_zip[0]

                with zf.open(jsonl_file) as f:
                    raw_jsonl_content = f.read()

                    # Save raw JSONL to disk if requested
                    if save_raw and tournament_name and year:
                        tournament_slug = self._slugify(tournament_name)
                        raw_file_path = self.cache_dir / year / tournament_slug / f"{series_id}.jsonl"
                        raw_file_path.parent.mkdir(parents=True, exist_ok=True)
                        raw_file_path.write_bytes(raw_jsonl_content)

                    # Parse JSONL
                    events = []
                    for line in raw_jsonl_content.decode('utf-8').splitlines():
                        if not line.strip():
                            continue
                        try:
                            wrapper = json.loads(line)
                            # Extract actual events from wrapper's "events" array
                            wrapper_events = wrapper.get("events", [])
                            # Add wrapper timestamp to each event if not present
                            wrapper_timestamp = wrapper.get("occurredAt")
                            for event in wrapper_events:
                                if "occurredAt" not in event and wrapper_timestamp:
                                    event["occurredAt"] = wrapper_timestamp
                                events.append(event)
                        except json.JSONDecodeError:
                            continue  # Skip malformed lines

                    # Cache the result
                    self._memory_cache[series_id] = events

                    return events

    async def get_series_events_summary(self, series_id: str) -> Dict[str, Any]:
        """Get a summary of events in a series.

        Args:
            series_id: GRID series ID

        Returns:
            Dictionary with event summary:
            {
                "series_id": "2629390",
                "total_events": 3214,
                "event_types": {"player-killed-player": 450, ...},
                "start_time": "2024-05-11T00:00:19.092Z",
                "end_time": "2024-05-11T02:55:41.747Z"
            }
        """
        try:
            events = await self.download_events(series_id)

            if not events:
                return {"error": f"No events found for series {series_id}"}

            # Count event types
            event_types: Dict[str, int] = {}
            for event in events:
                event_type = event.get("type", "unknown")
                event_types[event_type] = event_types.get(event_type, 0) + 1

            return {
                "series_id": series_id,
                "total_events": len(events),
                "event_types": event_types,
                "start_time": events[0].get("occurredAt") if events else None,
                "end_time": events[-1].get("occurredAt") if events else None,
            }

        except Exception as e:
            return {"error": f"Failed to get summary: {str(e)}"}

    async def close(self):
        """Close the client and clear cache."""
        self._memory_cache.clear()
