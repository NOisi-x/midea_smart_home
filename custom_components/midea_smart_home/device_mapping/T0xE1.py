"""T0xE1 dishwasher device mapping and E1-specific logic.

All E1-specific state computation, diff checking, and entity configuration
lives here to keep the codebase clean and device logic self-contained.
"""

import datetime as _dt
import logging
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorStateClass, SensorDeviceClass
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.const import Platform, UnitOfTemperature, UnitOfTime, PERCENTAGE
from homeassistant.exceptions import HomeAssistantError

# ---- E1 statusNum computation (matching mini-program const.js WORK_STATUS_DESCS) ----

_WORK_STATUS_NUM: dict[str, int] = {
    "power_off": 0,
    "power_on": 1,
    "cancel": 1,
    "standby": 1,
    "cancel_order": 1,
    "order": 2,
    "work": 3,
    "error": 4,
    "keep": 5,
    "dry": 6,
    "pipeInspect": 10,
}


def get_status_num(
    work_status: Optional[str],
    airswitch: Any = 0,
    air_left_hour: Any = 0,
    dryswitch: Any = 0,
    keep_start_now: bool = False,
) -> int:
    """Compute statusNum matching mini-program getStatusNumByDeviceStatus().

    Args:
        work_status: device's current work_status string
        airswitch: keep switch state
        air_left_hour: remaining keep hours
        dryswitch: dry switch state
        keep_start_now: from deviceSetting.setting.keepStartNow

    Returns:
        Numeric status code: -3=init, -2=keepInClose, -1=offline,
        0=power_off, 1=standby, 2=order, 3=work, 4=error, 5=keep, 6=dry
    """
    if work_status is None:
        return -3

    num = _WORK_STATUS_NUM.get(work_status, -3)

    if num == 1:
        try:
            if int(dryswitch) == 2:
                num = 6
        except (ValueError, TypeError):
            pass
        try:
            if int(airswitch) != 0 and int(air_left_hour) != 0 and keep_start_now:
                num = 5
        except (ValueError, TypeError):
            pass

    if num == 0:
        try:
            if int(air_left_hour) > 0 and int(airswitch) == 1:
                num = -2  # keepInClose
        except (ValueError, TypeError):
            pass

    if num == 10:
        num = 1

    return num


def calc_condition_result(
    mode_name: str,
    mode_conditions: dict,
    data: dict,
    bright_condition: bool,
) -> dict:
    """Compute condition-based time/temp/step matching mini-program calcConditionResult.

    Args:
        mode_name: Current mode key (e.g. "auto_wash")
        mode_conditions: Per-mode conditions from cloud config {mode: {list, default}}
        data: Full device status + local selections dict
        bright_condition: Whether brightCondition is enabled in cloud config

    Returns:
        Dict with keys from condition result: {time, temp, step}
        Empty dict if no conditions exist for this mode.
    """
    conditions = mode_conditions.get(mode_name)
    if not conditions:
        return {}

    # Collect current state values matching mini-program additionFunc.js line 188-189
    state: dict = {}

    # additional (int value like 0, 7, 9, 10, 12, 13, 14, 18, 21, 23)
    additional = data.get("additional")
    if additional is not None:
        state["additionalVal"] = int(additional)

    # region (wash_region: 0=all, 1=upper, 2=lower)
    region = data.get("wash_region")
    if region is not None:
        state["regionVal"] = int(region)

    # waterLevel
    water_level = data.get("water_level")
    if water_level is not None:
        state["waterLevelVal"] = int(water_level)

    # waterStrongLevel
    water_strong_level = data.get("water_strong_level")
    if water_strong_level is not None:
        state["waterStrongLevelVal"] = int(water_strong_level)

    # moreDryWash (bool switch)
    if "more_dry_wash" in data:
        state["moreDryWashVal"] = 1 if data["more_dry_wash"] else 0

    # moreDry (bool switch)
    if "more_dry" in data:
        state["moreDryVal"] = 1 if data["more_dry"] else 0

    # brightVal — only if brightCondition is enabled in cloud config
    if bright_condition:
        bright_lack = data.get("bright_lack")
        state["brightVal"] = 0 if bright_lack else 1

    # Match against conditions.list
    cond_list = conditions.get("list", [])
    if cond_list and state:
        for item in cond_list:
            cond_map = item.get("condition", {})
            if not cond_map:
                continue
            if all(state.get(k) == v for k, v in cond_map.items()):
                _LOGGER.info(
                    "Condition matched for mode=%s state=%s → result=%s",
                    mode_name, state, item.get("result")
                )
                return dict(item.get("result", {}))

    # Fallback to default
    return dict(conditions.get("default", {}))


