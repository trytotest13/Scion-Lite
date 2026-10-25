#!/usr/bin/env python3
"""scion-lite server.

Single source of truth for a repository: blobs, changesets and refs live here,
and every commit / rebase / merge is applied server-side (the Scion principle:
"assume the client is online and ask the server").

Storage layout (under --data):
    objects/<sha>            content-addressed blobs
    changesets/<id>.json     changeset records (full snapshots)
    refs/main                head pointer
    wal.jsonl                append-only write-ahead log of applies/conflicts

Run:  python server.py --port 8000 --data ./scion-data
Then open the dashboard:  http://127.0.0.1:8000/
"""

import argparse
import base64
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import hash_bytes, make_changeset, three_way_merge, tree_diff

# ids become file paths, so anything that is not plain lowercase hex is
# rejected before it can touch the filesystem (backslashes would traverse
# on Windows)
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Dashboard direction: developer tool (the audience is engineers watching a
# VCS server), so dark + monospace is a brand fit rather than a default.
# GitHub-dark text colors on #0d1117 hold WCAG AA contrast at 13px.
DASHBOARD = """<!doctype html><html><head><meta charset="utf-8"><title>scion-lite</title>
<style>
body{background:#0d1117;color:#c9d1d9;font-family:ui-monospace,Consolas,monospace;margin:0;padding:24px}
h1{color:#58a6ff;font-size:20px;margin-bottom:4px}
p.sub{color:#8b949e;margin-top:0;font-size:13px}
.stat{display:inline-block;background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px 14px;margin:14px 8px 4px 0;font-size:13px}
.stat.error{color:#f85149;border-color:#f85149}
.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;margin-top:12px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #21262d;font-size:13px;white-space:nowrap}
th{color:#8b949e}.id{color:#7ee787}.merged{color:#d2a8ff}
</style></head><body>
<h1>scion-lite</h1>
<p class="sub">server-side version control: every commit, merge and rebase happens here</p>
<div id="stats"><span class="stat">loading...</span></div>
<div class="wrap"><table><thead><tr><th>changeset</th><th>parent</th><th>author</th><th>message</th><th>files</th><th>apply</th><th>time</th></tr></thead>
<tbody id="rows"><tr><td colspan="7">loading...</td></tr></tbody></table></div>
<script>
async function refresh(){
  try{
    const s=await (await fetch('/api/state')).json();
    document.getElementById('stats').innerHTML=
      `<span class="stat">head <span class="id">${(s.head||'-').slice(0,10)}</span></span>`+
      `<span class="stat">${s.changesets} changesets</span>`+
      `<span class="stat">${s.blobs} blobs</span>`+
      `<span class="stat">${s.conflicts} conflicts</span>`;
    const log=await (await fetch('/api/log?n=15')).json();
    document.getElementById('rows').innerHTML=log.length?log.map(c=>
      `<tr><td class="id">${c.id.slice(0,10)}</td><td>${(c.parent||'-').slice(0,10)}</td>`+
      `<td>${c.author}</td><td>${c.message}</td><td>${c.files_changed.length}</td>`+
      `<td class="${c.merged?'merged':''}">${c.merged?'server-merge':'fast-forward'}</td>`+
      `<td>${new Date(c.timestamp*1000).toLocaleTimeString()}</td></tr>`).join('')
      :'<tr><td colspan="7">no changesets yet - run scion add + scion commit in a workspace</td></tr>';
  }catch(e){
    document.getElementById('stats').innerHTML=
      '<span class="stat error">server unreachable - retrying every 2s</span>';
  }
}
refresh();setInterval(refresh,2000);
</script></body></html>"""


class Store:
    def __init__(self, data_dir):
        self.dir = os.path.abspath(data_dir)
        self.objects = os.path.join(self.dir, "objects")
        self.changesets = os.path.join(self.dir, "changesets")
        self.refs = os.path.join(self.dir, "refs")
        self.wal_path = os.path.join(self.dir, "wal.jsonl")
        for d in (self.dir, self.objects, self.changesets, self.refs):
            os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self._recover()

    # ---- low level -------------------------------------------------------
    def _cs_path(self, cid):
        return os.path.join(self.changesets, cid + ".json")

    def _blob_path(self, sha):
        return os.path.join(self.objects, sha)

    def _wal(self, op):
        with open(self.wal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(op, sort_keys=True) + "\n")

    def _write_ref(self, cid):
        with open(os.path.join(self.refs, "main"), "w", encoding="utf-8") as f:
            f.write(cid)

    # ---- reads -----------------------------------------------------------
    def read_blob(self, sha):
        if not HEX64.fullmatch(sha or ""):
            return None
        path = self._blob_path(sha)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def get_changeset(self, cid):
        if not cid or not HEX64.fullmatch(cid):
            return None
        path = self._cs_path(cid)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def head(self):
        path = os.path.join(self.refs, "main")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                cid = f.read().strip()
                if cid:
                    return cid
        return None

    def head_tree(self):
        cs = self.get_changeset(self.head())
        return dict(cs["files"]) if cs else {}

    def counts(self):
        return {
            "head": self.head(),
            "changesets": len(os.listdir(self.changesets)),
            "blobs": len(os.listdir(self.objects)),
        }

    def conflict_count(self):
        n = 0
        if os.path.isfile(self.wal_path):
            with open(self.wal_path, "r", encoding="utf-8") as f:
                for line in f:
                    if '"conflict"' in line:
                        n += 1
        return n

    def log(self, n=20):
        out, cid = [], self.head()
        while cid and len(out) < n:
            cs = self.get_changeset(cid)
            if cs is None:
                break
            out.append({k: cs[k] for k in (
                "id", "parent", "based_on", "merged", "author",
                "message", "timestamp", "files_changed")})
            cid = cs["parent"]
        return out

    # ---- writes ----------------------------------------------------------
    def put_blob(self, data):
        sha = hash_bytes(data)
        path = self._blob_path(sha)
        if not os.path.isfile(path):
            with open(path, "wb") as f:  # content-addressed: racing writers write identical bytes
                f.write(data)
        return sha

    def apply(self, based_on, author, message, changed):
        """Apply a client changeset with a server-side merge.

        changed: {path: base64(content) | None(delete)} relative to based_on.
        Returns (http_code, body_dict).
        """
        with self.lock:
            head = self.head()
            if based_on and self.get_changeset(based_on) is None:
                return 400, {"error": "unknown base changeset", "based_on": based_on}
            base_cs = self.get_changeset(based_on)
            base_tree = dict(base_cs["files"]) if base_cs else {}
            head_tree = self.head_tree()

            client_tree = dict(base_tree)
            for path, b64 in changed.items():
                if b64 is None:
                    client_tree.pop(path, None)
                else:
                    client_tree[path] = self.put_blob(base64.b64decode(b64))

            merged, conflicts = three_way_merge(base_tree, head_tree, client_tree)
            if conflicts:
                self._wal({"op": "conflict", "based_on": based_on, "head": head,
                           "conflicts": conflicts, "author": author,
                           "message": message, "at": time.time()})
                return 409, {"status": "conflict", "conflicts": conflicts, "head": head}

            cs = make_changeset(
                parent=head, based_on=based_on, merged=based_on != head,
                author=author, message=message, files=merged,
                files_changed=sorted(tree_diff(base_tree, merged)))
            with open(self._cs_path(cs["id"]), "w", encoding="utf-8") as f:
                json.dump(cs, f, sort_keys=True)
            self._wal({"op": "apply", "id": cs["id"], "parent": head,
                       "head_before": head, "head_after": cs["id"],
                       "author": author, "message": message, "at": time.time()})
            self._write_ref(cs["id"])
            return 200, {"status": "applied", "id": cs["id"], "tree": merged,
                         "merged": cs["merged"], "files_changed": cs["files_changed"]}

    def _recover(self):
        """If refs/main is missing or stale (crash between apply and ref write),
        rebuild the head from the last valid apply op in the write-ahead log."""
        head = self.head()
        if head and self.get_changeset(head):
            return
        last = None
        if os.path.isfile(self.wal_path):
            with open(self.wal_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        op = json.loads(line)
                    except ValueError:
                        continue
                    if op.get("op") == "apply" and os.path.isfile(self._cs_path(op.get("id", ""))):
                        last = op["id"]
        if last:
            self._write_ref(last)


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep demo output clean
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            params = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
            if path in ("/", "/index.html"):
                return self._send(200, DASHBOARD, "text/html; charset=utf-8")
            if path == "/api/state":
                state = store.counts()
                state["conflicts"] = store.conflict_count()
                return self._send(200, state)
            if path == "/api/log":
                try:
                    n = max(1, min(int(params.get("n", "20")), 500))
                except ValueError:
                    n = 20
                return self._send(200, store.log(n))
            if path == "/api/tree":
                at = params.get("at") or store.head()
                cs = store.get_changeset(at)
                if cs is None:
                    return self._send(404, {"error": "changeset not found", "at": at})
                return self._send(200, {"at": at, "files": cs["files"]})
            if path.startswith("/api/blob/"):
                data = store.read_blob(path.rsplit("/", 1)[-1])
                if data is None:
                    return self._send(404, {"error": "blob not found"})
                return self._send(200, data, "application/octet-stream")
            return self._send(404, {"error": "not found", "path": path})

        def do_POST(self):
            if self.path != "/api/changeset":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return self._send(400, {"error": "invalid json"})
            try:
                code, resp = store.apply(
                    body.get("based_on"), body.get("author", "unknown"),
                    body.get("message", ""), body.get("changed", {}))
            except Exception as exc:  # keep the server alive on bad payloads
                return self._send(500, {"error": repr(exc)})
            return self._send(code, resp)

    return Handler


def build_server(data_dir, port=0):
    store = Store(data_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    return httpd, store


def main():
    ap = argparse.ArgumentParser(description="scion-lite server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default="./scion-data", help="storage directory")
    args = ap.parse_args()
    httpd, store = build_server(args.data, args.port)
    host, port = httpd.server_address[:2]
    print("scion-lite server  http://%s:%s/   (data: %s)" % (host, port, store.dir))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
