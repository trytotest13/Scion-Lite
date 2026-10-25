#!/usr/bin/env python3
"""scion-lite client.

A thin CLI over a scion-lite server. The client keeps only a bounded local
cache (.scion/): the server is the single source of truth, and every commit
is applied server-side with a merge. That is the Scion bet: assume the client
is online and ask the server.

Usage:
  python scion.py init [--server URL] [--author NAME]
  python scion.py add <file> [<file> ...]
  python scion.py rm <file> [<file> ...]
  python scion.py commit -m "message"
  python scion.py status
  python scion.py log [-n 10]
  python scion.py diff
  python scion.py pull
  python scion.py whoami
"""

import argparse
import base64
import difflib
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request

from core import hash_file, hash_bytes

SCION_DIR = ".scion"


class RepoError(Exception):
    pass


# ---- local state ---------------------------------------------------------

def config_path():
    return os.path.join(SCION_DIR, "config.json")


def cache_path():
    return os.path.join(SCION_DIR, "cache.json")


def staging_path():
    return os.path.join(SCION_DIR, "staging.json")


def load_json(path, default):
    if not os.path.isfile(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def empty_staging():
    return {"added": {}, "deleted": []}


def require_repo():
    if not os.path.isdir(SCION_DIR):
        raise RepoError("not a scion workspace - run: scion init")
    cfg = load_json(config_path(), None)
    if cfg is None:
        raise RepoError("broken workspace: missing %s" % config_path())
    cache = load_json(cache_path(), {"head": None, "tree": {}})
    staging = load_json(staging_path(), empty_staging())
    return cfg, cache, staging


def to_os_path(repo_path):
    return os.path.join(*repo_path.split("/"))


def to_repo_path(local_path):
    rel = os.path.relpath(local_path)
    return rel.replace(os.sep, "/")


# ---- server --------------------------------------------------------------

def api(server, method, path, body=None, timeout=15):
    url = server.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") or "{}"
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw}
    except (urllib.error.URLError, OSError) as e:
        raise RepoError("cannot reach server %s (%s)" % (server, e))


def api_blob(server, sha):
    url = server.rstrip("/") + "/api/blob/" + sha
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, OSError) as e:
        raise RepoError("cannot reach server %s (%s)" % (server, e))


# ---- commands ------------------------------------------------------------

def cmd_init(args):
    if os.path.exists(SCION_DIR):
        print("already a scion workspace")
        return 1
    cfg = {
        "server": args.server,
        "author": args.author or getpass.getuser(),
    }
    os.makedirs(SCION_DIR)
    save_json(config_path(), cfg)
    save_json(cache_path(), {"head": None, "tree": {}})
    save_json(staging_path(), empty_staging())
    print("initialized scion workspace (author: %s, server: %s)" % (cfg["author"], cfg["server"]))
    try:
        code, state = api(cfg["server"], "GET", "/api/state")
    except RepoError as e:
        print("warning: %s" % e)
        return 0
    if code == 200 and state.get("head"):
        do_pull(cfg, {"head": None, "tree": {}})
    return 0


def cmd_add(args):
    cfg, cache, staging = require_repo()
    for target in args.paths:
        if not os.path.isfile(target):
            raise RepoError("no such file: %s" % target)
        path = to_repo_path(target)
        if path in staging["deleted"]:
            staging["deleted"].remove(path)
        staging["added"][path] = hash_file(target)
        print("staged: %s" % path)
    save_json(staging_path(), staging)
    return 0


def cmd_rm(args):
    cfg, cache, staging = require_repo()
    for target in args.paths:
        path = to_repo_path(target)
        staging["added"].pop(path, None)
        if path in cache["tree"] and path not in staging["deleted"]:
            staging["deleted"].append(path)
            print("staged deletion: %s" % path)
        else:
            print("unstaged / not tracked: %s" % path)
    save_json(staging_path(), staging)
    return 0


