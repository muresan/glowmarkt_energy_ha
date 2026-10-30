"""Sensors for the Glowmarkt (Bright) energy integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, TARIFF_CURRENCY, TARIFF_SCALE
from .coordinator import GlowmarktCoordinator, Resource, ResourceData

DEVICE_CLASSES = {
    "kWh": SensorDeviceClass.ENERGY,
    "Wh": SensorDeviceClass.ENERGY,
    "MWh": SensorDeviceClass.ENERGY,
    "m³": SensorDeviceClass.GAS,
    "ft³": SensorDeviceClass.GAS,
    "GBP": SensorDeviceClass.MONETARY,
}


@dataclass(frozen=True, kw_only=True)
class GlowmarktSensorDescription(SensorEntityDescription):
    """How to read one sensor's value from a resource's data."""

    value_fn: Callable[[ResourceData], float | datetime | None]
    attrs_fn: Callable[[ResourceData], dict[str, Any] | None] = lambda _: None


def _hour_attrs(data: ResourceData) -> dict[str, Any] | None:
    if not data.latest_hour:
        return None
    ts, _, standing = data.latest_hour
    attrs: dict[str, Any] = {
        "period_start": dt_util.utc_from_timestamp(ts).isoformat(),
        "period_end": dt_util.utc_from_timestamp(ts + 3600).isoformat(),
    }
    if standing:  # cost resources: this hour's share of the daily standing charge
        attrs["standing_charge"] = round(standing, 6)
    return attrs


def _tariff_attrs(data: ResourceData) -> dict[str, Any] | None:
    tariff = data.tariff
    if tariff is None:
        return None
    attrs: dict[str, Any] = {
        "tariff_name": tariff.name,
        "tariff_type": tariff.type,
        "effective_from": tariff.effective_from,
    }
    rate_m3 = next((d["ratem3"] for d in tariff.details if "ratem3" in d), None)
    if rate_m3 is not None:
        attrs[f"rate_{TARIFF_CURRENCY}_per_m3"] = round(float(rate_m3) * TARIFF_SCALE, 5)
    if tariff.type != "flatrate":  # time-of-use tiers, dynamic tariff codes: pence, as returned
        attrs["plan_detail"] = tariff.details
    return attrs


def _scaled(value: float | None) -> float | None:
    return None if value is None else round(value * TARIFF_SCALE, 5)


def _total(key: str, suffix: str) -> GlowmarktSensorDescription:
    return GlowmarktSensorDescription(
        key=key, name=suffix, value_fn=lambda d: d.totals.get(key)
    )


# Consumption and cost resources: values in the resource's unit (kWh / GBP).
SENSORS: tuple[GlowmarktSensorDescription, ...] = (
    GlowmarktSensorDescription(
        key="last_hour",
        name="last hour",
        value_fn=lambda d: d.latest_hour[1] if d.latest_hour else None,
        attrs_fn=_hour_attrs,
    ),
    _total("today", "today"),
    _total("yesterday", "yesterday"),
    _total("week", "this week"),
    _total("month", "this month"),
    _total("year", "this year"),
)

# Consumption resources only.
CONSUMPTION_SENSORS: tuple[GlowmarktSensorDescription, ...] = (
    GlowmarktSensorDescription(
        key="unit_rate",
        name="unit rate",
        native_unit_of_measurement=f"{TARIFF_CURRENCY}/kWh",
        suggested_display_precision=4,
        value_fn=lambda d: _scaled(d.tariff.unit_rate) if d.tariff else None,
        attrs_fn=_tariff_attrs,
    ),
    GlowmarktSensorDescription(
        key="standing_charge",
        name="standing charge",
        native_unit_of_measurement=f"{TARIFF_CURRENCY}/day",
        suggested_display_precision=4,
        value_fn=lambda d: _scaled(d.tariff.standing_charge) if d.tariff else None,
        attrs_fn=_tariff_attrs,
    ),
    GlowmarktSensorDescription(
        key="data_until",
        name="data until",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        # end of the latest half-hour with a reading; smart-meter data often lags by hours
        value_fn=lambda d: dt_util.utc_from_timestamp(d.last_ts + 1800) if d.last_ts else None,
    ),
)


@dataclass(frozen=True, kw_only=True)
class GlowmarktApiSensorDescription(SensorEntityDescription):
    """How to read one API status value from the coordinator."""

    value_fn: Callable[[GlowmarktCoordinator], datetime | None]
    attrs_fn: Callable[[GlowmarktCoordinator], dict[str, Any] | None] = lambda _: None


# One set per meter (virtual entity) device; they describe the whole account's API access.
API_SENSORS: tuple[GlowmarktApiSensorDescription, ...] = (
    GlowmarktApiSensorDescription(
        key="api_last_success",
        name="API last successful fetch",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.last_success,
    ),
    GlowmarktApiSensorDescription(
        key="api_last_error",
        name="API last error",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.last_error,
        attrs_fn=lambda c: {"error": c.last_error_message} if c.last_error_message else None,
    ),
)


def _device_info(res: Resource) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, res.ve_id)},
        name=res.ve_name,
        manufacturer="Hildebrand (Glowmarkt / Bright)",
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: GlowmarktCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        GlowmarktSensor(coordinator, res, description)
        for res in coordinator.resources
        for description in SENSORS + (() if res.is_cost else CONSUMPTION_SENSORS)
    ]
    first_of_device = {res.ve_id: res for res in reversed(coordinator.resources)}
    entities.extend(
        GlowmarktApiSensor(coordinator, res, description)
        for res in first_of_device.values()
        for description in API_SENSORS
    )
    async_add_entities(entities)


class GlowmarktSensor(CoordinatorEntity[GlowmarktCoordinator], SensorEntity):
    """One value of one resource.

    These sensors deliberately have no state_class: the Energy dashboard should use the
    imported "<Electricity|Gas> consumption (hourly)" and "<Electricity|Gas> cost (hourly)"
    statistics instead, so nothing is counted twice.
    """

    _attr_has_entity_name = True
    entity_description: GlowmarktSensorDescription

    def __init__(
        self,
        coordinator: GlowmarktCoordinator,
        res: Resource,
        description: GlowmarktSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._res = res
        self._attr_unique_id = f"{res.resource_id}_{description.key}"
        self._attr_name = f"{res.label} {description.name}"
        if description.native_unit_of_measurement is None and description.device_class is None:
            self._attr_native_unit_of_measurement = res.unit
            self._attr_device_class = DEVICE_CLASSES.get(res.unit)
            self._attr_suggested_display_precision = 2 if res.is_cost else 3
        self._attr_device_info = _device_info(res)

    @property
    def _data(self) -> ResourceData | None:
        return (self.coordinator.data or {}).get(self._res.resource_id)

    @property
    def native_value(self) -> float | datetime | None:
        data = self._data
        return None if data is None else self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self._data
        return None if data is None else self.entity_description.attrs_fn(data)


class GlowmarktApiSensor(CoordinatorEntity[GlowmarktCoordinator], SensorEntity):
    """When the API last answered / last failed, for the device of a meter."""

    _attr_has_entity_name = True
    entity_description: GlowmarktApiSensorDescription

    def __init__(
        self,
        coordinator: GlowmarktCoordinator,
        res: Resource,
        description: GlowmarktApiSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{res.ve_id}_{description.key}"
        self._attr_device_info = _device_info(res)

    @property
    def available(self) -> bool:
        return True  # most useful exactly when an update has failed

    @property
    def native_value(self) -> datetime | None:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attrs_fn(self.coordinator)
