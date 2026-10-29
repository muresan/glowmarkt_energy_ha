"""Offline tests for the pure helpers and the API client (no Home Assistant / network needed).

Run:  python3 tests/test_logic.py   (or with pytest)
aiohttp is stubbed if it is not installed, so the API client is exercised with a fake session.
The fixtures in tests/fixtures are real API responses (2026-10-29) with IDs replaced.
"""
import asyncio
import importlib
import json
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "glowmarkt_energy"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Load the package WITHOUT running its __init__ (which imports Home Assistant).
pkg = types.ModuleType("gm")
pkg.__path__ = [str(PKG_DIR)]
sys.modules["gm"] = pkg

try:
    import aiohttp  # noqa: F401
except ImportError:  # minimal stub so api.py can be imported
    stub = types.ModuleType("aiohttp")
    class ClientError(Exception): ...
    class ClientTimeout:
        def __init__(self, total=None): self.total = total
    class ClientSession: ...
    stub.ClientError, stub.ClientTimeout, stub.ClientSession = ClientError, ClientTimeout, ClientSession
    sys.modules["aiohttp"] = stub

util = importlib.import_module("gm.util")
api_mod = importlib.import_module("gm.api")

UTC = timezone.utc
ELEC = "e1ec0001-0000-4000-8000-000000000001"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_windows():
    tz = ZoneInfo("Europe/London")
    now = datetime(2026, 10, 28, 14, 5, tzinfo=UTC)  # a Wednesday, GMT
    w = {x.key: x for x in util.period_windows(tz, now)}
    assert w["today"].start == datetime(2026, 10, 28) and w["today"].end == datetime(2026, 10, 28, 23, 59, 59)
    assert w["yesterday"].start == datetime(2026, 10, 27)
    assert w["week"].start == datetime(2026, 10, 26) and w["week"].start.weekday() == 0  # Monday
    assert w["week"].end == datetime(2026, 11, 1, 23, 59, 59)
    assert w["month"].start == datetime(2026, 10, 1) and w["month"].end == datetime(2026, 10, 31, 23, 59, 59)
    assert w["year"].start == datetime(2026, 1, 1) and w["year"].end == datetime(2026, 12, 31, 23, 59, 59)
    # the year is summed from monthly buckets: P1Y returns 0 for cost resources
    assert [w[k].period for k in ("today", "yesterday", "week", "month", "year")] == ["P1D", "P1D", "P1W", "P1M", "P1M"]
    assert w["today"].offset == 0  # GMT
    assert w["month"].offset == -60  # 1 October is still BST
    # summer: BST = UTC+1 -> offset -60 ; local date rolls over at 23:00 UTC
    summer = datetime(2026, 7, 15, 23, 30, tzinfo=UTC)
    ws = {x.key: x for x in util.period_windows(tz, summer)}
    assert ws["today"].start == datetime(2026, 7, 16) and ws["today"].offset == -60
    # sign check (UTC+2 -> -120, UTC-5 -> +300); the live API confirmed BST = -60
    assert util.offset_minutes(ZoneInfo("Europe/Bucharest"), datetime(2026, 1, 10)) == -120
    assert util.offset_minutes(ZoneInfo("America/New_York"), datetime(2026, 1, 10)) == 300


def test_hourly_and_cumulative():
    pts = [(1793059200, 0.3), (1793062800, 0.4), (1793062800, 0.5), (1793060000, 9.9)]  # dup + non-aligned
    h = util.hourly_points(pts)
    assert [round(v, 3) for _, v in h] == [0.3, 0.5]  # later duplicate wins, odd bucket dropped
    rows = util.cumulative(h, base=10.0)
    assert [r[1] for r in rows] == [10.3, 10.8]
    assert util.hourly_points(pts, not_before=h[1][0]) == [h[1]]
    assert util.hourly_points(pts, before=h[1][0]) == [h[0]]
    f = util.floor_to_step(datetime(2026, 10, 28, 14, 47, 12, tzinfo=UTC), 1800)
    assert f == datetime(2026, 10, 28, 14, 30, tzinfo=UTC)
    assert util.to_naive_utc(f).tzinfo is None


