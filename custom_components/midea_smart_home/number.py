import logging

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MideaCoordinator
from .entity import MideaBaseEntity, iter_midea_device_configs

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entities = []

    for coordinator, device_id, device_type, sn, sn8, device_name, model, device_mapping in iter_midea_device_configs(
        hass, entry
    ):
        entities_config = device_mapping.get("entities", {})
        number_config = entities_config.get(Platform.NUMBER, {})

        if number_config:
            for number_id, config in number_config.items():
                entities.append(
                    MideaNumberEntity(
                        coordinator, device_id, device_type, sn, sn8, device_name,
                        number_id, config, model
                    )
                )

    async_add_entities(entities)


class MideaNumberEntity(MideaBaseEntity, NumberEntity):
    def __init__(
        self,
        coordinator: MideaCoordinator,
        device_id: int,
        device_type: str,
        sn: str,
        sn8: str,
        device_name: str,
        entity_key: str,
        config: dict,
        model: str = None,
    ):
        super().__init__(
            coordinator, device_id, device_type, sn, sn8, device_name, entity_key, model,
            platform_name="number", config=config, condition=config.get("condition")
        )
        self._attr_native_min_value = config.get("min", 0.0)
        self._attr_native_max_value = config.get("max", 100.0)
        self._attr_native_step = config.get("step", 1.0)
        self._attr_mode = config.get("mode", "auto")

        if "unit_of_measurement" in config:
            self._attr_native_unit_of_measurement = config["unit_of_measurement"]

        if "device_class" in config:
            self._attr_device_class = config["device_class"]

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.get(self._entity_key)

        if value is None:
            return None

        try:
            return float(value)
        except (ValueError, TypeError):
            _LOGGER.warning("Failed to convert value '%s' to float for number entity %s", value, self._entity_key)
            return None

    async def async_set_native_value(self, value: float) -> None:
        # ── keepCanSetWhenOn guard (mirrors keep.js keepCanSetWhenOn) ──
        if self._entity_key == "air_set_hour":
            device_mapping = getattr(self.coordinator, "device_mapping", {})
            has_keep_btn = device_mapping.get("_has_keep_btn", False)
            has_keep_setting = device_mapping.get("entities", {}).get("switch", {}).get("airswitch") is not None
            can_set_keep_time = has_keep_setting  # mirror canSetKeepTime = keepStartNow || keepSetTime
            data = self.coordinator.data or {}
            airswitch = data.get("airswitch", 0)
            try:
                airswitch = int(airswitch)
            except (ValueError, TypeError):
                airswitch = 0
            if has_keep_btn and can_set_keep_time and airswitch > 0:
                raise HomeAssistantError("保管已开启，请先关闭保管再修改时长")

        command = self._config.get("command")

        if command and isinstance(command, dict):
            merged_command = {}
            for key, val in command.items():
                if isinstance(val, str) and "{value}" in val:
                    merged_command[key] = val.replace("{value}", str(int(value)))
                else:
                    merged_command[key] = val
            await self.coordinator.async_set_control(merged_command)
        else:
            await self.coordinator.async_set_control(self._entity_key, int(value))

        # Post-set side effects (keep/dry auto-enable)
        side_effect = self._config.get("side_effect")
        if side_effect:
            await self._apply_side_effect(side_effect, int(value))

    async def _apply_side_effect(self, effect: dict, value: int) -> None:
        """Apply post-set side effects like auto-enable keep/dry switch."""
        effect_type = effect.get("type", "")
        data = self.coordinator.data or {}
        coordinator = self.coordinator

        if effect_type == "keep_auto_enable":
            keep_start_now = getattr(coordinator, "keep_start_now", False)
            if not keep_start_now:
                return
            airswitch = data.get("airswitch", 0)
            try:
                airswitch = int(airswitch)
            except (ValueError, TypeError):
                airswitch = 0
            if value > 0 and airswitch == 0:
                await coordinator.async_set_control({"airswitch": 1})
            elif value == 0 and airswitch == 1:
                await coordinator.async_set_control({"airswitch": 0})

        elif effect_type == "dry_auto_enable":
            dryswitch = data.get("dryswitch", 0)
            try:
                dryswitch = int(dryswitch)
            except (ValueError, TypeError):
                dryswitch = 0
            if value > 0 and dryswitch == 0:
                await coordinator.async_set_control({"dryswitch": 1})
            elif value == 0 and dryswitch == 1:
                await coordinator.async_set_control({"dryswitch": 0})
