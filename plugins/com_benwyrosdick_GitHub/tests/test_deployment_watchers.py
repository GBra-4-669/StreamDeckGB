"""Unit tests for the page-independent deployment watcher.

`backend/deployment_watchers.py` deliberately has no GTK/StreamController
imports: every test injects a fake poller and uses the default (direct)
dispatcher, so nothing here touches the network, `gh`, or the GTK main loop.

These tests encode the bug the service exists for: the watch and its last known
state must outlive the key that shows them, and must not depend on that key's
page ever being on screen.
"""

import threading
import time

import pytest

import backend.deployment_watchers as watchers_module
from backend.deployment_watchers import DeploymentWatcherService, target_key
from backend.github_backend import RateLimitError


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    """Poll intervals are floored at MIN_INTERVAL seconds in production; the
    tests need the state machine to advance immediately."""
    monkeypatch.setattr(watchers_module, "MIN_INTERVAL", 0.01)
    monkeypatch.setattr(watchers_module, "DEFAULT_INTERVAL", 0.01)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class FakePoller:
    """Scripted stand-in for GitHubBackend.

    `ids` are handed out one per `latest_deployment_id` call (the last one
    repeats once the list is down to one), `states` likewise for
    `deployment_state`. `id_gates` / `state_gates` may hold one
    `threading.Event` per call index: that call blocks until a test releases
    it, which is how the in-flight states below are inspected.
    """

    def __init__(self, ids=("7",), states=("success",), id_error=None,
                 state_error=None, id_gates=None, state_gates=None):
        self.ids = list(ids)
        self.states = list(states)
        self.id_error = id_error
        self.state_error = state_error
        self.id_gates = list(id_gates or [])
        self.state_gates = list(state_gates or [])
        self.id_calls = []
        self.state_calls = []
        self.id_returns = []
        self.state_returns = []

    def _fail(self, error):
        if error is None:
            return None
        if isinstance(error, BaseException):
            raise error
        return "", error

    @staticmethod
    def _wait_gate(gates, index):
        if index < len(gates) and gates[index] is not None:
            gates[index].wait(5)

    def latest_deployment_id(self, repo, environment):
        index = len(self.id_calls)
        self.id_calls.append((repo, environment))
        self._wait_gate(self.id_gates, index)
        self.id_returns.append(repo)
        failure = self._fail(self.id_error)
        if failure is not None:
            return failure
        return (self.ids.pop(0) if len(self.ids) > 1 else self.ids[0]), None

    def deployment_state(self, repo, deployment_id):
        index = len(self.state_calls)
        self.state_calls.append((repo, deployment_id))
        self._wait_gate(self.state_gates, index)
        self.state_returns.append(deployment_id)
        failure = self._fail(self.state_error)
        if failure is not None:
            return failure
        return (self.states.pop(0) if len(self.states) > 1 else self.states[0]), None


class FakeView:
    """Stand-in for a DeploymentStatus action: a target plus a state sink."""

    def __init__(self, target):
        self.deployment_target = target
        self.states = []

    def on_deployment_state(self, state):
        self.states.append(state)


# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #
def test_target_key_normalises_casing_and_defaults():
    assert target_key(" Un-FAO ", "Repo", " Production ") == ("un-fao", "repo", "production")
    assert target_key("o", "r", "") == ("o", "r", "production")


def test_unknown_target_is_idle():
    svc = DeploymentWatcherService(FakePoller())
    assert svc.state_for(target_key("o", "r")) == "idle"
    assert svc.is_running(target_key("o", "r")) is False
    assert svc.snapshot() == {}


def test_arm_requires_a_repo():
    poller = FakePoller()
    svc = DeploymentWatcherService(poller)
    assert svc.arm(target_key("o", "")) is False
    assert svc.snapshot() == {}
    assert poller.id_calls == []


# --------------------------------------------------------------------------- #
# Watch lifecycle
# --------------------------------------------------------------------------- #
def test_watch_without_deployments_reports_no_deployment():
    svc = DeploymentWatcherService(FakePoller(ids=("",)))
    target = target_key("o", "r")
    assert svc.arm(target) is True
    assert wait_for(lambda: svc.state_for(target) == "no_deployment")
    assert svc.is_running(target) is False


def test_gh_failure_reports_auth():
    svc = DeploymentWatcherService(FakePoller(id_error="HTTP 404"))
    target = target_key("o", "r")
    svc.arm(target)
    assert wait_for(lambda: svc.state_for(target) == "auth")
    assert svc.is_running(target) is False


def test_rate_limit_reports_auth_and_notifies_the_plugin():
    seen = []
    svc = DeploymentWatcherService(FakePoller(id_error=RateLimitError(1234.0)),
                                   on_rate_limited=seen.append)
    target = target_key("o", "r")
    svc.arm(target)
    assert wait_for(lambda: svc.state_for(target) == "auth")
    assert seen == [1234.0]


