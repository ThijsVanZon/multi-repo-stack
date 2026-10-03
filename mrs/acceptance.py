"""Acceptance assessment of one exact candidate C: check records plus a separate non-author verdict.

Passing checks are check evidence only. An assessment is SUFFICIENT when every (check, environment) pair
that C's own criteria require has a passing record from a runner on that environment's native OS, every
record binds this repository, C, N, C's criteria identity and the selected shared/pstack sources, and a PASS
verdict from a declared non-author binds the same C, N and criteria, addresses criteria changes since the
previous release (or the frozen intent at a first release), decides every proposed reused observation and
leaves no limitation unresolved. Anything absent is INCOMPLETE; anything contradictory, failed or unbound is
REFUSED. These are content and binding checks for trusted operators: the tool cannot authenticate who wrote a
verdict or whether its statements are true.

A record counts only with the facts of an actual execution: C observed as dev at a UTC time, the runner's OS,
platform and runtimes, an exact checkout of C, the pstack source, and for each passing result the executable,
its start and finish times and its output identity. Receipt replay applies the same record validation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import config, sources

ACCEPTANCE_FORMAT = "multi-repo-stack/acceptance/1"
ATTESTATION_FORMAT = "multi-repo-stack/attestation/1"
RECORD_FORMAT = "multi-repo-stack/checks/1"
MAX_EVIDENCE_BYTES = 64 * 1024
SUFFICIENT, INCOMPLETE, REFUSED = "SUFFICIENT", "INCOMPLETE", "REFUSED"

_RECORD_KEYS = {"format", "repository", "candidate", "version", "criteria", "observed", "shared", "pstack", "runner",
                "checkout", "results", "unmet"}
_RESULT_KEYS = {"check", "environment", "argv", "executable", "started", "finished", "exit", "output", "evidence",
                "outcome", "reason"}
_ATTESTATION_KEYS = {"format", "repository", "candidate", "version", "criteria", "verifier", "behavior", "evidence",
                     "reuse", "limitations", "verdict"}
_OBSERVATION_KEYS = {"id", "source_commit", "harness", "scope"}
_RUNNER_KEYS = {"os", "wsl", "platform", "python", "git"}
_PSTACK_KEYS = {"repository", "commit", "path", "tree"}
_DEV = "refs/heads/dev"  # where collection observes C (state.DEV; state imports this module)
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_OID = re.compile(r"[0-9a-f]{40,}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass
class Assessment:
    status: str
    candidate: str
    problems: list[str] = field(default_factory=list)  # why it is REFUSED
    missing: list[str] = field(default_factory=list)  # what keeps it INCOMPLETE
    payload: dict | None = None  # the receipt's acceptance, only when SUFFICIENT


def _json(data: bytes, what: str):
    def no_duplicates(pairs):
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError(f"duplicate keys in {what}")
        return dict(pairs)
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{what} is not UTF-8 JSON: {exc}") from None


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sha256(value) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _utc(value) -> bool:
    """A UTC time exactly as collection writes it, so that such times also order as text."""
    try:
        return isinstance(value, str) and datetime.strptime(value, _STAMP).strftime(_STAMP) == value
    except ValueError:
        return False


def _evidence_problem(evidence, required: bool) -> str | None:
    if evidence == {"present": False}:
        return "required evidence is absent" if required else None
    if not isinstance(evidence, dict) or set(evidence) != {"present", "bytes", "sha256", "text"} \
            or evidence["present"] is not True or not isinstance(evidence["text"], str):
        return "evidence is malformed"
    try:
        data = evidence["text"].encode("utf-8")
    except UnicodeEncodeError:
        return "evidence is not UTF-8 text"
    if len(data) > MAX_EVIDENCE_BYTES or evidence["bytes"] != len(data) \
            or evidence["sha256"] != hashlib.sha256(data).hexdigest():
        return "evidence bytes do not match their recorded size and SHA-256, or exceed the limit"
    return None


def _provenance(record) -> list[str]:
    """Problems with the facts that every result of a record depends on. Without them no result counts."""
    problems = []
    observed, runner, pstack = record["observed"], record["runner"], record["pstack"]
    if (not isinstance(observed, dict) or set(observed) != {"ref", "at"} or observed["ref"] != _DEV
            or not _utc(observed["at"])):
        problems.append("C's observation as dev at a UTC time is missing")
    if (not isinstance(runner, dict) or set(runner) != _RUNNER_KEYS or runner["os"] not in config.NATIVE_OS
            or not isinstance(runner["wsl"], bool) or (runner["wsl"] and runner["os"] != "linux")
            or not all(_text(runner[key]) for key in ("platform", "python", "git"))):
        problems.append("the runner does not identify its actual OS, platform, Python and Git")
    elif record["checkout"] != {"bytes": "exact",
                                "executable_bits": "not represented" if runner["os"] == "windows" else "verified"}:
        problems.append(f"the checkout of C is not recorded as exact, as a {runner['os']} runner verifies it")
    if not _source(pstack):
        problems.append("the pstack source is not identified")
    return problems


def _source(pstack) -> bool:
    """A pstack source identity as collection records it: repository, commit, subtree path and tree."""
    return (isinstance(pstack, dict) and set(pstack) == _PSTACK_KEYS
            and all(isinstance(pstack[key], str) for key in _PSTACK_KEYS)
            and _text(pstack["repository"]) and _text(pstack["path"])
            and bool(_OID.fullmatch(pstack["commit"])) and bool(_OID.fullmatch(pstack["tree"])))


def _execution(result, observed_at: str | None) -> str | None:
    """Why a result recorded as passing does not show an actual execution after C was observed (a missing
    observation is reported once, by _provenance)."""
    executable, output = result["executable"], result["output"]
    started, finished = result["started"], result["finished"]
    if not isinstance(executable, dict) or set(executable) != {"name", "sha256"} or not _text(executable["name"]) \
            or not _sha256(executable["sha256"]):
        return "the executable that ran is not identified"
    if not (_utc(started) and _utc(finished) and started <= finished
            and (observed_at is None or observed_at <= started)):
        return "no UTC start and finish after C was observed"
    if not isinstance(output, dict) or set(output) != {"bytes", "sha256"} or type(output["bytes"]) is not int \
            or output["bytes"] < 0 or not _sha256(output["sha256"]):
        return "its output is not identified"
    if result["reason"] is not None:
        return f"a passing result states a reason: {result['reason']!r}"
    return None


def _record(record, selection: config.Selection, facts: dict) -> tuple[list[str], set]:
    """Problems with one check record, and the (check, environment) pairs it shows passing."""
    if not isinstance(record, dict) or set(record) != _RECORD_KEYS or record["format"] != RECORD_FORMAT:
        return [f"not a complete {RECORD_FORMAT} record"], set()
    problems, passed = [], set()
    for key in ("repository", "candidate", "version", "criteria", "shared", "pstack"):
        if key in facts and record[key] != facts[key]:
            problems.append(f"{key} is {record[key]!r}, but the assessed candidate has {facts[key]!r}")
    provenance = _provenance(record)
    problems += provenance
    runner = record["runner"].get("os") if isinstance(record["runner"], dict) else None
    at = record["observed"].get("at") if isinstance(record["observed"], dict) else None
    if not isinstance(record["results"], list):
        return problems + ["results are malformed"], passed
    for result in record["results"]:
        if not isinstance(result, dict) or set(result) != _RESULT_KEYS:
            problems.append("a result is malformed")
            continue
        name, label = result["check"], result["environment"]
        check = selection.checks.get(name) if isinstance(name, str) else None
        if check is None or label not in check.environments:
            problems.append(f"{name}/{label} is not a check/environment pair that the candidate's criteria require")
            continue
        where, required_os = f"{name}/{label}", selection.environments[label]
        if runner != required_os:
            problems.append(f"{where} requires {required_os}, but this record's runner observed {runner!r}")
        elif result["argv"] != list(check.argv):
            problems.append(f"{where} ran {result['argv']!r}, not the candidate's declared command")
        elif result["outcome"] != "pass":
            problems.append(f"{where} did not pass: {result['outcome']!r} ({result['reason']})")
        elif type(result["exit"]) is not int or result["exit"] != 0:
            problems.append(f"{where} is recorded as passing with exit status {result['exit']!r}")
        elif problem := (_execution(result, at if _utc(at) else None)
                         or _evidence_problem(result["evidence"], check.evidence_required)):
            problems.append(f"{where}: {problem}")
        elif not provenance:
            passed.add((name, label))
    return problems, passed


def _observation_problem(item) -> str | None:
    if (not isinstance(item, dict) or set(item) != _OBSERVATION_KEYS
            or not all(_text(item[key]) for key in ("id", "harness", "scope"))
            or not isinstance(item["source_commit"], str) or not _OID.fullmatch(item["source_commit"])):
        return f"reused observation {item!r} must name its id, exact source commit, harness and scope"
    return None


def _verdict(attestation, facts: dict, previous: str | None, proposals: list | None) -> list[str]:
    """Problems that keep `attestation` from being a PASS non-author verdict on exactly this candidate."""
    if (not isinstance(attestation, dict) or set(attestation) != _ATTESTATION_KEYS
            or attestation["format"] != ATTESTATION_FORMAT):
        return [f"the verdict is not a complete {ATTESTATION_FORMAT} bound to an inspected candidate"]
    problems = [f"the verdict inspected {key} {attestation[key]!r}, not {facts[key]!r}"
                for key in ("repository", "candidate", "version") if attestation[key] != facts[key]]
    criteria = attestation["criteria"]
    if not isinstance(criteria, dict) or set(criteria) != {"identity", "previous", "assessment"}:
        problems.append("the verdict does not state the criteria it inspected")
    else:
        if criteria["identity"] != facts["criteria"]:
            problems.append(f"the verdict addresses criteria {criteria['identity']!r}, not {facts['criteria']!r}")
        if criteria["previous"] != previous:
            problems.append(f"the verdict compares with criteria {criteria['previous']!r}, but the previous release "
                            f"has {previous!r}" + (" (a first release compares with the frozen intent)"
                                                   if previous is None else ""))
        if not _text(criteria["assessment"]):
            problems.append("the verdict does not address criteria changes since the previous release or intent")
    verifier = attestation["verifier"]
    if not isinstance(verifier, dict) or set(verifier) != {"relationship", "statement", "harness"} \
            or not _text(verifier["statement"]) or not _text(verifier["harness"]):
        problems.append("the verdict does not describe its verifier's non-authorship and actual harness")
    elif verifier["relationship"] != "non-author":
        problems.append(f"a verdict from {verifier['relationship']!r} is not a non-author verdict")
    for key in ("behavior", "evidence"):
        if not _text(attestation[key]):
            problems.append(f"the verdict does not describe the {key} it inspected")
    limitations = attestation["limitations"]
    if not isinstance(limitations, list) or not all(
            isinstance(item, dict) and set(item) == {"statement", "resolution"} and _text(item["statement"])
            and item["resolution"] in ("accepted", "unresolved") for item in limitations):
        problems.append("limitations must each be a statement resolved as 'accepted' or 'unresolved'")
    else:
        problems += [f"unresolved limitation: {item['statement']}" for item in limitations
                     if item["resolution"] == "unresolved"]
    reuse = attestation["reuse"]
    if not isinstance(reuse, list) or not all(
            isinstance(item, dict) and set(item) == {"observation", "decision", "rationale"}
            and item["decision"] in ("accepted", "rejected") and _text(item["rationale"]) for item in reuse):
        problems.append("reuse decisions must each accept or reject one observation with a rationale")
    else:
        problems += [problem for item in reuse if (problem := _observation_problem(item["observation"]))]
        decided = [item["observation"] for item in reuse]
        if proposals is not None:
            problems += [f"proposed reuse {item['id']!r} is not decided by the verdict"
                         for item in proposals if item not in decided]
            problems += [f"the verdict decides reuse {item.get('id')!r} that was not proposed"
                         for item in decided if isinstance(item, dict) and item not in proposals]
        if any(decided.count(item) > 1 for item in decided):
            problems.append("a reused observation is decided more than once")
    if attestation["verdict"] != "PASS":
        problems.append(f"the non-author verdict is {attestation['verdict']!r}, not PASS")
    return problems


def _missing(selection: config.Selection, passed: set) -> list[str]:
    return [f"no passing record for {name}/{label} on {selection.environments[label]}"
            for name, check in sorted(selection.checks.items()) for label in check.environments
            if (name, label) not in passed]


def previous_criteria(store, release) -> str | None:
    """Criteria identity of `release`, the contract release before a candidate, against which the candidate's
    criteria changes are judged; None at a first release, which is judged against the frozen intent."""
    return config.at_commit(store, release.candidate).criteria if release is not None else None


def assess(store, observed, candidate: str, records: list[bytes], attestation: bytes | None,
           proposals: bytes | None, pstack) -> Assessment:
    """Assess exactly `candidate`, which must be the observed dev of a supported lifecycle state, from
    collected records, an optional verdict and optional owner-proposed reused observations. The running tool
    and `pstack` must be the sources C selects, as in collection."""
    result = Assessment(REFUSED, candidate)
    if observed.kind != "LIFECYCLE" or observed.dev != candidate:
        result.problems.append(f"the candidate must be dev of a supported lifecycle state; dev is {observed.dev}: "
                               + "; ".join(observed.problems))
        return result
    try:
        selection = config.at_commit(store, candidate)
        if selection.checks is None:
            raise config.ConfigError(f"{candidate} declares no lifecycle checks in {config.FILENAME}")
        shared, pinned = sources.selected(selection, candidate, pstack)
        previous = previous_criteria(store, observed.releases[-1] if observed.releases else None)
    except (config.ConfigError, sources.SourceError) as exc:
        result.problems.append(str(exc))
        return result
    facts = {"repository": selection.repository, "candidate": candidate, "version": str(observed.active),
             "criteria": selection.criteria, "shared": shared, "pstack": pinned}
    parsed, passed = [], set()
    for index, data in enumerate(records):
        try:
            record = _json(data, f"record {index + 1}")
        except ValueError as exc:
            result.problems.append(str(exc))
            continue
        problems, ok = _record(record, selection, facts)
        result.problems += [f"record {index + 1}: {problem}" for problem in problems]
        parsed.append(record)
        passed |= ok
    result.missing += _missing(selection, passed)
    try:
        proposed = _json(proposals, "proposed reuse") if proposals is not None else []
        if not isinstance(proposed, list):
            raise ValueError("proposed reuse must be a list of observations")
        malformed = [problem for item in proposed if (problem := _observation_problem(item))]
        if malformed:
            raise ValueError("; ".join(malformed))
    except ValueError as exc:
        result.problems.append(str(exc))
        proposed = []
    verdict = None
    if attestation is None:
        result.missing.append("no non-author verdict on exact C"
                              + (f"; {len(proposed)} proposed reused observation(s) undecided" if proposed else ""))
    else:
        try:
            verdict = _json(attestation, "verdict")
            result.problems += _verdict(verdict, facts, previous, proposed)
        except ValueError as exc:
            result.problems.append(str(exc))
    if result.problems:
        return result
    if result.missing:
        result.status = INCOMPLETE
        return result
    result.status = SUFFICIENT
    result.payload = {"format": ACCEPTANCE_FORMAT, "criteria": {"identity": selection.criteria, "previous": previous},
                      "results": parsed, "verdict": verdict}
    return result


def payload_problems(store, candidate: str, version: str, repository: str, payload: dict) -> list[str]:
    """Why an assessed acceptance read back from a receipt does not hold for exactly this C, N and repository.
    Records and verdict are validated again against the facts C itself provides: its criteria, its shared pin
    and, when C pins pstack itself (as the shared project does), that pin. A consumer's pstack pin belongs to
    the shared revision and every pstack tree to a checkout; an observer has neither, so it checks those only
    for form and for agreement across records. The caller binds `previous` to the retained release history."""
    if set(payload) != {"format", "criteria", "results", "verdict"} or payload["format"] != ACCEPTANCE_FORMAT:
        return [f"not a complete {ACCEPTANCE_FORMAT} acceptance"]
    selection = config.at_commit(store, candidate)
    if selection.checks is None:
        return [f"{candidate} declares no lifecycle checks"]
    criteria = payload["criteria"]
    if not isinstance(criteria, dict) or set(criteria) != {"identity", "previous"} \
            or criteria["identity"] != selection.criteria:
        return [f"the acceptance does not bind the candidate's criteria {selection.criteria}"]
    records = payload["results"] if isinstance(payload["results"], list) else []
    facts = {"repository": repository, "candidate": candidate, "version": version, "criteria": selection.criteria,
             "shared": sources.shared_identity(selection, candidate)}
    problems, passed = [], set()
    for record in records:
        found, ok = _record(record, selection, facts)
        problems += found
        passed |= ok
    used = [record["pstack"] for record in records if isinstance(record, dict) and _source(record.get("pstack"))]
    if any(source != used[0] for source in used):
        problems.append("the records name different pstack sources")
    pin = selection.pstack
    if pin is not None and any((source["repository"], source["commit"], source["path"])
                               != (pin.repository, pin.commit, pin.path) for source in used):
        problems.append(f"a record's pstack source is not {pin.repository}@{pin.commit} ({pin.path}), which C pins")
    return problems + _missing(selection, passed) + _verdict(payload["verdict"], facts, criteria["previous"], None)