def build_start_command(
    data: dict,
    status_num: int,
    mode_features: dict,
    diff_flags: dict,
    last_user_mode: str = "",
) -> dict:
    """Build start-wash command matching mini-program operator.js start().

    Path A (statusNum == 1, idle): uses mode from data + conditional features
    Path B (statusNum == 2, order): uses mode from data + all non-keep/dry features
    Path keep (mode == "keep"): returns start_keep command
    Path dry (mode == "dry"): returns start_dry command
    """
    if status_num == 1:
        mode = data.get("mode", "")

        if mode == "keep":
            if diff_flags.get("turnOffOnKeepStart"):
                return {"_action": "cancel_keep_then_start"}
            return {"_action": "start_keep"}

        if mode == "dry":
            return {"_action": "start_dry"}

        # Normal washing mode
        # Fall back to the last valid mode when the device reports an
        # internal state (neutral_gear etc.) that isn't a real wash mode.
        # This matches the mini-program: curMode keeps the last user pick.
        if not mode or not mode_features.get(mode):
            if last_user_mode and mode_features.get(last_user_mode):
                mode = last_user_mode
            else:
                valid = [m for m in (mode_features or {}) if m not in ("keep", "dry")]
                mode = valid[0] if valid else ""
        if not mode:
            return {}
        cmd: dict = {"work_status": "work", "mode": mode}
        supported = mode_features.get(mode, set())

        if "additional" in supported:
            val = data.get("additional")
            if val is not None:
                cmd["additional"] = val
        if "water_level" in supported:
            val = data.get("water_level")
            if val is not None and int(val) > 0:
                cmd["water_level"] = val
        if "water_strong_level" in supported:
            val = data.get("water_strong_level")
            if val is not None and int(val) > 0:
                cmd["water_strong_level"] = val
        if "work_time" in supported:
            val = data.get("work_time")
            if val is not None and int(val) > 0:
                cmd["work_time"] = val
        if "wash_region" in supported:
            val = data.get("wash_region")
            if val is not None and int(val) in (1, 2):
                cmd["wash_region"] = val
        if "door_auto_open" in supported:
            val = data.get("door_auto_open")
            if val is not None:
                cmd["door_auto_open"] = 1 if val else 2
        if "auto_throw" in supported and diff_flags.get("autoThrowWithMode"):
            val = data.get("auto_throw")
            if val is not None:
                cmd["auto_throw"] = 1 if val else 0
        if "more_dry" in supported:
            val = data.get("more_dry")
            if val is not None:
                cmd["more_dry"] = 1 if val else 0
        if "more_dry_wash" in supported:
            val = data.get("more_dry_wash")
            if val is not None:
                cmd["more_dry_wash"] = 1 if val else 0

        return cmd

    if status_num == 2:
        mode = data.get("mode", "")
        cmd = {"work_status": "work", "mode": mode}

        for feature in mode_features.get(mode, set()):
            if feature in ("keep", "dry"):
                continue
            val = data.get(feature)
            if feature == "more_dry_wash":
                cmd[feature] = bool(val)
            else:
                cmd[feature] = val if val is not None else 0

        return cmd

    return {}


def validate_additional(
    option_key: str,
    options_map: dict,
    data: dict,
    diff_flags: dict,
) -> None:
    """Validate additional select change. Raises HomeAssistantError if blocked.

    Blocks "door_open_dry" (additional=18) when keep is active,
    matching mini-program conflictDeal / canSetFunc.
    """
    if option_key not in options_map:
        return

    value_map = options_map[option_key]
    if not isinstance(value_map, dict):
        return

    if value_map.get("additional") != 18:
        return

    if not diff_flags.get("additionalSync"):
        return

    try:
        if int(data.get("airswitch", 0)) > 0:
            raise HomeAssistantError("保管功能已开启，请先关闭保管再选择开门速干")
    except (ValueError, TypeError):
        pass


def validate_keep_dry(
    action: str,
    data: dict,
    diff_flags: dict,
) -> None:
    """Validate keep/dry mutual exclusion. Raises HomeAssistantError if blocked.

    Matching mini-program keepWithoutDry diff whitelist.
    """
    if not diff_flags.get("keepWithoutDry"):
        return

    if action == "dry":
        try:
            if int(data.get("airswitch", 0)) > 0:
                raise HomeAssistantError("保管功能已开启，请先关闭保管")
        except (ValueError, TypeError):
            pass
    elif action == "keep":
        try:
            if int(data.get("dryswitch", 0)) > 0:
                raise HomeAssistantError("烘干功能已开启，请先关闭烘干")
        except (ValueError, TypeError):
            pass


