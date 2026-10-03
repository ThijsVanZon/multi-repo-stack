"""Commands: preflight, inspect, collect, assess and task. They are read-only toward every target. The only write
is `task create`, which adds one new branch to the caller's local checkout. There is no publication command."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import acceptance, collect, git, sources, state, tasks

OK, NOT_PASSED, REFUSED, UNKNOWN, INVALIDATED = 0, 1, 3, 4, 5
_remove = collect.remove_tree


def _print(report: dict, as_json: bool, lines: list[str]) -> None:
    print(json.dumps(report, indent=2, sort_keys=True) if as_json else "\n".join(lines))


def _selection(report: dict) -> dict:
    """Identities a run depends on. A consumer's own HEAD moves during ordinary task work and is
    reported, not compared. The executing shared source is compared exactly, including the shared
    project's own checkout when it checks itself."""
    consumer = report.get("consumer", {})
    return {"repository": consumer.get("repository"), "applicability": consumer.get("applicability"),
            "shared": report.get("shared"), "pstack": report.get("pstack"), "load": report.get("load")}


def _record(path: str) -> dict:
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"the earlier preflight record {path} cannot be read: {exc}") from None
    if (not isinstance(record, dict) or record.get("verdict") != "OK"
            or not all(isinstance(record.get(key), dict) for key in ("consumer", "shared", "pstack"))):
        raise ValueError(f"{path} is not the --json output of a successful preflight")
    return record


def _preflight(args) -> int:
    try:
        report = sources.preflight(Path(args.consumer), Path(args.pstack) if args.pstack else None)
    except (sources.SourceError, git.GitError) as exc:
        report = {"verdict": "REFUSED", "reason": str(exc)}
        if args.expect:
            report = {"verdict": "INVALIDATED", "reason": f"selected sources no longer verify: {exc}"}
        _print(report, args.json, [f"PREFLIGHT {report['verdict']}: {report['reason']}"])
        return INVALIDATED if args.expect else REFUSED
    report["verdict"] = "OK"
    if args.expect:
        try:
            old, new = _selection(_record(args.expect)), _selection(report)
            changed = [key for key in old if old[key] != new[key]]
            reason = f"selection changed during the run: {', '.join(changed)}" if changed else None
        except ValueError as exc:
            reason = str(exc)
        if reason:
            report = {"verdict": "INVALIDATED", "reason": reason}
            _print(report, args.json, [f"PREFLIGHT INVALIDATED: {report['reason']}"])
            return INVALIDATED
    consumer, shared, pstack = report["consumer"], report["shared"], report["pstack"]
    _print(report, args.json, [
        "PREFLIGHT OK",
        f"consumer  {consumer['repository']}  applicability={consumer['applicability']}  commit={consumer['commit']}",
        f"shared    {shared['repository']}  commit={shared['commit']}" + ("  (this checkout)" if shared["self"] else ""),
        f"pstack    {pstack['repository']}  commit={pstack['commit']}  {pstack['path']} tree={pstack['tree']}",
        *[f"load      {doc}" for doc in report["load"]],
        f"runtime   {report['runtime']['git']} | Python {report['runtime']['python']} | {report['runtime']['os']}",
    ])
    return OK


def _inspect(args) -> int:
    scratch = tempfile.mkdtemp(prefix="mrs-inspect-")
    try:
        store = Path(scratch) / "observed.git"
        try:
            git.check(["init", "--quiet", "--bare", "--template=", str(store)])
            observed = state.observe(store, args.remote)
        except state.Redirected as exc:
            _print({"kind": "REFUSED", "reason": str(exc)}, args.json, [f"state     REFUSED: {exc}"])
            return REFUSED
        except (state.Unknown, git.GitError) as exc:
            _print({"kind": "UNKNOWN", "reason": str(exc)}, args.json, [f"state     UNKNOWN: {exc}"])
            return UNKNOWN
    finally:
        _remove(scratch)
    report = {
        "kind": observed.kind, "refs": observed.refs, "problems": observed.problems,
        "repository": observed.repository, "applicability": observed.applicability,
        "active": str(observed.active) if observed.active else None, "ignored_tags": observed.ignored_tags,
        "releases": [{"version": str(r.version), "tag": r.tag, "candidate": r.candidate, "next": r.next_commit,
                      "next_version": str(r.next_version)} for r in observed.releases],
    }
    _print(report, args.json, [
        f"state     {observed.kind}",
        *[f"ref       {ref} {oid}" for ref, oid in sorted(observed.refs.items())],
        *[f"release   {r['version']} tag={r['tag']} main-candidate={r['candidate']} next={r['next']} "
          f"({r['next_version']})" for r in report["releases"]],
        *([f"active    {report['active']}"] if report["active"] else []),
        *[f"problem   {problem}" for problem in observed.problems],
        *[f"ignored   tag {name}" for name in observed.ignored_tags],
    ])
    return OK


