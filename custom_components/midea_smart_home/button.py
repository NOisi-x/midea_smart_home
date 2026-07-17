import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MideaCoordinator
from .entity import MideaBaseEntity, iter_midea_device_configs
from .device_mapping.T0xE1 import (
    dispatch_validator,
    get_status_num,
    build_start_command,
    build_cancel_command,
    build_pause_command,
    build_order_command,
    calc_condition_result,
)

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
        button_config = entities_config.get(Platform.BUTTON, {})

        if button_config:
            for button_id, config in button_config.items():
                entities.append(
                    MideaButtonEntity(
                        coordinator, device_id, device_type, sn, sn8, device_name,
                        button_id, config, model
                    )
                )

    async_add_entities(entities)


class MideaButtonEntity(MideaBaseEntity, ButtonEntity):
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
            platform_name="button", config=config
        )

    async def async_press(self) -> None:
        # Device-specific validators (raise HomeAssistantError if blocked)
        validators = self._config.get("validator", [])
        if isinstance(validators, str):
            validators = [validators]
        for v in validators:
            await self._run_validator(v)

        command_builder = self._config.get("command_builder")
        if command_builder:
            command = await self._build_with_hook(command_builder)
        else:
            command = dict(self._config.get("command", {}))
            include_current = self._config.get("include_current", [])

            for attr in include_current:
                current_value = self._get_nested_value(attr)
                if current_value is not None:
                    command[attr] = current_value

        if command:
            await self.coordinator.async_set_control(command)
            # Clear local_only cache after start/order — values have been consumed
            if command_builder in ("start_wash", "order"):
                self.coordinator.device._local_data.clear()
                self.coordinator.device._notify_update()
        else:
            _LOGGER.warning("Button %s has no command configured", self._entity_key)

    async def _run_validator(self, validator_name: str) -> None:
        await dispatch_validator(validator_name, self.coordinator)

    async def _build_with_hook(self, builder_name: str) -> dict:
        """Call a device-specific command builder from T0xE1.py."""
        data = self.coordinator.data or {}
        status_num = get_status_num(
            data.get("work_status"),
            airswitch=data.get("airswitch", 0),
            air_left_hour=data.get("air_left_hour", 0),
            dryswitch=data.get("dryswitch", 0),
            keep_start_now=getattr(self.coordinator, "keep_start_now", False),
        )
        device_mapping = getattr(self.coordinator, "device_mapping", {})
        mode_conditions = device_mapping.get("_mode_conditions", {})
        bright_condition = device_mapping.get("_bright_condition", False)

        # Run condition matching for start/order to log predicted time/temp
        if builder_name in ("start_wash", "order") and mode_conditions:
            mode_name = data.get("mode", "")
            if mode_name and mode_name in mode_conditions:
                result = calc_condition_result(
                    mode_name, mode_conditions, data, bright_condition
                )
                if result:
                    _LOGGER.debug(
                        "Condition result for %s (mode=%s, bright_lack=%s): "
                        "time=%s, temp=%s, step=%s",
                        builder_name, mode_name,
                        data.get("bright_lack"), result.get("time"),
                        result.get("temp"), result.get("step")
                    )

        if builder_name == "start_wash":
            cmd = build_start_command(
                data, status_num,
                getattr(self.coordinator, "mode_features", {}),
                getattr(self.coordinator, "diff_flags", {}),
            )
            action = cmd.pop("_action", None)
            if action == "start_keep":
                cmd = {"airswitch": 2}
            elif action == "start_dry":
                cmd = {"dryswitch": 2}
            elif action == "cancel_keep_then_start":
                await self.coordinator.async_set_control({"airswitch": 0})
                cmd = {"airswitch": 2}
            return cmd

        if builder_name == "cancel":
            return build_cancel_command(data, status_num)

        if builder_name == "pause":
            return build_pause_command(data, status_num)

        if builder_name == "order":
            return build_order_command(
                data,
                getattr(self.coordinator, "mode_features", {}),
                getattr(self.coordinator, "diff_flags", {}),
            )

        return {}

