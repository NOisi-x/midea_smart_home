from importlib import import_module
from pathlib import Path
import json
import logging
import re

_LOGGER = logging.getLogger(__name__)

DEVICE_MAPPINGS = {}

def format_model(model: str) -> str:
    """Format model string for device mapping lookup.

    Args:
        model: Original model string

    Returns:
        Formatted model string: lowercase, non-alphanumeric chars replaced with underscore
    """
    if not model:
        return ""
    formatted = model.lower()
    formatted = re.sub(r'[^a-z0-9]', '_', formatted)
    formatted = re.sub(r'_+', '_', formatted)
    formatted = formatted.strip('_')
    return formatted

def load_device_mappings():
    """Load all device mapping files."""
    mapping_dir = Path(__file__).parent

    if not mapping_dir.exists():
        _LOGGER.error("Device mapping directory does not exist: %s", mapping_dir)
        return

    try:
        files = list(mapping_dir.iterdir())

        for file in files:
            if file.name.endswith('.py') and file.name != '__init__.py':
                try:
                    module_name = f"{__package__}.{file.stem}"
                    module = import_module(module_name)

                    if hasattr(module, 'DEVICE_MAPPING'):
                        device_type = int(file.stem.replace('T0x', ''), 16)
                        DEVICE_MAPPINGS[device_type] = module.DEVICE_MAPPING
                        _LOGGER.info(
                            "Loaded device mapping: %s -> device type: 0x%X",
                            file.stem, device_type
                        )
                except (ImportError, ValueError, AttributeError) as e:
                    _LOGGER.error("Failed to load device mapping file %s: %s", file.name, e)
    except OSError as e:
        _LOGGER.error("Failed to iterate device mapping directory: %s", e)

load_device_mappings()

def get_device_mapping(device_type: int, model: str = "", sn8: str = "", category: str = "") -> dict:
    """Get device mapping for specified device type.

    Args:
        device_type: Device type (e.g., 0xFB)
        model: Device model string, used to get specific device mapping (highest priority)
        sn8: Device model code (8 digits), used to get specific device mapping
        category: Product category from cloud, used for default mapping fallback

    Returns:
        Device mapping dict, returns model mapping if available,
        otherwise sn8 mapping if available,
        otherwise default+category if exists, finally fallback to default
    """
    mapping = DEVICE_MAPPINGS.get(device_type, {})

    if not mapping:
        return {}

    result = None

    formatted_model = format_model(model)
    if formatted_model:
        if formatted_model in mapping:
            result = mapping[formatted_model]
        else:
            for key in mapping:
                if isinstance(key, tuple) and formatted_model in key:
                    result = mapping[key]
                    break

    if result is None and sn8:
        if sn8 in mapping:
            result = mapping[sn8]
        else:
            for key in mapping:
                if isinstance(key, tuple) and sn8 in key:
                    result = mapping[key]
                    break

    if result is None and category:
        category_key = f"default_{category.replace('-', '_')}"
        if category_key in mapping:
            result = mapping[category_key]

    if result is None and "default" in mapping:
        result = mapping["default"]

    if result is None:
        result = mapping

    return result


def load_device_config(hass_config_dir: str, device_type: int, sn8: str) -> dict | None:
    """Load per-device configuration from local storage.

    Args:
        hass_config_dir: Home Assistant config directory
        device_type: Device type (e.g., 0xE1)
        sn8: Device model code (8 digits)

    Returns:
        Config dict on success, None if file not found
    """
    if not sn8:
        return None

    from ..const import DEVICE_CONFIG_PATH

    type_hex = f"T0x{device_type:X}"
    config_path = (
        Path(hass_config_dir) / DEVICE_CONFIG_PATH / f"{type_hex}_{sn8}.json"
    )

    if not config_path.exists():
        return None

    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _LOGGER.warning("Failed to load device config %s: %s", config_path, e)
        return None


def load_diff_config(hass_config_dir: str, device_type: int) -> dict | None:
    """Load per-device-type diff config from local storage.

    Falls back to the embedded default diff from the device mapping file.

    Args:
        hass_config_dir: Home Assistant config directory
        device_type: Device type (e.g., 0xE1)

    Returns:
        Diff config dict (with "diffType" key) on success, None if unavailable
    """
    from ..const import DEVICE_CONFIG_PATH

    type_hex = f"T0x{device_type:X}"
    config_path = (
        Path(hass_config_dir) / DEVICE_CONFIG_PATH / f"{type_hex}_DIFF.json"
    )

    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            _LOGGER.warning("Failed to load diff config %s: %s", config_path, e)

    # Fallback: embedded default diff from the device mapping module
    mapping = DEVICE_MAPPINGS.get(device_type, {})
    default_mapping = mapping.get("default", mapping)
    diff_data = default_mapping.get("_default_diff")
    if diff_data:
        _LOGGER.info(
            "Using embedded default diff for device type 0x%X", device_type
        )
        return diff_data

    return None


