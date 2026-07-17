import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
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
        rationale = device_mapping.get("rationale", ["off", "on"])
        switch_config = entities_config.get(Platform.SWITCH, {})

        if switch_config:
            for switch_id, config in switch_config.items():
                switch_rationale = config.get("rationale", rationale)
                condition = config.get("condition")
                command = config.get("command")
                include_current = config.get("include_current")
                local_only = config.get("local_only", False)
                entity = MideaSwitchEntity(
                    coordinator, device_id, device_type, sn, sn8, device_name,
                    switch_id, config, switch_rationale, condition, command, include_current, model, local_only
                )
                # ── Cloud name override (mirrors getMoreDryWashName) ──
                cloud_name = config.get("cloud_name")
                if cloud_name:
                    entity._attr_translation_key = None
                    entity._attr_name = cloud_name
                entities.append(entity)

    async_add_entities(entities)


class MideaSwitchEntity(MideaBaseEntity, SwitchEntity):
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: MideaCoordinator,
        device_id: int,
        device_type: str,
        sn: str,
        sn8: str,
        device_name: str,
        switch_id: str,
        switch_config: dict = None,
        rationale: list = None,
        condition: dict = None,
        command: dict = None,
        include_current: list = None,
        model: str = None,
        local_only: bool = False,
        # Backward-compatible alias — callers passing translation_key
        # still work; it is folded into switch_config internally.
        translation_key: str = None,
    ):
        # Merge legacy translation_key into switch_config for backward compat
        effective_config = dict(switch_config) if switch_config else {}
        if translation_key and "translation_key" not in effective_config:
            effective_config["translation_key"] = translation_key

        super().__init__(
            coordinator, device_id, device_type, sn, sn8, device_name, switch_id, model,
            platform_name="switch", config=effective_config, rationale=rationale, condition=condition
        )
        self._switch_id = switch_id
        self._command = command
        self._include_current = include_current or []
        self._local_only = local_only

    def _get_status_on_off(self, attribute_key: str) -> bool:
        data = self.coordinator.data or {}
        status_key = self._config.get("status_key", attribute_key)
        status = data.get(status_key)
        if status is None:
            return False
        try:
            return bool(self._rationale.index(status))
        except ValueError:
            if isinstance(status, int) or status in ['0', '1']:
                return int(status) != 0
            # Handle special states like 'delay_off' which should be treated as 'off'
            if status == 'delay_off':
                return False
            _LOGGER.warning(
                "The value of attribute %s ('%s') is not in rationale %s",
                attribute_key, status, self._rationale
            )
        return False

    async def _run_validator(self, validator_name: str) -> None:
        from .device_mapping.T0xE1 import dispatch_validator
        await dispatch_validator(validator_name, self.coordinator)

    @property
    def is_on(self) -> bool:
        return self._get_status_on_off(self._switch_id)

    async def _async_set_status_on_off(self, attribute_key: str, turn_on: bool) -> None:
        # Device-specific validators (raise HomeAssistantError if blocked)
        validators = self._config.get("validator", [])
        if isinstance(validators, str):
            validators = [validators]
        if turn_on:
            for v in validators:
                await self._run_validator(v)

        value = self._rationale[int(turn_on)]
        merged_command = {}

        # An explicit off_command marks a "full-command" switch (e.g. E1 power):
        # the command dict fully defines the payload and the on/off attribute
        # value is NOT appended. All other devices keep the original semantics:
        # command dict acts as extra params, plus attribute_key=value.
        off_command = self._config.get("off_command")
        full_command = off_command is not None
        if full_command:
            cmd = off_command if not turn_on else self._command
            if isinstance(cmd, dict):
                merged_command.update(cmd)
        else:
            if isinstance(self._command, dict):
                merged_command.update(self._command)
            merged_command[attribute_key] = value

        for attr in self._include_current:
            current_value = self._get_nested_value(attr)
            if current_value is not None:
                merged_command[attr] = current_value

        if self._local_only:
            self.coordinator.device.set_locals(merged_command)
            # ── autoThrow non-whitelist: send immediately to device ──
            if self._switch_id == "auto_throw":
                flags = getattr(self.coordinator, "diff_flags", {})
                if not flags.get("autoThrowWithMode"):
                    await self.coordinator.async_set_control(
                        {"auto_throw": int(turn_on)}
                    )
        elif full_command:
            # ── devOffKeep: close keep before power off ──
            if self._switch_id == "power" and not turn_on:
                flags = getattr(self.coordinator, "diff_flags", {})
                if flags.get("devOffKeep"):
                    data = self.coordinator.data or {}
                    try:
                        airswitch = int(data.get("airswitch", 0))
                    except (ValueError, TypeError):
                        airswitch = 0
                    if airswitch > 0:
                        _LOGGER.info("devOffKeep: closing keep before power off")
                        await self.coordinator.async_set_control({"airswitch": 0})
            # Full-command switch: send directly via set_attributes to avoid
            # centralized bundling of unrelated keys (e.g. power_off should
            # NOT bundle mode/water_level).
            await self.hass.async_add_executor_job(
                self.coordinator.device.set_attributes, merged_command
            )
        else:
            await self.coordinator.async_set_control(merged_command)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_status_on_off(self._switch_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_status_on_off(self._switch_id, False)
