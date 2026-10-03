from mrs import transactions
from tests.support import DEV, MAIN, NOW, GitTestCase, git, has_object, out, refs, selector, shared_snapshot


class PublicationScopeTests(GitTestCase):
    """Ambient push.followTags=true and push.recurseSubmodules=on-demand are active for every test."""

    def source_with_submodule(self) -> tuple:
        sub_remote = self.bare("submodule remote.git", fixture=False)
        seed = self.checkout("submodule seed")
        self.commit(seed, {"lib.txt": "v1\n"}, "Published submodule commit")
        out("-C", seed, "push", "--quiet", sub_remote, "HEAD:refs/heads/main")
        out("-C", sub_remote, "symbolic-ref", "HEAD", "refs/heads/main")
        source = self.checkout("app source")
        self.commit(source, {"VERSION": "26.1.0\n", "README.md": "# Example app\n",
                             "multi-repo-stack.json": selector("example/app", "lifecycle", shared_snapshot()[1])},
                    "Prepare example app")
        out("-C", source, "tag", "-a", "unrelated-annotated", "-m", "unrelated reachable tag", "HEAD")
        out("-C", source, "submodule", "--quiet", "add", sub_remote, "lib")
        unpublished = self.commit(source / "lib", {"lib.txt": "v2 not yet published\n"}, "Unpublished submodule commit")
        b = self.commit(source, {}, "Pin unpublished submodule commit")
        self.assertFalse(has_object(sub_remote, unpublished))
        return source, b, sub_remote, unpublished

    def test_bootstrap_changes_only_dev(self):
        """Publication scope: bootstrap publishes dev only; no tag and no submodule repository change."""
        source, b, sub_remote, unpublished = self.source_with_submodule()
        target = self.bare("target.git")
        sub_before = refs(sub_remote)
        work = self.tmp / "bootstrap op"
        transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)
        out("-C", work / "repo.git", "fetch", "--quiet", "--no-tags", source,
            "refs/tags/unrelated-annotated:refs/tags/unrelated-annotated")  # adversarial: tag inside the pushing repo

        self.assertEqual(transactions.apply(work).status, "APPLIED")

        self.assertEqual(refs(target), {DEV: b})
        self.assertEqual(refs(sub_remote), sub_before)
        self.assertFalse(has_object(sub_remote, unpublished))

        follow_control = self.bare("follow-tags control.git", fixture=False)
        git("-C", work / "repo.git", "push", "--quiet", "--recurse-submodules=no", "--force-with-lease=refs/heads/dev:",
            follow_control, f"{b}:refs/heads/dev")
        self.assertEqual(set(refs(follow_control)), {DEV, "refs/tags/unrelated-annotated"},
                         "control: the same push without --no-follow-tags publishes the unrelated tag")
        caller_control = self.bare("caller control.git", fixture=False)
        git("-C", source, "push", "--quiet", caller_control, f"{b}:refs/heads/dev")
        self.assertIn("refs/tags/unrelated-annotated", refs(caller_control))
        self.assertTrue(has_object(sub_remote, unpublished),
                        "control: a plain push from the caller checkout publishes the submodule commit")

    def test_release_changes_only_main_tag_dev(self):
        """Publication scope: release changes main, the release tag and dev; other tags and the separate
        submodule repository stay unchanged."""
        source, b, sub_remote, unpublished = self.source_with_submodule()
        target = self.bare("target.git")
        self.bootstrap(target, source, b)
        out("-C", source, "push", "--quiet", "--no-follow-tags", target, "refs/tags/unrelated-annotated")
        out("-C", source, "tag", "-a", "unrelated-local", "-m", "never published", b)
        before, sub_before = refs(target), refs(sub_remote)
        work, operation = self.prepare(target)
        out("-C", work / "repo.git", "fetch", "--quiet", "--no-tags", source,
            "refs/tags/unrelated-local:refs/tags/unrelated-local")  # adversarial: reachable tag in the pushing repo

        self.assertEqual(transactions.apply(work, now=NOW).status, "APPLIED")

        after = refs(target)
        changed = {ref for ref in set(before) | set(after) if before.get(ref) != after.get(ref)}
        self.assertEqual(changed, {MAIN, "refs/tags/26.1.0", DEV})
        self.assertEqual(after["refs/tags/unrelated-annotated"], before["refs/tags/unrelated-annotated"])
        self.assertEqual(refs(sub_remote), sub_before)
        self.assertFalse(has_object(sub_remote, unpublished))
        self.assertEqual(out("-C", target, "ls-tree", operation["next"]["commit"], "lib").split()[:3],
                         ["160000", "commit", unpublished], "D keeps C's gitlink unchanged")