def test_watch_follows_the_deployment_to_a_terminal_state():
    status_gate = threading.Event()
    poller = FakePoller(ids=("7",), states=("in_progress", "success"),
                        state_gates=[None, status_gate])
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    svc.arm(target, timeout=30)

    # Second status call is held back, so this is the in-flight state.
    assert wait_for(lambda: svc.state_for(target) == "in_progress")
    assert svc.is_running(target) is True

    status_gate.set()
    assert wait_for(lambda: svc.state_for(target) == "success")
    assert svc.is_running(target) is False
    assert poller.id_calls[0] == ("o/r", "production")
    assert poller.state_calls[-1] == ("o/r", "7")


def test_unknown_github_state_is_treated_as_pending():
    status_gate = threading.Event()
    poller = FakePoller(ids=("7",), states=("", "success"),
                        state_gates=[None, status_gate])
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    svc.arm(target, timeout=30)
    assert wait_for(lambda: svc.state_for(target) == "pending" and poller.state_calls)
    assert svc.is_running(target) is True
    status_gate.set()
    assert wait_for(lambda: svc.state_for(target) == "success")


def test_watch_gives_up_after_the_timeout():
    poller = FakePoller(ids=("7",), states=("in_progress",))
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    svc.arm(target, timeout=1)
    assert wait_for(lambda: svc.state_for(target) == "timeout", timeout=4)
    assert svc.is_running(target) is False


# --------------------------------------------------------------------------- #
# wait_for_new (a push just happened)
# --------------------------------------------------------------------------- #
def test_wait_for_new_follows_the_deployment_the_push_creates():
    # Two polls still see the old deployment, the third sees the new one.
    poller = FakePoller(ids=("10", "10", "11"), states=("success",))
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    svc.arm(target, wait_for_new=True, timeout=10)
    assert wait_for(lambda: svc.state_for(target) == "success")
    assert poller.state_calls[-1] == ("o/r", "11")


def test_wait_for_new_gives_up_when_no_new_deployment_appears():
    poller = FakePoller(ids=("10",), states=("success",))
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    svc.arm(target, wait_for_new=True, timeout=1)
    assert wait_for(lambda: svc.state_for(target) == "timeout", timeout=4)
    assert poller.state_calls == []


# --------------------------------------------------------------------------- #
# Superseding and cancelling
# --------------------------------------------------------------------------- #
def test_reset_wins_over_an_in_flight_worker():
    status_gate = threading.Event()
    poller = FakePoller(ids=("7",), states=("success",), state_gates=[status_gate])
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    svc.arm(target)

    # The worker is inside gh with the state in hand.
    assert wait_for(lambda: len(poller.state_calls) == 1)
    svc.reset(target)
    assert svc.state_for(target) == "idle"

    status_gate.set()
    # The worker finishes and tries to write its result: it must be dropped.
    assert wait_for(lambda: poller.state_returns)
    time.sleep(0.02)
    assert svc.state_for(target) == "idle"
    assert svc.snapshot() == {}


def test_re_arming_supersedes_the_previous_watch():
    first, second = threading.Event(), threading.Event()
    poller = FakePoller(ids=("7",), states=("success",), id_gates=[first, second])
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")

    svc.arm(target)
    assert wait_for(lambda: len(poller.id_calls) == 1)
    svc.arm(target, wait_for_new=True, timeout=1)   # a second push arrives
    assert svc.state_for(target) == "pending"

    first.set()   # the superseded worker stands down...
    assert wait_for(lambda: len(poller.id_returns) == 1)
    time.sleep(0.02)
    # ...without touching the state of the new watch.
    assert svc.state_for(target) == "pending"
    assert svc.is_running(target) is True

    # The new watch is the one still running: it waits for a deployment that
    # never appears and gives up on its own timeout.
    second.set()
    assert wait_for(lambda: svc.state_for(target) == "timeout", timeout=4)


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def test_state_is_pushed_to_matching_views_only():
    status_gate = threading.Event()
    poller = FakePoller(ids=("7",), states=("in_progress", "success"),
                        state_gates=[None, status_gate])
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    mine, theirs = FakeView(target), FakeView(target_key("o", "other"))
    svc.register_view(mine)
    svc.register_view(theirs)

    svc.arm(target, timeout=30)
    assert wait_for(lambda: "in_progress" in mine.states)
    assert mine.states[0] == "pending"        # arm paints the key immediately
    assert theirs.states == []

    status_gate.set()
    assert wait_for(lambda: "success" in mine.states)
    assert theirs.states == []


def test_unregistered_view_stops_receiving_updates():
    poller = FakePoller(ids=("7",), states=("success",))
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    view = FakeView(target)
    svc.register_view(view)
    svc.arm(target)
    assert wait_for(lambda: "success" in view.states)

    svc.unregister_view(view)
    svc.reset(target)
    assert view.states[-1] == "success"       # no "idle" push after unregister


def test_register_view_is_idempotent():
    poller = FakePoller(ids=("7",), states=("success",))
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    view = FakeView(target)
    svc.register_view(view)
    svc.register_view(view)                   # on_ready runs again on redraws
    svc.arm(target)
    assert wait_for(lambda: view.states)
    assert view.states.count("pending") == 1


