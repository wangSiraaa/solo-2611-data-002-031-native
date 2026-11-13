import argparse
import json
import threading
import time
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ap=argparse.ArgumentParser()
ap.add_argument("--port",type=int,required=True)
ap.add_argument("--mode",choices=["fast","blocked"],required=True)
ap.add_argument("--gate",default="release.gate")
ap.add_argument("--audit",required=True)
args=ap.parse_args()
lock=threading.Lock()

def log(value):
    with lock:
        with open(args.audit,"a") as f: f.write(json.dumps(value)+"\n")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path!="/item": self.send_error(404); return
        rid=uuid.uuid4().hex
        log({"event":"begin","id":rid,"monotonic":time.monotonic()})
        started=time.monotonic()
        try:
            if args.mode=="blocked":
                while not Path(args.gate).exists() and time.monotonic()-started<30:
                    time.sleep(.02)
            body=json.dumps({"source":args.mode,"item":"sample"}).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers(); self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError): pass
        finally: log({"event":"end","id":rid,"monotonic":time.monotonic()})
    def log_message(self,*_): pass

if __name__=="__main__":
    s=ThreadingHTTPServer(("127.0.0.1",args.port),Handler)
    print("origin",args.mode,"port",s.server_port,flush=True)
    s.serve_forever()
