"""Configuration module for VLML."""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Application configuration."""

    GRID_API_KEY = os.getenv("GRID_API_KEY")
    GRID_API_URL = os.getenv("GRID_API_URL", "https://api-op.grid.gg/central-data/graphql")
    SERIES_STATE_API_URL = "https://api-op.grid.gg/live-data-feed/series-state/graphql"
    FILE_DOWNLOAD_API_URL = "https://api.grid.gg/file-download"
    VALORANT_TITLE_ID = 6  # Valorant title ID from Central Data API

    @classmethod
    def validate(cls):
        """Validate required configuration."""
        if not cls.GRID_API_KEY:
            raise ValueError("GRID_API_KEY environment variable is required")
        return True
