"""Thin wrapper around the host `gh` (GitHub CLI).

This module deliberately has NO GTK or StreamController imports so it stays
pure and unit-testable. StreamController runs inside a flatpak sandbox, so the
host binary is reached via `flatpak-spawn --host gh ...`. When running outside
a sandbox (e.g. local development) the binary is called directly.

We use the *local* `gh` CLI (which carries the user's existing auth) rather
than a raw GitHub API token, so no extra credentials are needed.

Counts are obtained with a single GraphQL `search { issueCount }` call: it
returns just the total without fetching any result rows, which is by far the
cheapest way to count matching pull requests.
"""

import json
import os
import shutil
import subprocess
import threading
import time
from typing import Optional

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

        def warning(self, msg, *a):
            self._log.warning(self._fmt(msg, a))

        def error(self, msg, *a):
            self._log.error(self._fmt(msg, a))

        def exception(self, msg, *a):
            self._log.error(self._fmt(msg, a))

    log = _LogShim()

# Timeouts (seconds). Counts hit the network, so give them a generous budget;
# `gh run list`/`run view` (especially with a --workflow filter) can be slow,
# so they get more headroom; auth checks are local and quick; the deployments
# endpoints are polled in the background, so they get the most room.
COUNT_TIMEOUT = 8
RUN_TIMEOUT = 15
AUTH_TIMEOUT = 5
DEPLOY_TIMEOUT = 30


class RateLimitError(Exception):
    """Raised when a gh call fails because the GitHub API rate limit is hit.

    Carries the epoch (seconds) when the limit is expected to reset, so the UI
    can show a countdown and callers can stop polling until then.
    """
    def __init__(self, reset_epoch: float):
        super().__init__("github api rate limited")
        self.reset_epoch = reset_epoch


