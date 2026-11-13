import argparse
import json
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8107)
ap.add_argument("--audit", default="products-requests.jsonl")
args = ap.parse_args()
rows = json.loads(Path(__file__).with_name("products.json").read_text())
lock = threading.Lock()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlsplit(self.path)
        if u.path != "/products":
            self.send_error(404); return
        q = parse_qs(u.query, keep_blank_values=True)
        try:
            if set(q) - {"ids", "category", "fields"} or any(len(v) != 1 for v in q.values()):
                raise ValueError("unsupported parameter")
            fields = q.get("fields", ["id,name,category"])[0].split(",")
            if not fields or set(fields) - {"id", "name", "category"}:
                raise ValueError("invalid fields")
            selected = rows
            if "ids" in q:
                ids = {int(x) for x in q["ids"][0].split(",") if x}
                selected = [r for r in selected if r["id"] in ids]
            if "category" in q:
                selected = [r for r in selected if r["category"] == q["category"][0]]
            result = [{k:r[k] for k in fields} for r in selected]
        except ValueError as exc:
            self.send_error(400, str(exc)); return
        with lock:
            with open(args.audit, "a", encoding="utf-8") as f:
                f.write(json.dumps({"query":q, "returned_rows":len(result)}, ensure_ascii=False) + "\n")
        body = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass

if __name__ == "__main__":
    s = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("products fixture port", s.server_port, flush=True)
    s.serve_forever()