def test_complete_hours_end():
    # lastTs is the start of the latest half-hour with data (live: electricity 00:30, gas 01:00)
    at = lambda h, m: int(datetime(2026, 10, 29, h, m, tzinfo=UTC).timestamp())
    assert util.complete_hours_end(at(0, 30)) == datetime(2026, 10, 29, 1, tzinfo=UTC)  # 00:00 complete
    assert util.complete_hours_end(at(1, 0)) == datetime(2026, 10, 29, 1, tzinfo=UTC)  # 01:00 half done


def test_units():
    assert util.normalise_unit("kwh") == "kWh" and util.normalise_unit("m3") == "m³" and util.normalise_unit(None) == "kWh"
    assert util.normalise_unit("pence") == "GBP" and util.unit_scale("pence") == 0.01
    assert util.unit_scale("kWh") == 1.0 and util.unit_scale(None) == 1.0
    assert util.UNIT_CLASSES.get("GBP") is None and util.UNIT_CLASSES["kWh"] == "energy"


def test_recorded_hourly_usage_and_cost():
    """One real day (2026-10-27, GMT) of PT1H readings: cost = kWh x unit rate, per hour."""
    tariffs = {c: util.parse_tariff(fixture(f"tariff_{c}.json")["data"]) for c in ("electricity", "gas")}
    for commodity in ("electricity", "gas"):
        usage = fixture(f"readings_PT1H_2026-10-27_{commodity}.consumption.json")
        cost = fixture(f"readings_PT1H_2026-10-27_{commodity}.consumption.cost.json")
        assert usage["units"] == "kWh" and cost["units"] == "pence"
        assert len(usage["data"]) == len(cost["data"]) == 24
        assert usage["data"][0][0] == int(datetime(2026, 10, 27, tzinfo=UTC).timestamp())
        assert all(ts % 3600 == 0 for ts, _ in usage["data"])  # PT1H buckets are UTC hours
        rate = tariffs[commodity].unit_rate
        for (t1, kwh), (t2, pence) in zip(usage["data"], cost["data"]):
            assert t1 == t2 and abs(kwh * rate - pence) < 1e-6  # no standing charge in hourly cost

        # What the coordinator imports: cumulative kWh, and cumulative GBP.
        energy_rows = util.cumulative(util.hourly_points(usage["data"]), 0.0)
        scale = util.unit_scale(cost["units"])
        cost_rows = util.cumulative([(s, v * scale) for s, v in util.hourly_points(cost["data"])], 0.0)
        day_kwh = sum(v for _, v in usage["data"])
        assert abs(energy_rows[-1][1] - day_kwh) < 1e-6
        assert abs(cost_rows[-1][1] - day_kwh * rate / 100) < 1e-5
    assert abs(sum(v for _, v in fixture("readings_PT1H_2026-10-27_electricity.consumption.json")["data"]) - 7.574) < 1e-9


def test_parse_tariff():
    elec = util.parse_tariff(fixture("tariff_electricity.json")["data"])
    assert (elec.unit_rate, elec.standing_charge) == (26.11, 61.41)
    assert (elec.name, elec.type, elec.effective_from) == ("fromDCC", "flatrate", "2026-10-19 00:00:00")
    gas = util.parse_tariff(fixture("tariff_gas.json")["data"])
    assert (gas.unit_rate, gas.standing_charge) == (7.871, 29.886)
    assert {"ratem3": 84.963, "rate": 7.871} in gas.details
    # tariff-list entries (cost resources) use effectiveDate/displayName and no currentRates
    listed = util.parse_tariff(fixture("tariff-list_electricity.consumption.cost.json")["data"])
    assert (listed.unit_rate, listed.standing_charge, listed.name) == (26.11, 61.41, "fromDCC")
    # Swagger examples: time-of-use with string rates, dynamic without a rate; latest entry wins
    tou = {"from": "2020-12-12 00:00:00", "name": "My TOU Tariff", "plan": [{"planDetail": [
        {"standing": 25}, {"tier": 1, "rate": "16.26"}, {"tier": 2, "tourate": "5", "time": "20:30-00:00"}]}]}
    dynamic = {"from": "2019-12-12 00:00:00", "name": "DYNAMIC", "plan": [{"planDetail": [
        {"standing": 25}, {"dynamic": "DYNAMIC_TARIFF_CODE"}]}]}
    t = util.parse_tariff([dynamic, tou])
    assert (t.name, t.unit_rate, t.standing_charge) == ("My TOU Tariff", 16.26, 25.0)
    assert {"tier": 2, "tourate": "5", "time": "20:30-00:00"} in t.details
    d = util.parse_tariff([dynamic])
    assert d.unit_rate is None and d.standing_charge == 25.0
    assert util.parse_tariff([]) is None and util.parse_tariff(["junk"]) is None


