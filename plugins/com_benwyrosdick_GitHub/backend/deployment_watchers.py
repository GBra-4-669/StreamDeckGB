"""Page-independent deployment watcher for the Deployment Status action.

The watch used to live on the key: `on_key_down` started a thread bound to that
action object and every update went through `self._render`. StreamDeckGB does
not keep action objects around for the whole session - they are re-created when
a page is reloaded/re-cached, when the deck reconnects, and rendering is gated
on the key's page being the *active* one - so the status was only ever tracked
while that page happened to be on screen, and a re-created key came back cold.

This module owns the polling instead. One watch per (owner, repo, environment)
lives as long as the plugin does and keeps its last known state, whether or not
any page is showing it. Action objects are pure views: they register, read
`state_for()` and get pushed `on_deployment_state()` on every change.

No GTK or StreamController imports, so the state machine is unit-testable: the
caller passes the `gh` poller and the main-thread dispatcher (`GLib.idle_add`).
"""

import threading
import time
from typing import Callable

try:
    from loguru import logger as log
except Exception:  # pragma: no cover - loguru always present inside the app
    import logging

    class _LogShim:
        """Adapt loguru-style "{}" calls to stdlib logging (used only when
        loguru is missing, e.g. standalone tests)."""
        def __init__(self):
            self._log = logging.getLogger("github")

        @staticmethod
        def _fmt(msg, args):
            try:
                return msg.format(*args)
            except Exception:
                return msg

        def debug(self, msg, *a):
            self._log.debug(self._fmt(msg, a))

        def info(self, msg, *a):
            self._log.info(self._fmt(msg, a))

        def warning(self, msg, *a):
            self._log.warning(self._fmt(msg, a))

        def error(self, msg, *a):
            self._log.error(self._fmt(msg, a))

        def exception(self, msg, *a):
            self._log.error(self._fmt(msg, a))

    log = _LogShim()

from .github_backend import RateLimitError

# GitHub's own state vocabulary, split the way the key renders it.
PENDING_STATES = frozenset({"pending", "in_progress", "queued"})
TERMINAL_STATES = frozenset({"success", "failure", "error", "inactive"})

DEFAULT_TIMEOUT = 600
DEFAULT_INTERVAL = 10
MIN_INTERVAL = 3


def target_key(owner: str, repo: str, environment: str = "production"):
    """Identity of one watched deployment: (owner, repo, environment), all
    lowercased so a key and a pre-push hook agree on casing."""
    return (
        (owner or "").strip().lower(),
        (repo or "").strip().lower(),
        ((environment or "").strip().lower() or "production"),
    )