def validate_keep_on(
    data: dict,
    keep_start_now: bool,
) -> None:
    """Validate keep (airswitch) ON. Raises HomeAssistantError if blocked.

    When keepStartNow is enabled, requires air_set_hour > 0.
    """
    if not keep_start_now:
        return
    try:
        if int(data.get("air_set_hour", 0)) == 0:
            raise HomeAssistantError("请先设置保管时长")
    except (ValueError, TypeError):
        pass


def validate_can_operate(
    action: str,
    data: dict,
    status_num: int,
    diff_flags: dict = None,
) -> None:
    """Pre-operation guard matching mini-program canClick / utils.js.

    Blocks operations when device is in a state that can't accept them.
    Raises HomeAssistantError with a Chinese message.
    """
    if diff_flags is None:
        diff_flags = {}

    # Door open — blocks everything except power, cancel, lock toggle.
    # doorOff devices have unreliable door sensors; skip door check.
    if not diff_flags.get("doorOff"):
        if action not in ("power", "cancel"):
            if not data.get("doorswitch"):
                raise HomeAssistantError("门开中，请先关门后操作")

    # Child lock — blocks everything
    if data.get("lock") == "on":
        raise HomeAssistantError("童锁中，请先解锁")

    # Error state — blocks everything
    if status_num == 4:
        raise HomeAssistantError("设备故障中，请检查")

    # Piping — blocks start/order
    if action in ("start", "order"):
        if data.get("work_status") == "pipeInspect":
            raise HomeAssistantError("设备排水中，暂不可控制")

    # Mode required — blocks start and order
    if action in ("start", "order"):
        if not data.get("mode", ""):
            raise HomeAssistantError("请先选择洗涤模式")

    # Water lack — blocks start (keep/dry/germ modes exempt)
    if action == "start":
        mode = data.get("mode", "")
        if data.get("water_lack") and mode not in ("germ", "keep", "dry"):
            raise HomeAssistantError("缺水中，请检查进水")


def build_cancel_command(data: dict, status_num: int) -> dict:
    """Build cancel command matching mini-program operator.js cancel().

    Dispatches based on statusNum:
      3 (work)  → {work_status: "cancel"}
      2 (order) → {work_status: "cancel_order"}
      5 (keep)  → {airswitch: 0}
      6 (dry)   → {dryswitch: 0}
    """
    if status_num == 3:
        return {"work_status": "cancel"}
    if status_num == 2:
        return {"work_status": "cancel_order"}
    if status_num == 5:
        return {"airswitch": 0}
    if status_num == 6:
        return {"dryswitch": 0}
    return {}


async def dispatch_validator(
    validator_name: str,
    coordinator,
    option: Any = None,
    options_map: dict = None,
) -> None:
    """Unified validator dispatcher for entity action guards.

    Centralises the duplicated _run_validator logic from button/switch/select
    entities into a single entry point so that E1-specific validation rules
    are maintained in one place.

    Args:
        validator_name: Validation rule identifier (e.g. ``"keep"``,
            ``"dry"``, ``"keep_on"``, ``"can_operate:start"``,
            ``"additional"``).
        coordinator: The ``MideaCoordinator`` instance carrying device state.
        option: Current selected option value (used by *additional* only).
        options_map: Full options mapping dict (used by *additional* only).

    Raises:
        HomeAssistantError: When the validation check blocks the action.
    """
    data = coordinator.data or {}
    diff_flags = getattr(coordinator, "diff_flags", {})
    keep_start_now = getattr(coordinator, "keep_start_now", False)

    # ── keep / dry mutual exclusion ──
    if validator_name in ("keep", "dry"):
        validate_keep_dry(validator_name, data, diff_flags)

    # ── keep-on requires duration set ──
    elif validator_name == "keep_on":
        validate_keep_on(data, keep_start_now)

    # ── generic pre-operation guard (power/door/lock/error/piping/water) ──
    elif validator_name.startswith("can_operate:"):
        action = validator_name.split(":", 1)[1]
        status_num = get_status_num(
            data.get("work_status"),
            airswitch=data.get("airswitch", 0),
            air_left_hour=data.get("air_left_hour", 0),
            dryswitch=data.get("dryswitch", 0),
            keep_start_now=keep_start_now,
        )
        validate_can_operate(action, data, status_num, diff_flags)

    # ── additional-function conflict (door_open_dry vs keep) ──
    elif validator_name == "additional":
        validate_additional(option or "", options_map or {}, data, diff_flags)