def test_standing_charge_spreading():
    tz = ZoneInfo("Europe/London")
    assert util.local_day_hours(tz, date(2026, 10, 28)) == 24
    assert util.local_day_hours(tz, date(2026, 10, 25)) == 25  # BST -> GMT
    assert util.local_day_hours(tz, date(2026, 3, 29)) == 23  # GMT -> BST

    assert util.standing_charges(fixture("tariff-list_gas.consumption.cost.json")["data"]) == [
        (date(2026, 10, 19), 29.886)]
    history = util.standing_charges([  # unsorted, one entry without a standing charge
        {"effectiveDate": "2026-10-19 00:00:00", "plan": [{"planDetail": [{"standing": 61.41}]}]},
        {"effectiveDate": "2026-10-01 00:00:00", "plan": [{"planDetail": [{"standing": 50}]}]},
        {"effectiveDate": "2026-10-10 00:00:00", "plan": [{"planDetail": [{"rate": 20}]}]},
    ])
    assert history == [(date(2026, 10, 1), 50.0), (date(2026, 10, 19), 61.41)]
    assert util.standing_on(history, date(2026, 9, 1)) == 50.0  # before the oldest: the oldest
    assert util.standing_on(history, date(2026, 10, 18)) == 50.0
    assert util.standing_on(history, date(2026, 10, 19)) == 61.41
    assert util.standing_on([], date(2026, 10, 19)) is None

    # The 25 UTC hours of local 25 October share one standing charge.
    day = [datetime(2026, 10, 24, 23, tzinfo=UTC) + timedelta(hours=h) for h in range(25)]
    shares = util.standing_shares(day, tz, history)
    assert len(set(shares)) == 1 and abs(sum(shares) - 61.41) < 1e-9
    assert util.standing_shares(day[:1], tz, []) == [0.0]


class FakeResp:
    def __init__(self, status, payload): self.status, self._p = status, payload
    async def json(self, content_type=None): return self._p
    async def text(self): return str(self._p)
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class FakeSession:
    """Records requests; plays back recorded responses. Invalid tokens get 404, like the live API."""
    def __init__(self):
        self.calls = []; self.auth_status = 200; self.valid_tokens = set()
    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, headers, json))
        if self.auth_status != 200:
            return FakeResp(self.auth_status, {"valid": False})
        token = f"jwt{len(self.calls)}"
        self.valid_tokens.add(token)
        return FakeResp(200, {"valid": True, "token": token, "exp": 4102444800})
    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, headers, params))
        if headers["token"] not in self.valid_tokens:
            return FakeResp(404, {"error": "Not found"})
        if url.endswith("/virtualentity"):
            return FakeResp(200, fixture("virtualentity.json"))
        if url.endswith("/resources"):
            return FakeResp(200, fixture("ve_resources.json"))
        if url.endswith(f"{ELEC}/last-time"):
            return FakeResp(200, {"status": "OK", "data": {"lastTs": 1793233800}})
        if url.endswith(f"{ELEC}/first-time"):
            return FakeResp(200, {"status": "OK", "data": {"firstTs": 1783200600}})
        if url.endswith(("/last-time", "/first-time")):
            return FakeResp(404, "Not Found")
        if url.endswith("/tariff"):
            return FakeResp(200, fixture("tariff_electricity.json"))
        if url.endswith("/tariff-list"):
            return FakeResp(200, fixture("tariff-list_electricity.consumption.cost.json"))
        if url.endswith("/catchup"):  # recorded live response
            return FakeResp(200, {"status": "OK", "data": {"valid": True}})
        if url.endswith("/readings"):
            return FakeResp(200, fixture("readings_PT1H_2026-10-27_electricity.consumption.json"))
        return FakeResp(404, {"error": "Not found"})


