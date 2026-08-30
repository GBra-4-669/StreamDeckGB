"""
Zero-polling Bluetooth headphone battery for StreamDeckGB.

Tracks one paired Bluetooth headphone (by default the "Px7 S2e") through
BlueZ's org.bluez.Battery1 D-Bus interface. The plugin subscribes to BlueZ
PropertiesChanged / InterfacesAdded signals and only renders when BlueZ
reports a new battery percentage - there is no polling of any kind. The only
D-Bus method calls are one GetManagedObjects lookup at startup (and on each
connect event), both event-driven.

The last known percentage is persisted in the action settings, so the key
keeps showing a number even when the headphones are powered off (no "off"
state): you always know whether they need charging.

Display: big centered number, optional "%" suffix, colored by customizable
low / mid / high threshold zones (label font color, no background fill).
"""
import threading
from copy import copy

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib
from loguru import logger as log

from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport
from src.backend.DeckManagement.InputIdentifier import Input

from GtkHelper.GenerativeUI.SwitchRow import SwitchRow
from GtkHelper.GenerativeUI.SpinRow import SpinRow
from GtkHelper.GenerativeUI.ColorButtonRow import ColorButtonRow

# The headphone this plugin tracks, matched case-insensitively against the
# BlueZ device Name/Alias. Matching by name (instead of a hardcoded MAC) keeps
# working after re-pairing.
TARGET_DEVICE_NAME = "Px7 S2e"

BLUEZ = "org.bluez"
BATTERY_IFACE = "org.bluez.Battery1"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"

DEFAULTS = {
    "show_percent": True,
    "threshold_low": 10,
    "threshold_high": 20,
    "color_low": [255, 0, 0, 255],
    "color_mid": [255, 200, 0, 255],
    "color_high": [0, 180, 0, 255],
}


def mac_from_path(object_path: str):
    """'.../dev_EC_66_D1_B9_0A_D2' -> 'EC:66:D1:B9:0A:D2'"""
    marker = "/dev_"
    idx = object_path.find(marker)
    if idx < 0:
        return None
    return object_path[idx + len(marker):].replace("_", ":")


