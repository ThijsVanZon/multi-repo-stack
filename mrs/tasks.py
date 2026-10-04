"""Task branches of a lifecycle repository's active line: creation from the target's identified current dev,
and validation against the target's current state. pstack owns PRs, stacks, review and landing; this module
owns only the lifecycle policy they rely on.

A task branch is `<active VERSION>/<task>`, with a lowercase kebab-case task name that starts with a letter. A
root task targets dev. A stacked task targets a same-line parent task branch on the target and contains that
parent's current tip. Every task descends from the commit that opened the active line (the latest release's
next-line commit), or, before a first release, shares history with dev. It keeps the active VERSION and a
lifecycle selector for the same repository. A verdict describes the target as observed when it was made, so
check again immediately before landing.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import config, git, state, versions

TASK = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


class Refused(Exception):
    """The target or the request does not support a lifecycle task operation. Nothing was changed."""


def _active(observed: state.State) -> versions.Version:
    """The active line of a supported lifecycle target."""
    if observed.kind == "EMPTY":
        raise Refused("the target is empty, so there is no dev to work from; bootstrap comes first")
    if observed.applicability not in (None, "lifecycle"):
        raise Refused(f"{observed.repository} selects {observed.applicability!r} at dev; lifecycle task operations "
                      "are refused, and a context-only repository keeps its own branch rules")
    if observed.kind != "LIFECYCLE":
        raise Refused("the target's lifecycle state is unsupported: " + "; ".join(observed.problems))
    return observed.active


def name_problem(branch: str, active: versions.Version) -> str | None:
    prefix, _, task = branch.partition("/")
    if prefix != str(active) or not TASK.fullmatch(task):
        return (f"{branch!r} is not a task branch of the active line: expected {active}/<task>, with <task> in "
                "lowercase kebab-case starting with a letter")
    return None


def _local(path: Path) -> None:
    if not path.is_dir() or not git.run(["-C", str(path), "rev-parse", "--git-dir"]).ok:
        raise Refused(f"{path} is not a local Git repository")


def create(store: Path, repository: str, checkout: Path, task: str) -> dict:
    """Create `<active VERSION>/<task>` in the local working `checkout` at the target's current dev, expecting
    the branch to be absent here and on the target. Only that branch, its reflog and the objects it needs are
    written: the caller's HEAD, index, working tree, configuration and other refs stay untouched."""
    if not TASK.fullmatch(task):
        raise Refused(f"task name {task!r} must be lowercase kebab-case starting with a letter; the active "
                      "version prefix is added for you")
    _local(checkout)
    if git.run(["-C", str(checkout), "rev-parse", "--is-bare-repository"]).out != "false":
        raise Refused(f"{checkout} is not a working checkout; task branches are created only in local work")
    observed = state.observe(store, repository)
    branch = f"{_active(observed)}/{task}"
    ref = f"refs/heads/{branch}"
    if ref in observed.refs:
        raise Refused(f"{branch} already exists on the target; choose another task name")
    if git.run(["-C", str(checkout), "show-ref", "--verify", "--quiet", ref]).ok:
        raise Refused(f"{branch} already exists in {checkout}; it is left unchanged")
    fetched = git.run(["-C", str(checkout), "-c", "gc.auto=0", "-c", "maintenance.auto=false", "fetch", "--quiet",
                       "--no-tags", "--no-write-fetch-head", "--recurse-submodules=no", "--", str(store),
                       f"{state.OBSERVED}heads/dev"], env=git.transport_only(store))
    if not fetched.ok:
        raise git.GitError(f"dev {observed.dev} could not be brought into {checkout}: {fetched.err}")
    # An empty old value: Git creates the branch only if it still does not exist.
    created = git.run(["-C", str(checkout), "update-ref", "-m", f"mrs task create: dev {observed.dev}", ref,
                       observed.dev, ""])
    if not created.ok:
        raise Refused(f"{branch} was not created in {checkout}: {created.err}")
    return {"repository": observed.repository, "version": str(observed.active), "branch": branch,
            "commit": observed.dev}


def check(store: Path, repository: str, checkout: Path, branch: str, commit: str | None, base: str) -> dict:
    """Validate task `branch` at `commit` (default: that branch in `checkout`) against the target's current
    state, as a PR from it to `base` (dev, or a same-line parent task branch on the target) would land."""
    _local(checkout)
    head = git.run(["-C", str(checkout), "rev-parse", "--verify", "--quiet", "--end-of-options",
                    f"{commit or 'refs/heads/' + branch}^{{commit}}"])
    if not head.ok:
        raise Refused(f"{commit or branch} is not a commit in {checkout}")
    task = head.out
    refs = state.snapshot(store, repository)
    parent = f"refs/heads/{base}"
    state.fetch(store, repository, refs, extra=(parent,))
    observed = state.analyze(store, refs)
    active = _active(observed)
    # The store reads the task's history from the caller's object database, which it never writes to.
    objects = (checkout / git.check(["-C", str(checkout), "rev-parse", "--git-path", "objects"]).out).resolve()
    (store / "objects" / "info").mkdir(parents=True, exist_ok=True)
    with open(store / "objects" / "info" / "alternates", "ab") as handle:
        handle.write(str(objects).encode("utf-8") + b"\n")
    if git.object_type(store, task) != "commit":
        raise state.Unknown(f"the task commit {task} cannot be read from {checkout}")
    problems = [problem for problem in [name_problem(branch, active)] if problem]
    try:
        found = versions.parse_file(git.read_file(store, task, "VERSION") or b"")
        if found != active:
            problems.append(f"the task changes VERSION to {found}; ordinary tasks keep the active line {active}")
    except (versions.VersionError, git.GitError) as exc:
        problems.append(f"VERSION at the task is unusable: {exc}")
    try:
        selection = config.at_commit(store, task)
        if selection.applicability != "lifecycle" or selection.repository != observed.repository:
            problems.append(f"the task's {config.FILENAME} no longer selects the lifecycle for {observed.repository}")
    except (config.ConfigError, git.GitError) as exc:
        problems.append(f"the task's {config.FILENAME} is unusable: {exc}")
    opening = observed.releases[-1] if observed.releases else None
    if opening is not None:
        if not state.ancestor(store, opening.next_commit, task):
            problems.append(f"the task does not descend from {opening.next_commit}, which opened line {active} "
                            f"after release {opening.version}; port earlier work explicitly onto a new "
                            f"{active}/<task> branch")
    else:
        shared = git.run(["-C", str(store), "merge-base", observed.dev, task])
        if shared.returncode not in (0, 1):
            raise state.Unknown(f"missing history: {shared.err}")
        if shared.returncode == 1:
            problems.append("the task shares no history with dev")
    if base == "dev":
        base_commit = observed.dev
    elif name_problem(base, active) or base == branch:
        base_commit = None
        problems.append(f"a task targets dev or a same-line parent task branch, not {base!r}")
    else:
        base_commit = refs.get(parent)
        if base_commit is None:
            problems.append(f"the parent task branch {base} does not exist on the target")
        elif not state.ancestor(store, base_commit, task):
            problems.append(f"the task does not contain {base_commit}, the current tip of its parent {base}; "
                            "restack it onto that tip")
    return {"status": "INVALID" if problems else "VALID", "problems": problems,
            "repository": observed.repository, "version": str(active), "branch": branch, "commit": task,
            "base": base, "base_commit": base_commit, "dev": observed.dev}
