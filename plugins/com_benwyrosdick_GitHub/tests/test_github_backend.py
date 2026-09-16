"""Unit tests for the pure `gh`-CLI wrapper in backend/github_backend.py.

The backend deliberately has no GTK/StreamController imports, so it loads and
tests standalone. Every test stubs `GitHubBackend._run` (the single subprocess
seam), so nothing here touches the network or a real `gh` binary.
"""

import pytest

from backend.github_backend import GitHubBackend, RateLimitError


def make_backend(run_impl):
    """A backend whose `_run` is replaced by `run_impl(args, timeout=...)`."""
    b = GitHubBackend()
    b._run = run_impl  # type: ignore[method-assign]
    return b


def recording_run(result):
    """A `_run` stub that always returns `result` and records the args it saw."""
    calls = []

    def _run(args, timeout=None):
        calls.append(list(args))
        return result

    _run.calls = calls
    return _run


# --------------------------------------------------------------------------- #
# _build_cmd — sandbox vs. direct
# --------------------------------------------------------------------------- #
def test_build_cmd_sandboxed():
    b = GitHubBackend()
    b._sandboxed = True
    cmd = b._build_cmd(["auth", "status"])
    assert cmd[:4] == ["flatpak-spawn", "--host", "--directory=/", "gh"]
    assert cmd[4:] == ["auth", "status"]


def test_build_cmd_direct():
    b = GitHubBackend()
    b._sandboxed = False
    cmd = b._build_cmd(["auth", "status"])
    # First element resolves to a gh path (or literally "gh"); rest is verbatim.
    assert cmd[0].endswith("gh")
    assert cmd[1:] == ["auth", "status"]


# --------------------------------------------------------------------------- #
# count()
# --------------------------------------------------------------------------- #
def test_count_parses_issue_count():
    b = make_backend(recording_run((True, "5\n", "")))
    assert b.count("repo:o/r type:pr") == 5


def test_count_empty_query_short_circuits():
    # No query -> no subprocess call at all.
    b = make_backend(recording_run((True, "999", "")))
    assert b.count("   ") is None
    assert b._run.calls == []


def test_count_returns_none_on_failure():
    b = make_backend(recording_run((False, "", "boom")))
    assert b.count("repo:o/r") is None


def test_count_returns_none_on_empty_output():
    b = make_backend(recording_run((True, "", "")))
    assert b.count("repo:o/r") is None


# --------------------------------------------------------------------------- #
# latest_run()
# --------------------------------------------------------------------------- #
def test_latest_run_no_repo_returns_none():
    b = make_backend(recording_run((True, "[]", "")))
    assert b.latest_run("") is None
    assert b._run.calls == []


def test_latest_run_omits_optional_filters():
    b = make_backend(recording_run((True, '[{"status":"completed"}]', "")))
    b.latest_run("owner/repo")
    args = b._run.calls[0]
    assert "--workflow" not in args
    assert "--branch" not in args
    assert "-R" in args and "owner/repo" in args


def test_latest_run_includes_workflow_and_branch():
    b = make_backend(recording_run((True, '[{"status":"completed"}]', "")))
    b.latest_run("owner/repo", workflow="ci.yml", branch="main")
    args = b._run.calls[0]
    assert args[args.index("--workflow") + 1] == "ci.yml"
    assert args[args.index("--branch") + 1] == "main"


def test_latest_run_empty_array_is_empty_dict():
    b = make_backend(recording_run((True, "[]", "")))
    assert b.latest_run("owner/repo") == {}


def test_latest_run_returns_first_row():
    b = make_backend(recording_run((True, '[{"status":"in_progress"}]', "")))
    assert b.latest_run("owner/repo") == {"status": "in_progress"}


def test_latest_run_none_on_failure():
    b = make_backend(recording_run((False, "", "err")))
    assert b.latest_run("owner/repo") is None


# --------------------------------------------------------------------------- #
# notification_count()
# --------------------------------------------------------------------------- #
def test_notification_count_global_endpoint_counts_lines():
    b = make_backend(recording_run((True, "a\nb\nc\n", "")))
    assert b.notification_count() == 3
    args = b._run.calls[0]
    assert "/notifications" in args
    assert "--jq" in args and args[args.index("--jq") + 1] == ".[].id"


def test_notification_count_repo_scoped_endpoint():
    b = make_backend(recording_run((True, "", "")))
    assert b.notification_count(repo="owner/repo") == 0
    assert "/repos/owner/repo/notifications" in b._run.calls[0]


def test_notification_count_participating_flag():
    b = make_backend(recording_run((True, "x\n", "")))
    b.notification_count(participating=True)
    args = b._run.calls[0]
    assert "participating=true" in args


