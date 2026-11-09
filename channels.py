"""Initial WING setup shared by the CLI and Qt editor.

JSON keys are strip positions (1-24). Each entry has name, color, icon, and
mode ("mono" by default, or "stereo"). Inputs are allocated from LCL 1 in
strip order; stereo starts on an odd input and consumes the following even
input too. Alignment gaps cannot be used by later strips. Only named strips
consume inputs. Save and Apply reject setups exceeding 24 local input slots.

Applying replaces the main input routing of all 24 strips: unnamed strips
are disconnected, and unused LCL 1-24 inputs have their names cleared.
Clearing an unused input's name does not change its 48V or other settings.
It does not move processing, faders, or sends. Reading
imports identities and modes, not physical patch numbers; the next Apply
reallocates the local inputs. Other banks and mid/side sources are rejected.
Mono strips are moved and verified before stereo pairs are configured.
Names are limited to 12 characters. The boolean phantom field defaults to
false and controls 48V on every allocated input (both inputs for stereo).
"""

import argparse
import asyncio
import json
import os
from dataclasses import dataclass

from wing import WingConsole
from wing_tables import (
    WING_COLOR_NAMES, WING_COLORS, WING_ICON_NAMES, WING_ICONS,
)

DEFAULT_CONFIG_FILE = "channels.json"
NUM_CHANNELS = 24
NUM_INPUTS = 24
MAX_NAME_LENGTH = 12


@dataclass(frozen=True)
class ChannelConfig:
    name: str
    color: str = "Gray Blue"
    icon: str = "Empty"
    mode: str = "mono"
    phantom: bool = False


def allocate_inputs(config: dict[int, ChannelConfig]) -> dict[int, tuple[int, ...]]:
    """Allocate local inputs in strip order, aligning stereo to odd/even pairs.

    The returned plan may exceed NUM_INPUTS so the editor can preview overflow.
    """
    sources = {}
    next_input = 1
    for ch, entry in sorted(config.items()):
        if type(ch) is not int or not 1 <= ch <= NUM_CHANNELS:
            raise ValueError(f"channel {ch}: expected 1-{NUM_CHANNELS}")
        if not isinstance(entry.name, str) or not entry.name.strip():
            raise ValueError(f"channel {ch}: name must not be empty")
        if len(entry.name) > MAX_NAME_LENGTH:
            raise ValueError(
                f"channel {ch}: name must be at most {MAX_NAME_LENGTH} characters")
        if type(entry.phantom) is not bool:
            raise ValueError(f"channel {ch}: phantom must be true or false")
        if entry.color not in WING_COLORS:
            raise ValueError(f"channel {ch}: unknown color {entry.color!r}")
        if entry.icon not in WING_ICONS:
            raise ValueError(f"channel {ch}: unknown icon {entry.icon!r}")
        if entry.mode not in ("mono", "stereo"):
            raise ValueError(f"channel {ch}: unknown source mode {entry.mode!r}")
        width = 2 if entry.mode == "stereo" else 1
        if width == 2 and next_input % 2 == 0:
            next_input += 1
        sources[ch] = tuple(range(next_input, next_input + width))
        next_input += width
    return sources


def validate_capacity(config: dict[int, ChannelConfig]) -> dict[int, tuple[int, ...]]:
    sources = allocate_inputs(config)
    used = max((inputs[-1] for inputs in sources.values()), default=0)
    if used > NUM_INPUTS:
        raise ValueError(
            f"Setup needs {used} local input slots; only {NUM_INPUTS} are available "
            "(including stereo alignment gaps).")
    return sources

# Channel mapping: strip number -> identity and source mode.
# See wing_tables.py for all valid color and icon names
# (and wing_icon_reference.png for what each icon looks like).
DEFAULT_CHANNEL_CONFIG = {
    1: ChannelConfig("Kick In", "Red", "Kick In"),
    2: ChannelConfig("Snare Top", "Red", "Snare Top"),
    3: ChannelConfig("Bass DI", "Yellow", "Double Bass"),
    4: ChannelConfig("Gtr Lead", "Green", "Electric Guitar 1"),
    5: ChannelConfig("Lead Vox", "Magenta", "Male Vocal"),
}