def build_pause_command(data: dict, status_num: int) -> dict:
    """Build pause/restart command matching mini-program operator.js pause().

    Only valid when statusNum is 2 (order) or 3 (work).
    Toggles based on current operator state:
      operator == "start" → {operator: "pause"}
      operator != "start" → {operator: "start"}  (resume)
    """
    if status_num not in (2, 3):
        return {}
    if data.get("operator") == "start":
        return {"operator": "pause"}
    return {"operator": "start"}


def build_order_command(
    data: dict,
    mode_features: dict,
    diff_flags: dict,
    last_user_mode: str = "",
) -> dict:
    """Build order (schedule) command matching mini-program deviceOrder.js order().

    Reads absolute target time from time.order_set_time and computes
    remaining delay in real-time at button-press moment using pure minute
    arithmetic (matching mini-program getOrderTime).
    """
    mode = data.get("mode", "")
    # Exclude keep/dry from order modes (matches mini-program getOrderNameList)
    order_modes = {k: v for k, v in mode_features.items() if k not in ("keep", "dry")}
    if mode not in order_modes and order_modes:
        # Fall back to last user-selected mode first;
        # use first available mode only as last resort.
        if last_user_mode and last_user_mode in order_modes:
            mode = last_user_mode
        else:
            mode = next(iter(order_modes))
    target_h = data.get("order_target_hour")
    target_m = data.get("order_target_min")

    delay_h = 0
    delay_m = 0
    if target_h is not None and target_m is not None:
        now = _dt.datetime.now()
        now_min = now.hour * 60 + now.minute
        target_min = int(target_h) * 60 + int(target_m)
        if target_min <= now_min:
            target_min += 1440  # tomorrow
        total_min = target_min - now_min
        delay_h = total_min // 60
        delay_m = total_min % 60

    cmd: dict = {
        "work_status": "order",
        "mode": mode,
    }
    if diff_flags.get("minuteOrder"):
        # These devices only understand a single total-minute delay field.
        cmd["order_set_hour"] = 0
        cmd["order_set_min"] = delay_h * 60 + delay_m
    else:
        cmd["order_set_hour"] = delay_h
        cmd["order_set_min"] = delay_m

    supported = mode_features.get(mode, set())

    if "additional" in supported:
        val = data.get("additional")
        if val is not None:
            cmd["additional"] = val
    if "door_auto_open" in supported:
        val = data.get("door_auto_open")
        if val is not None:
            cmd["door_auto_open"] = 1 if val else 2
    if "auto_throw" in supported and diff_flags.get("autoThrowWithMode"):
        val = data.get("auto_throw")
        if val is not None:
            cmd["auto_throw"] = 1 if val else 0
    if "more_dry_wash" in supported:
        val = data.get("more_dry_wash")
        if val is not None:
            cmd["more_dry_wash"] = 1 if val else 0
    if "water_level" in supported:
        val = data.get("water_level")
        if val is not None and int(val) > 0:
            cmd["water_level"] = val
    if "water_strong_level" in supported:
        val = data.get("water_strong_level")
        if val is not None and int(val) > 0:
            cmd["water_strong_level"] = val
    if "wash_region" in supported:
        val = data.get("wash_region")
        if val is not None and int(val) in (1, 2):
            cmd["wash_region"] = val
    if "work_time" in supported:
        val = data.get("work_time")
        if val is not None and val:
            cmd["work_time"] = val

    return cmd


# ---- E1 status text mapping (mirrors mini-program STATUS_NAME / pannel.wxml) ----

_STATUS_TEXT: dict[int, str] = {
    -3: "初始化",
    -2: "保管中",   # keepInClose — overrides "已关机"
    -1: "已离线",
     0: "已关机",
     1: "待机中",
     2: "预约中",
     3: "运行中",
     4: "故障",
     5: "保管中",
     6: "烘干中",
    10: "待机中",
}


