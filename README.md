  # Glowmarkt Energy (Bright) – Home Assistant custom integration

Electricity and gas usage, cost and tariff from the Glowmarkt / Hildebrand "Bright" API
(`https://api.glowmarkt.com/api/v0-1`), as hourly long-term statistics for the Energy dashboard plus
day / week / month / year totals.

## Install
1. Copy `custom_components/glowmarkt_energy` into your Home Assistant `config/custom_components/` folder
   (or add this repo to HACS as a custom repository; HACS offers the GitHub releases only, not the latest commit).
2. Restart Home Assistant.
3. *Settings → Devices & services → Add integration → Glowmarkt Energy (Bright)* and sign in with your Bright
   username and password. Leave the Application ID at its default (the published ID for individual Bright users).

Requires Home Assistant 2024.11 or newer (tested against 2026.10.4). Entities appear after the first refresh,
which also imports up to 30 days of hourly history (configurable under the integration's *Configure* button).
The integration's icon (`brand/`) shows from Home Assistant 2026.3 on.

## What you get
For each resource the account has (Electricity, Gas, Electricity cost, Gas cost):

| Entity | Unit | Source | Notes |
|---|---|---|---|
| `… last hour` | kWh / GBP | `PT1H` | Latest hour that has complete readings (attributes `period_start`, `period_end`); cost includes the hour's share of the standing charge (attribute `standing_charge`) |
| `… today` / `… yesterday` | kWh / GBP | `P1D` | |
| `… this week` | kWh / GBP | `P1W` | Monday–Sunday |
| `… this month` / `… this year` | kWh / GBP | `P1M` | The year is the sum of its months (`P1Y` returns 0 for cost) |

For Electricity and Gas additionally:

| Entity | Unit | Notes |
|---|---|---|
| `… unit rate` | GBP/kWh | Current tariff; attributes `tariff_name`, `tariff_type`, `effective_from`; gas adds `rate_GBP_per_m3`; time-of-use and dynamic tariffs add `plan_detail` (pence, as returned) |
| `… standing charge` | GBP/day | |
| `… data until` (diagnostic) | timestamp | End of the latest half-hour the API has readings for |

And once per meter device, both diagnostic timestamps:

| Entity | Notes |
|---|---|
| `API last successful fetch` | End of the last poll the API answered (also when it had no new readings) |
| `API last error` | Last failed API request (poll, tariff, catch-up, login); the message is in the `error` attribute |

Both are kept in memory, so they show *Unknown* after a restart until the next poll / error. They stay available
when an update fails, while the other sensors become unavailable.

The API reports cost in pence; the integration converts it to GBP. All cost values **include the standing
charge**: the day/week/month/year totals as the API aggregates them, and the hourly cost because the integration
adds an equal share of each local day's standing charge to every hour of that day (23 or 25 hours on clock-change
days), taken from the tariff history (`/tariff-list`). A complete day of hourly cost therefore equals the API's
daily cost, i.e. the bill.

Smart-meter (DCC) readings usually arrive hours late. The API returns `0` for half-hours it has no reading for
yet, so the integration asks `/last-time` where the data ends and only imports complete hours up to there.
"today" therefore only covers the readings received so far. When the latest electricity or gas reading is more
than 4 hours old, the integration asks Glowmarkt to fetch newer readings from the DCC (`/catchup`, at most once
per half-hour per meter); they show up on a later poll.

### Energy dashboard
Home Assistant stores long-term statistics **per hour**, so the integration imports hourly data (no 30-minute
resolution). Statistics:

* `Electricity consumption (hourly)` / `Gas consumption (hourly)` – kWh
* `Electricity cost (hourly)` / `Gas cost (hourly)` – GBP, including standing charges

Under *Settings → Dashboards → Energy*: *Electricity grid → Add consumption*, pick `Electricity consumption (hourly)`
and, for costs, *Use an entity tracking the total costs* → `Electricity cost (hourly)`. Same for gas. The sensors
above intentionally have no `state_class` so nothing gets counted twice.

The last 24 h of hourly data are re-imported whenever new readings arrive, so late or corrected smart-meter data
replaces earlier values.

## Options
* **Update interval** (default 30 min, minimum 10). A poll asks `/last-time` for each resource and only fetches
  readings, totals and the tariff when new data arrived or the day changed.
* **Meter timezone** (default `Europe/London`). Day/week/month/year totals use it. It is sent to the API as the
  `offset` parameter.
* **Backfill days** – history imported the first time only, never earlier than the meter's first reading.

## Known limits
* Week/month/year queries send one UTC offset for the whole window, so a window that spans a clock change can be
  off by one hour at its edge.
* Tariff rates are assumed to be pence (the API gives no unit; it matches the cost resources).
* The API's own cost totals charge standing charges for every day of a week/month/year up to today, even days
  before the meter had readings, so "this year" can be too high in the first year. The hourly cost statistics do
  not have this problem.
* Whether `/catchup` actually shortens the delay is not verified yet.
* Discovery uses `GET /virtualentity` and `GET /virtualentity/{id}/resources` and looks for the classifiers
  `electricity.consumption`, `gas.consumption` and their `.cost` counterparts. Enable debug logging for
  `custom_components.glowmarkt_energy` to see what was found.

## Tests
* `python3 tests/test_logic.py` – no Home Assistant needed: window/offset maths, hourly sums, tariff parsing,
  the API client, and checks against recorded real responses in `tests/fixtures`.
* Full suite, running the integration inside Home Assistant (config flow, sensors, statistics, Energy validation)
  against an in-memory fake of the API (`tests/fake_glowmarkt.py`):
  ```
  python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
  .venv/bin/python -m pytest
  ```

## Releasing
Set `version` in `custom_components/glowmarkt_energy/manifest.json`, commit, then tag and push the tag:
```
git tag v0.4.1 && git push origin v0.4.1
```
The `Release` workflow checks that the tag matches the manifest version and publishes a GitHub release, which
HACS then offers as an update.