class GitHubBackend:
    def __init__(self):
        # The flatpak sandbox always has /.flatpak-info; the host does not.
        self._sandboxed = os.path.exists("/.flatpak-info")

    # ------------------------------------------------------------------ #
    # Low-level command construction / execution
    # ------------------------------------------------------------------ #
    def _build_cmd(self, args: list) -> list:
        if self._sandboxed:
            # --directory=/ pins a working dir that exists on the host. Without
            # it, flatpak-spawn forwards the caller's cwd (the app's install
            # dir), which the host can't chdir into, and every call fails with
            # "Failed to change to directory".
            return ["flatpak-spawn", "--host", "--directory=/", "gh", *args]
        return [shutil.which("gh") or "gh", *args]

    def _run(self, args: list, timeout: int = COUNT_TIMEOUT):
        """Run a gh command. Returns (ok, stdout, stderr); never raises."""
        cmd = self._build_cmd(args)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            ok = proc.returncode == 0
            if not ok:
                log.warning("[github] {} -> rc={} err={!r}", args, proc.returncode, (proc.stderr or "").strip()[:200])
            return ok, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            log.error("[github] {} -> TIMEOUT after {}s", cmd, timeout)
            return False, "", "timeout"
        except (FileNotFoundError, OSError) as e:
            log.error("[github] {} -> EXEC FAILED: {}", cmd, e)
            return False, "", str(e)

    # ------------------------------------------------------------------ #
    # Rate-limit detection
    # ------------------------------------------------------------------ #
    def _check_rate_limit(self, stderr: str) -> None:
        """Raise RateLimitError if `stderr` indicates GitHub rate limiting."""
        if "rate limit" not in (stderr or "").lower():
            return
        reset = self._rate_limit_reset()
        if reset is None:
            # Secondary/abuse limits aren't reflected in /rate_limit; back off
            # a minute and try again.
            reset = time.time() + 60
        raise RateLimitError(reset)

    def _rate_limit_reset(self) -> Optional[float]:
        """Soonest reset epoch among currently-exhausted resources, or None.

        The /rate_limit endpoint does NOT count against the rate limit, so it
        is always safe to call — even while rate limited.
        """
        ok, out, _ = self._run(
            ["api", "rate_limit", "--jq",
             "[.resources | to_entries[] | select(.value.remaining == 0) "
             "| .value.reset] | min"],
            timeout=AUTH_TIMEOUT,
        )
        if not ok:
            return None
        val = out.strip()
        if not val or val == "null":
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #
    def gh_available(self) -> bool:
        """True if `gh` is installed and authenticated.

        Raises RateLimitError if the check fails due to rate limiting (the
        token-validation request can itself be throttled).
        """
        ok, _, err = self._run(["auth", "status"], timeout=AUTH_TIMEOUT)
        if not ok:
            self._check_rate_limit(err)
        return ok

    def count(self, query: str, timeout: int = COUNT_TIMEOUT) -> Optional[int]:
        """Number of issues/PRs matching a GitHub search `query`.

        Uses GraphQL `search(type: ISSUE, first: 0) { issueCount }` so only the
        total comes back, not the matching rows. Returns None on any failure.
        """
        if not query or not query.strip():
            return None
        gql = (
            "query($q: String!) { search(query: $q, type: ISSUE, first: 0) "
            "{ issueCount } }"
        )
        # Both -f flags force string typing; the GraphQL variable is String!.
        # -f (vs -F) also avoids gh interpreting a leading '@' as a file path.
        ok, out, err = self._run(
            ["api", "graphql", "-f", f"query={gql}", "-f", f"q={query}",
             "--jq", ".data.search.issueCount"],
            timeout=timeout,
        )
        if not ok:
            self._check_rate_limit(err)
            return None
        if not out.strip():
            return None
        try:
            return int(out.strip())
        except (ValueError, TypeError):
            # Fall back to parsing a full JSON envelope just in case --jq is
            # unavailable on an older gh.
            try:
                return int(json.loads(out)["data"]["search"]["issueCount"])
            except (ValueError, TypeError, KeyError):
                return None

    def latest_run(self, repo: str, workflow: str = "", branch: str = "",
                   timeout: int = RUN_TIMEOUT):
        """Most recent GitHub Actions run for a repo (optionally filtered).

        Returns the run dict (status/conclusion/workflowName/headBranch/url/
        displayTitle), an empty dict `{}` when the repo has no matching runs,
        or None on any failure — letting the caller tell "no runs" apart from
        "couldn't fetch".
        """
        if not repo:
            return None
        args = [
            "run", "list", "-R", repo, "--limit", "1",
            "--json", "status,conclusion,workflowName,headBranch,displayTitle,url,databaseId",
        ]
        if workflow:
            args += ["--workflow", workflow]
        if branch:
            args += ["--branch", branch]
        ok, out, err = self._run(args, timeout=timeout)
        if not ok:
            self._check_rate_limit(err)
            return None
        if not out.strip():
            return None
        try:
            data = json.loads(out)
        except (ValueError, TypeError):
            return None
        if not data:
            return {}
        return data[0]

    def run_step_progress(self, repo: str, run_id, timeout: int = RUN_TIMEOUT):
        """(completed_steps, total_steps) for a run, aggregated across its jobs.

        Returns None on failure or when no steps are known yet. Counts every
        step GitHub reports (including the implicit set-up/complete steps), so
        it matches what the Actions UI shows per job. While a run is still
        going, the total grows as not-yet-started jobs register their steps.
        """
        if not repo or not run_id:
            return None
        ok, out, err = self._run(
            ["run", "view", str(run_id), "-R", repo, "--json", "jobs"],
            timeout=timeout,
        )
        if not ok:
            self._check_rate_limit(err)
            return None
        if not out.strip():
            return None
        try:
            data = json.loads(out)
        except (ValueError, TypeError):
            return None
        steps = [
            step
            for job in (data.get("jobs") or [])
            for step in (job.get("steps") or [])
        ]
        if not steps:
            return None
        total = len(steps)
        completed = sum(1 for s in steps if s.get("status") == "completed")
        return completed, total

    def latest_deployment_id(self, repo: str, environment: str = "",
                             timeout: int = DEPLOY_TIMEOUT):
        """Newest deployment of `environment` as (deployment_id, error).

        `deployment_id` is the empty string when the repo has no deployment for
        that environment (or none at all); `error` is a message, or None. The
        two are told apart so the caller can show "no deployment" instead of a
        failure. Raises RateLimitError like the other reads.
        """
        if not repo:
            return "", "no repo"
        args = ["api", f"repos/{repo}/deployments", "-X", "GET", "-f", "per_page=1"]
        if environment:
            args += ["-f", f"environment={environment}"]
        args += ["--jq", '.[0].id // ""']
        ok, out, err = self._run(args, timeout=timeout)
        if not ok:
            self._check_rate_limit(err)
            return "", (err or "").strip() or "failed"
        return out.strip(), None

    def deployment_state(self, repo: str, deployment_id, timeout: int = DEPLOY_TIMEOUT):
        """Current state of one deployment as (state, error).

        `state` is GitHub's own vocabulary: "pending"/"in_progress"/"queued"
        while it runs, "success"/"failure"/"error"/"inactive" when it ends. An
        empty string means GitHub has not published a status yet. Raises
        RateLimitError like the other reads.
        """
        if not repo or not deployment_id:
            return "", "no deployment"
        ok, out, err = self._run(
            ["api", f"repos/{repo}/deployments/{deployment_id}/statuses",
             "-X", "GET", "-f", "per_page=1", "--jq", '.[0].state // ""'],
            timeout=timeout,
        )
        if not ok:
            self._check_rate_limit(err)
            return "", (err or "").strip() or "failed"
        return out.strip(), None

    def notification_count(self, repo: str = "", participating: bool = False,
                           reason: str = "",
                           timeout: int = COUNT_TIMEOUT) -> Optional[int]:
        """Number of unread notifications for the authenticated user.

        Scoped to `repo` (owner/name) when given, else across all repos. With
        `participating` only threads the user is directly involved in count.
        `reason` filters to a single notification reason (e.g. "assign",
        "review_requested", "mention", "team_mention") client-side, mirroring
        the inbox's Filters — the REST API can't filter by reason server-side.
        Returns None on any failure. The REST endpoint returns only unread
        threads by default, matching the inbox badge.
        """
        endpoint = f"/repos/{repo}/notifications" if repo else "/notifications"
        args = ["api", "--paginate", endpoint,
                "-X", "GET", "-f", "per_page=100"]
        if participating:
            args += ["-f", "participating=true"]
        # Emit one line per matching thread id so pagination just accumulates
        # lines. A reason filter is a jq select on each thread's `.reason`.
        if reason:
            args += ["--jq", f'.[] | select(.reason == "{reason}") | .id']
        else:
            args += ["--jq", ".[].id"]
        ok, out, err = self._run(args, timeout=timeout)
        if not ok:
            self._check_rate_limit(err)
            return None
        return sum(1 for line in out.splitlines() if line.strip())

    # ------------------------------------------------------------------ #
    # Side effects
    # ------------------------------------------------------------------ #
    def open_in_browser(self, url: str) -> None:
        """Open `url` in the host's default browser; fire-and-forget."""
        if not url:
            return

        def worker():
            if self._sandboxed:
                cmd = ["flatpak-spawn", "--host", "--directory=/", "xdg-open", url]
            else:
                cmd = [shutil.which("xdg-open") or "xdg-open", url]
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            except Exception as e:  # never let a browser launch crash a key press
                log.error("[github] open_in_browser failed: {}", e)

        threading.Thread(target=worker, daemon=True).start()