def load_config(path: str = DEFAULT_CONFIG_FILE) -> dict[int, ChannelConfig]:
    """Load a channel config from a JSON file.

    Each strip has name/color/icon and mode ("mono" or "stereo").
    Legacy files default to mono and 48V off. Inputs are allocated in strip order.
    """
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Config must be an object keyed by channel number")
    config = {}
    for ch, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError(f"channel {ch}: expected an object")
        channel = int(ch)
        if channel in config:
            raise ValueError(f"duplicate channel {channel}")
        config[channel] = ChannelConfig(
            entry.get("name", ""), entry.get("color", "Gray Blue"),
            entry.get("icon", "Empty"), entry.get("mode", "mono"),
            entry.get("phantom", False))
    validate_capacity(config)
    return config


def save_config(config: dict[int, ChannelConfig],
                path: str = DEFAULT_CONFIG_FILE) -> None:
    """Save strip identities and modes; source inputs are derived from order."""
    validate_capacity(config)
    data = {str(ch): {"name": entry.name, "color": entry.color,
                     "icon": entry.icon, "mode": entry.mode,
                     "phantom": entry.phantom}
            for ch, entry in sorted(config.items())}
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def osc_value(value):
    """WING replies can be a single value or (display, normalized, raw)."""
    return value[0] if isinstance(value, (tuple, list)) else value


async def apply_channel_aesthetics(
        wing: WingConsole, config: dict[int, ChannelConfig]) -> list:
    """Apply an initial local-input setup, including routing and source mode."""
    sources = validate_capacity(config)
    results = []
    for ch in range(1, NUM_CHANNELS + 1):
        if ch not in config:
            address = f"/ch/{ch}/in/conn/grp"
            wing.send(address, "OFF")
            shown = str(osc_value(await wing.get(address)))
            if shown != "OFF":
                results.append((ch, f"MISMATCH (expected OFF, got {shown!r})"))
    # Free the stereo destinations by moving mono strips first, without
    # changing the input allocation derived from the displayed strip order.
    apply_order = sorted(config, key=lambda ch: (config[ch].mode == "stereo", ch))
    has_stereo = any(entry.mode == "stereo" for entry in config.values())
    for ch in apply_order:
        entry = config[ch]
        inputs = sources[ch]
        src = f"/io/in/LCL/{inputs[0]}"
        mode = "ST" if entry.mode == "stereo" else "M"
        for idx in inputs:
            wing.send(f"/io/in/LCL/{idx}/mode", "M")
        wing.send(f"{src}/mode", mode)
        wing.send(f"{src}/name", entry.name)
        wing.send(f"{src}/col", WING_COLORS[entry.color])
        wing.send(f"{src}/icon", WING_ICONS[entry.icon])
        wing.send(f"/ch/{ch}/in/conn/grp", "LCL")
        wing.send(f"/ch/{ch}/in/conn/in", inputs[0])
        for idx in inputs:
            wing.send(f"/io/in/LCL/{idx}/vph", int(entry.phantom))
        expected = {
            f"/ch/{ch}/in/conn/grp": "LCL",
            f"/ch/{ch}/in/conn/in": str(inputs[0]),
            f"{src}/mode": mode,
            f"/ch/{ch}/$name": entry.name,
            f"{src}/col": str(WING_COLORS[entry.color]),
            f"{src}/icon": str(WING_ICONS[entry.icon]),
        }
        for idx in inputs:
            expected[f"/io/in/LCL/{idx}/vph"] = str(int(entry.phantom))
        mismatches = []
        for address, value in expected.items():
            shown = str(osc_value(await wing.get(address)))
            if shown != value:
                mismatches.append(f"{address}: expected {value!r}, got {shown!r}")
        if mismatches and entry.mode == "mono" and has_stereo:
            raise RuntimeError(
                f"Channel {ch}: mono move could not be verified; stereo changes "
                f"were not applied: {'; '.join(mismatches)}")
        status = "MISMATCH (" + "; ".join(mismatches) + ")" if mismatches else "ok"
        label = "+".join(map(str, inputs))
        results.append((ch, f"LCL {label} ({entry.mode}): "
                            f"'{entry.name}' -> {status}"))
    allocated = {idx for inputs in sources.values() for idx in inputs}
    for idx in range(1, NUM_INPUTS + 1):
        if idx in allocated:
            continue
        address = f"/io/in/LCL/{idx}/name"
        wing.send(address, "")
        shown = str(osc_value(await wing.get(address)))
        if shown != "":
            raise RuntimeError(
                f"LCL {idx}: could not clear unused input name (shows {shown!r})")
    return sorted(results)