def _new_file(path: str) -> Path:
    """Records and logs stay outside every Git checkout, so evidence never becomes residue in C's source."""
    out = Path(path).resolve()
    if out.exists() or not out.parent.is_dir() or git.run(["-C", str(out.parent), "rev-parse", "--git-dir"]).ok:
        raise ValueError(f"{out} must be a new file in an existing folder outside any Git checkout")
    return out


def _collect(args) -> int:
    try:
        out = _new_file(args.out)
        record, outputs, invalidated = collect.collect(args.repository, Path(args.pstack) if args.pstack else None)
    except (ValueError, collect.Refused) as exc:
        _print({"status": "REFUSED", "reason": str(exc)}, args.json, [f"COLLECTION REFUSED: {exc}"])
        return REFUSED
    except (state.Unknown, git.GitError) as exc:
        _print({"status": "UNKNOWN", "reason": str(exc)}, args.json, [f"COLLECTION UNKNOWN: {exc}"])
        return UNKNOWN
    with open(out, "xb") as handle:
        handle.write(json.dumps(record, indent=2, sort_keys=True).encode("ascii") + b"\n")
    for pair, output in outputs.items():  # exact private output; the record holds its size and SHA-256
        with open(out.with_name(f"{out.name}.{pair}.log"), "xb") as handle:
            handle.write(output)
    passed = all(result["outcome"] == "pass" for result in record["results"])
    status = "INVALIDATED" if invalidated else "PASSED" if passed else "NOT PASSED"
    runner, shared, pstack = record["runner"], record["shared"], record["pstack"]
    _print({"status": status, "record_file": str(out), "record": record}, args.json, [
        f"CHECKS {status}: check evidence for one commit only; acceptance also needs every required "
        "check/environment pair and a non-author verdict",
        f"candidate {record['candidate']}  version={record['version']}  repository={record['repository']}",
        f"criteria  {record['criteria']}",
        f"runner    {runner['os']}{' (WSL)' if runner['wsl'] else ''}  {runner['platform']}  Python {runner['python']}"
        f"  {runner['git']}",
        f"shared    {shared['repository']}  commit={shared['commit']}",
        f"pstack    {pstack['repository']}  commit={pstack['commit']}  {pstack['path']} tree={pstack['tree']}",
        *[f"result    {r['check']}/{r['environment']}  {r['outcome']}" + (f"  ({r['reason']})" if r["reason"] else "")
          + (f"  {r['executable']['name']} sha256={r['executable']['sha256']}" if r["executable"] else "")
          for r in record["results"]],
        *[f"unmet     {u['check']}/{u['environment']} requires {u['os']}" for u in record["unmet"]],
        f"record    {out}",
    ])
    return INVALIDATED if invalidated else OK if passed else NOT_PASSED


def _assess(args) -> int:
    try:
        records = [Path(path).read_bytes() for path in args.record]
        attestation = Path(args.attestation).read_bytes() if args.attestation else None
        proposals = Path(args.reuse).read_bytes() if args.reuse else None
    except OSError as exc:
        _print({"status": "REFUSED", "reason": str(exc)}, args.json, [f"ASSESSMENT REFUSED: {exc}"])
        return REFUSED
    scratch = tempfile.mkdtemp(prefix="mrs-assess-")
    try:
        store = Path(scratch) / "observed.git"
        git.check(["init", "--quiet", "--bare", "--template=", str(store)])
        observed = state.observe(store, args.repository)
        result = acceptance.assess(store, observed, observed.dev, records, attestation, proposals,
                                   Path(args.pstack) if args.pstack else None)
    except state.Redirected as exc:
        _print({"status": "REFUSED", "reason": str(exc)}, args.json, [f"ASSESSMENT REFUSED: {exc}"])
        return REFUSED
    except (state.Unknown, git.GitError) as exc:
        _print({"status": "UNKNOWN", "reason": str(exc)}, args.json, [f"ASSESSMENT UNKNOWN: {exc}"])
        return UNKNOWN
    finally:
        _remove(scratch)
    meaning = {acceptance.SUFFICIENT: "every required pair passed and a PASS non-author verdict binds exact C",
               acceptance.INCOMPLETE: "not accepted; required evidence or the verdict is missing",
               acceptance.REFUSED: "not accepted; the evidence or verdict does not hold for exact C"}
    _print({"status": result.status, "candidate": result.candidate, "problems": result.problems,
            "missing": result.missing, "acceptance": result.payload}, args.json, [
        f"ASSESSMENT {result.status}: {meaning[result.status]}",
        f"candidate {result.candidate}",
        *[f"refused   {problem}" for problem in result.problems],
        *[f"missing   {item}" for item in result.missing],
    ])
    return {acceptance.SUFFICIENT: OK, acceptance.INCOMPLETE: NOT_PASSED}.get(result.status, REFUSED)


