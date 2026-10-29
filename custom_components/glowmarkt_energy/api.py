"""Minimal async client for the Glowmarkt / Bright REST API (v0-1)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from .const import API_BASE_URL
from .util import TIME_FORMAT

_LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_MARGIN = 3600  # re-authenticate an hour before the JWT expires
FALLBACK_TOKEN_LIFETIME = 6 * 86400  # used if `exp` is missing
# An invalid token is answered with 404, not 401. Log in again on a 404 at most this often, so a
# genuinely missing resource does not cause a login on every request.
NOT_FOUND_RELOGIN_AGE = 3600
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)


class GlowmarktError(Exception):
    """Generic API error."""


class GlowmarktAuthError(GlowmarktError):
    """Credentials rejected."""


class GlowmarktNotFoundError(GlowmarktError):
    """HTTP 404 (unknown resource, or no readings yet for /last-time)."""


@dataclass(frozen=True)
class Readings:
    """Result of GET /resource/{id}/readings."""

    points: list[tuple[int, float]]  # (epoch of bucket start, value); local time if offset sent
    unit: str | None


class GlowmarktApi:
    """Client handling the two-header auth model (`token` + `applicationId`)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        application_id: str,
        base_url: str = API_BASE_URL,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._application_id = application_id
        self._base_url = base_url.rstrip("/")
        self._token: str | None = None
        self._token_expiry = 0.0
        self._token_issued = 0.0
        self._lock = asyncio.Lock()

    async def authenticate(self) -> None:
        """POST /auth -> JWT (valid for 7 days)."""
        headers = {"applicationId": self._application_id, "Content-Type": "application/json"}
        payload = {"username": self._username, "password": self._password}
        try:
            async with self._session.post(
                f"{self._base_url}/auth", json=payload, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                status = resp.status
                data = await resp.json(content_type=None) if status < 400 else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise GlowmarktError(f"Cannot reach Glowmarkt: {err}") from err
        if status in (400, 401, 403):
            raise GlowmarktAuthError(f"Authentication rejected (HTTP {status})")
        if status >= 400:
            raise GlowmarktError(f"Authentication failed (HTTP {status})")
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise GlowmarktAuthError("Authentication response contained no token")
        self._token = token
        self._token_issued = time.time()
        exp = data.get("exp")
        self._token_expiry = float(exp) if exp else self._token_issued + FALLBACK_TOKEN_LIFETIME

    async def _ensure_token(self) -> None:
        if self._token and time.time() < self._token_expiry - TOKEN_REFRESH_MARGIN:
            return
        async with self._lock:
            if self._token and time.time() < self._token_expiry - TOKEN_REFRESH_MARGIN:
                return
            await self.authenticate()

    async def _get(self, path: str, params: dict[str, str] | None = None, retry: bool = True) -> Any:
        await self._ensure_token()
        headers = {"token": self._token or "", "applicationId": self._application_id}
        try:
            async with self._session.get(
                f"{self._base_url}{path}", headers=headers, params=params, timeout=REQUEST_TIMEOUT
            ) as resp:
                status = resp.status
                if status < 400:
                    body = await resp.json(content_type=None)
                else:
                    body = (await resp.text())[:200]
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise GlowmarktError(f"Request to {path} failed: {err}") from err
        stale = status == 404 and time.time() - self._token_issued > NOT_FOUND_RELOGIN_AGE
        if retry and (status in (401, 403) or stale):
            # token may have been revoked: log in again once
            self._token = None
            return await self._get(path, params, retry=False)
        if status in (401, 403):
            raise GlowmarktAuthError(f"Access denied for {path} (HTTP {status})")
        if status == 404:
            raise GlowmarktNotFoundError(f"HTTP 404 for {path}: {body}")
        if status >= 400:
            raise GlowmarktError(f"HTTP {status} for {path}: {body}")
        return body

    async def get_virtual_entities(self) -> list[dict[str, Any]]:
        """GET /virtualentity - each entity is e.g. a smart meter / Glow device."""
        data = await self._get("/virtualentity")
        if isinstance(data, dict):
            data = data.get("virtualEntities") or data.get("data") or []
        return [ve for ve in data if isinstance(ve, dict) and ve.get("veId")]

    async def get_ve_resources(self, ve_id: str) -> list[dict[str, Any]]:
        """GET /virtualentity/{id}/resources."""
        data = await self._get(f"/virtualentity/{ve_id}/resources")
        if isinstance(data, dict):
            data = data.get("resources") or []
        return [r for r in data if isinstance(r, dict) and r.get("resourceId")]

    async def get_resource(self, resource_id: str) -> dict[str, Any]:
        """GET /resource/{id}."""
        data = await self._get(f"/resource/{resource_id}")
        return data if isinstance(data, dict) else {}

    async def get_last_time(self, resource_id: str) -> int | None:
        """GET /resource/{id}/last-time -> UTC epoch of the latest half-hour with a reading."""
        try:
            data = await self._get(f"/resource/{resource_id}/last-time")
        except GlowmarktNotFoundError:  # documented for resources without readings
            return None
        inner = data.get("data") if isinstance(data, dict) else None
        last_ts = inner.get("lastTs") if isinstance(inner, dict) else None
        return int(last_ts) if isinstance(last_ts, (int, float)) else None

    async def get_first_time(self, resource_id: str) -> int | None:
        """GET /resource/{id}/first-time -> UTC epoch of the first half-hour with a reading."""
        try:
            data = await self._get(f"/resource/{resource_id}/first-time")
        except GlowmarktNotFoundError:
            return None
        inner = data.get("data") if isinstance(data, dict) else None
        first_ts = inner.get("firstTs") if isinstance(inner, dict) else None
        return int(first_ts) if isinstance(first_ts, (int, float)) else None

    async def get_tariff(self, resource_id: str) -> list[dict[str, Any]]:
        """GET /resource/{id}/tariff -> tariff entries (consumption and cost resources alike)."""
        data = await self._get(f"/resource/{resource_id}/tariff")
        entries = data.get("data") if isinstance(data, dict) else None
        return [e for e in entries or [] if isinstance(e, dict)]

    async def get_tariff_list(self, resource_id: str) -> list[dict[str, Any]]:
        """GET /resource/{id}/tariff-list -> tariff history (cost resources only; unsorted)."""
        data = await self._get(f"/resource/{resource_id}/tariff-list")
        entries = data.get("data") if isinstance(data, dict) else None
        return [e for e in entries or [] if isinstance(e, dict)]

    async def catchup(self, resource_id: str) -> None:
        """GET /resource/{id}/catchup: ask Glowmarkt to fetch the latest readings from the DCC.

        Asynchronous on the server side; new readings show up in later requests.
        """
        await self._get(f"/resource/{resource_id}/catchup")

    async def get_readings(
        self,
        resource_id: str,
        start: datetime,
        end: datetime,
        period: str,
        offset: int | None = None,
        nulls: bool = False,
    ) -> Readings:
        """GET /resource/{id}/readings with function=sum.

        `start`/`end` are naive datetimes formatted as yyyy-mm-ddThh:mm:ss, both inclusive. Without
        `offset` they are UTC; with `offset` (minutes, API sign convention) they are local time and
        so are the returned timestamps. Missing readings come back as 0 unless `nulls` is set, which
        only consumption resources honour.
        """
        params = {
            "from": start.strftime(TIME_FORMAT),
            "to": end.strftime(TIME_FORMAT),
            "period": period,
            "function": "sum",
        }
        if offset is not None:
            params["offset"] = str(offset)
        if nulls:
            params["nulls"] = "1"
        data = await self._get(f"/resource/{resource_id}/readings", params)
        if not isinstance(data, dict):
            raise GlowmarktError("Unexpected readings response")
        points: list[tuple[int, float]] = []
        for item in data.get("data") or []:
            try:
                if item[1] is None:
                    continue
                points.append((int(item[0]), float(item[1])))
            except (TypeError, ValueError, IndexError):
                continue
        return Readings(points, data.get("units"))