# Per-device-type feature maps for cloud config per-mode analysis.
# Each key is a device_type (e.g., 0xE1). The value maps cloud config
# feature keys to (platform, entity_id) tuples used in entity filtering.
# To add support for a new device type:
#   1. Add its device_type to const.DEVICE_CONFIG_SUPPORTED_TYPES
#   2. Add its feature map entry here
DEVICE_CONFIG_FEATURE_MAPS: dict[int, dict[str, tuple[str, str]]] = {
    0xE1: {
        "additional":       ("select", "additional"),
        "waterLevel":       ("select", "water_level"),
        "waterStrongLevel": ("select", "water_strong_level"),
        "region":           ("select", "wash_region"),
        "germ":             ("select", "work_time"),
        "autoOpen":         ("switch", "door_auto_open"),
        "autoThrow":        ("switch", "auto_throw"),
        "moreDry":          ("switch", "more_dry"),
        "moreDryWash":      ("switch", "more_dry_wash"),
    },
}


def apply_device_config(
    mapping: dict,
    device_config: dict,
    device_type: int = 0,
    device_version: int = 0,
) -> dict:
    """Merge cloud-downloaded device config into static mapping.

    Only filters/reduces entities; never adds new ones beyond
    what the static mapping already defines.

    Args:
        mapping: Static device mapping from T0xXX.py
        device_config: Cloud config JSON (with version_N keys)
        device_type: Device type (e.g., 0xE1)
        device_version: Current device firmware version

    Returns:
        Merged mapping dict
    """
    if not device_config or not mapping:
        return mapping

    # Select version-specific config block
    version_key = f"version_{device_version}"
    if version_key not in device_config:
        _LOGGER.debug(
            "No version config for %s, trying version_0", version_key
        )
        version_key = "version_0"
        if version_key not in device_config:
            return mapping

    config = device_config[version_key]
    if not isinstance(config, dict):
        return mapping

    mode_list = config.get("modeList", {})
    settings = config.get("setting", {})
    more = config.get("more", {})

    result = dict(mapping)
    entities = dict(result.get("entities", {}))

    from homeassistant.const import Platform
    select_config = dict(entities.get(Platform.SELECT, {}))
    switch_config = dict(entities.get(Platform.SWITCH, {}))
    lock_config = dict(entities.get(Platform.LOCK, {}))
    sensor_config = dict(entities.get(Platform.SENSOR, {}))
    number_config = dict(entities.get(Platform.NUMBER, {}))
    button_config = dict(entities.get(Platform.BUTTON, {}))

    # ── 1. Filter wash_mode options ──
    wash_mode_cfg = dict(select_config.get("wash_mode", {}))
    if wash_mode_cfg and mode_list:
        options = dict(wash_mode_cfg.get("options", {}))
        valid_modes = set(mode_list.keys()) & set(options.keys())
        if valid_modes:
            filtered = {k: options[k] for k in valid_modes}
            wash_mode_cfg["options"] = filtered
            _LOGGER.info(
                "Filtered wash_mode: %d modes from config (%d in static)",
                len(filtered), len(options)
            )
        select_config["wash_mode"] = wash_mode_cfg

    # ── 2. Per-mode feature analysis ──
    # Build dynamic SELECT options from cloud config per-mode feature lists.
    # Maps HA entity_id to cloud config feature_key and command field.
    _dynamic_selects = {
        "additional":       ("additional",       "additional"),
        "water_level":      ("waterLevel",       "water_level"),
        "water_strong_level": ("waterStrongLevel", "water_strong_level"),
        "wash_region":      ("region",           "wash_region"),
    }
    for entity_id, (feature_key, cmd_field) in _dynamic_selects.items():
        if entity_id not in select_config:
            continue
        all_items: dict[int, str] = {}
        for mode_data in mode_list.values():
            if not isinstance(mode_data, dict):
                continue
            feature_cfg = (mode_data.get("more", {}) or {}).get(feature_key)
            if not isinstance(feature_cfg, dict):
                continue
            flist = feature_cfg.get("list")
            if not flist:
                continue
            for item in flist:
                v = item.get("value")
                if v is not None:
                    all_items[v] = item.get("name", str(v))
        if all_items:
            opts = {}
            for v, name in sorted(all_items.items()):
                opts[name] = {cmd_field: v}
            select_config[entity_id]["options"] = opts
            _LOGGER.info("Built %s options from cloud config", entity_id)

    # ── 2b. Build dynamic work_time options from germ min/max ──
    # Unlike list-based features, germ uses a numeric range (matching
    # mini-program additionFunc.js: min..max → "X分钟")
    if "work_time" in select_config:
        work_time_min = 0
        work_time_max = 10
        for mode_data in mode_list.values():
            if not isinstance(mode_data, dict):
                continue
            germ_cfg = (mode_data.get("more", {}) or {}).get("germ")
            if isinstance(germ_cfg, dict):
                wkt_min = germ_cfg.get("min")
                wkt_max = germ_cfg.get("max")
                if wkt_min is not None and wkt_max is not None:
                    work_time_min = min(work_time_min, int(wkt_min))
                    work_time_max = max(work_time_max, int(wkt_max))
        wt_opts = {}
        for m in range(work_time_min, work_time_max + 1):
            wt_opts[str(m)] = {"work_time": m}
        select_config["work_time"]["options"] = wt_opts
        _LOGGER.info(
            "Built work_time options: %d..%d (%d values)",
            work_time_min, work_time_max, len(wt_opts)
        )

    # Collect the union of all sub-features supported by ANY mode.
    feature_map = DEVICE_CONFIG_FEATURE_MAPS.get(device_type, {})

    # Build union of features across ALL modes.
    # For SELECT features (additional, region, etc.), also verify
    # that the feature has a non-empty `list` — mirroring the
    # mini-program's additionFunc.js check: c[t].list.length > 0
    active_features: set[str] = set()
    _list_features = {"additional", "region", "waterLevel", "waterStrongLevel"}

    for mode_data in mode_list.values():
        if not isinstance(mode_data, dict):
            continue
        mode_more = mode_data.get("more", {})
        if not isinstance(mode_more, dict):
            continue
        for feature_key in mode_more:
            feature_cfg = mode_more[feature_key]
            if feature_key in _list_features:
                if isinstance(feature_cfg, dict):
                    flist = feature_cfg.get("list")
                    if not flist or len(flist) == 0:
                        continue  # empty list → feature not truly available
            active_features.add(feature_key)

    # Also consider device-level "more" for SWITCH entities
    # present at device level (not per-mode)
    if isinstance(more, dict):
        active_features.update(more.keys())

    _LOGGER.info(
        "Active features across all modes: %s",
        sorted(active_features)
    )

    # Remove entities for features NOT present in any mode
    removed_entities = []
    for feature_key, (platform, entity_id) in feature_map.items():
        if feature_key not in active_features:
            if platform == "select":
                select_config.pop(entity_id, None)
            elif platform == "switch":
                switch_config.pop(entity_id, None)
            removed_entities.append(entity_id)

    if removed_entities:
        _LOGGER.info(
            "Removed unsupported sub-feature entities: %s",
            removed_entities
        )

    # ── 3. Device-level feature flags ──
    if more:
        # lock
        if not more.get("lock"):
            lock_config.pop("lock", None)
        # bright (rinse aid)
        if not more.get("bright"):
            select_config.pop("rinse_aid", None)
        # salt
        salt = more.get("salt", {})
        if isinstance(salt, dict) and not salt.get("enable"):
            select_config.pop("softwater", None)
        # strainer affects sensor
        if not more.get("strainer"):
            # Keep sensors, just log
            pass

    # ── 4. Setting-based controls ──
    if settings:
        # hasKeepSetting = keepStartNow || keepSetTime (mirrors keep.js)
        if not (settings.get("keepStartNow") or settings.get("keepSetTime")):
            switch_config.pop("airswitch", None)
            number_config.pop("air_set_hour", None)
            removed_entities.extend(["airswitch", "air_set_hour"])
        elif not settings.get("keepSetTime"):
            number_config.pop("air_set_hour", None)

        # hasDrySetting = dryStartNow || drySetTime (mirrors dry.js)
        if not (settings.get("dryStartNow") or settings.get("drySetTime")):
            switch_config.pop("dryswitch", None)
            number_config.pop("dry_set_min", None)
            removed_entities.extend(["dryswitch", "dry_set_min"])
        elif not settings.get("drySetTime"):
            number_config.pop("dry_set_min", None)

    # ── 4a. hasKeepBtn / hasDryBtn detection (mirrors keep.js / dry.js) ──
    # hasKeepBtn: any mode has more.keep but modeList has no standalone "keep" mode
    has_keep_btn = False
    keep_mode_exists = "keep" in mode_list
    if not keep_mode_exists:
        for mode_data in mode_list.values():
            if not isinstance(mode_data, dict):
                continue
            mode_more = mode_data.get("more", {})
            if isinstance(mode_more, dict) and "keep" in mode_more:
                has_keep_btn = True
                break
    result["_has_keep_btn"] = has_keep_btn
    if has_keep_btn:
        _LOGGER.info("hasKeepBtn=true — keep is button form, hiding start_keep")
        button_config.pop("start_keep", None)
        removed_entities.append("start_keep")

    # hasDryBtn: !drySetTime (no dry time setting → button form)
    has_dry_btn = not bool(settings.get("drySetTime", False))
    result["_has_dry_btn"] = has_dry_btn
    if has_dry_btn:
        _LOGGER.info("hasDryBtn=true — dry is button form, hiding start_dry")
        button_config.pop("start_dry", None)
    else:
        _LOGGER.info("hasDryBtn=false — standalone dry mode, keeping start_dry")

    # keepTimeType for day+hour formatting (keep.js getKeepRightText)
    keep_time_type = settings.get("keepTimeType", 0)
    result["_keep_time_type"] = keep_time_type
    if keep_time_type == 2:
        _LOGGER.info("keepTimeType=2 — day+hour format enabled")

    # Cloud text names for keep/dry status display (mirrors pannel.wxml)
    config_text = config.get("text", {})
    if isinstance(config_text, dict):
        keep_text = config_text.get("keep", {})
        if isinstance(keep_text, dict) and keep_text.get("name"):
            result["_keep_text_name"] = keep_text["name"]
        dry_text = config_text.get("dry", {})
        if isinstance(dry_text, dict) and dry_text.get("name"):
            result["_dry_text_name"] = dry_text["name"]
        # moreDryWash dynamic name (mirrors getMoreDryWashName)
        mdw_text = config_text.get("moreDryWash", {})
        if isinstance(mdw_text, dict) and mdw_text.get("name") and "more_dry_wash" in switch_config:
            switch_config["more_dry_wash"]["cloud_name"] = mdw_text["name"]
            _LOGGER.info("moreDryWash cloud name: %s", mdw_text["name"])

    # ── 5. Build per-mode feature availability map ──
    # {mode_name: {entity_key, ...}}
    mode_features: dict[str, set[str]] = {}
    for mode_name, mode_data in mode_list.items():
        if not isinstance(mode_data, dict):
            continue
        mode_more = mode_data.get("more", {})
        if not isinstance(mode_more, dict):
            continue
        features: set[str] = set()
        for feature_key, (platform, entity_id) in feature_map.items():
            if feature_key in mode_more:
                features.add(entity_id)
        if features:
            mode_features[mode_name] = features

    result["_mode_features"] = mode_features
    _LOGGER.info("Per-mode features: %s", mode_features)

    # ── 5a. Extract per-mode conditions (bright_lack condition matching) ──
    # Mirror mini-program additionFunc.js calcConditionResult
    mode_conditions: dict[str, dict] = {}
    for mode_name, mode_data in mode_list.items():
        if not isinstance(mode_data, dict):
            continue
        conditions = mode_data.get("conditions")
        if isinstance(conditions, dict) and conditions.get("list"):
            mode_conditions[mode_name] = conditions
            _LOGGER.debug(
                "Mode %s has conditions: %d entries",
                mode_name, len(conditions.get("list", []))
            )
    if mode_conditions:
        result["_mode_conditions"] = mode_conditions
        _LOGGER.info(
            "Per-mode conditions: %s",
            list(mode_conditions.keys())
        )

    # ── 5b. Extract brightCondition from settings ──
    # When true, bright_lack from device affects condition matching
    bright_condition = bool(settings.get("brightCondition", False))
    result["_bright_condition"] = bright_condition
    if bright_condition:
        _LOGGER.info("brightCondition enabled — bright_lack affects mode conditions")

    # ── 6. Reassemble ──
    entities[Platform.SELECT] = select_config
    entities[Platform.SWITCH] = switch_config
    entities[Platform.LOCK] = lock_config
    entities[Platform.SENSOR] = sensor_config
    entities[Platform.NUMBER] = number_config
    entities[Platform.BUTTON] = button_config
    result["entities"] = entities
    return result
