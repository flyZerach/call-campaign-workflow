"""HTTP transport for the Assignment B mock telephony provider."""
from __future__ import annotations

from typing import Any

import httpx


class TelephonyProviderClient:
    """Thin HTTP client. Workflow decisions and persistence live elsewhere."""

    def __init__(self, base_url: str = "http://127.0.0.1:8099", *, http_client: Any = None):
        self.base_url = base_url.rstrip("/")
        self._owns_client = http_client is None
        self.http = http_client or httpx.Client(timeout=10.0)

    def place_call(self, payload: dict[str, str]):
        return self.http.post(f"{self.base_url}/v1/calls", json=payload)

    def find_by_request_id(self, request_id: str):
        return self.http.get(f"{self.base_url}/v1/calls", params={"request_id": request_id})

    def find_by_call_id(self, call_sid: str):
        return self.http.get(f"{self.base_url}/v1/calls/{call_sid}")

    def get_clock(self) -> dict:
        response = self.http.get(f"{self.base_url}/v1/clock")
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        if self._owns_client:
            self.http.close()
