"""Read-only commands. Slice 1 exposes no mutation command."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from . import git, sources, state

OK, REFUSED, UNKNOWN, INVALIDATED = 0, 3, 4, 5


def _remove(path: str) -> None:
    def retry(function, target, _info):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    shutil.rmtree(path, onerror=retry) if sys.version_info < (3, 12) else shutil.rmtree(path, onexc=retry)


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


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # paths may be non-ASCII; never depend on the console code page
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(prog="mrs", description="multi-repo-stack (Slice 1: read-only commands)")
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
    args = parser.parse_args(argv)
    return args.handler(args)
