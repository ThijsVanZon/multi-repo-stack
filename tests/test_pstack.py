import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from tests.consumer_support import PIN
from tests.support import GitTestCase, out, selector, shared_snapshot, tree_digest

TREES = {"claude-code": "harness/pstack/claude-code", "cursor": "pstack"}


def sync(*args) -> tuple[int, str, str]:
    proc = subprocess.run([sys.executable, "-I", "-B", str(shared_snapshot()[0] / "mrs"), "pstack", "sync",
                           *map(str, args)], capture_output=True)
    return proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")


class PstackDeliveryTests(GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.consumer = self.checkout("consumer")
        self.commit(self.consumer, {"multi-repo-stack.json": selector("example/app", "context", shared_snapshot()[1])},
                    "Connect to multi-repo-stack")
        self.cache = self.tmp / "cache"

    def run_sync(self, client: str, *extra, pstack: Path | None = None) -> tuple[int, dict]:
        code, text, err = sync("--consumer", self.consumer, "--pstack", pstack or self.pstack(), "--client", client,
                               "--cache", self.cache, "--json", *extra)
        return code, json.loads(text or err)

    def committed(self, subtree: str, mode: str | None = None) -> dict[str, str]:
        listing = out("-C", self.pstack(), "ls-tree", "-r", "--full-tree", PIN, "--", subtree)
        return {line.split("\t")[1][len(subtree) + 1:]: line.split()[2] for line in listing.splitlines()
                if mode in (None, line.split()[0])}

    def delivered(self, folder: Path) -> dict[str, str]:
        names = sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())
        hashed = out("-C", folder, "hash-object", "--no-filters", "--stdin-paths", input="\n".join(names).encode())
        return dict(zip(names, hashed.split("\n")))

    def test_each_client_gets_the_pinned_tree_byte_for_byte_and_its_own_handshake(self):
        """Delivery: the client's tree at the pin, rebuilt from Git objects, beside a handshake naming the exact
        shared and pstack commits; entries are content-addressed, so clients never share a path."""
        entries = {}
        for client, subtree in TREES.items():
            code, report = self.run_sync(client)
            self.assertEqual(code, 0, report)
            entry = Path(report["entry"])
            entries[client] = entry
            self.assertEqual(report["writes"], [str(entry)])
            self.assertEqual(entry.parent, self.cache.resolve() / "pstack" / client)
            self.assertEqual(self.delivered(entry / "pstack"), self.committed(subtree))
            if os.name != "nt":
                self.assertEqual({p.relative_to(entry / "pstack").as_posix() for p in (entry / "pstack").rglob("*")
                                  if p.is_file() and os.access(p, os.X_OK)}, set(self.committed(subtree, "100755")))
            line = f"MRS-HANDSHAKE client={client} multi-repo-stack={shared_snapshot()[1]} pstack={PIN}"
            self.assertEqual(report["handshake"], line)
            skill = (entry / "mrs-handshake" / "skills" / "mrs-handshake" / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(skill.endswith(f"\n{line}\n"), skill)
            self.assertEqual(report["plugin_paths"], [str(entry / "pstack"), str(entry / "mrs-handshake")])
        manifests = {client: sorted(p.name for p in (entry / "mrs-handshake").glob(".*-plugin"))
                     for client, entry in entries.items()}
        self.assertEqual(manifests, {"claude-code": [".claude-plugin"], "cursor": [".cursor-plugin"]})
        code, text, _ = sync("--consumer", self.consumer, "--pstack", self.pstack(), "--client", "claude-code",
                             "--cache", self.cache)
        paths = [str(entries["claude-code"] / name) for name in ("pstack", "mrs-handshake")]
        value = f"CLAUDE_CODE_PLUGIN_DIRS={os.pathsep.join(paths)}"  # the desktop app takes no flags
        self.assertTrue(any(line.endswith(value) for line in text.splitlines()), text)

    def test_sync_is_idempotent_and_writes_only_its_cache(self):
        """Isolation: a repeated sync writes nothing and re-verifies; consumer, pstack and the home folder (the
        default cache aside) stay byte-identical, so no client or account configuration is written."""
        home = self.tmp / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home), "XDG_CACHE_HOME": "",
                                          "LOCALAPPDATA": str(home / "AppData" / "Local")}):
            before = {path: tree_digest(path) for path in (self.consumer, self.pstack())}
            first = sync("--consumer", self.consumer, "--pstack", self.pstack(), "--client", "claude-code", "--json")
            second = sync("--consumer", self.consumer, "--pstack", self.pstack(), "--client", "claude-code", "--json")
        (code1, text1, _), (code2, text2, _) = first, second
        self.assertEqual((code1, code2), (0, 0), (text1, text2))
        report1, report2 = json.loads(text1), json.loads(text2)
        entry = Path(report1["entry"])
        self.assertEqual((report1["writes"], report2["writes"], report2["entry"]), ([str(entry)], [], str(entry)))
        cache = next(path for path in home.rglob("multi-repo-stack") if path.is_dir())
        self.assertEqual(entry.parent, cache.resolve() / "pstack" / "claude-code")  # macOS: /var is a link
        self.assertEqual([p for p in home.rglob("*") if p.is_file() and cache not in p.parents], [])
        self.assertEqual({path: tree_digest(path) for path in before}, before)

    def test_refusals_write_nothing_and_a_changed_entry_is_never_repaired(self):
        """Selection gate and integrity: an unverified pstack checkout, or a cache inside a checkout, is refused
        before anything is written; an entry changed after writing is refused, not rewritten."""
        stale = self.tmp / "stale pstack"
        out("clone", "--quiet", "--shared", "--config", "core.autocrlf=false", self.pstack(), stale)
        out("-C", stale, "commit", "--quiet", "--allow-empty", "-m", "Not the pin")  # CI fetches the pin alone
        code, report = self.run_sync("cursor", pstack=stale)
        self.assertEqual((code, report["status"]), (3, "REFUSED"), report)
        self.assertIn(f"but {PIN} is selected", report["reason"])
        for inside in (self.consumer / "cache", shared_snapshot()[0] / "cache"):
            code, text, _ = sync("--consumer", self.consumer, "--pstack", self.pstack(), "--client", "cursor",
                                 "--cache", inside, "--json")
            self.assertEqual(code, 3, text)
            self.assertIn("must be outside", json.loads(text)["reason"])
            self.assertFalse(inside.exists())
        self.assertFalse(self.cache.exists())

        code, report = self.run_sync("cursor")
        skill = Path(report["entry"]) / "pstack" / "skills" / "bro" / "SKILL.md"
        skill.write_bytes(skill.read_bytes() + b"\nchanged\n")
        code, report = self.run_sync("cursor")
        self.assertEqual(code, 3, report)
        self.assertIn("differs from its content digest", report["reason"])
        self.assertTrue(skill.read_bytes().endswith(b"\nchanged\n"))

    def test_cursor_hook_prints_only_plugin_paths_and_fails_open(self):
        """Entry hook: Cursor's workspaceOpen hook gets exactly {"pluginPaths": [...]}; a refusal loads nothing and
        still exits 0, so the workspace opens without the delivery. Only Cursor has a hook mode."""
        args = ("--consumer", self.consumer, "--client", "cursor", "--cache", self.cache, "--hook")
        code, text, _ = sync(*args, "--pstack", self.pstack())
        entry = next((self.cache / "pstack" / "cursor").iterdir())
        self.assertEqual((code, json.loads(text)),
                         (0, {"pluginPaths": [str(entry.resolve() / "pstack"), str(entry.resolve() / "mrs-handshake")]}))
        code, text, err = sync(*args)
        self.assertEqual((code, json.loads(text)), (0, {}))
        self.assertIn("PSTACK REFUSED: supply --pstack", err)
        code, _, err = sync("--consumer", self.consumer, "--client", "claude-code", "--hook")
        self.assertEqual(code, 2, err)  # Claude Code has no entry hook: the handshake line stays out of its context
