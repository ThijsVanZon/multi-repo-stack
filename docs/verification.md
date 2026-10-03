# Verification recipe (one recipe for every harness)

Method: the pinned pstack `skills/principle-prove-it-works/SKILL.md` and `skills/principle-test-behavior-not-implementation/SKILL.md`. Read them from the pstack checkout that preflight verified.

- **Prerequisites.** Python 3.11 or newer, and Git on PATH (Slice 1 exercised Git 2.37.3.windows.1; no minimum version is claimed). You also need a clean, byte-exact checkout of the pinned cursor/plugins commit (`git clone --config core.autocrlf=false`, then check out the pinned commit).
- **Run.** From this directory, set `MRS_PSTACK_CHECKOUT` to that checkout, then run `python -I -B -m unittest discover -s tests -t . -v`. Without `MRS_PSTACK_CHECKOUT` the selection tests fail rather than skip.
- **Evidence.** The unittest output. Each test docstring names the contract row it exercises, and every assertion reads refs and objects from real Git repositories.
- **Fixtures.** Every test creates disposable repositories under the system temporary directory, with spaces and non-ASCII in their paths, and removes them afterwards. Set `MRS_KEEP_FIXTURES=1` to keep them. Tests enable ambient `push.followTags` and on-demand submodule pushing through a test-owned global Git configuration. Your own configuration is never touched. Every Git process a test starts, including child processes, may use only the file transport: the test-owned configuration sets `protocol.allow=never` with `protocol.file.allow=always`, and the test environment sets `GIT_ALLOW_PROTOCOL=file`. Adversarial redirections therefore stay among disposable local repositories.
- **What a pass does not prove:** GitHub or provider behavior, operating systems other than the one that ran, agent integration, or real acceptance. Fixture receipts carry SIMULATED acceptance.
