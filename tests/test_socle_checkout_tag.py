# SPDX-License-Identifier: MPL-2.0
"""The socle a product fetches by commit knows which version it is.

`scripts/checkout-dependencies.sh` fetches the socle at the commit of the
`uses:` line, and a fetch by commit brings no tag. So `cut` run from that
checkout said "socle untagged" at every release, and `adopt` refused it
without `--allow-untagged` -- for a commit the same line names `# vX.Y.Z`.

The script brings that tag by name, as a label and nothing more. The
commit on the line decides what is checked out. The remote is asked what
the tag names before anything of it is fetched; the tag is created only
when what was fetched resolves, peeled, to that commit and, for a tag
object, calls itself by the name asked for. A comment that is not a
release tag asks nothing, a remote that stops answering is given up on,
and nothing of this fails the script.
"""
from __future__ import annotations

import pathlib
import shutil
import socket
import subprocess
import threading
import time
import unittest

from test_maelys_release import Product

TAG = "v9.9.9"


class SocleCheckoutTagTest(unittest.TestCase):
    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        self.dir = str(self.product.dir)
        # A socle of two commits, served like the pins. The product pins the
        # second; the first is where a tag that moved would point.
        self.source = self.product.work / "src" / "maelys-release"
        self.source.mkdir(parents=True)
        self.product.git(self.source, "init", "-q")
        for content in ("one\n", "two\n"):
            (self.source / "file").write_text(content, encoding="utf-8")
            self.product.git(self.source, "add", "file")
            self.product.git(self.source, "-c", "commit.gpgsign=false", "commit", "-q", "-m", content.strip())
        self.other = self.product.git(self.source, "rev-parse", "HEAD~1")
        self.pinned = self.product.git(self.source, "rev-parse", "HEAD")
        self.remote = self.product.work / "remotes" / "maelys-release.git"

    def tag(self, name: str, target: str, annotated: bool = True) -> None:
        form = ("-a", "-m", f"maelys-release {name[1:]}") if annotated else ()
        self.product.git(self.source, "-c", "tag.gpgsign=false", "tag", *form, name, target)

    def serve(self, tag_at: str | None = None, annotated: bool = True) -> None:
        if tag_at:
            self.tag(TAG, tag_at, annotated)
        # --mirror: every ref of the source as it is, a branch named like a
        # tag included.
        self.product.git(self.product.work, "clone", "-q", "--mirror", str(self.source), str(self.remote))

    def adopt(self, label: str = TAG) -> None:
        self.product.run("adopt", self.dir, "--apply", "--socle-sha", self.pinned, "--socle-tag", label)
        self.assertIn(f"release.yml@{self.pinned} # {label}\n", self.product.read(".github/workflows/release.yml"))

    def loop(self, *shell: str, env: dict | None = None) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
        destination = self.product.work / "runner"
        completed = subprocess.run(["sh", *shell, str(self.product.dir / "scripts" / "checkout-dependencies.sh"),
                                    str(destination)], cwd=self.product.dir, env=env or self.product.env,
                                   check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # Whatever happened to the tag, stdout is the two assignments and
        # nothing else: it lands in $GITHUB_ENV whole.
        assigned = dict(line.split("=", 1) for line in completed.stdout.splitlines())
        self.assertEqual(set(assigned), {"MAELYS_DEPENDENCIES_DIR", "MAELYS_RELEASE_DIR"}, completed.stdout)
        socle = destination / "maelys-release"
        self.assertEqual(pathlib.Path(assigned["MAELYS_RELEASE_DIR"]).resolve(), socle.resolve())
        # And the commit of the line is what is checked out, in every case.
        self.assertEqual(self.product.git(socle, "rev-parse", "HEAD"), self.pinned)
        self.assertEqual((socle / "file").read_text(encoding="utf-8"), "two\n")
        return completed, socle

    def described(self, socle: pathlib.Path) -> str:
        completed = subprocess.run(["git", "-C", str(socle), "describe", "--tags", "--exact-match"],
                                   env=self.product.env, check=False, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def tags(self, socle: pathlib.Path) -> str:
        return self.product.git(socle, "for-each-ref", "refs/tags")

    def said(self, completed: subprocess.CompletedProcess) -> list[str]:
        """What the script said of the socle, and nothing git said for it.

        The clone of the pin speaks first, in its own two lines; every other
        line of stderr is a sentence of this script. A raw `error:` or
        `fatal:` of git there is a line nobody wrote for a reader.
        """
        lines = [line for line in completed.stderr.splitlines()
                 if not line.startswith("maelys-system ")]
        for line in lines:
            self.assertTrue(line.startswith("checkout-dependencies: "), completed.stderr)
        return [line for line in lines if "cloned in" not in line]

    def has(self, socle: pathlib.Path, commit: str) -> bool:
        return subprocess.run(["git", "-C", str(socle), "cat-file", "-e", commit], env=self.product.env,
                              check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    def test_the_checkout_describes_itself_as_the_tag_the_line_names(self) -> None:
        self.serve(tag_at=self.pinned)
        self.adopt()
        completed, socle = self.loop()
        self.assertEqual(self.described(socle), TAG)
        # The tag object itself, not a second name for the commit: what was
        # signed is what is kept.
        self.assertEqual(self.product.git(socle, "cat-file", "-t", f"refs/tags/{TAG}"), "tag")
        self.assertEqual(self.product.git(socle, "rev-parse", f"refs/tags/{TAG}"),
                         self.product.git(self.remote, "rev-parse", f"refs/tags/{TAG}"))
        self.assertEqual(self.said(completed), [])

    def test_a_lightweight_tag_on_the_commit_is_a_label_as_good(self) -> None:
        self.serve(tag_at=self.pinned, annotated=False)
        self.adopt()
        completed, socle = self.loop()
        self.assertEqual(self.described(socle), TAG)
        self.assertEqual(self.said(completed), [])

    def test_a_tag_that_names_another_commit_is_not_fetched_and_that_is_said(self) -> None:
        self.serve(tag_at=self.other)
        # The remote does carry the tag: its absence below is the rule's
        # doing, not the fixture's.
        self.assertEqual(self.product.git(self.remote, "rev-parse", f"{TAG}^{{commit}}"), self.other)
        self.adopt()
        completed, socle = self.loop()
        self.assertEqual(self.tags(socle), "")
        self.assertEqual(self.described(socle), "")
        said = self.said(completed)
        self.assertEqual(len(said), 1, said)
        self.assertIn(f"says {TAG} is {self.other} and the uses: line pins {self.pinned}:"
                      " the tag is not fetched and not created", said[0])
        # Declining cost a question, not a fetch of that other commit.
        self.assertFalse(self.has(socle, self.other))

    def test_what_was_fetched_is_judged_and_not_what_was_announced(self) -> None:
        """Between the question and the fetch a tag may move, and a remote
        may answer one thing and serve another. A `git` in front of the real
        one answers `ls-remote` with the pinned commit; the remote's tag
        names the other."""
        self.serve(tag_at=self.other)
        self.adopt()
        front = self.product.work / "front"
        front.mkdir()
        real = shutil.which("git", path=self.product.env["PATH"])
        (front / "git").write_text(
            "#!/bin/sh\n"
            "for word in \"$@\"; do\n"
            f"    test \"$word\" = ls-remote && {{ printf '%s\\trefs/tags/{TAG}\\n' {self.pinned}; exit 0; }}\n"
            "done\n"
            f"exec {real} \"$@\"\n", encoding="utf-8")
        (front / "git").chmod(0o755)
        completed, socle = self.loop(env={**self.product.env, "PATH": f"{front}:{self.product.env['PATH']}"})
        # The fetch did happen: the announcement was believed that far.
        self.assertTrue(self.has(socle, self.other))
        self.assertEqual(self.tags(socle), "")
        self.assertEqual(self.described(socle), "")
        said = self.said(completed)
        self.assertEqual(len(said), 1, said)
        self.assertIn(f"served as {TAG} is {self.other} and the uses: line pins {self.pinned}:"
                      " the tag is not created", said[0])

    def test_a_tag_object_that_calls_itself_another_version_is_not_created(self) -> None:
        """`git describe` answers with the name inside the tag object. A
        remote serving refs/tags/v9.9.9 as an object named v99.0.0, on the
        pinned commit, passed the commit test and made the checkout describe
        itself as v99.0.0 -- which `adopt` would then have written."""
        self.tag("v99.0.0", self.pinned)
        self.product.git(self.source, "update-ref", f"refs/tags/{TAG}",
                         self.product.git(self.source, "rev-parse", "refs/tags/v99.0.0"))
        self.product.git(self.source, "update-ref", "-d", "refs/tags/v99.0.0")
        self.serve()
        # The fixture is the defect: the right commit under another name.
        self.assertEqual(self.product.git(self.remote, "rev-parse", f"refs/tags/{TAG}^{{commit}}"), self.pinned)
        self.assertIn("\ntag v99.0.0\n", self.product.git(self.remote, "cat-file", "tag", f"refs/tags/{TAG}") + "\n")
        self.adopt()
        completed, socle = self.loop()
        self.assertEqual(self.tags(socle), "")
        self.assertEqual(self.described(socle), "")
        said = self.said(completed)
        self.assertEqual(len(said), 1, said)
        self.assertIn(f"serves as {TAG} calls itself 'v99.0.0':", said[0])
        self.assertIn("the tag is not created", said[0])

    def test_a_branch_named_like_the_tag_is_not_the_tag(self) -> None:
        """The pattern of `ls-remote` and a bare name given to `fetch` both
        reach a branch. Only refs/tags/NAME is the tag."""
        for branch in (TAG, f"refs/tags/{TAG}"):
            with self.subTest(branch=branch):
                self.product.git(self.source, "update-ref", f"refs/heads/{branch}", self.pinned)
                self.serve()
                self.assertEqual(self.product.git(self.remote, "rev-parse", f"refs/heads/{branch}"), self.pinned)
                self.adopt()
                completed, socle = self.loop()
                self.assertEqual(self.tags(socle), "")
                self.assertEqual(self.described(socle), "")
                said = self.said(completed)
                self.assertEqual(len(said), 1, said)
                self.assertIn(f"has no tag {TAG}", said[0])
                self.product.git(self.source, "update-ref", "-d", f"refs/heads/{branch}")
                for path in (self.remote, self.product.work / "runner"):
                    shutil.rmtree(path)

    def test_a_branch_of_the_same_name_does_not_hide_the_tag(self) -> None:
        self.product.git(self.source, "update-ref", f"refs/heads/{TAG}", self.other)
        self.serve(tag_at=self.pinned)
        self.adopt()
        completed, socle = self.loop()
        self.assertEqual(self.described(socle), TAG)
        self.assertEqual(self.product.git(socle, "cat-file", "-t", f"refs/tags/{TAG}"), "tag")
        self.assertEqual(self.product.git(socle, "for-each-ref", "refs/heads"), "")
        self.assertEqual(self.said(completed), [])

    def test_a_tag_that_names_no_commit_is_declined_in_the_script_s_own_words(self) -> None:
        """A tag may name a tree or a blob. git said so itself, `error:
        FETCH_HEAD^{commit}...`, under the script's sentence."""
        tree = self.product.git(self.source, "rev-parse", "HEAD^{tree}")
        blob = self.product.git(self.source, "rev-parse", "HEAD:file")
        for target, annotated in ((tree, False), (tree, True), (blob, False), (blob, True)):
            with self.subTest(target=target, annotated=annotated):
                self.serve(tag_at=target, annotated=annotated)
                self.adopt()
                completed, socle = self.loop()
                self.assertEqual(self.tags(socle), "")
                said = self.said(completed)
                self.assertEqual(len(said), 1, said)
                self.assertIn(f"says {TAG} is {target} and the uses: line pins {self.pinned}", said[0])
                self.product.git(self.source, "tag", "-d", TAG)
                for path in (self.remote, self.product.work / "runner"):
                    shutil.rmtree(path)

    def test_a_candidate_under_trial_asks_the_remote_nothing(self) -> None:
        """`# untagged` is what a candidate writes; the remote's tags are not
        asked, even one that would have matched."""
        self.serve(tag_at=self.pinned)
        self.adopt("untagged")
        completed, socle = self.loop("-x")
        self.assertEqual(self.tags(socle), "")
        self.assertNotIn("refs/tags/", completed.stderr)
        self.assertNotIn("ls-remote", completed.stderr)
        # The trace is a witness only if it shows the fetch that did happen.
        self.assertIn(f"maelys-release.git {self.pinned}", completed.stderr)

    def test_a_comment_that_is_not_a_release_tag_asks_nothing(self) -> None:
        """What `git describe` says past a tag, or a word with a dash in
        front: the name goes to git only when it is `vX.Y.Z` and nothing else."""
        self.serve(tag_at=self.pinned)
        self.adopt()
        workflow = self.product.dir / ".github" / "workflows" / "release.yml"
        text = workflow.read_text(encoding="utf-8")
        for comment in (f"{TAG}-2-g{self.pinned[:7]}", f"{TAG} and more", "--upload-pack=x", f"refs/tags/{TAG}", ""):
            with self.subTest(comment=comment):
                line = f"release.yml@{self.pinned} # {comment}" if comment else f"release.yml@{self.pinned}"
                workflow.write_text(text.replace(f"release.yml@{self.pinned} # {TAG}", line), encoding="utf-8")
                completed, socle = self.loop("-x")
                self.assertEqual(self.tags(socle), "")
                self.assertNotIn("ls-remote", completed.stderr)
                self.assertNotIn("maelys-release.git refs/tags/", completed.stderr)
                shutil.rmtree(self.product.work / "runner")

    def test_a_remote_without_the_tag_fails_nothing(self) -> None:
        self.serve()
        self.adopt()
        completed, socle = self.loop()
        self.assertEqual(self.tags(socle), "")
        said = self.said(completed)
        self.assertEqual(len(said), 1, said)
        self.assertIn(f"has no tag {TAG}; the socle at {self.pinned} is checked out without it", said[0])

    def test_a_product_that_names_the_socle_in_its_ci_alone_gets_the_tag_too(self) -> None:
        """A product that publishes by its own means carries no release.yml
        of the socle: the line is the one that calls check-product.yml."""
        self.serve(tag_at=self.pinned)
        self.adopt()
        (self.product.dir / ".github" / "workflows" / "release.yml").unlink()
        self.assertIn(f"check-product.yml@{self.pinned} # {TAG}", self.product.read(".github/workflows/ci.yml"))
        _, socle = self.loop()
        self.assertEqual(self.described(socle), TAG)

    def test_both_calls_for_the_tag_carry_the_bound(self) -> None:
        self.serve(tag_at=self.pinned)
        self.adopt()
        completed, _ = self.loop("-x")
        bound = "-c http.lowSpeedLimit=1000 -c http.lowSpeedTime=10 "
        base = self.product.env["MAELYS_GIT_BASE"]
        self.assertIn(f"{bound}ls-remote {base}/maelys-release.git refs/tags/{TAG} ", completed.stderr)
        self.assertIn(f"{bound}fetch --quiet --depth 1 {base}/maelys-release.git refs/tags/{TAG}", completed.stderr)

    def test_a_remote_that_accepts_and_says_nothing_is_given_up_on(self) -> None:
        """The label is not worth a job. The question about the tag goes to a
        server that accepts the connection and never answers: the script's
        own settings cut it, says so, and the build has what it needs.

        The fetch by commit is not bounded that way, so it is left on the
        fixture's remote: a `git` in front of the real one sends `ls-remote`
        to the silent server and passes every other word through, the
        settings of the script included.
        """
        self.serve(tag_at=self.pinned)
        self.adopt()
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        held: list[socket.socket] = []
        self.addCleanup(lambda: [connection.close() for connection in held])

        def accept() -> None:
            while True:
                try:
                    held.append(listener.accept()[0])
                except OSError:
                    return
        threading.Thread(target=accept, daemon=True).start()
        silent = f"http://127.0.0.1:{listener.getsockname()[1]}"
        front = self.product.work / "front"
        front.mkdir()
        real = shutil.which("git", path=self.product.env["PATH"])
        base = self.product.env["MAELYS_GIT_BASE"]
        (front / "git").write_text(
            "#!/bin/sh\n"
            "asked=\nfor word in \"$@\"; do test \"$word\" = ls-remote && asked=1; done\n"
            f"test -n \"$asked\" || exec {real} \"$@\"\n"
            "for word in \"$@\"; do\n"
            "    shift\n"
            f"    case $word in {base}/*) word={silent}/${{word#{base}/}} ;; esac\n"
            "    set -- \"$@\" \"$word\"\n"
            "done\n"
            f"exec {real} \"$@\"\n", encoding="utf-8")
        (front / "git").chmod(0o755)
        env = {**self.product.env, "PATH": f"{front}:{self.product.env['PATH']}"}
        for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            env.pop(name, None)
        env["no_proxy"] = env["NO_PROXY"] = "127.0.0.1"
        started = time.monotonic()
        completed, socle = self.loop(env=env)
        elapsed = time.monotonic() - started
        self.assertTrue(held, "the silent server was never asked: the test proves nothing")
        self.assertEqual(self.tags(socle), "")
        self.assertIn(f"could not be asked for the tag {TAG}; the socle at {self.pinned} is checked out without it",
                      completed.stderr)
        # Cut by the bound, not by luck: not at once, and not much later.
        self.assertGreater(elapsed, 9, completed.stderr)
        self.assertLess(elapsed, 60, completed.stderr)


if __name__ == "__main__":
    unittest.main()