def test_api():
    s = FakeSession()
    api = api_mod.GlowmarktApi(s, "u@example.org", "pw", "app-id")

    async def run():
        r = await api.get_readings(ELEC, datetime(2026, 10, 27), datetime(2026, 10, 27, 23, 59, 59), "PT1H")
        assert r.unit == "kWh" and len(r.points) == 24 and r.points[0] == (1793059200, 0.322)
        method, url, headers, params = s.calls[-1]
        assert url.endswith(f"/resource/{ELEC}/readings") and headers["token"] == "jwt1" and headers["applicationId"] == "app-id"
        assert params == {"from": "2026-10-27T00:00:00", "to": "2026-10-27T23:59:59", "period": "PT1H", "function": "sum"}
        # offset / nulls only sent when given
        await api.get_readings(ELEC, datetime(2026, 7, 1), datetime(2026, 7, 1, 23, 59, 59), "P1D", -60, nulls=True)
        assert s.calls[-1][3]["offset"] == "-60" and s.calls[-1][3]["nulls"] == "1"
        assert sum(1 for c in s.calls if c[0] == "POST") == 1  # token reused
        # discovery: the resources endpoint carries classifier and baseUnit
        assert [v["veId"] for v in await api.get_virtual_entities()] == [
            "ve000001-0000-4000-8000-000000000001", "ve000002-0000-4000-8000-000000000002"]
        resources = await api.get_ve_resources("ve000001-0000-4000-8000-000000000001")
        assert {(r["classifier"], r["baseUnit"]) for r in resources} == {
            ("electricity.consumption", "kWh"), ("electricity.consumption.cost", "pence"),
            ("gas.consumption", "kWh"), ("gas.consumption.cost", "pence")}
        assert await api.get_last_time(ELEC) == 1793233800
        assert await api.get_last_time("no-data") is None  # 404 = no readings yet
        assert await api.get_first_time(ELEC) == 1783200600
        assert await api.get_first_time("no-data") is None
        assert (await api.get_tariff_list(ELEC))[0]["effectiveDate"] == "2026-10-19 00:00:00"
        await api.catchup(ELEC)
        assert s.calls[-1][1].endswith(f"/resource/{ELEC}/catchup")
        assert (await api.get_tariff(ELEC))[0]["currentRates"] == {"rate": 26.11, "standingCharge": 61.41}

        # Invalid token -> 404. A fresh token is trusted (404 is a real "not found")...
        s.valid_tokens.clear()
        try:
            await api.get_tariff(ELEC)
        except api_mod.GlowmarktNotFoundError:
            pass
        else:
            raise AssertionError("expected GlowmarktNotFoundError")
        assert sum(1 for c in s.calls if c[0] == "POST") == 1
        # ...but once the token is over an hour old, a 404 triggers one re-login.
        real_time = api_mod.time
        api_mod.time = types.SimpleNamespace(time=lambda: real_time.time() + 2 * 3600)
        try:
            assert (await api.get_tariff(ELEC))[0]["cid"] == "PWER"
        finally:
            api_mod.time = real_time
        assert sum(1 for c in s.calls if c[0] == "POST") == 2

        # bad credentials -> auth error (live API: 401 {"valid": false})
        bad = FakeSession(); bad.auth_status = 401
        try:
            await api_mod.GlowmarktApi(bad, "u", "x", "a").authenticate()
        except api_mod.GlowmarktAuthError:
            pass
        else:
            raise AssertionError("expected GlowmarktAuthError")
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok  ", name)
