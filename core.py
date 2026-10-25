"""Shared primitives for scion-lite.

scion-lite is a miniature, teaching-scale version control system inspired by
Anthropic's Scion (https://www.anthropic.com/research/redesigning-source-control):
content-addressed blobs, full-snapshot changesets, and three-way merges
resolved at file granularity on the server.
"""

import hashlib
import json
import time


def hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make_changeset(parent, based_on, merged, author, message, files, files_changed):
    """Build a changeset record. `files` is the full snapshot {path: blob_sha}.

    The id is the SHA-256 of the canonical record payload, so two identical
    commits from two agents at the same instant cannot silently collide.
    """
    record = {
        "parent": parent,        # true lineage: head at apply time
        "based_on": based_on,    # what the client started from
        "merged": merged,        # True when apply was not a fast-forward
        "author": author,
        "message": message,
        "timestamp": time.time(),
        "files": files,
        "files_changed": files_changed,
    }
    payload = json.dumps(record, sort_keys=True).encode("utf-8")
    record["id"] = hash_bytes(payload)
    return record


def tree_diff(old, new):
    """Diff two snapshots; returns {path: "add" | "mod" | "del"}."""
    old = old or {}
    new = new or {}
    diff = {}
    for path in set(old) | set(new):
        o, n = old.get(path), new.get(path)
        if o == n:
            continue
        if o is None:
            diff[path] = "add"
        elif n is None:
            diff[path] = "del"
        else:
            diff[path] = "mod"
    return diff


def three_way_merge(base, head, client):
    """File-granularity three-way merge of snapshots.

    Returns (merged_snapshot, conflicts). A path conflicts only when head and
    client both changed it differently relative to base (including one side
    deleting while the other edits).
    """
    base = base or {}
    merged, conflicts = {}, []
    for path in set(base) | set(head) | set(client):
        b, h, c = base.get(path), head.get(path), client.get(path)
        if h == c:
            if h is not None:
                merged[path] = h
        elif b == h:
            if c is not None:
                merged[path] = c
        elif b == c:
            if h is not None:
                merged[path] = h
        else:
            conflicts.append(path)
    return merged, sorted(conflicts)
