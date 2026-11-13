import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8106)
args = ap.parse_args()
seen = {}
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != "/events":
            self.send_error(404)
            return
        qs = parse_qs(url.query)
        case = qs.get("case", ["partial"])[0]
        client = qs.get("client", ["demo"])[0]
        if case not in {"normal", "partial", "conflict", "expired", "rotate"}:
            self.send_error(400)
            return
        with lock:
            attempt = seen.get((client, case), 0)
            seen[client, case] = attempt + 1
        last = self.headers.get("Last-Event-ID", "")
        if case == "expired" and last:
            raw = b'{"error":"retention_gap","first_available":5}'
            self.send_response(410)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        stream = "streamB" if case == "rotate" and attempt else "streamA"
        start = 1
        if last and last.startswith(stream + ":"):
            try:
                start = max(1, min(6, int(last.split(":")[1])))
            except ValueError:
                self.send_error(400)
                return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b": heartbeat\r\nretry: 30\r\n\r\n")
            for seq in range(start, 7):
                value = "改写" if case == "conflict" and last and seq == start else "正常"
                payload = {"stream_id": stream, "sequence": seq, "value": value}
                data = json.dumps(payload, ensure_ascii=False, indent=2)
                lines = ["event: reading", "id: %s:%d" % (stream, seq)]
                lines += ["data: " + line for line in data.splitlines()]
                raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
                if case in {"partial", "expired", "rotate"} and not attempt and seq == 4:
                    # Ends inside an actual multibyte code point, without event terminator.
                    cut = raw.index("正常".encode()) + 1
                    self.wfile.write(raw[:cut])
                    self.wfile.flush()
                    return
                # Writes split UTF-8 and CRLF boundaries. TCP may coalesce writes.
                for offset in range(0, len(raw), 7):
                    self.wfile.write(raw[offset:offset+7])
                    self.wfile.flush()
                    time.sleep(0.001)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("SSE fixture listening at http://127.0.0.1:%s/events" % server.server_port, flush=True)
    server.serve_forever()