class HeadphoneBatteryPlugin(PluginBase):
    def __init__(self):
        super().__init__()

        self.battery_holder = ActionHolder(
            plugin_base=self,
            action_base=HeadphoneBattery,
            action_id_suffix="HeadphoneBattery",
            action_name="Headphone Battery",
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.UNSUPPORTED,
                Input.Touchscreen: ActionInputSupport.UNSUPPORTED,
            },
        )
        self.add_action_holder(self.battery_holder)

        self.register(
            plugin_name="Headphone Battery",
            github_repo="https://github.com/gb/streamdeck-headphone-battery",
            plugin_version="1.0.0",
            app_version="1.0.0-alpha",
        )

        # --- D-Bus listener state (created lazily, one per app session) ---
        self._bus = None
        self._actions = set()          # interested HeadphoneBattery actions
        self._target_mac = None        # cached MAC of the tracked headphone
        self._target_mac_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Action registry
    # ------------------------------------------------------------------ #
    def register_action(self, action: "HeadphoneBattery") -> None:
        log.debug("[headphone-battery] action registered")
        self._actions.add(action)
        self._ensure_listener()
        # One-time check for the case where the headphone is already connected
        # (its Battery1 object existed before we subscribed). Not polling: it
        # runs once per action-ready and otherwise waits for signals.
        self._check_current_battery_async()

    def unregister_action(self, action: "HeadphoneBattery") -> None:
        self._actions.discard(action)

    def _notify_actions(self, percent: int) -> None:
        log.info(f"[headphone-battery] BlueZ reports {percent}%")
        for action in list(self._actions):
            try:
                action.on_battery_update(percent)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # BlueZ listener (GDBus signals - no polling)
    # ------------------------------------------------------------------ #
    def _ensure_listener(self) -> None:
        if self._bus is not None:
            return
        log.debug("[headphone-battery] subscribing to BlueZ signals")
        self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self._bus.signal_subscribe(
            BLUEZ, PROPERTIES_IFACE, "PropertiesChanged",
            None, None, Gio.DBusSignalFlags.NONE,
            self._on_properties_changed, None)
        self._bus.signal_subscribe(
            BLUEZ, OBJECT_MANAGER_IFACE, "InterfacesAdded",
            None, None, Gio.DBusSignalFlags.NONE,
            self._on_interfaces_added, None)

    def _resolve_target_mac(self):
        """Find the MAC of the tracked headphone via one GetManagedObjects
        call. Only called from worker threads (it blocks)."""
        with self._target_mac_lock:
            if self._target_mac is not None:
                return self._target_mac
            try:
                result = self._bus.call_sync(
                    BLUEZ, "/", OBJECT_MANAGER_IFACE, "GetManagedObjects",
                    None, None, Gio.DBusCallFlags.NONE, 5000, None)
                for path, interfaces in result.unpack()[0].items():
                    mac = mac_from_path(path)
                    if mac is None:
                        continue
                    device = interfaces.get("org.bluez.Device1", {})
                    alias = str(device.get("Alias") or device.get("Name") or "")
                    if TARGET_DEVICE_NAME.lower() in alias.lower():
                        self._target_mac = mac
                        log.info(
                            f"[headphone-battery] tracking {TARGET_DEVICE_NAME} "
                            f"({mac}) via BlueZ signals"
                        )
                        return mac
            except Exception:
                pass
            return None

    def _target_matches(self, object_path: str) -> bool:
        """Never blocks - the MAC is resolved asynchronously."""
        mac = mac_from_path(object_path)
        if mac is None or self._target_mac is None:
            return False
        return mac.lower() == self._target_mac.lower()

    # -- signal callbacks (main thread) -------------------------------- #
    def _on_properties_changed(self, connection, sender, path, interface,
                               signal, params, user_data):
        try:
            iface_name, changed, _invalidated = params.unpack()
            if iface_name != BATTERY_IFACE:
                return
            percent = changed.get("Percentage")
            if percent is None:
                return
            if self._target_mac is None:
                self._check_current_battery_async()  # resolve first, push state
                return
            if not self._target_matches(path):
                return
            self._notify_actions(int(percent))
        except Exception:
            pass

    def _on_interfaces_added(self, connection, sender, path, interface,
                             signal, params, user_data):
        # The headphone just connected (or its battery object appeared): the
        # InterfacesAdded payload carries the initial properties.
        try:
            object_path, interfaces = params.unpack()
            battery = interfaces.get(BATTERY_IFACE)
            if battery is None:
                return
            if self._target_mac is None:
                self._check_current_battery_async()
                return
            if not self._target_matches(object_path):
                return
            percent = battery.get("Percentage")
            if percent is not None:
                self._notify_actions(int(percent))
        except Exception:
            pass

    def _check_current_battery_async(self) -> None:
        """Resolve the target MAC (if needed) and read the current percentage
        once, off the main thread, then dispatch to the actions."""
        threading.Thread(
            target=self._check_current_battery_worker,
            name="hb-battery-read", daemon=True,
        ).start()

    def _check_current_battery_worker(self) -> None:
        try:
            log.debug("[headphone-battery] initial battery check")
            if self._resolve_target_mac() is None:
                log.debug("[headphone-battery] target not paired - waiting for signals")
                return  # not paired right now - signals will catch it later
            result = self._bus.call_sync(
                BLUEZ, "/", OBJECT_MANAGER_IFACE, "GetManagedObjects",
                None, None, Gio.DBusCallFlags.NONE, 5000, None)
            for path, interfaces in result.unpack()[0].items():
                if not self._target_matches(path):
                    continue
                battery = interfaces.get(BATTERY_IFACE)
                if battery is None:
                    continue
                percent = battery.get("Percentage")
                if percent is not None:
                    GLib.idle_add(self._notify_actions, int(percent))
                return
        except Exception:
            log.exception("[headphone-battery] initial battery check failed")


