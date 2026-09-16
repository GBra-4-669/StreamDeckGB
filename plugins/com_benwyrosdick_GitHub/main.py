# Import StreamController modules
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport
from src.backend.DeckManagement.InputIdentifier import Input

import json
import os
import time
import threading

from gi.repository import GLib
from loguru import logger as log

# Relative imports anchor every module to this plugin's package. Absolute
# top-level names like `actions`/`backend` would collide with the identically
# named packages other installed plugins register in sys.modules.
from .backend.github_backend import GitHubBackend, RateLimitError
from .backend.deployment_watchers import DeploymentWatcherService, target_key

from .actions.PRCount.PRCount import PRCount
from .actions.IssueCount.IssueCount import IssueCount
from .actions.CIStatus.CIStatus import CIStatus
from .actions.NotificationCount.NotificationCount import NotificationCount
from .actions.DeploymentStatus.DeploymentStatus import DeploymentStatus

DEPLOYMENT_ACTION_ID = "com_benwyrosdick_GitHub::DeploymentStatus"


class GitHubPlugin(PluginBase):
    def __init__(self):
        super().__init__()

        self.lm = self.locale_manager
        self.backend = GitHubBackend()

        # Deployment Status watch: owned here, not by the key. The service keeps
        # polling and caching the last state per repo/environment whatever page
        # is on screen, so a key that comes back (or a push that arrives while
        # its page is hidden) still sees the current status. See
        # backend/deployment_watchers.py.
        self.deployment_watchers = DeploymentWatcherService(
            poller=self.backend,
            dispatch=GLib.idle_add,
            on_rate_limited=self._note_rate_limited,
        )

        # Generic async value cache. Every backend read hits the network, so it
        # must never run on the UI thread: getters return the cached value
        # immediately and refresh in the background. Keyed by an opaque string
        # so unrelated buttons don't clobber each other and identical ones share
        # a single fetch. Cached values include None (failure) so a broken `gh`
        # isn't hammered every tick.
        self._cache = {}           # key -> {"value": Any, "ts": float}
        self._inflight = set()     # keys with a fetch currently running
        self._cache_lock = threading.Lock()

        # When GitHub rate-limits us, all polling is suspended until this epoch
        # (seconds) so we stop hammering the API; the actions render a countdown
        # to it. 0.0 means "not rate limited".
        self._rate_limit_until = 0.0
        self._rate_limit_lock = threading.Lock()

        # Register actions
        self.pr_count_holder = ActionHolder(
            plugin_base=self,
            action_base=PRCount,
            action_id_suffix="PRCount",
            action_name=self.lm.get("actions.pr-count.name"),
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.UNTESTED,
                Input.Touchscreen: ActionInputSupport.UNTESTED,
            },
        )
        self.add_action_holder(self.pr_count_holder)

        self.issue_count_holder = ActionHolder(
            plugin_base=self,
            action_base=IssueCount,
            action_id_suffix="IssueCount",
            action_name=self.lm.get("actions.issue-count.name"),
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.UNTESTED,
                Input.Touchscreen: ActionInputSupport.UNTESTED,
            },
        )
        self.add_action_holder(self.issue_count_holder)

        self.ci_status_holder = ActionHolder(
            plugin_base=self,
            action_base=CIStatus,
            action_id_suffix="CIStatus",
            action_name=self.lm.get("actions.ci-status.name"),
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.UNTESTED,
                Input.Touchscreen: ActionInputSupport.UNTESTED,
            },
        )
        self.add_action_holder(self.ci_status_holder)

        self.notification_count_holder = ActionHolder(
            plugin_base=self,
            action_base=NotificationCount,
            action_id_suffix="NotificationCount",
            action_name=self.lm.get("actions.notification-count.name"),
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.UNTESTED,
                Input.Touchscreen: ActionInputSupport.UNTESTED,
            },
        )
        self.add_action_holder(self.notification_count_holder)

        self.deployment_status_holder = ActionHolder(
            plugin_base=self,
            action_base=DeploymentStatus,
            action_id_suffix="DeploymentStatus",
            action_name="Deployment Status",
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.UNTESTED,
                Input.Touchscreen: ActionInputSupport.UNTESTED,
            },
        )
        self.add_action_holder(self.deployment_status_holder)

        # Register plugin
        self.register(
            plugin_name=self.lm.get("plugin.name"),
            github_repo="https://github.com/benwyrosdick/streamdeck-github",
            plugin_version="1.0.0",
            app_version="1.5.0-beta.14",
        )

    # ------------------------------------------------------------------ #
    # Generic asynchronous, cached reads
    # ------------------------------------------------------------------ #
    def _get_async(self, key: str, fetcher, max_age: float):
        """Return the cached value for `key` (or None), refreshing in the
        background when missing or older than max_age. Never blocks.

        While rate limited, no new fetches are started — the last cached value
        (if any) is returned and polling effectively pauses until the reset."""
        if self.rate_limited_until() is not None:
            with self._cache_lock:
                entry = self._cache.get(key)
            return entry["value"] if entry else None

        now = time.monotonic()
        with self._cache_lock:
            entry = self._cache.get(key)
            fresh = entry is not None and (now - entry["ts"]) < max_age
            if not fresh and key not in self._inflight:
                self._inflight.add(key)
                threading.Thread(
                    target=self._refresh, args=(key, fetcher), daemon=True
                ).start()
            return entry["value"] if entry else None

    def _refresh(self, key: str, fetcher):
        try:
            value = fetcher()
        except RateLimitError as e:
            # Suspend all polling until the reset; leave the cached value (and
            # its stale timestamp) untouched so we re-fetch once the limit lifts.
            self._note_rate_limited(e.reset_epoch)
            with self._cache_lock:
                self._inflight.discard(key)
            return
        except Exception:
            value = None
        with self._cache_lock:
            prev = self._cache.get(key)
            if value is None and prev is not None and prev.get("value") is not None:
                # Transient failure (timeout, flaky keyring/auth, etc.): keep the
                # last good value on screen instead of blanking it. Reset the
                # timestamp so we back off one max_age window before retrying.
                self._cache[key] = {"value": prev["value"], "ts": time.monotonic()}
            else:
                self._cache[key] = {"value": value, "ts": time.monotonic()}
            self._inflight.discard(key)

    def invalidate(self, key: str):
        """Drop a cached value so the next read re-fetches."""
        with self._cache_lock:
            self._cache.pop(key, None)

    # ------------------------------------------------------------------ #
    # Rate-limit state
    # ------------------------------------------------------------------ #
    def _note_rate_limited(self, reset_epoch: float):
        """Record that we're rate limited until `reset_epoch` (epoch seconds)."""
        with self._rate_limit_lock:
            self._rate_limit_until = max(self._rate_limit_until, reset_epoch or 0.0)

    def rate_limited_until(self):
        """Epoch seconds when the rate limit resets, or None if not limited."""
        with self._rate_limit_lock:
            until = self._rate_limit_until
        if until and time.time() < until:
            return until
        return None

    # ------------------------------------------------------------------ #
    # Deployment Status watch (called by the D-Bus API)
    # ------------------------------------------------------------------ #
    def trigger_deployment(self, owner: str, repository: str,
                           environment: str = "production") -> bool:
        """Arm the deployment watch for a branch that was just pushed.

        Called by the app's `--trigger-deployment` (the local pre-push hook).
        Matching is done against the configured page files rather than the
        pages on screen, so the watch starts even when the key's page is not
        the active one - or was not loaded at all in this session. Returns
        False when no key with push auto-trigger watches that repo.
        """
        target = target_key(owner, repository, environment)
        settings = self._deployment_key_settings(target)
        if settings is None:
            return False
        return self.deployment_watchers.arm(
            target,
            wait_for_new=True,
            timeout=max(1, int(settings.get("timeout_seconds", 600))),
            interval=max(3, int(settings.get("poll_interval_seconds", 10))),
        )

    def _deployment_key_settings(self, target):
        """Settings of the first DeploymentStatus key configured for `target`.

        Reads the page files directly: the keys that care about a deployment
        are not necessarily on a loaded page when the push arrives, and the
        settings of one of them are all the watch needs (timeout, interval).
        """
        try:
            import globals as gl
            pages_dir = os.path.join(gl.DATA_PATH, "pages")
            page_names = sorted(os.listdir(pages_dir))
        except Exception as e:
            log.warning("[github] could not list pages for trigger: {}", e)
            return None

        for name in page_names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(pages_dir, name), "r") as f:
                    page = json.load(f)
            except (OSError, ValueError):
                continue
            for collection in ("keys", "dials", "touchscreens"):
                for input_data in (page.get(collection) or {}).values():
                    for state_data in (input_data.get("states") or {}).values():
                        for action in (state_data.get("actions") or []):
                            if not action or action.get("id") != DEPLOYMENT_ACTION_ID:
                                continue
                            settings = action.get("settings") or {}
                            if not settings.get("auto_trigger"):
                                continue
                            key_target = target_key(
                                settings.get("owner", ""), settings.get("repo", ""),
                                settings.get("environment", "production"),
                            )
                            if key_target == target:
                                return settings
        return None

    # ------------------------------------------------------------------ #
    # Typed wrappers used by the actions
    # ------------------------------------------------------------------ #
    def get_gh_available(self, max_age: float = 300.0):
        """Cached `gh auth status` check. Returns True/False, or None until the
        first check completes. Cached because the check is a network call we
        don't want to run on every tick."""
        return self._get_async("ghauth", self.backend.gh_available, max_age)

    def get_count(self, query: str, max_age: float = 45.0):
        if not query:
            return None
        return self._get_async(query, lambda: self.backend.count(query), max_age)

    @staticmethod
    def _run_key(repo: str, workflow: str, branch: str) -> str:
        return f"run\x00{repo}\x00{workflow}\x00{branch}"

    def get_run(self, repo: str, workflow: str = "", branch: str = "",
                max_age: float = 30.0):
        if not repo:
            return None
        key = self._run_key(repo, workflow, branch)
        return self._get_async(
            key, lambda: self.backend.latest_run(repo, workflow, branch), max_age
        )

    def invalidate_run(self, repo: str, workflow: str = "", branch: str = ""):
        self.invalidate(self._run_key(repo, workflow, branch))

    @staticmethod
    def _notif_key(repo: str, participating: bool, reason: str) -> str:
        return f"notif\x00{repo}\x00{int(participating)}\x00{reason}"

    def get_notification_count(self, repo: str = "", participating: bool = False,
                               reason: str = "", max_age: float = 60.0):
        """Cached count of unread notifications. Keyed by scope + filter so a
        repo-scoped key and the global one don't share a fetch."""
        key = self._notif_key(repo, participating, reason)
        return self._get_async(
            key,
            lambda: self.backend.notification_count(repo, participating, reason),
            max_age,
        )

    def invalidate_notification_count(self, repo: str = "",
                                      participating: bool = False,
                                      reason: str = ""):
        self.invalidate(self._notif_key(repo, participating, reason))

    def get_run_progress(self, repo: str, run_id, max_age: float = 15.0):
        """(completed, total) steps for a run, or None. Cached briefly (shorter
        than the run cache) so the live counter advances while a run is going."""
        if not repo or not run_id:
            return None
        key = f"prog\x00{repo}\x00{run_id}"
        return self._get_async(
            key, lambda: self.backend.run_step_progress(repo, run_id), max_age
        )
