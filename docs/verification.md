# Verification recipe (one recipe for every harness)

Method: read these from the pstack checkout that preflight verified:

- `skills/principle-prove-it-works/SKILL.md`;
- `skills/principle-test-behavior-not-implementation/SKILL.md`;
- the doctor, drive, evidence and cleanup structure of `skills/create-verification-skill/SKILL.md`.

## Doctor

You need:

- Python 3.11 or newer, available as `python` on PATH;
- Git on PATH;
- a clean, byte-exact checkout of the pinned cursor/plugins commit. Clone it with `git clone --config core.autocrlf=false`, then check out the pinned commit.

From this directory, `python -I -B mrs preflight --pstack <that checkout>` must print `PREFLIGHT OK`. No minimum Git version is claimed; this revision was exercised with Git 2.37.3.windows.1.

## Suite

From this directory, set `MRS_PSTACK_CHECKOUT` to the pstack checkout, then run:

```text
python -I -B tests
```

This is unittest discovery over `tests/`, and it is this project's declared `suite` check. Without `MRS_PSTACK_CHECKOUT`, the selection tests fail rather than skip.

## This project's exact-commit record

`collect` judges a repository's `dev`. To collect for the commit under test:

1. Publish that commit to a task-owned disposable bare repository, using only the file transport.
2. Run `collect` from a clean checkout of that same commit, with `MRS_PSTACK_CHECKOUT` set as above.

```text
git init --bare <scratch>/integration.git
git -c protocol.allow=never -c protocol.file.allow=always push --no-follow-tags <scratch>/integration.git HEAD:refs/heads/dev
python -I -B mrs collect --repository <scratch>/integration.git --pstack <pstack checkout> --out <folder outside any checkout>/suite.json
python -I -B mrs assess --repository <scratch>/integration.git --pstack <pstack checkout> --record <folder>/suite.json
```

The record's evidence lists the tests run, failures, errors and skips. `assess` stays INCOMPLETE until both of these hold:

- every OS that `multi-repo-stack.json` names has a passing record for the same commit;
- a separately commissioned non-author verdict binds that exact commit.

Remove the scratch repository afterwards. Keep the record and its private logs.

The scratch repository's `dev` is a local mapping of the commit under test, made only to drive `collect`. The record is evidence about that exact commit. It does not show that the commit was ever the real target's `dev`, and it carries no acceptance.

## Hosted CI (prepared, not yet run)

`.github/workflows/checks.yml` has read-only permissions. It never pushes, tags, comments, merges, changes settings or states a verdict. It pins its actions by commit and reads the pstack pin from `multi-repo-stack.json`. Each matrix row is one native OS (Linux, Windows, macOS) on Python 3.11, the declared minimum, and runs the full suite once.

- **Pull requests** get task evidence. A `task` job runs `mrs task check` for the PR's head commit against the target's current state. Each row checks out the head commit, never GitHub's synthetic merge commit, and runs preflight and the suite. It keeps the runner facts, the preflight output and the suite summary. None of this is a record of an integrated C.
- **Pushes to `dev`** get integrated evidence. Each row runs `mrs collect` against the target itself, from a checkout of the pushed commit. If `dev` has moved on by then, collection refuses, because this checkout is no longer the observed `dev`; the later push's run covers the new commit. A record names its candidate, so a later `dev` never relabels it.
- **Retain the records.** Artifacts and logs expire. Download each row's `record.json` (for example `gh run download <run> --name <artifact>`) and keep it with the release evidence. Acceptance still needs passing records from all three OSes for the same commit, and the non-author verdict.
- A green PR run describes the target when it ran. CI is not a lock, and it proves nothing about GitHub's atomic release behavior. Check the task again immediately before landing.

## Evidence

- The unittest output.
- `collect` records and their logs.
- `assess` reports.

Each test's docstring names the contract row it exercises. Every assertion reads refs, objects, files and processes from real Git repositories.

## Fixtures

- Every test creates disposable repositories under the system temporary directory, with spaces and non-ASCII in their paths, and removes them afterwards. Set `MRS_KEEP_FIXTURES=1` to keep them.
- Tests enable ambient `push.followTags` and on-demand submodule pushing through a test-owned global Git configuration. Your own configuration is never touched.
- Every Git process a test starts, including child processes, may use only the file transport. The test-owned configuration sets `protocol.allow=never` with `protocol.file.allow=always`, and the test environment sets `GIT_ALLOW_PROTOCOL=file`. Adversarial redirections therefore stay among disposable local repositories.
- The acceptance tests build three tiny consumers from `tests/consumers`:
  - an implementation shape that builds and runs a program;
  - a research shape that checks a synthetic result;
  - a probe shape for the collection boundaries.
- Their verdicts are SIMULATED attestations written by test code.

## First-release gates this project keeps open

- **Runtime:** a passing `suite` record from native Windows, Linux and macOS for the same exact commit. The read-only workflow above is the intended route, once it is activated.
- **Agent integration:** fresh `AGENTS.md` entry in each of these:
  - Claude Code on native Windows;
  - Claude Code on native macOS;
  - Codex in the Linux/cloud environment.
- **Provider:** behavior established through separately authorized tests.
- **Independent verification:** a non-author verdict on the integrated release candidate.

`assess` enforces the runtime pairs and the verdict. Agent and provider evidence reach acceptance only through that verdict, which must accept or reject each reused observation.

## What a pass does not prove

- GitHub or provider behavior.
- Operating systems other than the one that ran.
- Agent integration.
- Real acceptance. Fixture receipts and verdicts are SIMULATED.
