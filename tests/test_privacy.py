# SPDX-License-Identifier: MPL-2.0
"""What the tracked files say of the machine and the person behind them.

The guard reads values by their shape. These tests break each of its rules on
purpose and measure both ends: a tree with the defect, a clean tree, a machine
whose local tools are absent, a runner that has no author.

Every fixture is fabricated when the test runs. No name, host, path, address
or identifier is written here whole: this file is tracked, and the guard
reads it like any other.

Every tree is read twice where it can be: as a directory, where each file is
opened, and as a repository, where git names the files that hold a shape and
only those are opened. The two must find the same things: what git is asked
is a superset of the rules, never a subset.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

from test_maelys_release import MODULE, FakeHost, Product, using_host

privacy = MODULE.privacy

# Assembled, never spelled: a person, their machine on a home network, and
# what a file would carry of them.
PERSON = "ali" + "ce"
MACHINE = "Atelier" + "-" + "Pro"
HOST = "atelier" + "." + "home"
PRINTER = "printer" + "." + "lan"
HOME = "/" + "Users" + "/" + PERSON
LINUX_HOME = "/" + "home" + "/" + PERSON
ADDRESS = PERSON + "@" + "fabrique-" + uuid.uuid4().hex[:8] + ".org"
INSTALLATION = str(uuid.uuid4())
TERM = "zebu" + "lon"
SECRETS = (PERSON, HOST, PRINTER, "atelier", ADDRESS, INSTALLATION, TERM)


def local(machines=(), terms=(), asked=True):
    return privacy.Local(list(machines), list(terms), asked=asked)


def tree(files: dict, directory: pathlib.Path) -> pathlib.Path:
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return directory


def git(directory, *arguments, identity=("t", "t@example.invalid"), data=None) -> str:
    completed = subprocess.run(["git", "-C", str(directory), "-c", f"user.name={identity[0]}",
                                "-c", f"user.email={identity[1]}", "-c", "commit.gpgsign=false",
                                "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main", *arguments],
                               check=True, capture_output=True, input=data)
    return completed.stdout.decode("utf-8", errors="replace").strip()


def where(finding: dict) -> tuple:
    return (finding["family"], finding["path"], finding.get("in"), finding.get("key"), finding.get("line"))


class Tree(unittest.TestCase):
    def setUp(self) -> None:
        self.work = tempfile.TemporaryDirectory(prefix="maelys-privacy-")
        self.addCleanup(self.work.cleanup)
        self.dir = pathlib.Path(self.work.name).resolve() / "product"
        self.dir.mkdir()

    def guard(self, hosted, machines=(), terms=(), history=False):
        original = privacy.read_local
        privacy.read_local = lambda: local(machines, terms)
        try:
            with using_host(hosted):
                return privacy.check_tree(self.dir, history=history)
        finally:
            privacy.read_local = original

    def read(self, files: dict, conf: str = "", machines=(), terms=(), tracked: bool = True):
        """(findings by family, the check lines, the data) of a tree holding FILES.

        Read as a directory, then as a repository when TRACKED: the findings
        must be the same, by place.
        """
        if conf:
            files = {**files, "maelys-release.conf": conf}
        shutil.rmtree(self.dir)
        self.dir.mkdir()
        tree(files, self.dir)
        checks, data = self.guard(FakeHost(), machines, terms)
        if tracked:
            git(self.dir, "init", "-q")
            git(self.dir, "add", "-A")
            again, searched = self.guard(MODULE.Host(), machines, terms)
            self.assertEqual(sorted(map(repr, map(where, searched["findings"]))),
                             sorted(map(repr, map(where, data["findings"]))),
                             "what git names as candidates is narrower than the rule")
            self.assert_silent(again, searched)
        by_family: dict = {}
        for finding in data["findings"]:
            if "excused" not in finding:
                by_family.setdefault(finding["family"], []).append(finding)
        return by_family, checks, data

    def assert_silent(self, checks, data, secrets=SECRETS) -> None:
        """No output shows what a family matched: neither the lines nor the data."""
        said = (json.dumps(data) + "\n".join(message for _, message in checks)).lower()
        for secret in secrets:
            self.assertNotIn(secret.lower(), said)


class ShapeTest(Tree):
    def test_a_value_is_a_host_and_a_field_access_is_not(self) -> None:
        """The measurement that set the rule: three struct-field accesses on a clean product."""
        found, checks, data = self.read({
            "src/main.c": "if (args.home) { f.home = args.home; }\nreturn config.local || net.lan;\n",
            "src/lib.rs": "let path = self.home.join(dirs.local);\n",
            "README.md": "The field `args.home` is where it lands; see " + HOST + " for nothing.\n",
        })
        self.assertEqual(found, {})
        self.assertIn("nothing found", checks[0][1])
        found, checks, data = self.read({
            "records/one.json": json.dumps({"result": {"peerName": HOST, "port": 8080}}),
            "records/two.jsonl": json.dumps({"id": 1}) + "\n" + json.dumps({"peerName": "http://" + HOST + ":80/x"}) + "\n",
            "settings.yml": f"server:\n  peerName: {HOST}\n  peers:\n    - {PRINTER}\n",
            "settings.toml": f'peerName = "{HOST}"\nothers = ["nas.local", "kube.svc.cluster.local"]\n',
        })
        self.assertEqual(sorted(map(where, found["host"])), [
            ("host", "records/one.json", None, "peerName", None), ("host", "records/two.jsonl", None, "peerName", 2),
            ("host", "settings.toml", None, "others", 2), ("host", "settings.toml", None, "peerName", 1),
            ("host", "settings.yml", None, "peerName", 2), ("host", "settings.yml", None, "peers", 4)])
        self.assert_silent(checks, data)
        self.assertTrue(any("under the key peerName in 4 tracked files" in message for _, message in checks), checks)

    def test_a_host_under_each_suffix_brings_its_file_to_be_read(self) -> None:
        for suffix in ("home", "lan", "local", "home.arpa"):
            with self.subTest(suffix=suffix):
                found, checks, data = self.read({"only.yml": "peer: nas" + "." + suffix + "\n"})
                self.assertEqual([(finding["key"], finding["line"]) for finding in found["host"]], [("peer", 1)])

    def test_a_host_is_found_wherever_a_value_holds_it(self) -> None:
        found, checks, data = self.read({"a.json": json.dumps({
            "share": "//" + HOST + "/public", "list": HOST + ", " + PRINTER, "said": "connect to " + HOST + " first",
            "arpa": "atelier.home.arpa", "url": "smb://" + PERSON + "@" + HOST + ":445/x"})})
        self.assertEqual(sorted(finding["key"] for finding in found["host"]), ["arpa", "list", "list", "said", "share", "url"])
        self.assert_silent(checks, data)

    def test_reserved_names_a_cluster_domain_and_the_words_of_code_are_nobodys_network(self) -> None:
        found, _, _ = self.read({"a.json": json.dumps({"hosts": [
            "example.local", "localhost.local", "test.lan", "api.default.svc.cluster.local", "_http._tcp.local",
            ".env.local", "settings", "user.home", "self.home", "args.home", "settings.local", "tsconfig.local",
            "application.local", "nas.home.example.com"]}),
            "b.yml": "java: user.home\nfile: settings.local\n"})
        self.assertEqual(found, {})

    def test_an_identifier_is_one_under_an_identity_key_and_only_when_it_was_not_invented(self) -> None:
        real = INSTALLATION
        found, checks, data = self.read({"a.json": json.dumps({
            "installationId": real, "device_id": real, "machine-id": real, "AccountID": real,
            "githubUserId": 48213907, "organization_id": "org_" + uuid.uuid4().hex,
            # written as people write them, which the search before the parse must let through
            "Installation Id": real, "User ID": 48213907, "user.id": 48213907, "device__id": real,
            "deviceIds": [real, "00000000-0000-0000-0000-000000000000"],
            # the same value under a key that names nobody
            "requestId": real, "traceId": real, "valid": real, "paid": 48213907,
            # invented, each in its own way
            "installation_id": "00000000-0000-0000-0000-000000000000",
            "deviceId": "11111111-1111-1111-1111-111111111111",
            "machineId": "12345678-1234-1234-1234-123456789abc",
            "accountId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "userId": 123456, "user_id": "test-user-0000000000000042",
            "organizationId": "123e4567-e89b-12d3-a456-426614174000",
            # too short to identify anything
            "user-id": "u42", "UserId": 7})})
        # A key with a space in it is found and is not a word a message repeats.
        self.assertEqual(sorted(str(finding.get("key")) for finding in found["identifier"]),
                         sorted(["AccountID", "None", "None", "device__id", "device_id", "deviceIds", "githubUserId",
                                 "installationId", "machine-id", "organization_id", "user.id"]))
        self.assert_silent(checks, data)

    def test_one_key_at_a_time_through_the_search_git_does(self) -> None:
        """One definition: what the rule accepts, the search that precedes the parse lets through."""
        for key in ("installationId", "Installation Id", "User ID", "user.id", "device__id", "accountIds",
                    "organisation-id", "MACHINE_ID"):
            with self.subTest(key=key):
                self.assertTrue(privacy.identity_key(key))
                # A number, so that nothing but the key brings the file to be read.
                found, _, _ = self.read({"only.json": json.dumps({key: 48213907})})
                self.assertEqual(len(found.get("identifier", [])), 1)
        for key in ("valid", "paid", "requestId", "grid", "identity", "userName"):
            self.assertFalse(privacy.identity_key(key), key)

    def test_an_id_under_an_object_named_for_an_identity_is_one(self) -> None:
        payload = {"action": "created", "installation": {"id": 48213907, "node_id": "x"},
                   "sender": {"id": 73110425, "login": "x"}, "repository": {"id": 91524367, "owner": {"id": 5}},
                   "user": {"id": 583231004}, "account": {"id": 2}, "organization": {"id": 60237415},
                   "users": [{"id": 77001234}, {"id": 1}]}
        found, checks, data = self.read({
            "payload.json": json.dumps(payload),
            "pretty.json": json.dumps(payload, indent=2),
            "payload.yml": "installation:\n  node: x\n  id: 48213907\nsender:\n  id: 73110425\nusers:\n  - id: 77001234\n    name: x\n",
            "payload.toml": "[installation]\nnode = 'x'\nid = 48213907\n\n[sender]\nid = 73110425\n",
            # a pretty-printed document that does not parse is read by its lines, and the object is still seen
            "broken.json": json.dumps({"installation": {"node": "x", "id": 48213907}, "sender": {"id": 73110425}},
                                      indent=2)[:-2] + ",\n}\n",
        })
        self.assertEqual(sorted((finding["path"], finding.get("key")) for finding in found["identifier"]), [
            ("broken.json", "installation.id"),
            ("payload.json", "installation.id"), ("payload.json", "organization.id"), ("payload.json", "user.id"),
            ("payload.json", "users.id"),
            ("payload.toml", "installation.id"), ("payload.yml", "installation.id"), ("payload.yml", "users.id"),
            ("pretty.json", "installation.id"), ("pretty.json", "organization.id"), ("pretty.json", "user.id"),
            ("pretty.json", "users.id")])
        self.assertEqual(data["files"]["astext"], ["broken.json"])
        self.assert_silent(checks, data)

    def test_the_rule_for_an_invented_identifier_is_stated_and_holds(self) -> None:
        for invented in ("00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff",
                         "00000000-0000-0000-0000-000000000001", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                         "01234567-89ab-cdef-0123-456789abcdef", "12345678-1234-1234-1234-123456789abc",
                         "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",
                         "123456789012", "100000", "424242", "9876543", "example-installation-7f3a9c2e51"):
            with self.subTest(invented=invented):
                self.assertTrue(privacy.synthetic(invented))
                self.assertFalse(privacy.identifier(invented))
        for _ in range(2000):
            drawn = str(uuid.uuid4())
            self.assertTrue(privacy.identifier(drawn), drawn)
        self.assertTrue(privacy.identifier("{" + str(uuid.uuid4()).upper() + "}"))
        self.assertFalse(privacy.identifier(True))
        self.assertFalse(privacy.identifier("short-1"))
        self.assertFalse(privacy.identifier("a-sentence-of-words-without-a-digit"))
        # A number: two different digits at most, or a run. Every number of
        # six digits is tried, and the share taken for an invention is the
        # one the conventions state.
        taken = sum(privacy.synthetic(str(number)) for number in range(100000, 1000000))
        self.assertEqual(taken, 2538)
        self.assertLess(taken / 900000, 1 / 340)
        for real in (48213907, 583231, 135790, 102030):
            self.assertTrue(privacy.identifier(real), real)

    def test_an_assignment_is_read_a_line_at_a_time_in_yaml_and_toml(self) -> None:
        found, checks, data = self.read({
            "config.yaml": f"installationId: \"{INSTALLATION}\"  # recorded\nuserId: 123456\nname: x\n",
            "config.toml": f"[agent]\ndevice_id = '{INSTALLATION}'\n",
            ".env.sample": f"ACCOUNT_ID={INSTALLATION}\n",
            # code is not a document: a constant there is the reader's own business
            "src/fixture.py": f"installation_id = '{INSTALLATION}'\n",
        })
        self.assertEqual(sorted((finding["path"], finding["key"], finding["line"]) for finding in found["identifier"]),
                         [(".env.sample", "ACCOUNT_ID", 1), ("config.toml", "device_id", 2),
                          ("config.yaml", "installationId", 1)])
        self.assert_silent(checks, data)

    def test_a_personal_path_is_a_name_and_not_a_marker_an_account_or_the_tail_of_a_url(self) -> None:
        clean = "\n".join((
            "/Users/<user>/x", "/Users/$USER/x", "/Users/USER/x", "/home/{name}/x", "/Users/Shared/x", "/home/runner/work",
            "/home/linuxbrew/.linuxbrew", "/Users/runner/x", "/home/x/y", "https://site.example/home/page",
            "$ROOT/home/thing", "../home/thing", "~/home/thing", "prefix/Users/thing", "/home/synthetic/a",
            "root /home/www;", "WORKDIR /home/appuser", "/home/YOUR_USERNAME/x")) + "\n"
        routes = ("router.push('/home/dashboard'); <a href=\"/home/about\">; path: `/home/settings`\n"
                  # and the same inside a string, where the quote that closes it is escaped
                  "const escaped = " + json.dumps('<a href="/home/terms">') + ";\n")
        found, _, _ = self.read({"notes.md": clean, "app.js": routes})
        self.assertEqual(found, {})
        found, checks, data = self.read({
            "notes.md": f"first\nrun it from {HOME}/work\nand {LINUX_HOME}\n",
            "log.txt": "C:" + "\\" + "Users" + "\\" + PERSON + "\\" + "x\nc:" + "\\" + "users" + "\\" + PERSON + "\n",
            "mounts.txt": "\n".join(("/mnt/c" + HOME, "/System/Volumes/Data" + HOME, "/Volumes/Backup" + HOME,
                                     "/var" + LINUX_HOME, "/Users/" + PERSON.upper() + "/x")) + "\n",
            "app.js": "const where = '" + LINUX_HOME + "/work';\n",
            "trace.json": json.dumps({"cwd": HOME + "/src", "argv": ["file://" + HOME + "/a"]}),
            "escaped.json": '{"cwd": "' + HOME.replace("/", "\\/") + '"}',
        })
        self.assertEqual(sorted((finding["path"], finding.get("key"), finding.get("line")) for finding in found["path"]),
                         [("app.js", None, 1), ("escaped.json", "cwd", None), ("log.txt", None, 1), ("log.txt", None, 2),
                          ("mounts.txt", None, 1), ("mounts.txt", None, 2), ("mounts.txt", None, 3),
                          ("mounts.txt", None, 4), ("mounts.txt", None, 5), ("notes.md", None, 2),
                          ("notes.md", None, 3), ("trace.json", "argv", None), ("trace.json", "cwd", None)])
        self.assert_silent(checks, data)

    def test_a_body_recorded_as_a_string_is_read_as_the_document_it_is(self) -> None:
        body = json.dumps({"installationId": INSTALLATION, "peerName": HOST})
        found, checks, data = self.read({"record.json": json.dumps({"request": {"body": body}})})
        self.assertEqual((found["identifier"][0]["key"], found["host"][0]["key"]), ("installationId", "peerName"))
        self.assert_silent(checks, data)

    def test_what_does_not_parse_is_parsed_where_it_can_be_and_said_where_it_cannot(self) -> None:
        document = {"peerName": HOST, "installationId": INSTALLATION}
        one = json.dumps(document)
        found, checks, data = self.read({
            "bom.json": "\ufeff" + one,
            "bom-pretty.json": "\ufeff" + json.dumps(document, indent=2),
            "concatenated.json": one + "\n" + one + "\n",
            "first-line.jsonl": "recorded on a tuesday\n" + one + "\n",
            "tail.log": "12:00:01 INFO request " + one + "\n",
            "truncated.json": json.dumps(document, indent=2)[:-3],
            "comma.json": json.dumps(document, indent=2)[:-2] + ",\n}\n",
            "comments.jsonc": "// a record\n" + json.dumps(document, indent=2) + "\n",
        })
        seen = sorted((finding["path"], finding["family"], finding.get("key")) for finding in found["host"] + found["identifier"])
        self.assertEqual(seen, sorted(
            (name, family, key) for name in ("bom.json", "bom-pretty.json", "concatenated.json", "first-line.jsonl", "tail.log",
                                             "truncated.json", "comma.json", "comments.jsonc")
            for family, key in (("host", "peerName"), ("identifier", "installationId"))))
        # Never "read" without saying how: the three that are named as JSON
        # and are not were read a line at a time, and are listed.
        self.assertEqual(data["files"]["astext"], ["comma.json", "comments.jsonc", "truncated.json"])
        self.assertTrue(any("3 files named as JSON did not parse" in message for _, message in checks), checks)
        self.assert_silent(checks, data)


class MaskTest(Tree):
    """A key and a name are values: examined by every family, found, and shown by position."""

    def test_a_key_of_a_document_that_is_a_value_is_found_and_never_shown(self) -> None:
        found, checks, data = self.read({"map.json": json.dumps({
            "peers": {HOST: {"up": True}}, INSTALLATION: 1, HOME + "/x": 2, ADDRESS: {"note": LINUX_HOME + "/y"},
            "servers." + HOST: 3})})
        self.assertEqual(sorted((finding["family"], str(finding.get("in")), str(finding.get("key")))
                                for finding in data["findings"]),
                         [("email", "key", "None"), ("host", "key", "None"), ("host", "key", "None"),
                          ("identifier", "key", "None"), ("path", "None", "note"), ("path", "key", "None")])
        self.assert_silent(checks, data)
        self.assertTrue(any("written as a key, which is not shown" in message for _, message in checks), checks)

    def test_a_key_that_holds_what_an_identifier_is_made_of_is_not_shown_whatever_it_is(self) -> None:
        found, checks, data = self.read({"a.json": json.dumps({
            "k48213907": HOST, "h" + "0f" * 8: HOST, "peer_2": HOST, "x" * 70: HOST, "with space": HOST})})
        # One value under four keys that are not shown is one finding without a key.
        self.assertEqual(sorted(str(finding.get("key")) for finding in found["host"]), ["None", "peer_2"])

    def test_a_key_of_a_line_read_file_is_examined_like_any_other(self) -> None:
        found, checks, data = self.read({
            "conf/hosts.yml": f"{HOST}: {PRINTER}\n{MACHINE.lower()}: up\n{TERM}: 1\n",
            "conf/hosts.toml": f'"{HOST}" = "x"\n',
            "conf/hosts.ini": f"{INSTALLATION} = y\n",
            ".env": f"{PERSON}@{HOST}=z\n",
            "fallback.json": '{\n  "' + HOST + '": "' + PRINTER + '",\n}\n',
        }, machines=[MACHINE], terms=[(4, TERM)])
        keys = sorted((finding["family"], finding["path"], finding["line"]) for finding in data["findings"]
                      if finding.get("in") == "key")
        self.assertEqual(keys, [("host", ".env", 1), ("host", "conf/hosts.toml", 1), ("host", "conf/hosts.yml", 1),
                                ("host", "fallback.json", 2), ("identifier", "conf/hosts.ini", 1),
                                ("machine", "conf/hosts.yml", 2), ("term", "conf/hosts.yml", 3)])
        # The value under a key that is not shown is found, and names no key.
        self.assertEqual([(finding["path"], finding.get("key")) for finding in found["host"]
                          if finding.get("in") is None], [("conf/hosts.yml", None), ("fallback.json", None)])
        self.assert_silent(checks, data)

    def test_the_name_of_a_file_is_a_value_and_is_shown_by_its_position(self) -> None:
        other = "bob" + "@" + "fabrique-" + uuid.uuid4().hex[:8] + ".net"
        found, checks, data = self.read({
            f"records/{HOST}/b.json": "{}", f"fixtures/Users/{PERSON}/c.json": "{}",
            f"dumps/{INSTALLATION}.bin": b"\0\1", f"mail/{other}.png": b"\x89PNG",
            f"hosts/{MACHINE.lower()}/notes.txt": "x\n", f"notes/{TERM}.md": "x\n",
            f"rootfs/home/{PERSON}/.profile": "x\n", "src/pages/home/dashboard/index.html": "x\n",
        }, machines=[MACHINE], terms=[(2, TERM)])
        self.assertEqual(sorted((finding["family"], finding["in"]) for finding in data["findings"]),
                         [("email", "name"), ("host", "name"), ("identifier", "name"), ("machine", "name"),
                          ("path", "name"), ("path", "name"), ("term", "name")])
        for finding in data["findings"]:
            self.assertRegex(finding["path"], r"^tracked path [0-9]+, .*\(not shown\)")
        # What was not read is listed under the same names.
        self.assertEqual(sorted(entry["reason"] for entry in data["files"]["unread"]), ["binary", "image"])
        for entry in data["files"]["unread"]:
            self.assertIn("(not shown)", entry["path"])
        self.assertTrue(any("in the name of 2 tracked files, shown by position" in message for _, message in checks))
        self.assert_silent(checks, data, SECRETS + (other,))

    def test_a_name_is_shown_by_its_position_before_any_gate_is_asked(self) -> None:
        reading = privacy.Reading(privacy.Settings(), local([MACHINE]))
        for relative, shown in ((f"records/{HOST}/b.json", "tracked path 4, records/(not shown)/b.json"),
                                (f"records/{HOST}.json", "tracked path 4, records/(not shown)"),
                                (f"Users/{PERSON}/x", "tracked path 4, Users/(not shown)/x"),
                                (f"a/{MACHINE.lower()}/{INSTALLATION}.txt", "tracked path 4, a/(not shown)/(not shown)")):
            with self.subTest(shown=shown):
                self.assertEqual(privacy.name_of(reading, relative, 4)[0], shown)
        self.assertEqual(privacy.name_of(reading, "src/pages/home/dashboard/index.html", 4),
                         ("src/pages/home/dashboard/index.html", []))

    def test_a_value_found_in_one_place_is_shown_in_no_other(self) -> None:
        """The last gate: a name the first reader took for a word is caught by what another reader found."""
        found, checks, data = self.read({
            "a.json": json.dumps({"cwd": HOME + "/src", PERSON: {"peerName": HOST}}),
            f"people/{PERSON}.yml": "role: x\n" + "home: " + LINUX_HOME + "\n",
            f"portraits/{PERSON}.png": b"\x89PNG",
        })
        self.assertEqual(sorted((finding["path"].split(",")[0], finding.get("key")) for finding in data["findings"]),
                         [("a.json", "cwd"), ("a.json", "peerName"), ("people/(not shown).yml", "home")])
        self.assertEqual(data["files"]["unread"], [{"path": "portraits/(not shown).png", "reason": "image"}])
        self.assert_silent(checks, data)


class AddressTest(Tree):
    def test_an_address_is_a_finding_unless_it_is_reserved_silent_or_published_here(self) -> None:
        found, _, _ = self.read({
            "LICENSING.md": f"Contact: {ADDRESS}\n",
            ".github/SECURITY.md": "Report to security@" + "fabrique-published.org\n",
            "share/allowed-signers": "signer@" + 'fabrique-signers.org namespaces="git" ssh-ed25519 AAAA\n'
                                     "*@" + 'fabrique-everyone.org namespaces="git" ssh-ed25519 AAAA\n',
            # the file under the name another repository gives it
            ".github/release-allowed-signers": "releaser@" + 'fabrique-release.org namespaces="git" ssh-ed25519 AAAA\n',
            "src/a.py": "\n".join((
                f"# {ADDRESS}", "# security@" + "fabrique-published.org", "# signer@" + "fabrique-signers.org",
                "# anyone@" + "fabrique-everyone.org", "# releaser@" + "fabrique-release.org",
                "# someone@example.org x@sub.example.com y@host.invalid",
                "# 1234+name@users.noreply.github.com noreply@" + "fabrique-other.org",
                "# git@github.com:owner/name.git ssh://git@github.com/owner/name",
                "# https://user:secret@" + "fabrique-registry.org/path smtp://relay@" + "fabrique-mail.org:25",
                "# icon@2x.png getty@tty1.service pkg@1.2.3 actions/checkout@v4",
                "y = W@x.data + A@self.weights  # a matrix product", "@app.route", "")),
        })
        self.assertEqual(found, {})
        other = "bob" + "@" + "fabrique-" + uuid.uuid4().hex[:8] + ".net"
        found, checks, data = self.read({
            "vendor/lib/LICENSE": f"Copyright (c) 2019 Somebody <{other}>\n",
            "vendor/lib/lib.c": f"/* Copyright (c) 2019 Somebody <{other}> */\n",
            "package.json": json.dumps({"author": {"email": ADDRESS}}),
            "notes.md": f"write to {ADDRESS}\n",
        })
        self.assertEqual(len(found["email"]), 4)
        self.assertEqual(sorted(finding["path"] for finding in found["email"] if finding.get("notice")),
                         ["vendor/lib/LICENSE", "vendor/lib/lib.c"])
        lines = [message for _, message in checks if "e-mail" in message]
        # One line for the family, whatever it holds: it drowns nothing.
        self.assertEqual(len(lines), 1, checks)
        self.assertIn("2 e-mail addresses this repository does not publish itself (1 in a copyright or authorship"
                      " notice) in 4 tracked files", lines[0])
        self.assert_silent(checks, data, SECRETS + (other,))

    def test_the_address_git_invents_on_an_unconfigured_machine_is_a_host(self) -> None:
        found, checks, data = self.read({"log.md": f"Author: {PERSON} <{PERSON}@{HOST}>\n"})
        self.assertEqual(list(found), ["host"])
        self.assert_silent(checks, data)


class LocalTest(Tree):
    def test_this_machine_is_found_whatever_separates_its_words_and_whatever_their_case(self) -> None:
        spellings = ("atelier-pro", "AtelierPro", "ATELIER_PRO", "Atelier Pro", "atelier.pro")
        found, checks, data = self.read({"a.txt": "\n".join(spellings) + "\n",
                                         "b.json": json.dumps({"peerName": "atelier-pro"}),
                                         # a longer word is another word
                                         "c.txt": "atelier-production atelierprofessional preatelier-pro9\n"},
                                        machines=[MACHINE])
        self.assertEqual(sorted((finding["path"], finding.get("line"), finding.get("key")) for finding in found["machine"]),
                         [("a.txt", number, None) for number in range(1, 6)] + [("b.json", None, "peerName")])
        self.assert_silent(checks, data)
        self.assertIn("this machine's name was looked for", checks[0][1])
        self.assertIn("privacy: this machine's name in 2 tracked files", checks[1][1])

    def test_a_name_too_short_or_too_common_names_no_machine(self) -> None:
        for name in ("mbp", "MacBook-Pro", "localhost", "ubuntu"):
            with self.subTest(name=name):
                found, checks, data = self.read({"a.txt": f"the {name} and the macbook pro\n"}, machines=[name],
                                                tracked=False)
                self.assertEqual(found, {})
                self.assertEqual(data["local"]["machineNames"], 0)
                self.assertIn("too short or too common to be specific", checks[0][1])

    def test_the_names_are_asked_of_the_host_and_their_absence_is_said(self) -> None:
        asked = []

        def answer(command, cwd=None, env=None):
            asked.append(command)
            name = {("hostname", "-s"): MACHINE + ".home\n", ("scutil", "--get", "LocalHostName"): MACHINE + "\n",
                    ("scutil", "--get", "ComputerName"): "Atelier Pro\n"}[tuple(command)]
            return subprocess.CompletedProcess(command, 0, name, "")

        class Named(FakeHost):
            def which(self, name):
                return "/usr/bin/" + name if name in ("hostname", "scutil") else None

            def private_terms(self):
                return None

            def is_runner(self):
                return False

        with using_host(Named(run=answer)):
            here = privacy.read_local()
        self.assertEqual(asked, [["hostname", "-s"], ["scutil", "--get", "LocalHostName"],
                                 ["scutil", "--get", "ComputerName"]])
        self.assertEqual(here.machines, ["atelierpro"])
        self.assertTrue(here.find("on atelier pro today"))

        class Bare(FakeHost):
            def is_runner(self):
                return False

        # The local tools absent: nothing is asked, nothing is looked for, and it is said.
        with using_host(Bare()) as silent:
            absent = privacy.read_local()
        self.assertEqual((silent.commands, absent.machines, absent.asked), ([], [], False))
        self.assertIn("not looked for (no hostname command here)", absent.says())

    def test_a_runner_has_a_name_and_no_author_and_neither_is_looked_for_there(self) -> None:
        """A runner called after a word of the tree would find it, and refuse a repository for it."""
        asked = []

        class Runner(FakeHost):
            def which(self, name):
                asked.append(name)
                return "/usr/bin/" + name

            def is_runner(self):
                return True

        with using_host(Runner()):
            there = privacy.read_local()
        self.assertEqual((asked, there.names, there.runner), ([], [], True))
        self.assertIn("not looked for on a CI runner", there.says())
        saved = {name: os.environ.pop(name, None) for name in ("CI", "GITHUB_ACTIONS")}
        try:
            self.assertFalse(MODULE.Host().is_runner())
            for name in ("CI", "GITHUB_ACTIONS"):
                os.environ[name] = "true"
                self.assertTrue(MODULE.Host().is_runner())
                del os.environ[name]
        finally:
            os.environ.update({name: value for name, value in saved.items() if value is not None})

    def test_a_self_test_asks_no_machine_its_name_but_the_one_a_test_wrote(self) -> None:
        host = MODULE.Host()
        self.assertIsNone(host.which("hostname"))
        self.assertIsNone(host.which("scutil"))
        fake = pathlib.Path(self.work.name).resolve() / "bin" / "hostname"
        fake.parent.mkdir()
        fake.write_text("#!/bin/sh\necho fixture\n", encoding="utf-8")
        fake.chmod(0o755)
        previous = os.environ["PATH"]
        try:
            os.environ["PATH"] = f"{fake.parent}{os.pathsep}{previous}"
            self.assertEqual(host.which("hostname"), str(fake))
        finally:
            os.environ["PATH"] = previous

    def test_a_private_term_is_named_by_its_line_in_a_list_that_lives_outside_every_repository(self) -> None:
        found, checks, data = self.read({"a.md": f"x\nthe {TERM.upper()} project\n", "b.json": json.dumps({"n": TERM})},
                                        terms=[(3, TERM), (4, "ab")])
        self.assertEqual(sorted((finding["path"], finding["term"]) for finding in found["term"]),
                         [("a.md", 3), ("b.json", 3)])
        self.assertEqual(data["local"]["terms"], 1)
        self.assertTrue(any("the term on line 3 of your private list in 2 tracked files" in message
                            for _, message in checks), checks)
        self.assert_silent(checks, data)

    def test_a_term_is_matched_as_it_was_written_whatever_it_holds(self) -> None:
        address, pair, route = "carol" + "@" + "fabrique-terms.org", "moulin" + " & " + "fils", "atelier" + "/" + "nord"
        found, checks, data = self.read(
            {"a.md": f"write to {address}\nby {pair.upper().replace(' ', '')}\nunder {route}\nnot moulinfils nor ateliernord\n"},
            terms=[(1, address), (2, pair), (3, route)])
        self.assertEqual(sorted((finding["term"], finding["line"]) for finding in found["term"]), [(1, 1), (2, 2), (3, 3)])
        self.assert_silent(checks, data, (address, "moulin", "nord"))

    def test_the_list_is_read_from_the_configuration_directory_and_only_a_tests_own_in_a_self_test(self) -> None:
        home = pathlib.Path(self.work.name).resolve() / "config"
        (home / "maelys-release").mkdir(parents=True)
        (home / "maelys-release" / "private-terms").write_text(f"# mine\n\n{TERM}\n", encoding="utf-8")
        previous = os.environ.get("XDG_CONFIG_HOME")

        class Bare(FakeHost):
            def is_runner(self):
                return False

        try:
            os.environ["XDG_CONFIG_HOME"] = str(home)
            self.assertEqual(MODULE.Host().private_terms(), home / "maelys-release" / "private-terms")
            with using_host(Bare()):
                self.assertEqual(privacy.read_local().terms, [(3, TERM)])
            # The developer's own list is not a fixture: outside the temporary
            # directory a self-test reads none, set or not.
            os.environ["XDG_CONFIG_HOME"] = str(pathlib.Path.home() / ".config")
            self.assertIsNone(MODULE.Host().private_terms())
            del os.environ["XDG_CONFIG_HOME"]
            self.assertIsNone(MODULE.Host().private_terms())
        finally:
            if previous is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = previous


class DeclarationTest(Tree):
    LEAK = {"records/a.json": json.dumps({"peerName": HOST, "installationId": INSTALLATION}),
            "records/b.json": json.dumps({"peerName": HOST}), "notes.md": f"{HOME}\n"}

    def test_the_guard_is_born_a_note_and_a_repository_raises_it(self) -> None:
        found, checks, data = self.read(dict(self.LEAK))
        self.assertEqual(data["mode"], "note")
        self.assertEqual({status for status, _ in checks}, {"ok", "note"})
        self.assertIn("nothing above is refused", checks[-1][1])
        found, checks, data = self.read(dict(self.LEAK), conf="[privacy]\nrefuse\n")
        self.assertEqual((data["mode"], data["refuses"]), ("refuse", list(privacy.FAMILIES)))
        self.assertEqual([status for status, _ in checks], ["ok", "refused", "refused", "refused"])
        found, checks, data = self.read(dict(self.LEAK), conf="[privacy]\nrefuse host identifier\n")
        self.assertEqual(sorted((status, message.split(" in ")[0]) for status, message in checks[1:]),
                         [("note", "privacy: a personal path"),
                          ("refused", "privacy: a home-network host name under the key peerName"),
                          ("refused", "privacy: an installation or account identifier under the key installationId")])

    def test_a_refusal_that_was_mistyped_is_a_violation_and_never_a_note(self) -> None:
        """A repository that asked for a refusal must not be left, silently, with less."""
        for line in ("refuse hosts", "refuse  # every family", "refuse host, path", "Refuse", "refuse\nrefuse host"):
            with self.subTest(line=line):
                found, checks, data = self.read(dict(self.LEAK), conf=f"[privacy]\n{line}\n")
                refused = [message for status, message in checks if status == "refused" and "[privacy] line" in message]
                self.assertEqual(len(refused), 1, checks)
                self.assertIn("the line is not applied", refused[0])

    def test_a_refused_line_is_named_by_its_number_and_never_quoted(self) -> None:
        conf = f"[privacy]\n{HOST}\nrefuse {HOST}\nfabricated {PERSON}!\nexcept {HOME} family path x\nexcept a family {TERM} y\n"
        found, checks, data = self.read({"a.txt": "nothing\n"}, conf=conf, tracked=False)
        refused = [message for status, message in checks if status == "refused"]
        self.assertEqual(len(refused), 5)
        for number, message in zip(range(2, 7), refused):
            self.assertIn(f"[privacy] line {number}:", message)
        self.assert_silent(checks, data)

    def test_a_fabricated_name_is_declared_and_stops_being_a_finding(self) -> None:
        conf = f"[privacy]\nrefuse\nfabricated {PERSON} {HOST}\n"
        found, checks, _ = self.read({"notes.md": f"{HOME}\n", "a.json": json.dumps({"peerName": HOST})}, conf=conf)
        self.assertEqual(found, {})
        self.assertEqual([status for status, _ in checks], ["ok"])

    def test_an_exception_names_a_file_and_a_family_and_never_what_it_excuses(self) -> None:
        conf = ("[privacy]\nrefuse\n"
                "except records/*.json family host key peerName recorded before the guard, kept as recorded\n"
                "except records/ family identifier the same\n"
                "except notes.md family path a transcript kept as it was said\n"
                "except docs/old/ family email nothing is there any more\n")
        found, checks, data = self.read(dict(self.LEAK), conf=conf)
        self.assertEqual(found, {})
        self.assertEqual(sorted(finding["excused"] for finding in data["findings"]), [3, 3, 4, 5])
        self.assertIn("4 finding(s) excused by maelys-release.conf", checks[0][1])
        self.assertEqual(checks[1:], [("note", "privacy: maelys-release.conf line 6 excuses nothing in the tracked files;"
                                               " 'check DIR --history' says whether the history still needs it")])

    def test_an_exception_excuses_what_it_names_and_nothing_beside_it(self) -> None:
        files = {
            "records/a.json": json.dumps({"peerName": HOST, "cwd": HOME, "other": PRINTER}),
            "records/deep/er/b.json": json.dumps({"peerName": HOST}),
            "records/a.json.bak/c.json": json.dumps({"peerName": HOST}),
            "data[1].json": json.dumps({"peerName": HOST}),
            "data1.json": json.dumps({"peerName": HOST}),
            "top.json": json.dumps({"peerName": HOST}),
        }

        def live(conf):
            found, _, data = self.read(dict(files), conf="[privacy]\n" + conf + "\n")
            return sorted((finding["path"], finding["family"], finding.get("key"))
                          for family in found.values() for finding in family)

        everything = live("refuse")
        self.assertEqual(len(everything), 8)
        # One family, and under one key when a key is named: the path and the
        # other host of the same file stay.
        self.assertEqual(sorted(set(everything) - set(live("except records/a.json family host key peerName x"))),
                         [("records/a.json", "host", "peerName")])
        self.assertEqual(sorted(set(everything) - set(live("except records/a.json family host x"))),
                         [("records/a.json", "host", "other"), ("records/a.json", "host", "peerName")])
        # A star is one component, and the pattern is the whole path.
        self.assertEqual(sorted({path for path, _, _ in set(everything) - set(live("except records/*.json family host x"))}),
                         ["records/a.json"])
        self.assertEqual(sorted({path for path, _, _ in set(everything) - set(live("except *.json family host x"))}),
                         ["data1.json", "data[1].json", "top.json"])
        # A directory is named with its slash, and covers what is under it.
        self.assertEqual(sorted({path for path, _, _ in set(everything) - set(live("except records/ family host x"))}),
                         ["records/a.json", "records/a.json.bak/c.json", "records/deep/er/b.json"])
        self.assertEqual(live("except records family host x"), everything)
        # Brackets are themselves.
        self.assertEqual(sorted({path for path, _, _ in set(everything) - set(live("except data[1].json family host x"))}),
                         ["data[1].json"])

    def test_an_exception_that_says_what_it_excuses_is_the_leak_moved(self) -> None:
        files = {"a.json": json.dumps({"url": "https://" + HOST + ":8443/x", "cwd": HOME + "/src",
                                       "installationId": "{" + INSTALLATION + "}", "mail": ADDRESS})}
        base = ("[privacy]\nrefuse\nexcept a.json family host x\nexcept a.json family path x\n"
                "except a.json family identifier x\nexcept a.json family email x\n")
        found, checks, data = self.read(dict(files), conf=base)
        self.assertEqual([status for status, _ in checks], ["ok"])
        for line, said in ((f"# the host is {HOST.upper()}", 3), (f"# in the home of {PERSON}", 4),
                           (f"# {INSTALLATION.upper()}", 5), (f"# {ADDRESS}", 6)):
            with self.subTest(said=said):
                found, checks, data = self.read(dict(files), conf=base + line + "\n", tracked=False)
                self.assertIn(("refused", "privacy: maelys-release.conf holds, in the file itself, what its exception"
                                          f" at line {said} excuses: an exception designates a file and a family and"
                                          " never says what is there"), checks)
                self.assert_silent(checks, data)
        # Each alone names its own line and no other.
        found, checks, data = self.read(dict(files), conf=base + f"# the host is {HOST.upper()}\n", tracked=False)
        self.assertEqual(len([status for status, _ in checks if status == "refused"]), 1)

    def test_the_grammar_refuses_what_it_does_not_know_and_what_looks_like_a_digest(self) -> None:
        def refused(line):
            try:
                privacy.parse_line(line, 7, privacy.Settings())
            except ValueError as error:
                return str(error)
            return ""

        for line in ("refuse", "refuse host path", "fabricated a b", "except a/b.json family host because",
                     "except a/ family host key peerName re-recorded in 0.64.0", "except a/*.json family email x"):
            self.assertEqual(refused(line), "", line)
        for line, says in (
                ("refuse everything", "refuse takes nothing, or families among"),
                ("allow x", "neither refuse, fabricated nor except"),
                ("fabricated", "fabricated names nothing"),
                ("except a.json family host", "gives its reason on the same line"),
                ("except a.json key peerName because", "'PATH family FAMILY REASON'"),
                ("except a.json family secrets because", "with a family among"),
                ("except /etc/x family host because", "a path inside the repository"),
                ("except a/**/b.json family host because", "`*` stands for one component at most"),
                ("except * family host because", "not every one of them"),
                ("except */* family host because", "not every one of them"),
                ("except a.json family host key a" + "9f" * 16 + " because", "looks like a digest"),
                ("except a.json family host sha256:" + "ab" * 32, "looks like a digest"),
                ("except a.json family host md5:" + "0f" * 16, "looks like a digest"),
                ("except a.json family host its sha256=" + "c4" * 32 + " is known", "looks like a digest"),
                ("except a.json family host YXRlbGllci5ob21lIGlzIGEgaG9zdA9x", "looks like a digest"),
                ("except fixtures/" + "0a1b2c3d" * 2 + ".json family host kept", "looks like a digest"),
        ):
            with self.subTest(line=line):
                self.assertTrue(refused(line).startswith("line 7: "), refused(line))
                self.assertIn(says, refused(line))
        # Not one of the words a line was refused for is in the refusal.
        for word in ("everything", "allow", "secrets", "9f9f", "abab", "YXRl", "0a1b"):
            self.assertNotIn(word, "".join(refused(line) for line in (
                "refuse everything", "allow x", "except a.json family secrets because",
                "except a.json family host key a" + "9f" * 16 + " because", "except a.json family host sha256:" + "ab" * 32,
                "except a.json family host YXRlbGllci5ob21lIGlzIGEgaG9zdA9x",
                "except fixtures/" + "0a1b2c3d" * 2 + ".json family host kept")))
        # parse_release names the section and judges none of its lines: the guard does, for every repository.
        MODULE.parse_release("[privacy]\nrefuse everything\n")


class UnreadTest(Tree):
    def test_what_is_not_read_is_counted_and_nothing_is_said_of_it(self) -> None:
        leak = (HOME + "\n").encode()
        found, checks, data = self.read({
            "blob.bin": b"\0\1\2" + leak, "bundle.tar.gz": leak, "picture.png": leak, "manual.pdf": leak,
            "unnamed": b"\0\1\2" + leak,
            "big.txt": leak + b"x" * privacy.MAX_BYTES, "utf16.txt": ("\ufeff" + HOME).encode("utf-16"),
            "latin.txt": b"caf\xe9 " + leak, "ok.txt": "nothing\n"}, tracked=False)
        self.assertEqual({entry["path"]: entry["reason"] for entry in data["files"]["unread"]},
                         {"blob.bin": "binary", "bundle.tar.gz": "archive", "picture.png": "image",
                          "manual.pdf": "image", "big.txt": "size", "unnamed": "binary"})
        self.assertEqual((data["files"]["tracked"], data["files"]["read"]), (9, 3))
        self.assertIn("3 of 9 files read, every file since this is not a git repository (not read, and nothing is"
                      " said of them: 1 archive, 2 binary, 2 image, media or document whose metadata is not read,"
                      " 1 above 4 MiB)", checks[0][1])
        # What is text in another encoding is read.
        self.assertEqual(sorted(finding["path"] for finding in found["path"]), ["latin.txt", "utf16.txt"])

    def test_in_a_repository_git_names_the_candidates_and_only_those_are_opened(self) -> None:
        leak = (HOME + "\n").encode()
        tree({"tracked.md": f"{HOME}\n", "untracked.md": f"{LINUX_HOME}\n", "inner/file": "x\n",
              "quiet/a.txt": "nothing\n", "quiet/b.py": "x = 1\n", "unnamed": b"\0\1\2" + leak,
              "big.txt": leak + b"x" * privacy.MAX_BYTES, "picture.png": leak}, self.dir)
        git(self.dir / "inner", "init", "-q")
        git(self.dir / "inner", "add", "file")
        git(self.dir / "inner", "commit", "-qm", "inner")
        os.symlink(HOME + "/elsewhere", self.dir / "link")
        git(self.dir, "init", "-q")
        git(self.dir, "add", "tracked.md", "link", "inner", "quiet", "unnamed", "big.txt", "picture.png")
        opened = []
        original = privacy.decode
        privacy.decode = lambda data: opened.append(len(data)) or original(data)
        try:
            checks, data = self.guard(MODULE.Host())
        finally:
            privacy.decode = original
        self.assertEqual(sorted(finding["path"] for finding in data["findings"]), ["link", "tracked.md"])
        self.assertEqual(sorted((entry["path"], entry["reason"]) for entry in data["files"]["unread"]),
                         [("big.txt", "size"), ("inner", "submodule"), ("picture.png", "image"), ("unnamed", "binary")])
        # Two files were opened: the one that holds a path, and the binary git named for the same bytes.
        self.assertEqual(len(opened), 2)
        self.assertEqual((data["files"]["tracked"], data["files"]["searched"], data["files"]["read"]), (8, 4, 1))
        self.assertIn("privacy: 4 of 8 tracked files searched for the shapes of six families, and the 1 that held one"
                      " read (not read, and nothing is said of them: 1 binary, 1 image, media or document whose"
                      " metadata is not read, 1 above 4 MiB, 1 submodule)", checks[0][1])

    def test_a_tracked_name_that_is_not_utf_8_is_counted_and_ends_nothing(self) -> None:
        class Bytes(FakeHost):
            def run(self, command, cwd=None, env=None):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

            def feed(self, command, data, cwd=None):
                return subprocess.CompletedProcess(command, 0, b"a.md\0caf\xe9.txt\0", b"")

        (self.dir / ".git").mkdir()
        with using_host(Bytes()):
            self.assertEqual(privacy.tracked_files(self.dir), (["a.md"], 1))


class CostTest(Tree):
    """Linear in what it reads, and bounded: `check` runs on every pull request."""

    def shapes(self, size: int) -> dict:
        def fill(unit):
            return (unit * (size // len(unit) + 1))[:size]

        return {
            "paths.log": fill(LINUX_HOME + "/work/file\n"),
            "addresses.log": fill("bob" + "@" + "fabrique-a.org wrote\n"),
            "exempt.txt": fill("someone@example.org wrote\n"),
            "hosts.yml": fill("peer: " + PRINTER + "\n"),
            "items.yml": "peers:\n" + fill("  - " + PRINTER + "\n"),
            "spaces.yml": "key: value" + " " * (size - 20) + "# end\n",
            "identity-spaces.yml": "installationId: a" + " " * (size - 30) + "b\n",
            "identities.yml": fill("installationId: " + INSTALLATION + "\n"),
            "dense.json": "[" + ",".join(['{"a":1,"userId":48213907}'] * (size // 27)) + "]",
            "strings.json": "[" + ",".join(['"' + LINUX_HOME + '/x"'] * (size // 17)) + "]",
            "oneline.txt": fill("word " + HOME + " "),
            "lines.jsonl": fill(json.dumps({"peerName": PRINTER, "n": 1}) + "\n"),
            "nothing.txt": fill("nothing here to see at all\n"),
        }

    def cpu(self, name: str, text: str, who) -> float:
        """The least of three readings of TEXT, in processor time.

        One reading is a few hundredths of a second for most shapes, and a
        ratio between two single ones is as much a measure of the machine
        that minute as of the reading: on a shared runner, with the suite's
        other classes running beside it, 0.224 s against a bound of 0.205 s
        failed a reading that had not changed. The least of three is the
        reading; the rest is what else the machine was doing.
        """
        spent = []
        for _ in range(3):
            reading = privacy.Reading(privacy.Settings(), who)
            reading.open(name)
            started = time.process_time()
            reading.text(text, privacy.kind(name))
            spent.append(time.process_time() - started)
        return min(spent)

    def test_every_shape_is_read_in_time_linear_in_its_size(self) -> None:
        """Measured where it was written: 4 MiB of any of these in under a second, 0.73 s for the worst.

        Held here at half the size, on processor time, by two bounds. The
        ratio is the one that says linear: twice the text in less than three
        times the time, which each of the three readings that were not linear
        failed -- they took 2.3 s, 53 s and 58 s. The clock only says bounded,
        and is held wide: the worst shape at this size takes 0.57 s where this
        was written and took 1.04 s on a shared macOS runner, which a bound of
        one second called a failure of a reading that had not changed.
        """
        mebibyte = 1024 * 1024
        who = local([MACHINE], [(1, TERM)])
        one, two = self.shapes(mebibyte), self.shapes(2 * mebibyte)
        for name in one:
            with self.subTest(name=name):
                first = self.cpu(name, one[name], who)
                second = self.cpu(name, two[name], who)
                self.assertLess(second, 3.0)
                self.assertLess(second, 3 * first + 0.05)

    def test_a_file_that_holds_too_much_is_read_up_to_there_and_said_to_be(self) -> None:
        lines = "".join(f"{LINUX_HOME}{number}/x\n" for number in range(3 * privacy.KEPT))
        found, checks, data = self.read({"paths.log": lines, "small.md": HOME + "\n"})
        self.assertEqual(len([finding for finding in found["path"] if finding["path"] == "paths.log"]), privacy.KEPT)
        self.assertEqual(data["files"]["partial"], ["paths.log"])
        self.assertTrue(any(f"1 file held more than {privacy.KEPT} findings of one family" in message
                            and "paths.log" in message for _, message in checks), checks)

    def test_a_tree_of_small_files_costs_one_reading_by_git_and_no_opening_here(self) -> None:
        """50 000 files that hold nothing: 3 to 4 s where this was written, against 55 s when each was opened.

        Held here on a twentieth of that tree, by what makes the difference
        and not by a clock: no file is opened that git did not name.
        """
        for directory in range(50):
            tree({f"d{directory:02d}/f{number:02d}.txt": f"file {number}\nnothing here\n" for number in range(50)}, self.dir)
        tree({"d00/held.txt": HOME + "\n"}, self.dir)
        git(self.dir, "init", "-q")
        git(self.dir, "add", "-A")
        opened = []
        original = privacy.decode
        privacy.decode = lambda data: opened.append(1) or original(data)
        started = time.process_time()
        try:
            checks, data = self.guard(MODULE.Host(), machines=[MACHINE], terms=[(1, TERM)])
        finally:
            privacy.decode = original
        self.assertEqual((data["files"]["tracked"], data["files"]["searched"], data["files"]["read"]), (2501, 2501, 1))
        self.assertEqual(len(opened), 1)
        self.assertLess(time.process_time() - started, 1.0)
        # And where nothing holds a shape, nothing is opened and the line says so.
        (self.dir / "d00" / "held.txt").write_text("nothing\n", encoding="utf-8")
        checks, data = self.guard(MODULE.Host())
        self.assertIn("privacy: 2501 of 2501 tracked files searched for the shapes of six families, and none held one;",
                      checks[0][1])
        self.assertEqual(data["files"]["read"], 0)

    def test_git_is_asked_in_the_dialect_it_has_and_both_mean_the_same(self) -> None:
        """A git built without Perl's expressions answers in POSIX's, and names the same files."""
        files = {
            "a.json": json.dumps({"peerName": HOST}), "b.yml": "User ID: 48213907\n", "c.toml": "[user]\nid = 48213907\n",
            "d.md": HOME + "\n", "e.py": "# " + ADDRESS + "\n", "f.txt": "C:" + "\\" + "Users" + "\\" + PERSON + "\n",
            "g.json": '{"cwd": "' + HOME.replace("/", "\\/") + '"}', "h.ini": INSTALLATION + " = 1\n",
            "i.json": json.dumps({"peer": "nas.home.arpa"}), "j.c": "x = args.home;\n", "k.txt": "nothing\n",
        }
        expected, _, _ = self.read(dict(files), tracked=False)
        expected = sorted(map(repr, (where(finding) for family in expected.values() for finding in family)))
        git(self.dir, "init", "-q")
        git(self.dir, "add", "-A")
        asked = []

        class Plain(MODULE.Host):
            def run(self, command, cwd=None, env=None):
                if command[:2] == ["git", "grep"]:
                    asked.append(command[5])
                    if "-P" in command:
                        return subprocess.CompletedProcess(command, 128, "", "fatal: cannot use Perl-compatible regexes")
                return super().run(command, cwd=cwd, env=env)

        saved = list(privacy.DIALECTS)
        try:
            for hosted, dialects in ((MODULE.Host(), ["-P", "-E"]), (Plain(), ["-P", "-E"]), (MODULE.Host(), ["-E"])):
                privacy.DIALECTS[:] = dialects
                _, data = self.guard(hosted)
                self.assertEqual(sorted(map(repr, map(where, data["findings"]))), expected)
                self.assertEqual(data["files"]["read"], 9)
            # Refused once, and not asked that way again.
            self.assertEqual(asked, ["-P", "-E", "-E"])
        finally:
            privacy.DIALECTS[:] = saved

    def test_a_name_of_this_machine_reaches_git_on_its_input_and_in_no_argument(self) -> None:
        tree({"a.txt": "on " + MACHINE.lower() + " today\n", "b.txt": "the " + TERM + " project\n"}, self.dir)
        git(self.dir, "init", "-q")
        git(self.dir, "add", "-A")
        seen = []

        class Watching(MODULE.Host):
            def run(self, command, cwd=None, env=None):
                seen.append(" ".join(command))
                return super().run(command, cwd=cwd, env=env)

            def feed(self, command, data, cwd=None):
                seen.append(" ".join(command))
                return super().feed(command, data, cwd=cwd)

        checks, data = self.guard(Watching(), machines=[MACHINE], terms=[(1, TERM)])
        self.assertEqual(sorted(finding["family"] for finding in data["findings"]), ["machine", "term"])
        asked = [command for command in seen if command.startswith("git grep")]
        self.assertEqual(len(asked), 2)
        for command in seen:
            self.assertNotIn("atelier", command.lower())
            self.assertNotIn(TERM, command.lower())
        # What is not read for its name is not searched either: git is told so, in both calls.
        for command in asked:
            for suffix in (".png", ".gz", ".woff2"):
                self.assertIn(f":(exclude,icase)*{suffix}", command)


class HistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = tempfile.TemporaryDirectory(prefix="maelys-privacy-")
        self.addCleanup(self.work.cleanup)
        self.dir = pathlib.Path(self.work.name).resolve() / "product"
        self.dir.mkdir()
        self.git("init", "-q", "-b", "main")

    def git(self, *arguments: str, identity=("t", "t@example.invalid"), directory=None, data=None) -> str:
        return git(directory or self.dir, *arguments, identity=identity, data=data)

    def commit(self, files: dict, message: str, **options) -> None:
        tree(files, self.dir)
        self.git("add", "-A")
        self.git("commit", "-qm", message, **options)

    def history(self, directory=None, machines=(), terms=()):
        original = privacy.read_local
        privacy.read_local = lambda: local(machines, terms)
        try:
            with using_host(MODULE.Host()):
                return privacy.check_tree(directory or self.dir, history=True)
        finally:
            privacy.read_local = original

    def silent(self, checks, data) -> None:
        said = (json.dumps(data) + "".join(message for _, message in checks)).lower()
        for secret in SECRETS:
            self.assertNotIn(secret.lower(), said)

    def test_a_clean_tree_does_not_clear_its_history(self) -> None:
        record = json.dumps({"peerName": HOST, "installationId": INSTALLATION})
        self.commit({"records/a.json": record, "records/copy.json": record, "README.md": "x\n"}, "records")
        self.commit({"records/a.json": json.dumps({"peerName": "host.example"}),
                     "records/copy.json": "{}"}, f"anonymized, they were recorded in {HOME}")
        self.git("commit", "-q", "--allow-empty", "-m", "from an unconfigured machine",
                 identity=(PERSON, f"{PERSON}@{HOST}"))
        self.git("tag", "-a", "v1.0.0", "-m", f"cut on {HOST}, see {LINUX_HOME}")
        checks, data = self.history()
        # The tree is clean, and says so.
        self.assertEqual(data["findings"], [])
        history = data["history"]
        found = sorted((finding["family"], finding["path"].split(" of ")[0], finding.get("key"))
                       for finding in history["findings"])
        # One blob under two paths is one blob: it is read once, under the first name it has.
        self.assertEqual(found, [("host", "records/a.json", "peerName"),
                                 ("host", "the author", None), ("host", "the committer", None),
                                 ("identifier", "records/a.json", "installationId"),
                                 ("path", "the message", None), ("path", "the message", None)])
        self.assertEqual((history["commits"], history["refs"]), (4, {"branch": 1, "tag": 1}))
        self.silent(checks, data)
        self.assertTrue(any("privacy: history: an installation or account identifier under the key installationId"
                            " in 1 blob of 1 path" in message for _, message in checks), checks)
        self.assertTrue(any("in what 2 commits or tags say, or in who made them" in message for _, message in checks))
        # Nothing in a history refuses, whatever the repository asked of its tree.
        tree({"maelys-release.conf": "[privacy]\nrefuse\n"}, self.dir)
        checks, _ = self.history()
        self.assertEqual({status for status, _ in checks}, {"ok", "note"})

    def test_every_blob_of_a_path_is_counted_and_named_by_what_it_is(self) -> None:
        for number in range(3):
            self.commit({"records/a.json": json.dumps({"peerName": HOST, "n": number})}, f"record {number}")
        self.commit({"records/a.json": "{}"}, "clean")
        checks, data = self.history()
        blobs = [finding["blob"] for finding in data["history"]["findings"]]
        self.assertEqual(len(set(blobs)), 3)
        self.assertEqual(sorted(blobs), sorted(
            self.git("rev-parse", f"HEAD~{back}:records/a.json")[:12] for back in (1, 2, 3)))
        self.assertTrue(any("under the key peerName in 3 blobs of 1 path" in message for _, message in checks), checks)

    def test_every_ref_is_read_and_not_the_one_that_is_checked_out(self) -> None:
        self.commit({"a.txt": "x\n"}, "one")
        base = self.git("rev-parse", "HEAD")
        for ref, name in (("refs/heads/other", "branch"), ("refs/tags/kept", "tag"),
                          ("refs/remotes/origin/gone", "remote-tracking ref"), ("refs/pull/7/head", "pull request head"),
                          ("refs/notes/x", "other ref")):
            with self.subTest(ref=ref):
                self.git("checkout", "-q", "--detach", base)
                self.commit({"records/a.json": json.dumps({"peerName": HOST, "from": ref})}, "a record")
                self.git("update-ref", ref, "HEAD")
                self.git("checkout", "-q", "main")
                checks, data = self.history()
                self.assertEqual(data["findings"], [])
                self.assertEqual([(finding["family"], finding["key"]) for finding in data["history"]["findings"]],
                                 [("host", "peerName")])
                self.assertEqual(data["history"]["refs"], {"branch": 1, name: 1} if name != "branch" else {"branch": 2})
                self.git("update-ref", "-d", ref)

    def test_the_name_of_a_ref_is_a_value_and_is_searched(self) -> None:
        self.commit({"a.txt": "x\n"}, "one")
        self.git("branch", "record/" + HOST)
        self.git("branch", "wip/" + MACHINE.lower())
        self.git("tag", "from-" + INSTALLATION)
        self.git("branch", "fix/the-parser")
        checks, data = self.history(machines=[MACHINE])
        found = sorted((finding["family"], finding["in"], finding["path"]) for finding in data["history"]["findings"])
        self.assertEqual([(family, place) for family, place, _ in found],
                         [("host", "ref"), ("identifier", "ref"), ("machine", "ref")])
        for _, _, path in found:
            self.assertRegex(path, r"^ref \((branch|tag)\) [0-9]+, .*\(not shown\)")
        self.assertTrue(any("and the name of each ref" in message for _, message in checks))
        self.assertTrue(any("this machine's name in the name of 1 ref" in message for _, message in checks), checks)
        self.silent(checks, data)

    def test_it_says_what_it_did_not_read(self) -> None:
        self.commit({"a.txt": "x\n", "b.png": b"\x89PNG", "c.bin": b"\0\1", "unnamed": b"\0\1" + HOME.encode(),
                     "big.txt": HOME + "\n" + "x" * privacy.MAX_BYTES}, "one")
        self.commit({"a.txt": "y\n", "big.txt": "small now\n", "unnamed": "text now\n"}, "two")
        checks, data = self.history()
        last = checks[-1][1]
        self.assertIn("refs/pull/N/head) are not in an ordinary clone and were not read", last)
        self.assertIn("objects not read: 2 binary, 1 image, media or document whose metadata is not read, 1 above 4 MiB",
                      last)
        self.assertEqual(data["history"]["blobs"], {"read": 4, "unread": {"binary": 2, "image": 1, "size": 1}})
        self.assertEqual(data["history"]["findings"], [])
        self.assertFalse(data["history"]["pullRequestHeads"])
        self.assertNotIn("shallow", last)
        self.assertNotIn("could not list", last)
        # A pull request head that was fetched is read like any ref, and no longer missing.
        self.git("update-ref", "refs/pull/7/head", self.git("rev-parse", "HEAD~1"))
        checks, data = self.history()
        self.assertNotIn("refs/pull/N/head", checks[-1][1])
        self.assertTrue(data["history"]["pullRequestHeads"])
        # A shallow clone says the history beyond it was not read.
        shallow = pathlib.Path(self.work.name).resolve() / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + str(self.dir), str(shallow)],
                       check=True, capture_output=True)
        checks, data = self.history(shallow)
        self.assertTrue(data["history"]["shallow"])
        self.assertIn("this clone is shallow, and the history beyond what it holds was not read", checks[-1][1])

    def test_a_listing_git_refused_is_said_and_is_not_an_empty_history(self) -> None:
        self.commit({"records/a.json": json.dumps({"peerName": HOST})}, "one")

        class Refusing(MODULE.Host):
            def feed(self, command, data, cwd=None):
                if command[:2] == ["git", "rev-list"]:
                    return subprocess.CompletedProcess(command, 128, b"", b"fatal")
                return super().feed(command, data, cwd=cwd)

        original = privacy.read_local
        privacy.read_local = lambda: local()
        try:
            with using_host(Refusing()):
                checks, data = privacy.check_tree(self.dir, history=True)
        finally:
            privacy.read_local = original
        self.assertIn("git could not list the objects of this clone, and its trees were not read", checks[-1][1])
        self.assertEqual(data["history"]["blobs"]["read"], 0)

    def test_a_path_that_is_not_utf_8_is_counted_and_ends_nothing(self) -> None:
        """No traceback, ever: a history holds names no terminal can print."""
        self.commit({"a.txt": "x\n"}, "one")
        held = subprocess.run(["git", "-C", str(self.dir), "hash-object", "-w", "--stdin"], check=True,
                              capture_output=True, input=json.dumps({"peerName": HOST}).encode()).stdout.strip()
        listed = subprocess.run(["git", "-C", str(self.dir), "mktree", "-z"], check=True, capture_output=True,
                                input=b"100644 blob " + held + b"\tcaf\xe9.json\0").stdout.strip().decode()
        made = self.git("commit-tree", listed, "-p", "HEAD", "-m", "a name from another age")
        self.git("update-ref", "refs/heads/old", made)
        checks, data = self.history()
        self.assertEqual(data["history"]["blobs"]["unread"], {"undecodable": 1})
        self.assertIn("objects not read: 1 under a name that is not UTF-8", checks[-1][1])
        self.assertEqual(data["history"]["findings"], [])
        completed = subprocess.run([str(pathlib.Path(MODULE.__file__)), "check", str(self.dir), "--history",
                                    "--socle-sha", "f" * 40, "--socle-tag", "v9.9.9"],
                                   capture_output=True, text=True, check=False)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)
        self.assertIn("under a name that is not UTF-8", completed.stdout)

    def test_an_exception_reaches_the_history_and_is_said_once(self) -> None:
        conf = "[privacy]\nexcept records/ family host recorded before the guard\nexcept old/ family path gone\n"
        self.commit({"records/a.json": json.dumps({"peerName": HOST, "installationId": INSTALLATION}),
                     "maelys-release.conf": conf}, "one")
        self.commit({"records/a.json": "{}"}, "clean")
        checks, data = self.history()
        self.assertEqual(sorted((finding["family"], finding.get("excused")) for finding in data["history"]["findings"]),
                         [("host", 2), ("identifier", None)])
        said = [message for _, message in checks if "excuses nothing" in message]
        # The line the history still needs is not said to excuse nothing; the other is, once.
        self.assertEqual(said, ["privacy: maelys-release.conf line 3 excuses nothing in the tracked files nor in the"
                                " history: the line can go"])
        self.assertFalse(any("host" in message and "blob" in message for _, message in checks), checks)
        # Without the history, the tree cannot know, and says where to ask.
        original = privacy.read_local
        privacy.read_local = lambda: local()
        try:
            with using_host(MODULE.Host()):
                checks, _ = privacy.check_tree(self.dir)
        finally:
            privacy.read_local = original
        self.assertEqual(len([message for _, message in checks if "says whether the history still needs it" in message]), 2)

    def test_an_object_a_partial_clone_never_fetched_is_said_to_be_outside_the_clone(self) -> None:
        self.commit({"a.txt": "x\n", "b.txt": "y\n"}, "one")
        self.git("config", "uploadpack.allowFilter", "true")
        partial = pathlib.Path(self.work.name).resolve() / "partial"
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", "file://" + str(self.dir),
                        str(partial)], check=True, capture_output=True)
        # The remote stops answering: what was never fetched cannot be now.
        self.git("remote", "set-url", "origin", "file://" + str(partial / "gone"), directory=partial)
        checks, data = self.history(partial)
        self.assertEqual(data["history"]["blobs"], {"read": 0, "unread": {"absent": 2}})
        self.assertIn("objects not read: 2 named by a tree and not in this clone", checks[-1][1])
        self.assertNotIn("working tree", checks[-1][1])

    def test_the_addresses_of_a_history_are_counted_and_left_to_the_author(self) -> None:
        self.commit({"a.txt": "x\n"}, "one", identity=(PERSON, ADDRESS))
        self.commit({"a.txt": "y\n"}, "two\n\nCo-authored-by: Bob <" + "bob@" + "fabrique-coauthor.org>")
        checks, data = self.history()
        self.assertEqual(data["history"]["addresses"], 2)
        self.assertEqual(data["history"]["findings"], [])
        self.assertTrue(any("2 distinct e-mail addresses in author, committer and tagger lines" in message
                            and "git log --all --format=%ae%n%ce" in message for _, message in checks), checks)
        self.assertNotIn("fabrique", json.dumps(data) + "".join(message for _, message in checks))


class CommandTest(unittest.TestCase):
    """Through the executable: the verdict, the exit code, and what leaves the process."""

    def setUp(self) -> None:
        self.product = Product()
        self.addCleanup(self.product.close)
        self.dir = str(self.product.dir)
        self.product.run("adopt", self.dir, "--apply")
        # A runner says so in its environment, and these tests are about an author's machine.
        for name in ("CI", "GITHUB_ACTIONS"):
            self.product.env.pop(name, None)

    def test_a_product_that_was_green_stays_green_and_reads_a_note(self) -> None:
        clean = self.product.json("check", self.dir)["data"]
        self.assertTrue(clean["valid"])
        self.assertEqual(clean["privacy"]["findings"], [])
        self.assertTrue([check for check in clean["checks"] if check["message"].startswith("privacy: ")
                         and check["status"] == "ok" and "nothing found" in check["message"]])
        self.product.write("records/a.json", json.dumps({"peerName": HOST, "installationId": INSTALLATION}))
        data = self.product.json("check", self.dir)["data"]
        self.assertTrue(data["valid"])
        self.assertEqual(sorted((finding["family"], finding["path"], finding["key"])
                                for finding in data["privacy"]["findings"]),
                         [("host", "records/a.json", "peerName"),
                          ("identifier", "records/a.json", "installationId")])
        text = self.product.run("check", self.dir).stdout
        self.assertIn("note     privacy: a home-network host name under the key peerName in 1 tracked file:"
                      " records/a.json", text)
        for secret in SECRETS:
            self.assertNotIn(secret.lower(), (text + json.dumps(data)).lower())

    def test_a_repository_that_asked_for_a_refusal_gets_one_and_adopt_is_not_held_by_it(self) -> None:
        self.product.write("maelys-release.conf", self.product.read("maelys-release.conf") + "\n[privacy]\nrefuse\n")
        self.assertTrue(self.product.json("check", self.dir)["data"]["valid"])
        self.product.write("records/a.json", json.dumps({"peerName": HOST}))
        data = self.product.json("check", self.dir, expect=2)["data"]
        self.assertFalse(data["conventions"]["valid"])
        self.assertEqual(len(data["violations"]), 1)
        self.assertIn("REFUSED  privacy: a home-network host name under the key peerName",
                      self.product.run("check", self.dir, expect=2).stdout)
        # The guard is check's: an adoption writes its files whatever the records hold.
        self.product.run("adopt", self.dir, "--apply")
        self.assertTrue(self.product.json("declarations", self.dir)["data"]["valid"])

    def test_a_mistyped_refusal_is_a_violation_whatever_publishes_the_repository(self) -> None:
        conf = self.product.read("maelys-release.conf")
        self.product.write("maelys-release.conf", conf + "\n[privacy]\nrefuse hosts\n")
        self.product.write("records/a.json", json.dumps({"peerName": HOST}))
        released = self.product.json("check", self.dir, expect=2)["data"]
        self.assertFalse(released["conventions"]["valid"])
        self.assertEqual([violation for violation in released["violations"] if "[privacy] line" in violation],
                         released["conventions"]["violations"])
        self.assertEqual(len(released["conventions"]["violations"]), 1)
        # The same file in a repository the socle does not release: the same violation, and nothing less.
        own = self.product.work / "own"
        tree({"VERSION": "1.2.3\n", "CHANGELOG.md": "# Changelog\n\n## 1.2.3 — 2026-09-03\n\n- x\n",
              "maelys-release.conf": "[privacy]\nrefuse hosts\n",
              "records/a.json": json.dumps({"peerName": HOST})}, own)
        custom = self.product.json("check", str(own), "--mechanism", "custom", "--product", "own", expect=2)["data"]
        said = [violation for violation in custom["conventions"]["violations"] if "[privacy] line 2" in violation]
        self.assertEqual(len(said), 1, custom["conventions"])
        self.assertIn("the line is not applied", said[0])
        self.assertNotIn("hosts", said[0])

    def test_history_is_asked_for_and_is_not_read_otherwise(self) -> None:
        self.assertNotIn("history", self.product.json("check", self.dir)["data"]["privacy"])
        # A directory that is not a repository has none, and is told so rather than "zero".
        nothing = self.product.json("check", self.dir, "--history")["data"]
        self.assertNotIn("history", nothing["privacy"])
        self.assertTrue([check for check in nothing["checks"]
                         if "is not a git repository, and has no history to read" in check["message"]])
        git = self.product.git
        git(self.product.dir, "init", "-q")
        self.product.write("records/a.json", json.dumps({"peerName": HOST}))
        git(self.product.dir, "add", "-A")
        git(self.product.dir, "commit", "-qm", "with the record")
        self.product.write("records/a.json", "{}")
        git(self.product.dir, "commit", "-qam", "without")
        data = self.product.json("check", self.dir, "--history")["data"]
        self.assertEqual(data["privacy"]["findings"], [])
        self.assertEqual([(finding["family"], finding["path"], finding["key"])
                          for finding in data["privacy"]["history"]["findings"]],
                         [("host", "records/a.json", "peerName")])

    def test_a_value_in_every_place_a_repository_has_and_in_no_output_of_any_form(self) -> None:
        """One tree, a fabricated value in each position, and every form `check` can answer in."""
        other = "bob" + "@" + "fabrique-" + uuid.uuid4().hex[:8] + ".net"
        device = str(uuid.uuid4())
        secrets = SECRETS + (other, device)
        product, root = self.product, self.product.dir
        # What names this machine and what its author keeps private, both written by the test.
        (product.work / "bin" / "hostname").write_text(f"#!/bin/sh\necho {MACHINE}\n", encoding="utf-8")
        (product.work / "bin" / "hostname").chmod(0o755)
        terms = product.work / "config" / "maelys-release"
        terms.mkdir(parents=True)
        (terms / "private-terms").write_text(TERM + "\n", encoding="utf-8")
        product.env["XDG_CONFIG_HOME"] = str(product.work / "config")
        product.write("maelys-release.conf", product.read("maelys-release.conf")
                      + f"\n[privacy]\nrefuse {HOST}\n{PRINTER}\nexcept {HOME} family path {TERM}\n"
                        f"fabricated {device}!\n")
        git = product.git
        git(root, "init", "-q")
        # a JSON value, a JSON key, the key of a line-read file
        product.write("records/value.json", json.dumps({
            "peerName": HOST, "installationId": INSTALLATION, "cwd": HOME + "/src", "mail": ADDRESS,
            "on": MACHINE.lower(), "for": TERM, "device": {"id": device}}))
        product.write("records/key.json", json.dumps({HOST: 1, INSTALLATION: 2, ADDRESS: 3, HOME + "/x": 4,
                                                        MACHINE.lower(): 5, TERM: 6, PERSON: {"peerName": PRINTER}}))
        product.write("conf/keys.yml", f"{HOST}: {PRINTER}\n{MACHINE.lower()}: up\n{TERM}: 1\n{device}: 2\n")
        product.write("broken.json", '{\n  "' + HOST + '": "' + PRINTER + '",\n}\n')
        # the name of a file, the name of a directory
        product.write(f"hosts/{HOST}/notes.txt", "x\n")
        product.write(f"dumps/{device}.bin", "x\n")
        product.write(f"fixtures/Users/{PERSON}/c.txt", "x\n")
        product.write(f"mail/{other}.txt", "x\n")
        product.write(f"notes/{TERM}/{MACHINE.lower()}.md", "x\n")
        git(root, "add", "-A")
        # a commit message, an identity, the name of a ref
        subprocess.run(["git", "-C", str(root), "-c", f"user.name={PERSON}", "-c", f"user.email={PERSON}@{HOST}",
                        "-c", "commit.gpgsign=false", "commit", "-qm",
                        f"recorded on {MACHINE} for {TERM}, in {HOME}, by {other}"],
                       check=True, env=product.env, capture_output=True)
        git(root, "branch", "record/" + HOST)
        git(root, "branch", "wip/" + MACHINE.lower() + "/" + TERM)
        git(root, "tag", "from-" + device)
        # and what was there before and is not any more
        product.write("records/value.json", "{}")
        git(root, "commit", "-qam", "anonymized")
        product.write("records/value.json", json.dumps({"peerName": HOST, "installationId": INSTALLATION,
                                                          "cwd": HOME + "/src", "mail": ADDRESS,
                                                          "on": MACHINE.lower(), "for": TERM, "device": {"id": device}}))
        forms = (("check", self.dir), ("check", self.dir, "--history"),
                 ("check", self.dir, "--history", "--format", "json"),
                 ("check", self.dir, "--history", "--format", "json", "--compact"),
                 ("check", self.dir, "--history", "--format", "jsonl", "--field", "checks"),
                 ("check", self.dir, "--history", "--format", "jsonl", "--field", "privacy"),
                 ("check", self.dir, "--history", "--format", "jsonl", "--field", "violations"),
                 ("check", self.dir, "--format", "text", "--field", "checks"),
                 ("preflight", self.dir), ("preflight", self.dir, "--format", "json"))
        for form in forms:
            with self.subTest(form=" ".join(form[2:]) or form[0]):
                completed = product.cli(*form)
                said = (completed.stdout + completed.stderr).lower()
                self.assertNotIn("traceback", said)
                self.assertTrue("privacy" in said or "findings" in said, said[:200])
                for secret in secrets:
                    self.assertNotIn(secret.lower(), said)
        # Not silent for having found nothing: each position is a finding, by its place.
        data = product.json("check", self.dir, "--history", expect=2)["data"]["privacy"]
        self.assertEqual(data["local"], {"machineNames": 1, "terms": 1, "runner": False})
        places = {(finding["family"], finding.get("in")) for finding in data["findings"]}
        for family in privacy.FAMILIES:
            for place in (None, "key", "name"):
                self.assertIn((family, place), places)
        past = {(finding["family"], finding.get("in")) for finding in data["history"]["findings"]}
        self.assertTrue({("host", "ref"), ("machine", "ref"), ("term", "ref"), ("identifier", "ref")} <= past, past)
        # A name the history gave a file is read where every name is, whatever blob it shares.
        self.assertTrue({(family, "name") for family in privacy.FAMILIES} <= past, past)
        self.assertTrue({"host", "path", "machine", "term"} <= {finding["family"] for finding in data["history"]["findings"]
                                                              if "commit" in finding})
        # The four lines of [privacy] that are not lines of it: four violations, by number.
        checks = product.json("check", self.dir, expect=2)["data"]["checks"]
        self.assertEqual(len([check for check in checks if check["status"] == "refused"
                              and "[privacy] line" in check["message"]]), 4)

    def test_on_a_runner_the_machine_and_the_terms_are_not_looked_for_and_refuse_nothing(self) -> None:
        (self.product.work / "bin" / "hostname").write_text("#!/bin/sh\necho fixture\n", encoding="utf-8")
        (self.product.work / "bin" / "hostname").chmod(0o755)
        self.product.write("maelys-release.conf", self.product.read("maelys-release.conf") + "\n[privacy]\nrefuse\n")
        # The tree holds the very word the machine is called: `maelys-fixture`, in every managed file.
        refused = self.product.json("check", self.dir, expect=2)["data"]
        self.assertEqual({finding["family"] for finding in refused["privacy"]["findings"]}, {"machine"})
        for name in ("CI", "GITHUB_ACTIONS"):
            with self.subTest(name=name):
                self.product.env[name] = "true"
                there = self.product.json("check", self.dir)["data"]
                del self.product.env[name]
                self.assertTrue(there["valid"])
                self.assertEqual(there["privacy"]["findings"], [])
                self.assertEqual(there["privacy"]["local"], {"machineNames": 0, "terms": 0, "runner": True})
                self.assertTrue([check for check in there["checks"] if "not looked for on a CI runner" in check["message"]])
        # One definition of a runner, shared with the address note: a variable
        # set to `false` or `0` on an author's machine is an author's machine.
        for value in ("false", "0"):
            with self.subTest(value=value):
                self.product.env["CI"] = value
                here = self.product.json("check", self.dir, expect=2)["data"]
                del self.product.env["CI"]
                self.assertFalse(here["privacy"]["local"]["runner"])
                self.assertEqual({finding["family"] for finding in here["privacy"]["findings"]}, {"machine"})


class SecretScanningTest(unittest.TestCase):
    """preflight's note, from the document it already read: no call of its own."""

    def says(self, body) -> list:
        return MODULE.github.secret_scanning("o/r", MODULE.github.Repository("ok", body))

    def test_a_public_repository_without_them_is_told_and_nobody_else_is(self) -> None:
        def analysis(scanning, protection):
            return {"secret_scanning": {"status": scanning}, "secret_scanning_push_protection": {"status": protection}}

        both = self.says({"visibility": "public", "security_and_analysis": analysis("disabled", "disabled")})
        self.assertEqual([status for status, _ in both], ["note"])
        self.assertIn("o/r is public and secret scanning and push protection are off", both[0][1])
        one = self.says({"visibility": "public", "security_and_analysis": analysis("enabled", "disabled")})
        self.assertIn("o/r is public and push protection is off", one[0][1])
        self.assertEqual(self.says({"visibility": "public", "security_and_analysis": analysis("enabled", "enabled")}),
                         [("ok", "o/r is public, with secret scanning and push protection on")])
        # A private repository is not offered them, and a reader who does not
        # administer the repository is not shown the member: nothing is said.
        self.assertEqual(self.says({"visibility": "private", "private": True,
                                    "security_and_analysis": analysis("disabled", "disabled")}), [])
        self.assertEqual(self.says({"visibility": "public"}), [])
        self.assertEqual(MODULE.github.secret_scanning("o/r", MODULE.github.Repository("unreadable", None)), [])

    def test_preflight_says_it_from_a_constructed_answer_and_only_when_the_member_says_off(self) -> None:
        """Through the reader `preflight` and `cut` call, on a scripted host.

        The recorded reference no longer shows this note: its recorder
        drops `security_and_analysis` from every answer, since what the
        member says of a real repository is not the reference's to publish.
        So the note is held here, end to end, on answers that are nobody's.
        """
        def analysis(scanning, protection):
            return {"secret_scanning": {"status": scanning}, "secret_scanning_push_protection": {"status": protection}}

        def said(body) -> list:
            def read(path):
                return ("ok", body) if path == "repos/o/r" else ("absent", None)
            with tempfile.TemporaryDirectory(prefix="maelys-preflight-") as directory:
                decl = MODULE.Declarations(pathlib.Path(directory), "maelys-fixture", "maelys-release")
                with using_host(FakeHost(read=read)) as host:
                    found = MODULE.repository_checks(decl)
            # From the document already read: the repository is asked for once.
            self.assertEqual(host.reads.count("repos/o/r"), 1)
            return [(status, message) for status, message in found
                    if "secret scanning" in message or "push protection" in message]

        off = said({"visibility": "public", "security_and_analysis": analysis("disabled", "disabled")})
        self.assertEqual([status for status, _ in off], ["note"])
        self.assertIn("o/r is public and secret scanning and push protection are off", off[0][1])
        self.assertIn("gh api -X PATCH repos/o/r", off[0][1])
        half = said({"visibility": "public", "security_and_analysis": analysis("enabled", "disabled")})
        self.assertEqual([status for status, _ in half], ["note"])
        self.assertIn("o/r is public and push protection is off", half[0][1])
        # On: an ok line, and no note. Absent, as for any reader who does not
        # administer the repository and as in every recorded answer: nothing.
        self.assertEqual(said({"visibility": "public", "security_and_analysis": analysis("enabled", "enabled")}),
                         [("ok", "o/r is public, with secret scanning and push protection on")])
        self.assertEqual(said({"visibility": "public"}), [])
        self.assertEqual(said({"visibility": "private", "private": True,
                               "security_and_analysis": analysis("disabled", "disabled")}), [])

    def test_preflight_reads_it_from_the_repository_document_and_asks_nothing_more(self) -> None:
        import sys
        source = pathlib.Path(sys.modules["maelys_socle.release_checks"].__file__).read_text(encoding="utf-8")
        self.assertIn("found.extend(secret_scanning(repository, repository_data))", source)
        body = pathlib.Path(MODULE.github.__file__).read_text(encoding="utf-8")
        function = body[body.index("def secret_scanning"):body.index("def read_repository")]
        for call in ("github_read", "github_api", "github_list", "host."):
            self.assertNotIn(call, function)


if __name__ == "__main__":
    unittest.main()