async def read_channel_aesthetics(wing: WingConsole,
                                  channels=range(1, NUM_CHANNELS + 1)) -> dict:
    """Read name/color/icon of each channel's source from the console.

    Returns strip identities and source modes. Only local inputs are supported;
    applying the result reallocates inputs in strip order.
    """
    config = {}
    for ch in channels:
        grp = osc_value(await wing.get(f"/ch/{ch}/in/conn/grp"))
        if grp == "OFF":
            continue
        if grp != "LCL":
            raise ValueError(f"channel {ch}: source bank {grp!r} is not LCL")
        idx = int(osc_value(await wing.get(f"/ch/{ch}/in/conn/in")))
        if not 1 <= idx <= NUM_INPUTS:
            raise ValueError(f"channel {ch}: local input {idx} is outside 1-24")
        src = f"/io/in/{grp}/{idx}"
        name = str(osc_value(await wing.get(f"{src}/name")))
        col_id = int(osc_value(await wing.get(f"{src}/col")))
        icon_id = int(osc_value(await wing.get(f"{src}/icon")))
        mode = osc_value(await wing.get(f"{src}/mode"))
        if mode not in ("M", "ST"):
            raise ValueError(f"channel {ch}: unsupported source mode {mode!r}")
        phantom = int(osc_value(await wing.get(f"{src}/vph")))
        if phantom not in (0, 1):
            raise ValueError(f"channel {ch}: invalid 48V state {phantom!r}")
        if mode == "ST":
            if idx % 2 == 0 or idx + 1 > NUM_INPUTS:
                raise ValueError(f"channel {ch}: invalid stereo pair starting at {idx}")
            right_phantom = int(osc_value(
                await wing.get(f"/io/in/{grp}/{idx + 1}/vph")))
            if right_phantom != phantom:
                raise ValueError(
                    f"channel {ch}: stereo inputs have different 48V states; "
                    "set both inputs to the same state before reading")
        if name.strip():
            config[ch] = ChannelConfig(
                name, WING_COLOR_NAMES[col_id], WING_ICON_NAMES[icon_id],
                "stereo" if mode == "ST" else "mono", bool(phantom))
    validate_capacity(config)
    return config


async def main():
    parser = argparse.ArgumentParser(
        description="Apply local input routing, modes and identities to a WING")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE,
                        help="JSON config file (default: %(default)s)")
    args = parser.parse_args()

    if os.path.exists(args.config):
        config = load_config(args.config)
        print(f"Loaded {len(config)} channels from {args.config}")
    else:
        config = DEFAULT_CHANNEL_CONFIG
        print(f"{args.config} not found, using built-in default config")

    wing = await WingConsole.find()
    print(f"Found: {wing.info}")
    async with wing:
        for ch, status in await apply_channel_aesthetics(wing, config):
            print(f"Ch {ch}: {status}")

if __name__ == "__main__":
    asyncio.run(main())
