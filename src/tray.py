import os
import sys
import glob
import subprocess
from gi.repository import GLib
from loguru import logger as log
import globals as gl
from src.backend.trayicon import DBusTrayIcon, DBusMenu

class TrayIcon(DBusTrayIcon):
    MenuPath = "/com/gb/streamdeckgb/Menu"
    IndicatorPath = "/org/ayatana/NotificationItem/com_gb_streamdeckgb_TrayIcon"
    AppId = "com.gb.streamdeckgb.TrayIcon"
    IconName = "com.gb.streamdeckgb"

    def __init__(self):
        if gl.IS_MAC:
            return
        self.menu = DBusMenu()
        self.menu.add_menu_item(1, "Show Window", callback=self.on_show)
        self.menu.add_menu_item(2, menu_type="separator")
        self.menu.add_menu_item(3, "Settings", callback=self.on_settings)
        self.menu.add_menu_item(4, "Store", callback=self.on_store)
        self.menu.add_menu_item(5, "About", callback=self.on_about)
        self.menu.add_menu_item(6, menu_type="separator")
        self.menu.add_menu_item(7, "Quit", callback=self.on_quit)
        self.menu.add_menu_item(8, "Quit and Restart", callback=self.on_restart)
        super().__init__(
            self.menu,
            self.IndicatorPath,
            self.MenuPath,
            self.AppId,
            "StreamDeckGB",
            activate_callback=self.on_show,
        )
        self.set_icon(self.IconName, path=self.get_icon_theme_path())
        self.set_tooltip("StreamDeckGB")

        # Hardware comes and goes while the app runs, and a fake deck can stand in
        # only while none is attached, so re-check rather than deciding once at
        # startup. Here and not in start(), which the settings window calls on
        # every toggle - that would stack up one timer per toggle.
        GLib.timeout_add_seconds(2, self._sync_tick)

        self.main_win = None
        self.show_about_action = None
        self.show_store_action = None
        self.show_settings_action = None
        self.quit_app_action = None
        self.activate_id = -1

    def get_icon_theme_path(self) -> str:
        """
        The tray host resolves the icon on its own side, so IconThemePath is only useful if it
        points somewhere the host can actually read. Inside a Flatpak it never can - and it
        doesn't have to, because Flatpak exports our icon into the host's icon theme. Hosts
        like the GNOME AppIndicator extension show a placeholder instead of falling back to
        the theme once a path is set, so only hand out a path if the icon is missing from the
        installed theme, which is the case when running from source.
        """
        log.info(f"{self.IconName} using the bundled StreamDeckGB icon")
        return os.path.join(gl.MAIN_PATH, "Assets", "icons")

    @log.catch
    def initialize(self, main_win):
        self.main_win = main_win
        self.show_about_action = main_win.menu_button.open_about_action
        self.show_store_action = main_win.menu_button.open_store_action
        self.show_settings_action = main_win.menu_button.open_settings_action
        self.quit_app_action = main_win.menu_button.quit_action
        app_settings = gl.settings_manager.get_app_settings()
        show_now = app_settings.get("ui",{}).get("tray-icon", True)
        if show_now:
            self.register()

    @log.catch
    def start(self):
        self.register()

    @log.catch
    def stop(self):
        self.unregister()

    def _sync_tick(self) -> bool:
        self.sync_with_hardware()
        return True

    @log.catch
    def sync_with_hardware(self) -> None:
        """
        Optional: with no hardware attached an on-screen deck can supply the bar
        icon for the very same thing, and this tray icon would then sit next to it
        as a second, contradictory icon. Opt in with
        dev.hide-tray-icon-without-hardware - off by default, because a tray icon
        that disappears on its own is surprising unless you asked for it.
        """
        app_settings = gl.settings_manager.get_app_settings()
        if not app_settings.get("dev", {}).get("hide-tray-icon-without-hardware"):
            return
        if not app_settings.get("ui", {}).get("tray-icon", True):
            return  # the tray icon is switched off in the settings anyway
        manager = gl.deck_manager
        if manager is None:
            return
        if manager.has_hardware_deck():
            self.register()
        else:
            self.unregister()

    def _ensure_window(self) -> bool:
        """Make sure the main window exists and the tray actions are wired.

        In daemon-only mode the window is created lazily; until it exists,
        self.main_win and the captured actions are None, which used to make
        every tray click a silent no-op.
        """
        if self.main_win is not None:
            return True
        app = gl.app
        if app is None or not hasattr(app, "ensure_main_window"):
            return False
        app.ensure_main_window()
        return self.main_win is not None

    @log.catch
    def on_show(self):
        GLib.idle_add(lambda: (self._ensure_window() and self.main_win.present(), False)[1])

    @log.catch
    def on_settings(self):
        GLib.idle_add(lambda: (self._ensure_window() and self.show_settings_action.activate(), False)[1])

    @log.catch
    def on_store(self):
        GLib.idle_add(lambda: (self._ensure_window() and self.show_store_action.activate(), False)[1])

    @log.catch
    def on_about(self):
        GLib.idle_add(lambda: (self._ensure_window(), False)[1])
        GLib.idle_add(lambda: (self.main_win is not None and self.main_win.present(), False)[1])
        GLib.idle_add(lambda: (self.show_about_action is not None and self.show_about_action.activate(), False)[1])

    @log.catch
    def on_quit(self):
        if self.main_win is None or self.quit_app_action is None:
            # Daemon-only: no window/action yet - quit the app directly.
            GLib.idle_add(lambda: (gl.app.on_quit(), False)[1])
            return
        GLib.idle_add(lambda: (self.quit_app_action.activate(), False)[1])

    @log.catch
    def on_restart(self):
        pid = str(os.getpid())
        restart_cmd = [sys.executable, *sys.argv]

        restart_script = """
pid="$1"
shift

sleep 0.5

if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null
fi

while kill -0 "$pid" 2>/dev/null; do
    sleep 0.2
done

exec "$@"
"""

        subprocess.Popen(
            ["/bin/sh", "-c", restart_script, "streamdeckgb-restart", pid, *restart_cmd],
            cwd=gl.MAIN_PATH,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if self.main_win is None or self.quit_app_action is None:
            GLib.idle_add(lambda: (gl.app.on_quit(), False)[1])
            return
        GLib.idle_add(lambda: (self.quit_app_action.activate(), False)[1])



# import os
# os.environ["PYSTRAY_BACKEND"] = "appindicator"
# # import pystray

# from pystray import _appindicator
# from PIL import Image
# from pystray import Menu, MenuItem

# class TrayIcon(_appindicator.Icon):
#     def __init__(self):
#         icon_image = Image.open("flatpak/icon_256.png")
#         menu = Menu(
#             MenuItem("StreamDeckGB", enabled=False, action=None),
#             MenuItem("Open", self.on_open),
#             MenuItem("Quit", self.on_quit)
#         )
#         super().__init__("StreamDeckGB", icon_image, menu=menu, title="StreamDeckGB")

#     def on_open(self):
#         print("Open")

#     def on_quit(self):
#         print("Quit")
#         self.stop()  # Stop the icon running when "Quit" is clicked

# if __name__ == "__main__":
#     # Usage
#     stream_controller_icon = TrayIcon()
#     stream_controller_icon.run()