def test_a_broken_view_does_not_stop_the_others():
    class Boom(FakeView):
        def on_deployment_state(self, state):
            raise RuntimeError("view is gone")

    poller = FakePoller(ids=("7",), states=("success",))
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    good = FakeView(target)
    svc.register_view(Boom(target))
    svc.register_view(good)
    svc.arm(target)
    assert wait_for(lambda: "success" in good.states)


def test_pushes_go_through_the_dispatcher():
    dispatched = []
    svc = DeploymentWatcherService(
        FakePoller(ids=("7",), states=("success",)),
        dispatch=lambda func, *args: dispatched.append((func, args)),
    )
    target = target_key("o", "r")
    view = FakeView(target)
    svc.register_view(view)
    svc.arm(target)
    assert dispatched
    assert dispatched[0][1] == ("pending",)
    assert view.states == []                  # dispatch owns the call, not us


# --------------------------------------------------------------------------- #
# The regression the service was written for
# --------------------------------------------------------------------------- #
def test_state_outlives_the_view_that_started_the_watch():
    """A key whose page was swapped out (or re-created) comes back to the
    current state instead of a cold one, and the watch is still owned by the
    plugin rather than by the dead action object."""
    status_gate = threading.Event()
    poller = FakePoller(ids=("7",), states=("in_progress", "success"),
                        state_gates=[None, status_gate])
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")

    first_view = FakeView(target)
    svc.register_view(first_view)
    svc.arm(target, timeout=30)
    assert wait_for(lambda: svc.state_for(target) == "in_progress")

    # Page switched away: the app drops the action object.
    svc.unregister_view(first_view)

    # The watch keeps polling while nothing is showing it.
    status_gate.set()
    assert wait_for(lambda: svc.state_for(target) == "success")

    # Page switched back: a brand new action object reads the state.
    second_view = FakeView(target)
    svc.register_view(second_view)
    assert svc.state_for(target) == "success"


# --------------------------------------------------------------------------- #
# Reading a status with no watch (a key that nothing has pushed to)
# --------------------------------------------------------------------------- #
def test_read_once_paints_the_newest_state_without_watching():
    """The bug: a key whose target nothing has pushed to painted nothing at
    all, because a watch - started by a push or a press - was the only thing
    that ever fetched. Reading once must show the real status, and must not
    leave a watch behind."""
    poller = FakePoller(ids=("7",), states=("success",))
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    view = FakeView(target)
    svc.register_view(view)

    assert svc.read_once(target) is True
    assert wait_for(lambda: view.states == ["success"])
    assert svc.state_for(target) == "success"
    assert svc.is_running(target) is False
    assert svc.snapshot() == {}                 # nothing is being followed
    assert poller.id_calls == [("o/r", "production")]


def test_read_once_without_deployments_says_so():
    svc = DeploymentWatcherService(FakePoller(ids=("",)))
    target = target_key("o", "r")
    assert svc.read_once(target) is True
    assert wait_for(lambda: svc.state_for(target) == "no_deployment")


def test_read_once_reports_a_failed_query_as_auth():
    svc = DeploymentWatcherService(FakePoller(id_error="gh: not logged in"))
    target = target_key("o", "r")
    assert svc.read_once(target) is True
    assert wait_for(lambda: svc.state_for(target) == "auth")


def test_read_once_is_one_read_however_often_it_is_asked():
    gate = threading.Event()
    poller = FakePoller(ids=("7",), states=("in_progress",), state_gates=[gate])
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    view = FakeView(target)
    svc.register_view(view)

    assert svc.read_once(target) is True
    assert wait_for(lambda: len(poller.state_calls) == 1)
    assert svc.read_once(target) is False        # in flight: no second read
    gate.set()
    assert wait_for(lambda: svc.state_for(target) == "in_progress")
    assert svc.read_once(target) is False        # already known
    assert len(poller.id_calls) == 1


def test_read_once_never_joins_a_running_watch():
    """A watch owns the target; a read must not race it or clobber its state."""
    gate = threading.Event()
    poller = FakePoller(ids=("7",), states=("in_progress",), state_gates=[gate])
    svc = DeploymentWatcherService(poller)
    target = target_key("o", "r")
    assert svc.arm(target, timeout=30) is True
    assert svc.read_once(target) is False
    assert len(poller.id_calls) == 1             # the watch's own first read
    gate.set()
    assert wait_for(lambda: svc.state_for(target) == "in_progress")


def test_reset_forgets_a_read_state_too():
    """A press clears the key (press again for a fresh watch), and that must
    include a state that came from a read rather than from a watch."""
    svc = DeploymentWatcherService(FakePoller(ids=("7",), states=("success",)))
    target = target_key("o", "r")
    view = FakeView(target)
    svc.register_view(view)
    svc.read_once(target)
    assert wait_for(lambda: svc.state_for(target) == "success")

    svc.reset(target)
    assert svc.state_for(target) == "idle"
    assert view.states[-1] == "idle"