def get_status_text(
    status_num: int,
    keep_name: str = "",
    dry_name: str = "",
) -> str:
    """Get human-readable status text using computed statusNum.

    Mirrors mini-program pannel.wxml status display logic:
      - statusNum==-2 or 5 → keepName + "中"
      - statusNum==6      → dryName + "中"
      - else              → _STATUS_TEXT lookup

    Args:
        status_num: Computed status number (from get_status_num)
        keep_name: Cloud config text.keep.name (e.g. "保管", "抑菌储存")
        dry_name: Cloud config text.dry.name (e.g. "烘干", "热风烘干")

    Returns:
        Human-readable status string
    """
    if status_num == -2 or status_num == 5:
        name = keep_name or "保管"
        return f"{name}中"
    if status_num == 6:
        name = dry_name or "烘干"
        return f"{name}中"
    return _STATUS_TEXT.get(status_num, _STATUS_TEXT[-3])


DEVICE_MAPPING = {
    "default": {
        "rationale": ["off", "on"],
        "centralized": [],
        "entities": {
            Platform.LOCK: {
                "lock": {
                    "translation_key": "child_lock"
                }
            },
            Platform.SWITCH: {
                "power": {
                    "device_class": SwitchDeviceClass.SWITCH,
                    "rationale": ["power_off", "power_on", "cancel", "cancel_order", "order", "work", "error", "keep", "dry", "standby", "pipeInspect"],
                    "command": {"work_status": "power_on"},
                    "off_command": {"work_status": "power_off"},
                    "status_key": "work_status",
                    "validator": ["can_operate:power"]
                },
                "dryswitch": {
                    "device_class": SwitchDeviceClass.SWITCH,
                    "rationale": [0, 1],
                    "translation_key": "wash_dry",
                    "validator": ["dry", "can_operate:dry"]
                },
                "airswitch": {
                    "device_class": SwitchDeviceClass.SWITCH,
                    "rationale": [0, 1],
                    "translation_key": "wash_keep",
                    "validator": ["keep", "keep_on", "can_operate:keep"]
                },
                "door_auto_open": {
                    "device_class": SwitchDeviceClass.SWITCH,
                    "rationale": [0, 1],
                    "mode_dependent": True,
                    "local_only": True
                },
                "auto_throw": {
                    "device_class": SwitchDeviceClass.SWITCH,
                    "rationale": [0, 1],
                    "mode_dependent": True,
                    "local_only": True
                },
                "more_dry": {
                    "device_class": SwitchDeviceClass.SWITCH,
                    "rationale": [0, 1],
                    "mode_dependent": True,
                    "local_only": True
                },
                "more_dry_wash": {
                    "device_class": SwitchDeviceClass.SWITCH,
                    "rationale": [0, 1],
                    "mode_dependent": True,
                    "local_only": True
                }
            },
            Platform.BINARY_SENSOR: {
                "doorswitch": {
                    "device_class": BinarySensorDeviceClass.OPENING,
                    "rationale": [1, 0],
                    "translation_key": "door_opened"
                },
                "water_lack": {
                    "device_class": BinarySensorDeviceClass.PROBLEM,
                    "translation_key": "lack_water"
                },
                "softwater_lack": {
                    "device_class": BinarySensorDeviceClass.PROBLEM
                },
                "bright_lack": {
                    "device_class": BinarySensorDeviceClass.PROBLEM
                },
                "air_status": {
                    "device_class": BinarySensorDeviceClass.RUNNING
                }
            },
            Platform.NUMBER: {
                "air_set_hour": {
                    "min": 0,
                    "max": 72,
                    "step": 1,
                    "unit_of_measurement": UnitOfTime.HOURS,
                    "side_effect": {"type": "keep_auto_enable"}
                },


                "dry_set_min": {
                    "min": 0,
                    "max": 180,
                    "step": 10,
                    "unit_of_measurement": UnitOfTime.MINUTES,
                    "side_effect": {"type": "dry_auto_enable"}
                },
            },
            Platform.TIME: {
                "order_set_time": {
                    "target_keys": {"hour": "order_target_hour", "minute": "order_target_min"},
                    "time_mode": "direct",
                    "local_only": True
                },
            },
            Platform.BUTTON: {
                "start_wash": {
                    "command_builder": "start_wash",
                    "validator": ["can_operate:start"]
                },
                "start_keep": {
                    "command": {"airswitch": 2},
                    "validator": ["keep", "keep_on", "can_operate:start"]
                },
                "start_dry": {
                    "command": {"dryswitch": 2},
                    "validator": ["dry", "can_operate:start"]
                },
                "cancel": {
                    "command_builder": "cancel",
                    "validator": ["can_operate:cancel"]
                },
                "pause": {
                    "command_builder": "pause",
                    "validator": ["can_operate:pause"]
                },
                "start_order": {
                    "command_builder": "order",
                    "validator": ["can_operate:order"]
                },
            },
            Platform.SELECT: {
                "wash_mode": {
                    "local_only": True,
                    "options": {
                        "standard_wash": {"mode": "standard_wash"},
                        "strong_wash": {"mode": "strong_wash"},
                        "eco_wash": {"mode": "eco_wash"},
                        "glass_wash": {"mode": "glass_wash"},
                        "fast_wash": {"mode": "fast_wash"},
                        "soak_wash": {"mode": "soak_wash"},
                        "self_clean": {"mode": "self_clean"},
                        "fruit_wash": {"mode": "fruit_wash"},
                        "germ": {"mode": "germ"},
                        "seafood_wash": {"mode": "seafood_wash"},
                        "hotpot_wash": {"mode": "hotpot_wash"},
                        "auto_wash": {"mode": "auto_wash"},
                        "high_temp_wash": {"mode": "high_temp_wash"},
                        "quietnight_wash": {"mode": "quietnight_wash"},
                        "keep": {"mode": "keep"},
                        "dry": {"mode": "dry"},
                        "diy": {"mode": "diy"},
                        "wash_dry": {"mode": "wash_dry"},
                        "cloud_wash": {"mode": "cloud_wash"},
                        "wahin_wash_dry": {"mode": "wahin_wash_dry"},
                    }
                },
                "softwater": {
                    "options": {
                        "1": {"softwater": 1},
                        "2": {"softwater": 2},
                        "3": {"softwater": 3},
                        "4": {"softwater": 4},
                        "5": {"softwater": 5},
                        "6": {"softwater": 6}
                    }
                },
                "rinse_aid": {
                    "options": {
                        "1": {"bright": 1},
                        "2": {"bright": 2},
                        "3": {"bright": 3},
                        "4": {"bright": 4},
                        "5": {"bright": 5}
                    }
                },
                "additional": {
                    "validator": "additional",
                    "options": {
                        "off": {"additional": 0},
                        "half_load": {"additional": 7},
                        "extra_rinse_1": {"additional": 9},
                        "extra_rinse_2": {"additional": 10},
                        "strong_wash_func": {"additional": 12},
                        "half_rinse_1": {"additional": 13},
                        "half_rinse_2": {"additional": 14},
                        "door_open_dry": {"additional": 18},
                        "steam": {"additional": 21},
                        "speed_up": {"additional": 23},
                    },
                    "translation_key": "additional_func",
                    "mode_dependent": True,
                    "local_only": True
                },
                "water_level": {
                    "options": {
                        "auto": {"water_level": 0},
                        "1": {"water_level": 1},
                        "2": {"water_level": 2},
                        "3": {"water_level": 3},
                    },
                    "mode_dependent": True,
                    "local_only": True
                },
                "water_strong_level": {
                    "options": {
                        "auto": {"water_strong_level": 0},
                        "1": {"water_strong_level": 1},
                        "2": {"water_strong_level": 2},
                    },
                    "translation_key": "water_strong",
                    "mode_dependent": True,
                    "local_only": True
                },
                "wash_region": {
                    "options": {
                        "all": {"wash_region": 0},
                        "upper": {"wash_region": 1},
                        "lower": {"wash_region": 2},
                    },
                    "mode_dependent": True,
                    "local_only": True
                },
                "work_time": {
                    "options": {
                        "0": {"work_time": 0},
                        "1": {"work_time": 1},
                        "2": {"work_time": 2},
                        "3": {"work_time": 3},
                        "4": {"work_time": 4},
                        "5": {"work_time": 5},
                        "6": {"work_time": 6},
                        "7": {"work_time": 7},
                        "8": {"work_time": 8},
                        "9": {"work_time": 9},
                        "10": {"work_time": 10},
                    },
                    "mode_dependent": True,
                    "local_only": True
                },
            },
            Platform.SENSOR: {
                "error_code": {
                    "device_class": SensorDeviceClass.ENUM
                },
                "temperature": {
                    "device_class": SensorDeviceClass.TEMPERATURE,
                    "unit_of_measurement": UnitOfTemperature.CELSIUS,
                    "state_class": SensorStateClass.MEASUREMENT
                },
                "left_time": {
                    "device_class": SensorDeviceClass.DURATION,
                    "unit_of_measurement": UnitOfTime.MINUTES,
                    "state_class": SensorStateClass.MEASUREMENT
                },
                "air_left_hour": {
                    "device_class": SensorDeviceClass.DURATION,
                    "unit_of_measurement": UnitOfTime.HOURS,
                    "state_class": SensorStateClass.MEASUREMENT
                },
                "device_status": {
                    "device_class": SensorDeviceClass.ENUM,
                    "computed_status": True
                },
                "wash_stage": {
                    "device_class": SensorDeviceClass.ENUM
                },
                "order_left_time": {
                    "device_class": SensorDeviceClass.DURATION,
                    "unit_of_measurement": UnitOfTime.MINUTES,
                    "state_class": SensorStateClass.MEASUREMENT,
                    "attribute": "order_left_total"
                }
            }
        },
        "_default_diff": {
            "diffType": {
                "withoutLockOnStart": ["000W5601", "760RX10P", "760Y0002", "760Y0004", "7600JV20", "7600061Y", "7600062Q", "7600646H", "7600643T", "76000613", "76000624", "76006440", "76006441", "76000628", "7600644L", "76006452", "76006456", "76006457", "76006458", "76006455", "76006450", "7600645A", "76006451", "7600645B", "76006459", "7600644Z", "7600645J", "7600645L", "760Y0009", "7600645Q", "7600059A", "7600645D", "7600645N", "7600645F", "760Y0010", "76006469", "7600646A", "76006460", "76006467", "7600646G", "7600646T", "7600646L", "7600646N", "7600646W", "7600646Q"],
                "keepOffOnPowerOff": ["000000X4", "000000Y1", "00000X4S", "0000V1E6"],
                "deviceUploadFaltDataOnPower": ["000000K2", "000000K1", "7600062E"],
                "doorOff": ["00003906"],
                "diffBrand": ["7600001E", "000000E8", "000000E6", "760B108B", "00W9601B", "760CDB31", "760DB312", "760DB412", "760FB212", "7600001A", "7600001D", "76000013", "76000014"],
                "devOffKeep": ["0000D25A", "0000D26A", "0000D26W", "000000E6", "000000E8", "000000H1", "000000H3", "00000H3P", "000000H4", "000000H5", "0HW2601C", "000000K1", "000000K2", "000000L2", "000000L3", "000000M6", "00000P30", "000000Q1", "0000RX30", "000000V1", "0000V1E6", "00W2601C", "00W3602H", "00W3602K", "00W3802H", "00W3909R", "000W8501", "00W9601B", "000000X3", "000000X4", "00000X4S", "000000X5", "000000X6", "000000Y1", "760BLV66", "760BLV88", "760JD103", "760P0P23", "760P40T1", "760SN101", "760TM101", "760V1E10", "760WE203", "7600JV13", "7600P30S", "7600P40P", "7600RX50", "7600645H", "7600V1E7", "7600V1E9", "76000D25", "76000P40", "760000E1", "7600061B", "7600061C", "7600061D", "7600061F", "7600061H", "7600061J", "7600061K", "7600062B", "7600062L", "7600643V", "76000002", "76000009", "76000572", "76000610", "76000614", "7600062C", "76000623", "000000E6", "000000E8", "00W9601B", "760B108B"],
                "withoutOrder": ["00W2601C"],
                "withoutPower": [],
                "minuteOrder": ["000000MT", "760P30PL", "7600V1E0", "76000CF4", "760000GZ", "760000V5", "7600061E", "7600062R"],
                "turnOffOnKeepStart": ["7600644N", "76000014", "760DB412", "760B108B", "00000D18", "0000D25A", "0000D26A", "0000D26W", "000000E6", "000000E8", "000000F1", "00000F2A", "00000F3B", "000000F4", "000000H1", "000000H3", "00000H3D", "00000H3P", "00000H3S", "000000H5", "0HW2601C", "000000J1", "000000K1", "000000K2", "000000L1", "000000L2", "000000L3", "000000M6", "000000M7", "00000M10", "000000MT", "00000P10", "00000P30", "000000Q1", "0000RX10", "0000RX30", "000000S2", "000000S3", "000000V1", "0000V1E6", "00W2601C", "00W3602H", "00W3602K", "00W3802H", "0W3905CN", "00W3909R", "000W5601", "00W7635R", "000W8501", "00W9601B", "000000X3", "000000X4", "00000X4S", "000000X5", "000000X6", "000000Y1", "760BLV66", "760BLV88", "760BVL68", "760GX6HP", "760GX600", "760JD103", "760JD201", "760M10CD", "760P0P23", "760P0P36", "760P40T1", "760RX20G", "760RX20S", "760SN101", "760TM101", "760V1E10", "760WE203", "00003906", "7600BF01", "7600BF03", "7600JV13", "7600M10P", "7600P23Q", "7600P30S", "7600P40P", "7600RX20", "7600RX50", "7600645H", "7600V1E0", "7600V1E7", "7600V1E9", "11111M10", "76000CF4", "76000D25", "76000JV8", "76000MG1", "76000NS8", "76000P40", "760000C1", "760000E1", "760000LX", "760000M1", "760000M8", "760000Q7", "760000V2", "760000V3", "7600061B", "7600061C", "7600061D", "7600061F", "7600061H", "7600061J", "7600061K", "7600061Z", "7600062B", "7600062E", "7600062G", "7600062L", "7600643V", "7603602D", "7603905P", "76000002", "76000007", "76000009", "76000012", "76000018", "76000572", "76000596", "76000601", "7600646B", "76000610", "76000614", "76000619", "76000623"],
                "autoWashTimeType": ["000W8501", "00W9601B", "760000V2", "00W9601B"],
                "additionalSync": ["76000015", "76000017", "76000628", "7600H0P9"],
                "autoThrowWithMode": ["7600062S"],
                "deviceOrder": ["000000X3", "00003906", "00W3906B", "00W9601B", "000W8501"],
                "cloudOrder": ["760Y0010", "76006450", "0000RX30", "000000K1"],
                "keepWithoutDry": ["00000P30", "000000L3", "760WE203", "760P0P23", "7600JV13", "7600P30S", "7600RX50", "760P30PL", "00003906", "76000610", "7600061F", "7600644N", "7600061C", "7600061D"],
                "tdsLinkage": ["760Y0010"],
                "additionalWithSteps": ["76000572", "76000594", "76000595", "76000608", "76000611", "76000614", "76000615", "76000616", "76000618", "7600061A", "7600061K", "7600061L", "7600061M", "76000621", "76000623", "7600062B", "7600062C", "7600062D", "7600062H", "7600062L", "7600062Q", "7600646H", "7600062T", "7600062U", "7600063Z", "76000642", "76000P40", "76000X5B", "7600643V", "76006440", "76006441", "76006442", "76006444", "76006446", "76006447", "76006449", "7600644A", "7600644B", "7600644D", "7600644H", "7600644J", "7600644L", "7600644Q", "7600645D", "76006462", "76006466", "760GX800", "760JV800", "760Y0001", "760Y0003", "760Y0005"],
                "versionMixed": ["76000594"],
                "ABandMoreVersion": ["000000H1"]
            },
            "otherType": {
                "IFTTT_default_region_3": ["76000593", "76000594", "76000608", "76000611", "76000612", "76000613", "76000615", "76000616", "7600061M", "7600061P", "7600061T", "7600061Y", "76000621", "76000624", "76000629", "7600062H", "7600062J", "7600062K", "7600062S", "7600062T", "7600062U", "76000642", "76000X5B", "7600643T", "7600643U", "7600643Z", "76006442", "76006444", "76006449", "7600644A", "7600644B", "7600644C", "7600644D", "7600644F", "7600644J", "7600GX1K", "760GX800", "760JV800", "760Y0001", "760Y0003"]
            },
            "Filter": {
                "G1Filter": ["000W5601", "760GX600", "760GX6HP", "7600GX1K", "760GX800", "76000X5B", "76000608", "76000611", "7600061M", "76000618", "7600061A", "76000615", "7600061P", "760JV800", "7600062U", "76000617", "7600062K", "7600643Z", "7600062S", "7600062H", "7600062D", "7600643T", "7600062J", "76000613", "76000621", "76000616", "7600061L", "76000619", "7600061Y", "76000624", "76000629", "7600001E", "7600001F", "7600001A", "7600001D", "76000103", "76000102", "76000013", "760Y0001"]
            },
            "specialLua": {
                "000000X3": 1, "00003906": 1, "00W3906B": 1,
                "00W9601B": 1, "000W8501": 1, "0000RX30": 1, "000000K1": 1
            },
            "additionalAbnormal": ["76000594_2"],
            "singleKeepDeviceList": ["000000H3", "000000H5", "000000K1", "000000K2", "000000MT", "000000X3", "000000X4", "000000X5", "000000X6", "000000Y1", "00000X4S", "0000V1E6", "00W2601C", "76000002", "760000E1", "7600061H", "7600061J", "7600062E", "76000CF4", "7600V1E0", "7600V1E7", "760JD103", "76000596"]
        }
    }
}
