import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib
from gi.repository import Gtk
from loguru import logger as log

from ...backend.deployment_watchers import PENDING_STATES, TERMINAL_STATES, target_key
from ..base.GitHubActionBase import GitHubActionBase


class DeploymentStatus(GitHubActionBase):
    """Shows a repo deployment's status on a key.

    The key is only a view: the plugin's DeploymentWatcherService
    (backend/deployment_watchers.py) owns the polling and the state per repo,
    so the watch keeps running - and keeps its last result - no matter which
    page is on screen. This class reads the state when it renders and is pushed
    updates through `on_deployment_state`.
    """

    HOOK_MARKER = "# streamcontroller-github-deployment-watcher"
    PENDING = PENDING_STATES
    TERMINAL = TERMINAL_STATES

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True
        self._blink = False
        self._blink_timer_id = None
        # Target of this key, resolved from the settings while the page is
        # loaded and kept afterwards: the watch outlives the action object and
        # we must still be able to name our target when the page is swapped out
        # (ActionCore.get_settings() returns {} once the action has no page).
        self._target = None
        self._watch_settings = {}

    @property
    def deployment_target(self):
        """(owner, repo, environment) this key shows, or None before the first
        render. Read by the watcher service to route state updates."""
        return self._target

    def _resolve_target(self):
        """Refresh the cached target from the settings; keep the last one when
        the settings are unavailable (page gone, action out of the page)."""
        settings = self.get_settings()
        if not settings:
            return self._target
        self._target = target_key(
            settings.get("owner", ""), settings.get("repo", ""),
            settings.get("environment", "production"),
        )
        self._watch_settings = {
            "timeout": max(1, int(settings.get("timeout_seconds", 600))),
            "interval": max(3, int(settings.get("poll_interval_seconds", 10))),
        }
        return self._target

    def _state(self):
        target = self._target
        if target is None or not target[1]:
            return "idle"
        return self.plugin_base.deployment_watchers.state_for(target)

    def get_config_rows(self) -> list:
        rows = []
        for key, title, default in (
            ("owner", "Owner", ""),
            ("repo", "Repository", ""),
            ("environment", "Environment", "production"),
        ):
            row = Adw.EntryRow(title=title)
            row.set_text(self.get_settings().get(key, default))
            row.connect("changed", self._setting_changed, key)
            rows.append(row)

        settings = self.get_settings()
        default_path = str(Path.home() / "Documents" / "GitHub" / settings.get("repo", "repository"))
        if not settings.get("local_repository_path"):
            settings["local_repository_path"] = default_path
            self.set_settings(settings)
        self.repository_path_row = Adw.EntryRow(title="Local repository path")
        self.repository_path_row.set_text(settings.get("local_repository_path", default_path))
        self.repository_path_row.connect("changed", self._setting_changed, "local_repository_path")
        rows.append(self.repository_path_row)

        self.auto_trigger_row = Adw.SwitchRow(title="Allow push auto-trigger")
        self.auto_trigger_row.set_active(bool(settings.get("auto_trigger", False)))
        self.auto_trigger_row.connect("notify::active", self._switch_changed, "auto_trigger")
        rows.append(self.auto_trigger_row)

        hook_row = Adw.ActionRow(
            title="Local push hook",
            subtitle=self._hook_status_text(),
        )
        self.hook_row = hook_row
        setup_button = Gtk.Button(label="Set up")
        setup_button.set_valign(Gtk.Align.CENTER)
        setup_button.connect("clicked", self._setup_hook)
        hook_row.add_suffix(setup_button)
        remove_button = Gtk.Button(label="Remove")
        remove_button.set_valign(Gtk.Align.CENTER)
        remove_button.connect("clicked", self._remove_hook)
        hook_row.add_suffix(remove_button)
        self.setup_button = setup_button
        setup_button.set_sensitive(self.auto_trigger_row.get_active())
        rows.append(hook_row)

        self.timeout_row = Adw.SpinRow.new_with_range(1, 3600, 1)
        self.timeout_row.set_title("Timeout (seconds)")
        self.timeout_row.set_value(self.get_settings().get("timeout_seconds", 600))
        self.timeout_row.connect("changed", self._number_changed, "timeout_seconds")
        rows.append(self.timeout_row)

        self.poll_row = Adw.SpinRow.new_with_range(3, 300, 1)
        self.poll_row.set_title("Poll interval (seconds)")
        self.poll_row.set_value(self.get_settings().get("poll_interval_seconds", 10))
        self.poll_row.connect("changed", self._number_changed, "poll_interval_seconds")
        rows.append(self.poll_row)
        return rows

    def _hook_status_text(self):
        local_path = self.get_settings().get("local_repository_path", "").strip()
        if not local_path:
            return "Not configured"
        hook_path = Path(local_path) / ".git" / "hooks" / "pre-push"
        try:
            active = hook_path.is_file() and self.HOOK_MARKER in hook_path.read_text()
        except OSError:
            active = False
        return "Active" if active else "Not active"

    def _switch_changed(self, row, _param, key):
        settings = self.get_settings()
        settings[key] = row.get_active()
        self.set_settings(settings)
        self.setup_button.set_sensitive(row.get_active())

    def _hook_command(self, action: str):
        settings = self.get_settings()
        repo = settings.get("repo", "").strip()
        owner = settings.get("owner", "").strip()
        environment = settings.get("environment", "production").strip() or "production"
        local_path = settings.get("local_repository_path", "").strip()
        if (action == "install" and (not settings.get("auto_trigger") or not owner or not repo or not local_path)):
            return None
        installer = Path(__file__).resolve().parents[4] / "tools" / "install-deployment-pre-push-hook.sh"
        if action == "uninstall":
            return [str(installer), action, "--repo", local_path]
        return [
            str(installer), action, "--repo", local_path, "--owner", owner,
            "--github-repo", repo, "--environment", environment,
            "--cli", str(Path(__file__).resolve().parents[4] / "main.py"),
        ]

    def _run_hook_command(self, action):
        command = self._hook_command(action)
        if command is None:
            return
        if action == "uninstall":
            command.append("--yes")
        result = subprocess.run(command, capture_output=True, text=True)
        message = "Hook set up" if action == "install" and result.returncode == 0 else \
            "Hook removed" if action == "uninstall" and result.returncode == 0 else \
            (result.stderr.strip() or f"Hook operation failed (exit {result.returncode})")
        GLib.idle_add(self.hook_row.set_subtitle, f"{message} - {self._hook_status_text()}")

    def _setup_hook(self, _button):
        threading.Thread(target=self._run_hook_command, args=("install",), daemon=True).start()

    def _remove_hook(self, _button):
        threading.Thread(target=self._run_hook_command, args=("uninstall",), daemon=True).start()

    def _setting_changed(self, row, key):
        settings = self.get_settings()
        settings[key] = row.get_text().strip()
        self.set_settings(settings)

    def _number_changed(self, row, key):
        settings = self.get_settings()
        settings[key] = int(row.get_value())
        self.set_settings(settings)

    # ------------------------------------------------------------------ #
    # Lifecycle: this key renders the watcher's state, it never owns it
    # ------------------------------------------------------------------ #
    def on_ready(self):
        target = self._resolve_target()
        try:
            self.plugin_base.deployment_watchers.register_view(self)
        except Exception:
            pass
        # Nothing is known about this target yet (a fresh app start, or a repo
        # nobody has pushed to since): read the newest deployment once, so the
        # key paints the real status instead of the empty key "idle" draws. It
        # only reads - pressing the key still arms a watch that follows one.
        try:
            if target:
                self.plugin_base.deployment_watchers.read_once(target)
        except Exception:
            pass
        # Resumes from the watcher's cached state, so a key whose page was off
        # screen (or re-created) paints the current status instead of nothing.
        log.debug("[github] deployment key ready target={} state={}",
                  self._target, self._state())
        self._render(self._state())

    def on_remove(self):
        self._unregister_view()

    def on_removed_from_cache(self):
        # The page this key lives on was dropped: the watch itself carries on.
        self._unregister_view()
        super().on_removed_from_cache()

    def _unregister_view(self):
        try:
            self.plugin_base.deployment_watchers.unregister_view(self)
        except Exception:
            pass

    def on_deployment_state(self, state):
        """Pushed by the watcher service (on the main thread) on every change."""
        self._resolve_target()
        log.debug("[github] deployment key {} -> {}", self._target, state)
        self._render(state)

    def on_tick(self):
        self._resolve_target()
        self._render(self._state())

    def on_key_down(self):
        target = self._resolve_target()
        watchers = self.plugin_base.deployment_watchers
        if target is None or not target[1]:
            self._render("no_deployment")
            return
        state = watchers.state_for(target)
        if state != "idle":
            # Press again to drop the current watch/result; the next press
            # starts a fresh one. Painted from the main thread, as before.
            watchers.reset(target)
            GLib.idle_add(self._set_idle)
            return
        watchers.arm(target, wait_for_new=False, **self._watch_settings)
        self._render(self._state())

    def _render(self, state):
        if state in self.PENDING:
            self.set_status_badge((255, 200, 0, 255))
            self.safe_set_label("top", "", font_size=1)
            self.safe_set_label("center", "", font_size=1)
        elif state == "success":
            self.set_status_badge((0, 255, 0, 255))
            self.safe_set_label("top", "", font_size=1)
            self.safe_set_label("center", "", font_size=1)
        elif state in {"failure", "error"}:
            self.set_status_badge((255, 0, 0, 255))
            self.safe_set_label("top", "", font_size=1)
            self.safe_set_label("center", "", font_size=1)
        elif state == "inactive":
            self.set_status_badge((128, 128, 128, 255))
            self.safe_set_label("top", "", font_size=1)
            self.safe_set_label("center", "", font_size=1)
        elif state == "no_deployment":
            self.set_status_badge((128, 128, 128, 255))
            self.safe_set_label("top", "N/A", font_size=14)
            self.get_input().update()
        elif state == "timeout":
            self._blink = not self._blink
            self.set_status_badge((128, 128, 128, 255) if self._blink else None)
            self.safe_set_label("top", "TO", font_size=14)
            self.get_input().update()
            if self._blink_timer_id is None:
                self._blink_timer_id = GLib.timeout_add(500, self._blink_timeout)
        elif state == "auth":
            self.set_status_badge((40, 100, 220, 255))
            self.safe_set_label("top", "AUTH", font_size=12)
            self.get_input().update()
        elif state == "idle":
            self._set_idle()
        self.commit_render()
        return GLib.SOURCE_REMOVE

    def _blink_timeout(self):
        self._blink_timer_id = None
        if self._state() == "timeout":
            self._render("timeout")
        return GLib.SOURCE_REMOVE

    def _set_idle(self):
        self.set_status_badge(None)
        self.safe_set_label("top", "", font_size=1)
        self.safe_set_label("center", "", font_size=1)
        self.commit_render()
        return GLib.SOURCE_REMOVE