def test_notification_count_reason_uses_jq_select():
    b = make_backend(recording_run((True, "x\ny\n", "")))
    b.notification_count(reason="review_requested")
    args = b._run.calls[0]
    jq = args[args.index("--jq") + 1]
    assert 'select(.reason == "review_requested")' in jq


def test_notification_count_ignores_blank_lines():
    b = make_backend(recording_run((True, "a\n\n \nb\n", "")))
    assert b.notification_count() == 2


def test_notification_count_none_on_failure():
    b = make_backend(recording_run((False, "", "err")))
    assert b.notification_count() is None


# --------------------------------------------------------------------------- #
# latest_deployment_id() / deployment_state()
# --------------------------------------------------------------------------- #
def test_latest_deployment_id_reads_the_newest_id():
    b = make_backend(recording_run((True, "4242\n", "")))
    assert b.latest_deployment_id("owner/repo", "production") == ("4242", None)
    args = b._run.calls[0]
    assert "repos/owner/repo/deployments" in args
    assert "environment=production" in args
    assert args[args.index("--jq") + 1] == '.[0].id // ""'


def test_latest_deployment_id_without_environment_omits_the_filter():
    b = make_backend(recording_run((True, "1", "")))
    b.latest_deployment_id("owner/repo")
    assert not any(a.startswith("environment=") for a in b._run.calls[0])


def test_latest_deployment_id_no_deployment_is_not_an_error():
    b = make_backend(recording_run((True, "\n", "")))
    assert b.latest_deployment_id("owner/repo") == ("", None)


def test_latest_deployment_id_failure_reports_the_error():
    b = make_backend(recording_run((False, "", "HTTP 404")))
    assert b.latest_deployment_id("owner/repo") == ("", "HTTP 404")


def test_latest_deployment_id_no_repo_short_circuits():
    b = make_backend(recording_run((True, "1", "")))
    assert b.latest_deployment_id("") == ("", "no repo")
    assert b._run.calls == []


def test_latest_deployment_id_raises_on_rate_limit():
    b = GitHubBackend()
    b._run = lambda args, timeout=None: (False, "", "API rate limit exceeded")  # type: ignore[method-assign]
    b._rate_limit_reset = lambda: 99.0  # type: ignore[method-assign]
    with pytest.raises(RateLimitError):
        b.latest_deployment_id("owner/repo")


def test_deployment_state_reads_the_status_of_one_deployment():
    b = make_backend(recording_run((True, "in_progress\n", "")))
    assert b.deployment_state("owner/repo", 7) == ("in_progress", None)
    args = b._run.calls[0]
    assert "repos/owner/repo/deployments/7/statuses" in args
    assert args[args.index("--jq") + 1] == '.[0].state // ""'


def test_deployment_state_without_a_deployment_short_circuits():
    b = make_backend(recording_run((True, "success", "")))
    assert b.deployment_state("owner/repo", "") == ("", "no deployment")
    assert b._run.calls == []


def test_deployment_state_failure_reports_the_error():
    b = make_backend(recording_run((False, "", "boom")))
    assert b.deployment_state("owner/repo", 1) == ("", "boom")


# --------------------------------------------------------------------------- #
# _check_rate_limit()
# --------------------------------------------------------------------------- #
def test_check_rate_limit_raises_with_reset_epoch():
    b = GitHubBackend()
    b._rate_limit_reset = lambda: 1234.0  # type: ignore[method-assign]
    with pytest.raises(RateLimitError) as exc:
        b._check_rate_limit("API rate limit exceeded for user")
    assert exc.value.reset_epoch == 1234.0


def test_check_rate_limit_falls_back_when_reset_unknown():
    b = GitHubBackend()
    b._rate_limit_reset = lambda: None  # type: ignore[method-assign]
    with pytest.raises(RateLimitError) as exc:
        b._check_rate_limit("secondary rate limit hit")
    # Falls back to ~now + 60s; just assert it's a sane future-ish epoch.
    assert exc.value.reset_epoch > 0


def test_check_rate_limit_noop_without_rate_limit_text():
    b = GitHubBackend()
    # Should not raise for unrelated errors.
    b._check_rate_limit("some other failure")


def test_count_raises_is_swallowed_to_none_is_not(monkeypatch):
    # count() calls _check_rate_limit on failure; if that raises RateLimitError,
    # it should propagate (the caller/cache layer handles it), not become None.
    b = GitHubBackend()
    b._run = lambda args, timeout=None: (False, "", "API rate limit exceeded")  # type: ignore[method-assign]
    b._rate_limit_reset = lambda: 42.0  # type: ignore[method-assign]
    with pytest.raises(RateLimitError):
        b.count("repo:o/r")
