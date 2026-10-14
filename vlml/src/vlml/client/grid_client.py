"""GRID API GraphQL client."""
from typing import Any, Dict, Optional
from gql import Client, gql
from gql.transport.httpx import HTTPXAsyncTransport
from vlml.config import Config


class GRIDClient:
    """Async GraphQL client for GRID esports API."""

    def __init__(self, api_key: Optional[str] = None, api_url: Optional[str] = None):
        """Initialize GRID client.

        Args:
            api_key: GRID API key (defaults to Config.GRID_API_KEY)
            api_url: GRID API URL (defaults to Config.GRID_API_URL)
        """
        self.api_key = api_key or Config.GRID_API_KEY
        self.api_url = api_url or Config.GRID_API_URL

        if not self.api_key:
            raise ValueError("GRID_API_KEY is required")

        # Configure transport with authentication header
        self.transport = HTTPXAsyncTransport(
            url=self.api_url,
            headers={
                "x-api-key": self.api_key,
            },
            timeout=30.0,
        )

        self.client = Client(
            transport=self.transport,
            fetch_schema_from_transport=False,  # Skip schema introspection for speed
        )

    async def execute(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute a GraphQL query.

        Args:
            query: GraphQL query string
            variables: Query variables

        Returns:
            Query result dictionary
        """
        async with self.client as session:
            result = await session.execute(gql(query), variable_values=variables or {})
            return result

    async def close(self):
        """Close the client connection."""
        await self.transport.close()
