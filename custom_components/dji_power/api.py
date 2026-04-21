"""DJI Power Station API client."""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import aiohttp

from .const import (
    HOME_API,
    HOME_API_FALLBACK,
    DEVICES_LIST_PATH,
    MQTT_TOKEN_PATH,
    WELCOME_REGION_PATH,
)

_LOGGER = logging.getLogger(__name__)

APP_HEADERS = {
    "version-name": "1.5.15",
    "version-code": "17821",
    "package-name": "com.dji.home",
    "platform": "android",
    "language": "en",
    "User-Agent": "DJI-Home/1.5.15",
}


class DJIAuthError(Exception):
    """Raised when authentication fails (token invalid/expired)."""


class DJIAPIError(Exception):
    """Raised on other API errors."""


class DJIPowerAPI:
    """Client for DJI Home cloud API."""

    def __init__(self, member_token: str, session: aiohttp.ClientSession) -> None:
        self._token = member_token
        self._session = session
        self._base_url = HOME_API

    def _headers(self) -> dict[str, str]:
        return {
            **APP_HEADERS,
            "Content-Type": "application/json",
            "x-member-token": self._token,
            "life-cycle-id": str(uuid.uuid4()),
            "x-request-id": str(uuid.uuid4()),
            "x-request-start": str(int(time.time() * 1000)),
        }

    async def _post(self, path: str, body: dict) -> dict[str, Any]:
        url = self._base_url + path
        try:
            async with self._session.post(
                url, headers=self._headers(), json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DJIAPIError(f"Network error: {exc}") from exc

        result = data.get("result", {})
        code = result.get("code", -1)
        if code == 121011:
            raise DJIAuthError("Invalid token type — token is a web token, not a mobile token")
        if code in (121001, 401):
            raise DJIAuthError("Token expired or invalid")
        if code != 0:
            raise DJIAPIError(f"API error code={code}: {result.get('message', '')}")
        return data.get("data", {})

    async def _get(self, path: str) -> dict[str, Any]:
        url = self._base_url + path
        try:
            async with self._session.get(url, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DJIAPIError(f"Network error: {exc}") from exc

        result = data.get("result", {})
        code = result.get("code", -1)
        if code == 121011:
            raise DJIAuthError("Invalid token type — token is a web token, not a mobile token")
        if code == 121001 or code == 401:
            raise DJIAuthError("Token expired or invalid")
        if code != 0:
            raise DJIAPIError(f"API error code={code}: {result.get('message', '')}")

        return data.get("data", {})

    async def validate_token(self) -> bool:
        """Check if token is valid — returns True on success."""
        try:
            await self._get(WELCOME_REGION_PATH)
            return True
        except DJIAuthError:
            return False

    async def get_devices(self) -> list[dict[str, Any]]:
        """Return list of DY power station devices."""
        data = await self._get(DEVICES_LIST_PATH)
        return data.get("dy_devices", [])

    async def get_mqtt_credentials(self) -> dict[str, Any]:
        """Return fresh MQTT credentials."""
        return await self._get(MQTT_TOKEN_PATH)

    async def set_ac_output(self, sn: str, enabled: bool) -> None:
        """Enable or disable AC output on the device."""
        await self._post(
            f"/app/api/v1/devices/{sn}/thing/property/set",
            {
                "sn": sn,
                "properties": {
                    "output_power_enable": {"ac": 1 if enabled else 0},
                },
            },
        )
