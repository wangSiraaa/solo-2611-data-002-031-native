import argparse
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8109)
args = ap.parse_args()
sessions = set()
reports = {}
lock = threading.Lock()

class Handler(BaseHTTPRequestHandler):
    def reply(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except (ValueError, json.JSONDecodeError):
            self.reply(400,{"error":"bad_json"}); return
        path = urlsplit(self.path).path
        if path == "/sessions":
            if body != {"user":"demo","password":"demo-secret"}:
                self.reply(401,{"error":"credentials"}); return
            sid = "s_" + uuid.uuid4().hex
            with lock: sessions.add(sid)
            self.reply(201,{"session":{"id":sid}}); return
        parts = path.strip("/").split("/")
        if len(parts)==3 and parts[0]=="sessions" and parts[2]=="reports":
            sid = parts[1]
            with lock:
                valid = sid in sessions and self.headers.get("Authorization")=="Bearer "+sid
            if not valid:
                self.reply(401,{"error":"session"}); return
            if body != {"period":"2026-10"}:
                self.reply(400,{"error":"period"}); return
            rid = "r_" + uuid.uuid4().hex
            with lock: reports[rid] = {"session":sid,"period":body["period"]}
            self.reply(201,{"report":{"id":rid}}); return
        self.reply(404,{"error":"not_found"})
    def do_GET(self):
        parts = urlsplit(self.path).path.strip("/").split("/")
        with lock:
            record = reports.get(parts[1]) if len(parts)==2 and parts[0]=="reports" else None
        if record is None:
            self.reply(404,{"error":"not_found"}); return
        if self.headers.get("Authorization") != "Bearer "+record["session"]:
            self.reply(401,{"error":"session"}); return
        self.reply(200,{"report":{"id":parts[1]},"period":record["period"],"total":"42.50"})
    def log_message(self,*_): pass

if __name__=="__main__":
    s=ThreadingHTTPServer(("127.0.0.1",args.port),Handler)
    print("workflow fixture port",s.server_port,flush=True)
    s.serve_forever()