class DeploymentWatcherService:
    """Owns every deployment watch and the last known state of each target.

    `poller` must provide `latest_deployment_id(repo, environment)` and
    `deployment_state(repo, deployment_id)`, both returning `(value, error)`
    (see GitHubBackend). `dispatch` runs a callable on the caller's UI thread -
    actions pass `GLib.idle_add`, so views are only ever touched from the GTK
    main thread (a bare call is used when it is omitted, for tests).
    """

    def __init__(self, poller, dispatch: Callable = None, on_rate_limited: Callable = None):
        self._poller = poller
        self._dispatch = dispatch or (lambda func, *args: func(*args))
        self._on_rate_limited = on_rate_limited
        self._lock = threading.RLock()
        self._watches = {}   # target -> watch dict
        self._known = {}     # target -> last state read, watch or not
        self._reads = set()  # targets with a one-shot read in flight
        self._views = []     # live DeploymentStatus actions

    # ------------------------------------------------------------------ #
    # Views
    # ------------------------------------------------------------------ #
    def register_view(self, view) -> None:
        """Track `view` (a DeploymentStatus action) for state pushes. Idempotent:
        the app calls on_ready again on every redraw."""
        with self._lock:
            if not any(existing is view for existing in self._views):
                self._views.append(view)

    def unregister_view(self, view) -> None:
        with self._lock:
            self._views = [existing for existing in self._views if existing is not view]

    def _notify(self, target, state: str) -> None:
        """Push `state` to every view that is currently showing `target`."""
        with self._lock:
            views = list(self._views)
        for view in views:
            try:
                if getattr(view, "deployment_target", None) != target:
                    continue
                self._dispatch(view.on_deployment_state, state)
            except Exception as e:
                log.error("[github] deployment view update failed: {}", e)

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    def state_for(self, target) -> str:
        """Last known state of `target`, or "idle" when nothing is known."""
        with self._lock:
            watch = self._watches.get(target)
            if watch:
                return watch["state"]
            return self._known.get(target, "idle")

    def is_running(self, target) -> bool:
        with self._lock:
            watch = self._watches.get(target)
            return bool(watch and watch["running"])

    def read_once(self, target) -> bool:
        """Read the newest deployment's state once, without starting a watch.

        A watch follows a deployment that is being created; this only answers
        "what is the status right now", which is what a key with nothing known
        about it needs - so it paints the real state instead of an empty key,
        and pressing the key afterwards still starts a proper watch.
        """
        if not target or not target[1]:
            return False
        with self._lock:
            watch = self._watches.get(target)
            if watch and watch["running"]:
                return False                      # a watch owns this target already
            if self._known.get(target, "idle") != "idle":
                return False                      # a state is already known
            if target in self._reads:
                return False                      # its read is in flight
            self._reads.add(target)
        threading.Thread(target=self._read, args=(target,),
                         daemon=True, name=f"github-deployment-read:{target[1]}").start()
        return True

    def _read(self, target) -> None:
        owner, repo, environment = target
        try:
            deployment_id, error = self._poller.latest_deployment_id(
                f"{owner}/{repo}", environment)
            if error:
                self._remember(target, "auth")
                return
            if not deployment_id:
                self._remember(target, "no_deployment")
                return
            state, error = self._poller.deployment_state(
                f"{owner}/{repo}", deployment_id)
            if error:
                self._remember(target, "auth")
                return
            state = (state or "").lower()
            if state not in PENDING_STATES and state not in TERMINAL_STATES:
                state = "pending"
            self._remember(target, state)
        except RateLimitError as e:
            if self._on_rate_limited is not None:
                try:
                    self._on_rate_limited(e.reset_epoch)
                except Exception:
                    pass
            self._remember(target, "auth")
        except Exception as e:
            log.error("[github] deployment read for {} failed: {}", target, e)
            self._remember(target, "auth")
        finally:
            with self._lock:
                self._reads.discard(target)

    def _remember(self, target, state: str) -> None:
        """Cache a state read outside a watch, and push it to the views."""
        with self._lock:
            watch = self._watches.get(target)
            if watch and watch["running"]:
                return          # a live watch owns the state now
            if self._known.get(target) == state:
                return
            self._known[target] = state
        log.debug("[github] deployment {} read as {}", target, state)
        self._notify(target, state)

    def arm(self, target, wait_for_new: bool = False,
            timeout: int = None, interval: int = None) -> bool:
        """Start watching `target`, replacing any watch already running for it.

        An existing watch is superseded rather than joined: its worker is
        cancelled and its later writes are dropped (see `_set_state`), so a
        push never blocks the D-Bus thread on a poll that is already in flight.
        With `wait_for_new` the watch first waits for a deployment other than
        the current newest one, which is what a pre-push trigger wants.
        """
        if not target or not target[1]:
            return False
        timeout = max(1, int(DEFAULT_TIMEOUT if timeout is None else timeout))
        interval = max(MIN_INTERVAL, int(DEFAULT_INTERVAL if interval is None else interval))
        with self._lock:
            previous = self._watches.get(target)
            watch = {
                "state": "pending",
                "cancel": threading.Event(),
                "running": True,
                "wait_for_new": bool(wait_for_new),
                "timeout": timeout,
                "interval": interval,
            }
            self._watches[target] = watch
            if previous is not None:
                previous["cancel"].set()
        log.debug(
            "[github] watching {} as wait_for_new={} (timeout={}s interval={}s)",
            target, wait_for_new, timeout, interval,
        )
        self._notify(target, "pending")
        threading.Thread(
            target=self._run, args=(target, watch),
            daemon=True, name=f"github-deployment:{target[1]}",
        ).start()
        return True

    def reset(self, target) -> None:
        """Stop watching `target` and forget its state (key press to toggle,
        so the next press starts a fresh watch)."""
        with self._lock:
            watch = self._watches.pop(target, None)
            self._known.pop(target, None)
            if watch is not None:
                watch["cancel"].set()
        self._notify(target, "idle")

    def snapshot(self):
        """{target: (state, running)} - for logs and tests."""
        with self._lock:
            return {t: (w["state"], w["running"]) for t, w in self._watches.items()}

    def _set_state(self, target, watch, state: str, running: bool) -> None:
        with self._lock:
            if self._watches.get(target) is not watch:
                return  # superseded by a newer watch: never clobber its state
            if watch["state"] == state and watch["running"] == running:
                return
            watch["state"] = state
            watch["running"] = running
            # A finished watch still leaves a status behind: the key is a view
            # of the last known state, and "no watch" must not mean "no status".
            self._known[target] = state
        log.debug("[github] deployment {} -> {}{}", target, state,
                  "" if running else " (done)")
        self._notify(target, state)

    def _finish(self, target, watch, state: str) -> None:
        self._set_state(target, watch, state, running=False)

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #
    def _run(self, target, watch) -> None:
        owner, repo, environment = target
        cancel = watch["cancel"]
        interval = watch["interval"]
        started = time.monotonic()
        try:
            deployment_id, error = self._poller.latest_deployment_id(
                f"{owner}/{repo}", environment)
            if error or not deployment_id:
                self._finish(target, watch, "auth" if error else "no_deployment")
                return

            if watch["wait_for_new"]:
                # A push was just made: this key must follow the deployment that
                # push creates, not the (already finished) one it replaced.
                baseline = deployment_id
                deployment_id = ""
                while not cancel.is_set() and time.monotonic() - started < watch["timeout"]:
                    deployment_id, error = self._poller.latest_deployment_id(
                        f"{owner}/{repo}", environment)
                    log.debug("[github] poll {} for a new deployment (newest={})",
                              target, deployment_id or "none")
                    if error:
                        self._finish(target, watch, "auth")
                        return
                    if deployment_id and deployment_id != baseline:
                        break
                    cancel.wait(interval)
                if not deployment_id or deployment_id == baseline:
                    self._finish(target, watch, "timeout")
                    return

            while not cancel.is_set():
                if time.monotonic() - started >= watch["timeout"]:
                    self._finish(target, watch, "timeout")
                    return
                state, error = self._poller.deployment_state(
                    f"{owner}/{repo}", deployment_id)
                log.debug("[github] poll {} deployment {} -> {}", target,
                          deployment_id, state or "no status yet")
                if error:
                    self._finish(target, watch, "auth")
                    return
                state = (state or "").lower()
                if state not in PENDING_STATES and state not in TERMINAL_STATES:
                    # GitHub has not published a status for it yet.
                    state = "pending"
                if state in TERMINAL_STATES:
                    self._finish(target, watch, state)
                    return
                self._set_state(target, watch, state, running=True)
                cancel.wait(interval)
        except RateLimitError as e:
            if self._on_rate_limited is not None:
                try:
                    self._on_rate_limited(e.reset_epoch)
                except Exception:
                    pass
            self._finish(target, watch, "auth")
        except Exception as e:
            log.error("[github] deployment watch for {} failed: {}", target, e)
            self._finish(target, watch, "auth")
