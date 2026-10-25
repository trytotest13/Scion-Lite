#!/usr/bin/env python3
"""End-to-end demo of scion-lite.

Boots a server on an ephemeral port, then plays two coding agents (alice and
bob) committing against it from separate workspaces:

  1. fast-forward commit          (agent commits on top of head)
  2. server-side merge            (stale base, non-overlapping files)
  3. conflict -> pull -> re-commit(the two-agents-one-file story)

Run:  python demo.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import build_server

SCION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scion.py")


def sh(cwd, *args):
    r = subprocess.run([sys.executable, SCION, *args], cwd=cwd,
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        raise SystemExit("scion %s failed in %s" % (args, cwd))
    return r.stdout.strip()


def write(ws, path, text):
    with open(os.path.join(ws, path), "w", encoding="utf-8") as f:
        f.write(text)


def section(title):
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def main():
    tmp = tempfile.mkdtemp(prefix="scion-lite-demo-")
    httpd, store = build_server(os.path.join(tmp, "server-data"), 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % port

    alice = os.path.join(tmp, "agent-alice")
    bob = os.path.join(tmp, "agent-bob")
    os.makedirs(alice)
    os.makedirs(bob)
    print("server: %s" % url)
    print("dashboard: %s/   (open it in a browser while this runs)" % url)
    print("workspaces: %s" % tmp)

    section("1. alice commits on top of head  ->  fast-forward")
    sh(alice, "init", "--server", url, "--author", "alice")
    write(alice, "intro.md", "# project\n\nbuilt by agents.\n")
    sh(alice, "add", "intro.md")
    print(sh(alice, "commit", "-m", "agent alice: add intro"))

    section("2. bob joins, then both commit from the same base")
    sh(bob, "init", "--server", url, "--author", "bob")
    print("bob cloned head: " + sh(bob, "pull"))
    write(bob, "api.md", "# api\n\ntodo\n")
    sh(bob, "add", "api.md")
    write(alice, "notes.md", "- ship it\n")
    sh(alice, "add", "notes.md")
    print("alice: " + sh(alice, "commit", "-m", "agent alice: notes"))
    print("bob (stale base, different file -> server merges):")
    print(sh(bob, "commit", "-m", "agent bob: api docs"))

    section("3. both edit the same file  ->  conflict, pull, re-commit")
    write(alice, "shared.txt", "alice's line\n")
    sh(alice, "add", "shared.txt")
    print("alice: " + sh(alice, "commit", "-m", "agent alice: edits shared"))
    write(bob, "shared.txt", "bob's line\n")
    sh(bob, "add", "shared.txt")
    r = subprocess.run([sys.executable, SCION, "commit", "-m", "agent bob: edits shared"],
                       cwd=bob, capture_output=True, text=True)
    print("bob: rc=%d (conflict rejected by the server)" % r.returncode)
    print(r.stdout.strip())
    print("bob pulls (server wins, local edit backed up): " + sh(bob, "pull"))
    write(bob, "shared.txt", "alice's line\nbob's line\n")
    sh(bob, "add", "shared.txt")
    print("bob re-applies intent: " + sh(bob, "commit", "-m", "agent bob: rebases shared"))

    section("final history (server-side of truth)")
    print(sh(bob, "log", "-n", "10"))

    section("cleanup")
    httpd.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print("done - temp workspaces removed.")


if __name__ == "__main__":
    main()
