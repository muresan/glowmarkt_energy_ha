"""Constants for the Glowmarkt (Bright) energy integration."""
from __future__ import annotations

DOMAIN = "glowmarkt_energy"

API_BASE_URL = "https://api.glowmarkt.com/api/v0-1"
# Published applicationId for individual Bright app users.
DEFAULT_APPLICATION_ID = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"

CONF_APPLICATION_ID = "application_id"
CONF_TIMEZONE = "timezone"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_BACKFILL_DAYS = "backfill_days"

# Bright / Glowmarkt is a UK service; day/week/month boundaries follow the meter's timezone.
DEFAULT_TIMEZONE = "Europe/London"
DEFAULT_SCAN_INTERVAL = 30  # minutes; a poll without new readings costs one request per resource
DEFAULT_BACKFILL_DAYS = 30

# Resource classifier -> label used in entity and statistic names
CLASSIFIERS: dict[str, str] = {
    "electricity.consumption": "Electricity",
    "gas.consumption": "Gas",
    "electricity.consumption.cost": "Electricity cost",
    "gas.consumption.cost": "Gas cost",
}

# Tariff rates (GET /resource/{id}/tariff) carry no unit; they are pence, like the cost resources.
TARIFF_CURRENCY = "GBP"
TARIFF_SCALE = 0.01

# Aggregation periods understood by GET /resource/{id}/readings. P1Y is not used: it returns 0
# for cost resources, so the yearly total is summed from P1M buckets instead.
PERIOD_1H = "PT1H"
PERIOD_1D = "P1D"
PERIOD_1W = "P1W"
PERIOD_1M = "P1M"

HOURLY_CHUNK_DAYS = 28  # API allows at most 31 days per PT1H query
REIMPORT_HOURS = 24  # re-import this much history each poll to pick up late data
MAX_CATCHUP_DAYS = 365

# Ask Glowmarkt to fetch readings from the DCC (GET /resource/{id}/catchup) when the latest one is
# older than this; the API asks for at most one such request per half-hour.
DCC_CATCHUP_AFTER_HOURS = 4
DCC_CATCHUP_MIN_INTERVAL_MINUTES = 30