def cmd_commit(args):
    cfg, cache, staging = require_repo()
    if not staging["added"] and not staging["deleted"]:
        print("nothing staged (use: scion add <file> | scion rm <file>)")
        return 1
    changed = {}
    for path in staging["added"]:
        local = to_os_path(path)
        if not os.path.isfile(local):
            raise RepoError("staged file vanished: %s" % path)
        with open(local, "rb") as f:
            changed[path] = base64.b64encode(f.read()).decode("ascii")
    for path in staging["deleted"]:
        changed[path] = None

    code, resp = api(cfg["server"], "POST", "/api/changeset", {
        "based_on": cache["head"],
        "author": cfg["author"],
        "message": args.message,
        "changed": changed,
    })
    if code == 200:
        save_json(cache_path(), {"head": resp["id"], "tree": resp["tree"]})
        save_json(staging_path(), empty_staging())
        kind = "server-merge" if resp["merged"] else "fast-forward"
        print("applied %s  (%s, %d file(s) changed)"
              % (resp["id"][:10], kind, len(resp["files_changed"])))
        return 0
    if code == 409:
        print("server-side merge CONFLICT - the head moved and both of you touched:")
        for p in resp["conflicts"]:
            print("  " + p)
        print("run: scion pull   (sync to the winning head, re-apply your edit, commit again)")
        return 2
    print("server error %s: %s" % (code, resp))
    return 3


def cmd_status(args):
    cfg, cache, staging = require_repo()
    head = cache["head"] or "no commits yet"
    print("On head %s" % (head[:10] if cache["head"] else head))
    if staging["added"]:
        for path in sorted(staging["added"]):
            print("  staged:    A %s" % path)
    if staging["deleted"]:
        for path in sorted(staging["deleted"]):
            print("  staged:    D %s" % path)

    tracked = set(cache["tree"])
    untracked, modified, deleted = [], [], []
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d != SCION_DIR]
        for fn in files:
            untracked.append(to_repo_path(os.path.join(root, fn)))
    for path in sorted(tracked):
        local = to_os_path(path)
        if path in staging["deleted"]:
            continue
        if not os.path.isfile(local):
            deleted.append(path)
        elif hash_file(local) != cache["tree"][path]:
            modified.append(path)
    for path in sorted(untracked):
        if path not in tracked and path not in staging["added"]:
            print("  untracked: ? %s" % path)
    for path in modified:
        if path not in staging["added"]:
            print("  modified:  M %s" % path)
    for path in deleted:
        print("  deleted:   D %s" % path)
    if not (staging["added"] or staging["deleted"] or modified or deleted
            or [p for p in untracked if p not in tracked]):
        print("working tree clean")
    return 0


def cmd_log(args):
    cfg, cache, staging = require_repo()
    code, entries = api(cfg["server"], "GET", "/api/log?n=%d" % args.n)
    if code != 200:
        print("server error %s: %s" % (code, entries))
        return 1
    if not entries:
        print("no commits yet")
        return 0
    for c in entries:
        marker = "  (server-merge)" if c["merged"] else ""
        print("* %s  %-8s %s%s" % (c["id"][:10], c["author"], c["message"], marker))
        print("    %d file(s) changed: %s"
              % (len(c["files_changed"]), ", ".join(c["files_changed"]) or "-"))
    return 0


def cmd_diff(args):
    cfg, cache, staging = require_repo()
    paths = set(cache["tree"])
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d != SCION_DIR]
        for fn in files:
            paths.add(to_repo_path(os.path.join(root, fn)))
    shown = 0
    for path in sorted(paths):
        local = to_os_path(path)
        old_sha = cache["tree"].get(path)
        new_text = None
        if os.path.isfile(local):
            with open(local, "rb") as f:
                new_text = f.read().decode("utf-8", "replace")
        old_text = None
        if old_sha:
            code, blob = api_blob(cfg["server"], old_sha)
            if code == 200:
                old_text = blob.decode("utf-8", "replace")
        if old_text == new_text:
            continue  # identical, or file unchanged on disk
        if old_sha is None:
            print("new file: %s" % path)
            continue
        print("--- %s (server)" % path)
        print("+++ %s (working tree)" % path)
        for line in difflib.unified_diff(
                (old_text or "").splitlines(), (new_text or "").splitlines(),
                lineterm="", n=2):
            print(line)
            shown += 1
            if shown > 400:
                print("... (diff truncated)")
                return 0
    return 0