def _task(args) -> int:
    scratch = tempfile.mkdtemp(prefix="mrs-task-")
    try:
        store = Path(scratch) / "observed.git"
        git.check(["init", "--quiet", "--bare", "--template=", str(store)])
        checkout = Path(args.checkout).resolve()
        if args.task_command == "create":
            report = tasks.create(store, args.repository, checkout, args.task)
        else:
            report = tasks.check(store, args.repository, checkout, args.branch, args.commit, args.base)
    except (tasks.Refused, state.Redirected) as exc:
        _print({"status": "REFUSED", "reason": str(exc)}, args.json, [f"TASK REFUSED: {exc}"])
        return REFUSED
    except (state.Unknown, git.GitError) as exc:
        _print({"status": "UNKNOWN", "reason": str(exc)}, args.json, [f"TASK UNKNOWN: {exc}"])
        return UNKNOWN
    finally:
        _remove(scratch)
    if args.task_command == "create":
        report["status"] = "CREATED"
        _print(report, args.json, [f"TASK CREATED: {report['branch']} at dev {report['commit']} of "
                                   f"{report['repository']}, in {checkout}; nothing else changed"])
        return OK
    _print(report, args.json, [
        f"TASK {report['status']}: {report['branch']} at {report['commit']}, targeting {report['base']} "
        f"({report['base_commit']}), against dev {report['dev']} of {report['repository']} on line {report['version']}",
        *[f"problem   {problem}" for problem in report["problems"]],
        "This describes the target as observed now; check again immediately before landing.",
    ])
    return OK if report["status"] == "VALID" else NOT_PASSED


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # paths may be non-ASCII; never depend on the console code page
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(prog="mrs", description="multi-repo-stack (no command publishes anything)")
    commands = parser.add_subparsers(dest="command", required=True)
    pre = commands.add_parser("preflight", help="verify the consumer selection and selected shared/pstack checkouts")
    pre.add_argument("--consumer", default=".", help="consumer checkout root (default: current directory)")
    pre.add_argument("--pstack", help="clean checkout of the pinned canonical pstack repository")
    pre.add_argument("--expect", help="JSON from an earlier --json preflight; any change invalidates the run")
    pre.add_argument("--json", action="store_true")
    pre.set_defaults(handler=_preflight)
    ins = commands.add_parser("inspect", help="observe a target repository and classify its lifecycle state")
    ins.add_argument("--remote", required=True, help="path or URL of the target repository (read-only)")
    ins.add_argument("--json", action="store_true")
    ins.set_defaults(handler=_inspect)
    col = commands.add_parser("collect", help="run the declared checks of the observed dev's exact commit here")
    col.add_argument("--repository", required=True, help="path or URL of the lifecycle repository (read-only)")
    col.add_argument("--pstack", help="clean checkout of the pinned canonical pstack repository")
    col.add_argument("--out", required=True, help="new record file, outside any Git checkout")
    col.add_argument("--json", action="store_true")
    col.set_defaults(handler=_collect)
    ass = commands.add_parser("assess", help="assess check records and a non-author verdict for the observed dev")
    ass.add_argument("--repository", required=True, help="path or URL of the lifecycle repository (read-only)")
    ass.add_argument("--pstack", help="clean checkout of the pinned canonical pstack repository")
    ass.add_argument("--record", action="append", default=[], help="a check record from collect (repeatable)")
    ass.add_argument("--attestation", help="the separately commissioned non-author verdict on exact C")
    ass.add_argument("--reuse", help="owner-proposed reused agent observations, each to be decided by the verdict")
    ass.add_argument("--json", action="store_true")
    ass.set_defaults(handler=_assess)
    task = commands.add_parser("task", help="create or check a task branch of a lifecycle repository's active line")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    new = task_commands.add_parser("create", help="create <active version>/<task> in a local checkout at the "
                                                  "target's current dev; nothing else changes")
    new.add_argument("--repository", required=True, help="path or URL of the lifecycle repository (read-only)")
    new.add_argument("--checkout", default=".", help="local working checkout to add the branch to (default: .)")
    new.add_argument("--task", required=True, help="lowercase kebab-case task name, without the version prefix")
    new.add_argument("--json", action="store_true")
    chk = task_commands.add_parser("check", help="validate a task branch against the target's current state")
    chk.add_argument("--repository", required=True, help="path or URL of the lifecycle repository (read-only)")
    chk.add_argument("--checkout", default=".", help="local repository holding the task commit (default: .)")
    chk.add_argument("--branch", required=True, help="the task branch name, <active version>/<task>")
    chk.add_argument("--commit", help="the task commit (default: the branch in the checkout)")
    chk.add_argument("--base", default="dev", help="dev, or the same-line parent task branch of a stacked task")
    chk.add_argument("--json", action="store_true")
    task.set_defaults(handler=_task)
    args = parser.parse_args(argv)
    return args.handler(args)
