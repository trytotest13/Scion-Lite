"""Integration tests for scion-lite.

Each test boots a real server on an ephemeral port and drives the real CLI
through subprocesses, so the whole stack (client -> HTTP -> store -> WAL)
is exercised.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import Store, build_server

SCION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scion.py")


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


class ScionLiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="scion-lite-test-")
        self.data_dir = os.path.join(self.tmp, "data")
        self.httpd, self.store = build_server(self.data_dir, 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.port

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------
    def run_cli(self, cwd, *args, expect=0):
        r = subprocess.run([sys.executable, SCION, *args], cwd=cwd,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, expect,
                         "scion %s -> rc=%s\nstdout:\n%s\nstderr:\n%s"
                         % (args, r.returncode, r.stdout, r.stderr))
        return r.stdout

    def workspace(self, name, author):
        ws = os.path.join(self.tmp, name)
        os.makedirs(ws)
        self.run_cli(ws, "init", "--server", self.base, "--author", author)
        return ws

    def write(self, ws, path, text):
        local = os.path.join(ws, path)
        os.makedirs(os.path.dirname(local) or ws, exist_ok=True)
        with open(local, "w", encoding="utf-8") as f:
            f.write(text)
        return local

    # ---- tests -----------------------------------------------------------
    def test_commit_log_status_roundtrip(self):
        ws = self.workspace("alice", "alice")
        self.write(ws, "hello.txt", "hello world\n")
        out = self.run_cli(ws, "add", "hello.txt")
        self.assertIn("staged: hello.txt", out)
        out = self.run_cli(ws, "commit", "-m", "first commit")
        self.assertIn("applied", out)
        self.assertIn("fast-forward", out)

        out = self.run_cli(ws, "log")
        self.assertIn("first commit", out)
        self.assertIn("alice", out)

        out = self.run_cli(ws, "status")
        self.assertIn("working tree clean", out)

        # modify -> status + diff see it, pull restores it
        self.write(ws, "hello.txt", "hello world, edited\n")
        out = self.run_cli(ws, "status")
        self.assertIn("modified:  M hello.txt", out)
        out = self.run_cli(ws, "diff")
        self.assertIn("hello world, edited", out)
        out = self.run_cli(ws, "pull")
        self.assertIn("1 file(s) written", out)
        self.assertIn("working tree clean", self.run_cli(ws, "status"))

    def test_deletion_staging(self):
        ws = self.workspace("alice", "alice")
        self.write(ws, "gone.txt", "bye\n")
        self.run_cli(ws, "add", "gone.txt")
        self.run_cli(ws, "commit", "-m", "add gone")
        os.remove(os.path.join(ws, "gone.txt"))
        self.run_cli(ws, "rm", "gone.txt")
        out = self.run_cli(ws, "commit", "-m", "delete gone")
        self.assertIn("applied", out)
        files = get_json(self.base + "/api/tree")["files"]
        self.assertNotIn("gone.txt", files)
        self.assertIn("working tree clean", self.run_cli(ws, "status"))

    def test_server_side_merge_non_overlapping(self):
        alice = self.workspace("alice", "alice")
        bob = self.workspace("bob", "bob")
        self.write(alice, "base.txt", "base\n")
        self.run_cli(alice, "add", "base.txt")
        self.run_cli(alice, "commit", "-m", "seed")
        self.run_cli(bob, "pull")  # bob starts from the same head

        # both work from the same base; alice lands first
        self.write(alice, "a.md", "from alice\n")
        self.run_cli(alice, "add", "a.md")
        self.run_cli(alice, "commit", "-m", "alice adds a")

        # bob's base is now stale, but he touched a different file:
        # the server must merge for him
        self.write(bob, "b.md", "from bob\n")
        self.run_cli(bob, "add", "b.md")
        out = self.run_cli(bob, "commit", "-m", "bob adds b")
        self.assertIn("server-merge", out)

        files = get_json(self.base + "/api/tree")["files"]
        self.assertIn("base.txt", files)
        self.assertIn("a.md", files)
        self.assertIn("b.md", files)

    def test_conflict_then_recover(self):
        alice = self.workspace("alice", "alice")
        bob = self.workspace("bob", "bob")
        self.write(alice, "shared.txt", "version 1\n")
        self.run_cli(alice, "add", "shared.txt")
        self.run_cli(alice, "commit", "-m", "seed")
        self.run_cli(bob, "pull")

        self.write(alice, "shared.txt", "alice's version\n")
        self.run_cli(alice, "add", "shared.txt")
        self.run_cli(alice, "commit", "-m", "alice edits shared")

        self.write(bob, "shared.txt", "bob's version\n")
        self.run_cli(bob, "add", "shared.txt")
        r = self.run_cli(bob, "commit", "-m", "bob edits shared", expect=2)
        self.assertIn("CONFLICT", r)
        self.assertIn("shared.txt", r)

        # server wins: pull gives alice's content (bob's edit backed up),
        # bob re-applies his intent on top and lands cleanly
        out = self.run_cli(bob, "pull")
        self.assertIn("backed up", out)
        with open(os.path.join(bob, "shared.txt"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "alice's version\n")
        self.write(bob, "shared.txt", "alice's version + bob's addition\n")
        self.run_cli(bob, "add", "shared.txt")
        out = self.run_cli(bob, "commit", "-m", "bob rebases shared")
        self.assertIn("fast-forward", out)

        self.assertEqual(self.store.conflict_count(), 1)

    def test_wal_recovery_rebuilds_head(self):
        ws = self.workspace("alice", "alice")
        self.write(ws, "f.txt", "data\n")
        self.run_cli(ws, "add", "f.txt")
        self.run_cli(ws, "commit", "-m", "durable?")
        head = self.store.head()
        self.assertIsNotNone(head)

        # simulate a crash between apply and ref write: wipe refs, reopen store
        os.remove(os.path.join(self.data_dir, "refs", "main"))
        recovered = Store(self.data_dir)
        self.assertEqual(recovered.head(), head)
        self.assertEqual(recovered.get_changeset(head)["message"], "durable?")

    def test_blob_and_changeset_paths_reject_non_hex_ids(self):
        # trust boundary: ids become file paths, so backslash paths must not
        # be able to traverse out of the storage dir (Windows separates with \)
        for url in (self.base + "/api/blob/..\\..\\win.ini",
                    self.base + "/api/blob/" + "a" * 64 + "x",
                    self.base + "/api/tree?at=..\\..\\win"):
            try:
                urllib.request.urlopen(url, timeout=10)
                self.fail("expected 404 for %s" % url)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404)

    def test_dashboard_and_unknown_base(self):
        r = urllib.request.urlopen(self.base + "/", timeout=10)
        self.assertEqual(r.status, 200)
        self.assertIn(b"scion-lite", r.read())

        ws = self.workspace("alice", "alice")
        self.write(ws, "f.txt", "x\n")
        self.run_cli(ws, "add", "f.txt")
        self.run_cli(ws, "commit", "-m", "seed")
        # a client that claims a base that does not exist gets a 400
        import base64
        payload = {"based_on": "0" * 64, "author": "x", "message": "bad",
                   "changed": {"f.txt": base64.b64encode(b"y\n").decode()}}
        req = urllib.request.Request(self.base + "/api/changeset",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
