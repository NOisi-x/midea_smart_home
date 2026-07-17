import logging
from typing import Any, Optional

from homeassistant.components.sensor import (
    EntityCategory,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import Platform
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MideaCoordinator
from .entity import MideaBaseEntity, iter_midea_device_configs

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entities = []

    for coordinator, device_id, device_type, sn, sn8, device_name, model, device_mapping in iter_midea_device_configs(hass, entry):
        entities_config = device_mapping.get("entities", {})
        sensor_config = entities_config.get(Platform.SENSOR, {})

        if sensor_config:
            for sensor_id, config in sensor_config.items():
                name = config.get("name", sensor_id)
                device_class = config.get("device_class")
                unit = config.get("unit_of_measurement")
                translation_key = config.get("translation_key")
                state_class = config.get("state_class")
                suggested_display_precision = config.get("suggested_display_precision")
                options = config.get("options")
                attribute = config.get("attribute")
                computed_status = config.get("computed_status", False)
                entities.append(
                    MideaSensorEntity(
                        coordinator, device_id, device_type, sn, sn8, device_name,
                        sensor_id, name, device_class, unit, translation_key, state_class, model,
                        suggested_display_precision, options, attribute, computed_status
                    )
                )

        ip_address = None
        if hasattr(coordinator.device, 'controller') and hasattr(coordinator.device.controller, 'ip'):
            ip_address = coordinator.device.controller.ip
        elif hasattr(coordinator.device, 'ip_address'):
            ip_address = coordinator.device.ip_address

        if ip_address:
            entities.append(
                MideaLanIPEntity(
                    coordinator, device_id, device_type, sn, sn8, device_name,
                    model, ip_address
                )
            )

    async_add_entities(entities)


class MideaSensorEntity(MideaBaseEntity, SensorEntity):

    def __init__(
        self,
        coordinator: MideaCoordinator,
        device_id: int,
        device_type: str,
        sn: str,
        sn8: str,
        device_name: str,
        sensor_id: str,
        name: str,
        device_class: Optional[SensorDeviceClass],
        unit: str,
        translation_key: str = None,
        state_class: Optional[SensorStateClass] = None,
        model: str = None,
        suggested_display_precision: Optional[int] = None,
        options: Optional[list] = None,
        attribute: Optional[str] = None,
        computed_status: bool = False,
    ):
        config = {"translation_key": translation_key} if translation_key else {}
        super().__init__(
            coordinator, device_id, device_type, sn, sn8, device_name, sensor_id, model,
            platform_name="sensor", config=config
        )
        self._sensor_id = sensor_id
        self._attribute = attribute or sensor_id
        self._computed_status = computed_status

        if options is not None:
            self._attr_options = options

        if device_class and isinstance(device_class, str):
            try:
                device_class = SensorDeviceClass(device_class)
            except ValueError:
                device_class = None

        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_suggested_display_precision = suggested_display_precision

        if state_class is not None:
            if isinstance(state_class, str):
                try:
                    state_class = SensorStateClass(state_class)
                except ValueError:
                    state_class = None
            self._attr_state_class = state_class
        elif device_class == SensorDeviceClass.ENUM:
            self._attr_state_class = None
        elif device_class == SensorDeviceClass.ENERGY:
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        elif device_class in (SensorDeviceClass.TEMPERATURE, SensorDeviceClass.HUMIDITY,
                              SensorDeviceClass.PRESSURE, SensorDeviceClass.POWER,
                              SensorDeviceClass.CURRENT, SensorDeviceClass.VOLTAGE):
            self._attr_state_class = SensorStateClass.MEASUREMENT
        else:
            self._attr_state_class = None

    @property
    def native_value(self) -> Optional[Any]:
        """Return the state of the sensor."""
        if not self.available:
            return None

        data = self.coordinator.data or {}

        # ── computed_status: use statusNum-based text (mirrors pannel.wxml) ──
        if self._computed_status:
            from .device_mapping.T0xE1 import get_status_text, get_status_num
            device_mapping = getattr(self.coordinator, "device_mapping", {})
            keep_text = device_mapping.get("_keep_text_name", "")
            dry_text = device_mapping.get("_dry_text_name", "")
            status_num = get_status_num(
                data.get("work_status"),
                airswitch=data.get("airswitch", 0),
                air_left_hour=data.get("air_left_hour", 0),
                dryswitch=data.get("dryswitch", 0),
                keep_start_now=getattr(self.coordinator, "keep_start_now", False),
            )
            return get_status_text(status_num, keep_text, dry_text)

        value = data.get(self._attribute)

        if value is None or value == "":
            return None

        return value

    @property
    def extra_state_attributes(self) -> dict:
        """Return extra attributes for keep/dry time formatting."""
        attrs = {}

        # keepTimeType=2 → air_set_hour formatted as "X天Y时"
        if self._sensor_id == "air_set_hour":
            device_mapping = getattr(self.coordinator, "device_mapping", {})
            keep_time_type = device_mapping.get("_keep_time_type", 0)
            if keep_time_type == 2:
                data = self.coordinator.data or {}
                try:
                    hours = int(data.get("air_set_hour", 0))
                except (ValueError, TypeError):
                    hours = 0
                if hours >= 24:
                    days = hours // 24
                    rem = hours % 24
                    attrs["formatted"] = f"{days}天{rem:02d}时"
                else:
                    attrs["formatted"] = f"{hours}小时"

        return attrs if attrs else None


class MideaLanIPEntity(MideaBaseEntity, SensorEntity):

    def __init__(
        self,
        coordinator: MideaCoordinator,
        device_id: int,
        device_type: str,
        sn: str,
        sn8: str,
        device_name: str,
        model: str,
        ip_address: str,
    ):
        super().__init__(
            coordinator, device_id, device_type, sn, sn8, device_name, "lan_ip", model,
            platform_name="sensor", config={"translation_key": "lan_ip"}
        )
        self._ip_address = ip_address
        self._attr_device_class = None
        self._attr_native_unit_of_measurement = None
        self._attr_state_class = None
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> Optional[str]:
        """Return the LAN IP address."""
        if not self.coordinator.device or not self.coordinator.device.available:
            return None

        if hasattr(self.coordinator.device, 'controller') and hasattr(self.coordinator.device.controller, 'ip'):
            return self.coordinator.device.controller.ip
        elif hasattr(self.coordinator.device, 'ip_address'):
            return self.coordinator.device.ip_address

        return self._ip_address