class HeadphoneBattery(ActionBase):
    """Displays the battery percentage of the tracked Bluetooth headphone.

    Fully signal-driven: no on_tick work at all. The last known value is kept
    in the action settings so the number stays visible while the headphone is
    off.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True

        # Last value applied to the key, for "update only when needed".
        self._last_value: int | None = None
        self._last_shown: tuple | None = None

        # Configuration rows (GenerativeUI auto-persists via var_name).
        self.show_percent_row = SwitchRow(
            self, "show_percent", True,
            title="Show % sign",
            on_change=lambda *a: self.render(force=True),
        )
        self.threshold_low_row = SpinRow(
            self, "threshold_low", 10, 0, 100,
            title="Red below (%)", step=1, digits=0,
            on_change=lambda *a: self.render(force=True),
        )
        self.threshold_high_row = SpinRow(
            self, "threshold_high", 20, 0, 100,
            title="Yellow below (%)", step=1, digits=0,
            on_change=lambda *a: self.render(force=True),
        )
        self.color_low_row = ColorButtonRow(
            self, "color_low", (255, 0, 0, 255),
            title="Low battery color",
            on_change=lambda *a: self.render(force=True),
        )
        self.color_mid_row = ColorButtonRow(
            self, "color_mid", (255, 200, 0, 255),
            title="Mid battery color",
            on_change=lambda *a: self.render(force=True),
        )
        self.color_high_row = ColorButtonRow(
            self, "color_high", (0, 180, 0, 255),
            title="High battery color",
            on_change=lambda *a: self.render(force=True),
        )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def on_ready(self):
        self._claim_label_control()
        try:
            self.plugin_base.register_action(self)
        except Exception:
            pass
        self.render(force=True)

    def _claim_label_control(self):
        """Make sure this action is allowed to set all three label positions
        (top/center/bottom). The page's label-control-actions config can
        otherwise silently drop our label updates - the number on the deck
        then keeps showing a stale value while the persisted one changed."""
        try:
            state = self.get_state()
            if state is None:
                return
            index = self.get_own_action_index()
            if index is None or index < 0:
                return
            apm = state.action_permission_manager
            for position in (0, 1, 2):  # top, center, bottom
                if apm.get_label_control_index(position) != index:
                    apm.set_label_control_index(
                        position, index, reload_pages=False, reload_self=False
                    )
        except Exception:
            pass

    def on_remove(self):
        try:
            self.plugin_base.unregister_action(self)
        except Exception:
            pass

    def on_removed_from_cache(self) -> None:
        try:
            self.plugin_base.unregister_action(self)
        except Exception:
            pass

    def on_tick(self):
        # Intentionally empty: this action is fully signal-driven (zero
        # polling). The app calls on_tick on its own schedule, but we never
        # read anything here.
        pass

    # ------------------------------------------------------------------ #
    # Battery updates (main thread, from the plugin's BlueZ listener)
    # ------------------------------------------------------------------ #
    def on_battery_update(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        if percent == self._last_value:
            return  # update only when needed
        self._last_value = percent

        # Persist the latest number so it survives app restarts and stays
        # visible while the headphone is off.
        try:
            settings = dict(self.get_settings() or {})
            settings["last_battery"] = percent
            self.set_settings(settings)
        except Exception:
            pass

        self.render()

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    def _settings(self) -> dict:
        merged = dict(DEFAULTS)
        merged.update(self.get_settings() or {})
        return merged

    def render(self, force: bool = False) -> None:
        settings = self._settings()

        percent = settings.get("last_battery")
        if percent is None:
            # Unknown (no reading yet): neutral label, no zone color.
            text, color = "—", None
        else:
            percent = int(percent)
            show_pct = bool(settings.get("show_percent", True))
            text = f"{percent}%" if show_pct else f"{percent}"
            low = int(settings.get("threshold_low", 10))
            high = int(settings.get("threshold_high", 20))
            if low > high:
                low, high = high, low
            # The zone color is applied to the label (font), not the background.
            if percent <= low:
                color = list(settings.get("color_low", DEFAULTS["color_low"]))
            elif percent <= high:
                color = list(settings.get("color_mid", DEFAULTS["color_mid"]))
            else:
                color = list(settings.get("color_high", DEFAULTS["color_high"]))

        shown = (text, tuple(color) if color else None)
        if not force and shown == self._last_shown:
            return  # update only when needed
        self._last_shown = shown

        log.debug(f"[headphone-battery] render label={text!r} color={color} "
                  f"label_control={self.has_label_control(1)}")

        try:
            self.set_center_label(text, color=color, font_size=30, update=False)
        except (AttributeError, TypeError):
            try:
                self.set_center_label(text, color=color, font_size=30)
            except Exception:
                pass
        # Apply the zone color to every label position (top/center/bottom) -
        # the action's color wins over the page template in all of them.
        self._apply_label_colors(color)
        try:
            self.get_input().update()
        except Exception:
            pass

    def _apply_label_colors(self, color):
        """Set the zone color on all three label positions. Pass None to clear
        the colors again (page/default colors apply then). Only the color is
        touched - texts stay as they are (the action's own or the page's)."""
        try:
            state = self.get_state()
            if state is None:
                return
            lm = state.label_manager
            for position in ("top", "center", "bottom"):
                current = lm.action_labels.get(position)
                if current is None or current.color == color:
                    continue
                updated = copy(current)
                updated.color = color
                lm.set_action_label(position, updated, update=False)
        except Exception:
            pass
