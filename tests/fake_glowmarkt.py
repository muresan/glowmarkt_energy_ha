"""In-memory stand-in for the Glowmarkt API, for tests that run the integration in Home Assistant.

Every behaviour below was observed on the live API on 2026-10-29 (DCC-sourced SMETS2 account):

- POST /auth -> {"valid": true, "token", "exp"}; bad credentials -> 401 {"valid": false}.
- A request with an unknown token -> 404 {"error": "Not found"}, not 401.
- GET /virtualentity lists entities whose resources lack `classifier`;
  GET /virtualentity/{id}/resources returns the entity with full resources (classifier, baseUnit).
- GET /resource/{id}/readings: `from`/`to` select half-hours by their start, both inclusive.
  Without `offset` they are UTC; with `offset` they are local wall-clock time and so are the
  returned timestamps. A bucket is returned for every period in the range: half-hours without
  data count as 0, or the bucket is null with nulls=1 (consumption only; cost resources ignore
  `nulls`). Day-or-longer buckets that start after today are left out. Cost = kWh x unit rate,
  in pence; P1D and longer cost buckets also add the daily standing charge for every day up to
  today (even days before the meter had data), and P1Y returns 0 for cost resources.
  PT1H/P1D spans over 31 days -> 400.
- GET /resource/{id}/last-time -> {"data": {"lastTs": start of the latest half-hour with data}};
  first-time -> {"data": {"firstTs": ...}}.
- GET /resource/{id}/tariff -> tests/fixtures/tariff_<commodity>.json; tariff-list -> the
  tariff-list fixture for cost resources, 400 "resource is not cost" otherwise.
- GET /resource/{id}/catchup -> {"data": {"valid": true}} (the fetch from the DCC is asynchronous;
  the fake does not simulate it).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://api.glowmarkt.com/api/v0-1"
FMT = "%Y-%m-%dT%H:%M:%S"
HALF_HOUR = timedelta(minutes=30)

ELEC = "e1ec0001-0000-4000-8000-000000000001"
ELEC_COST = "e1ec0c05-0000-4000-8000-000000000002"
GAS = "9a500001-0000-4000-8000-000000000003"
GAS_COST = "9a500c05-0000-4000-8000-000000000004"
VE_ID = "ve000001-0000-4000-8000-000000000001"

USERNAME = "user@example.org"
PASSWORD = "secret"

# resource id -> (commodity, is_cost)
RESOURCES = {
    ELEC: ("electricity", False),
    ELEC_COST: ("electricity", True),
    GAS: ("gas", False),
    GAS_COST: ("gas", True),
}
# pence per kWh, pence per day (as in the tariff fixtures)
RATES = {"electricity": (26.11, 61.41), "gas": (7.871, 29.886)}


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def epoch(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


class FakeGlowmarkt:
    """Serve recorded and generated responses through Home Assistant's aiohttp mocker."""

    def __init__(self) -> None:
        # commodity -> {UTC epoch of half-hour start: kWh}
        self.half_hours: dict[str, dict[int, float]] = {"electricity": {}, "gas": {}}
        self.today = date(2026, 10, 29)  # local date used for standing charges
        self.password = PASSWORD
        self.tokens: set[str] = set()
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.fail_paths: dict[str, int] = {}  # path suffix -> HTTP status to return
        self.tariff_lists: dict[str, dict] = {}  # resource id -> tariff-list response override
        self._mocker: AiohttpClientMocker | None = None

    def register(self, mocker: AiohttpClientMocker) -> None:
        self._mocker = mocker
        pattern = re.compile(re.escape(BASE) + r"/.*")
        mocker.post(pattern, side_effect=self._handle)
        mocker.get(pattern, side_effect=self._handle)

    # --- data helpers ---------------------------------------------------------------------
    def fill(self, commodity: str, start: datetime, end: datetime, kwh_per_half_hour) -> None:
        """Store readings for half-hours in [start, end); `kwh_per_half_hour(dt)` gives values."""
        slot = start
        while slot < end:
            self.half_hours[commodity][int(slot.timestamp())] = kwh_per_half_hour(slot)
            slot += HALF_HOUR

    def last_ts(self, commodity: str) -> int | None:
        return max(self.half_hours[commodity], default=None)

    def readings_calls(self, resource_id: str | None = None) -> list[dict[str, str]]:
        return [
            p
            for path, p in self.calls
            if path.endswith("/readings") and (resource_id is None or f"/{resource_id}/" in path)
        ]

    def paths(self, action: str) -> list[str]:
        """Paths of the requests made to /resource/{id}/<action>, in order."""
        return [path for path, _ in self.calls if path.endswith(f"/{action}")]

    # --- request handling -----------------------------------------------------------------
    async def _handle(self, method: str, url, data) -> AiohttpClientMockResponse:
        path = url.path.removeprefix("/api/v0-1")
        params = dict(url.query)
        self.calls.append((path, params))

        def reply(body: Any, status: int = 200) -> AiohttpClientMockResponse:
            return AiohttpClientMockResponse(method, url, status=status, json=body)

        if method.upper() == "POST" and path == "/auth":
            if data != {"username": USERNAME, "password": self.password}:
                return reply({"valid": False}, 401)
            token = f"jwt-{len(self.tokens) + 1}"
            self.tokens.add(token)
            return reply({"valid": True, "token": token, "exp": 4102444800, "name": "Test"})

        headers = self._mocker.mock_calls[-1][3] or {}
        if headers.get("token") not in self.tokens:
            return reply({"error": "Not found"}, 404)
        for suffix, status in self.fail_paths.items():
            if path.endswith(suffix):
                return reply({"error": "simulated failure"}, status)

        if path == "/virtualentity":
            return reply(load_fixture("virtualentity.json"))
        if path == f"/virtualentity/{VE_ID}/resources":
            return reply(load_fixture("ve_resources.json"))
        match = re.fullmatch(r"/resource/([^/]+)/([a-z-]+)", path)
        if not match or match.group(1) not in RESOURCES:
            return reply({"error": "Not found"}, 404)
        rid, action = match.groups()
        commodity, is_cost = RESOURCES[rid]
        if action in ("last-time", "first-time"):
            pick = max if action == "last-time" else min
            stamp = pick(self.half_hours[commodity], default=None)
            if stamp is None:
                return reply("Not Found", 404)
            key = "lastTs" if action == "last-time" else "firstTs"
            return reply({"status": "OK", "resourceId": rid, "data": {key: stamp}})
        if action == "tariff":
            return reply(load_fixture(f"tariff_{commodity}.json"))
        if action == "tariff-list":
            if not is_cost:
                error = {"error": "incorrect elements -resource is not cost"}
                return reply({**error, "incorrectElements": ["resourceID"]}, 400)
            return reply(self.tariff_lists.get(rid) or load_fixture(
                f"tariff-list_{commodity}.consumption.cost.json"
            ))
        if action == "catchup":
            return reply({"status": "OK", "resourceId": rid, "data": {"valid": True}})
        if action == "readings":
            return self._readings(rid, params, reply)
        return reply({"error": "Not found"}, 404)

    def _readings(self, rid: str, params: dict[str, str], reply) -> AiohttpClientMockResponse:
        commodity, is_cost = RESOURCES[rid]
        rate, standing = RATES[commodity]
        period = params["period"]
        offset = timedelta(minutes=int(params.get("offset", 0)))  # local + offset = UTC
        first = datetime.strptime(params["from"], FMT)
        last = datetime.strptime(params["to"], FMT)
        if period in ("PT1H", "P1D") and last - first > timedelta(days=31):
            return reply(
                {
                    "valid": False,
                    "error": "ERROR, query limit exceeded 31 days for a daily/hourly aggregation",
                    "incorrectElements": ["period", "from", "to"],
                },
                400,
            )
        nulls = params.get("nulls") == "1" and not is_cost

        buckets: dict[datetime, list[float | None]] = {}
        days: dict[datetime, set[date]] = {}
        slot = first.replace(minute=first.minute - first.minute % 30, second=0)
        while slot <= last:
            value = self.half_hours[commodity].get(epoch(slot + offset))
            key = self._bucket(slot, period)
            buckets.setdefault(key, []).append(value)
            days.setdefault(key, set()).add(slot.date())
            slot += HALF_HOUR

        data = []
        for key, values in sorted(buckets.items()):
            if period not in ("PT30M", "PT1H") and key.date() > self.today:
                continue  # no buckets for future days / months
            present = [v for v in values if v is not None]
            if nulls and not present:
                data.append([epoch(key), None])
                continue
            total = sum(present)
            if is_cost:
                total *= rate
                if period in ("P1D", "P1W", "P1M"):
                    total += standing * sum(1 for d in days[key] if d <= self.today)
                elif period == "P1Y":
                    total = 0
            data.append([epoch(key), round(total, 8)])
        return reply(
            {
                "status": "OK",
                "resourceId": rid,
                "query": {k: params[k] for k in ("from", "to", "period", "function")},
                "data": data,
                "units": "pence" if is_cost else "kWh",
                "classifier": f"{commodity}.consumption" + (".cost" if is_cost else ""),
            }
        )

    @staticmethod
    def _bucket(slot: datetime, period: str) -> datetime:
        if period == "PT30M":
            return slot
        if period == "PT1H":
            return slot.replace(minute=0)
        day = slot.replace(hour=0, minute=0)
        if period == "P1D":
            return day
        if period == "P1W":
            return day - timedelta(days=day.weekday())
        if period == "P1M":
            return day.replace(day=1)
        if period == "P1Y":
            return day.replace(month=1, day=1)
        raise ValueError(period)