def backup_file(local):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(SCION_DIR, "backup", stamp)
    os.makedirs(bdir, exist_ok=True)
    dest = os.path.join(bdir, to_repo_path(local).replace("/", "__"))
    if os.path.isfile(local):
        with open(local, "rb") as src, open(dest, "wb") as dst:
            dst.write(src.read())
    return dest


def do_pull(cfg, cache):
    """Server is the source of truth: sync the working tree to the server head.

    Local files that differ from the incoming tree are backed up under
    .scion/backup/ before being overwritten or removed.
    """
    code, state = api(cfg["server"], "GET", "/api/state")
    if code != 200 or not state.get("head"):
        print("server has no commits yet")
        save_json(cache_path(), cache)
        return 0
    head = state["head"]
    code, tree = api(cfg["server"], "GET", "/api/tree?at=%s" % head)
    if code != 200:
        raise RepoError("cannot fetch tree: %s" % tree)
    files = tree["files"]

    written = removed = backed = 0
    for path, sha in sorted(files.items()):
        local = to_os_path(path)
        disk_sha = hash_file(local) if os.path.isfile(local) else None
        if disk_sha == sha:
            continue
        if disk_sha is not None:
            backup_file(local)
            backed += 1
        os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
        code, blob = api_blob(cfg["server"], sha)
        if code != 200:
            raise RepoError("blob %s missing on server" % sha)
        with open(local, "wb") as f:
            f.write(blob)
        written += 1
    for path in cache["tree"]:
        if path not in files:
            local = to_os_path(path)
            if os.path.isfile(local):
                backup_file(local)
                os.remove(local)
                removed += 1
    save_json(cache_path(), {"head": head, "tree": files})
    print("pulled %s  (%d file(s) written, %d removed, %d backed up)"
          % (head[:10], written, removed, backed))
    return 0


def cmd_pull(args):
    cfg, cache, staging = require_repo()
    if staging["added"] or staging["deleted"]:
        print("note: staged changes are discarded by pull (the server wins); "
              "backups land in .scion/backup/")
    return do_pull(cfg, cache)


def cmd_whoami(args):
    cfg, cache, staging = require_repo()
    print("author: %s" % cfg["author"])
    print("server: %s" % cfg["server"])
    print("head:   %s" % (cache["head"] or "no commits yet"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="scion",
        description="scion-lite client - server-side version control "
                    "(inspired by Anthropic's Scion)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create a workspace bound to a server")
    p.add_argument("--server", default="http://127.0.0.1:8000")
    p.add_argument("--author", default=None)
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("add", help="stage file(s) for the next commit")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("rm", help="stage deletion of file(s)")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_rm)

    p = sub.add_parser("commit", help="apply a changeset (merged server-side)")
    p.add_argument("-m", "--message", required=True)
    p.set_defaults(fn=cmd_commit)

    sub.add_parser("status", help="working tree vs server cache").set_defaults(fn=cmd_status)

    p = sub.add_parser("log", help="changeset history from the server")
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(fn=cmd_log)

    sub.add_parser("diff", help="working tree vs last synced tree").set_defaults(fn=cmd_diff)
    sub.add_parser("pull", help="authoritative sync to the server head").set_defaults(fn=cmd_pull)
    sub.add_parser("whoami", help="show workspace identity").set_defaults(fn=cmd_whoami)

    args = ap.parse_args(argv)
    try:
        return args.fn(args) or 0
    except RepoError as e:
        print("error: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
