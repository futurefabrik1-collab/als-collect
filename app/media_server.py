"""
Tiny local HTTP server that streams audio files to the app's <audio> element.

Bound to 127.0.0.1 on a random port, every request must carry the per-run
token, and only files explicitly registered by the app can be fetched (by an
opaque id, never by path).  Supports HTTP Range so WKWebView can seek.
"""

import hashlib
import mimetypes
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MIME = {
    ".wav": "audio/wav", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4", ".mp4": "video/mp4", ".mov": "video/quicktime", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".caf": "audio/x-caf", ".aac": "audio/aac",
}


class MediaServer:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self._files = {}          # id -> absolute path
        self._lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):   # quiet
                pass

            def do_HEAD(self):
                self._guard(True)

            def do_GET(self):
                self._guard(False)

            def _guard(self, head):
                try:
                    self._serve(head)
                except (BrokenPipeError, ConnectionResetError):
                    pass   # WebKit aborts its speculative 0-END request routinely

            def handle_error(self, request, client_address):   # keep stderr clean
                pass

            def _serve(self, head):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if q.get("t", [None])[0] != server.token:
                    self.send_error(403)
                    return
                fid = u.path.rsplit("/", 1)[-1]
                path = server.path_for(fid)
                if not path or not os.path.isfile(path):
                    self.send_error(404)
                    return
                size = os.path.getsize(path)
                start, end = 0, size - 1
                status = 200
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    a, _, b = rng[6:].partition("-")
                    try:
                        if a:
                            start = int(a)
                            end = int(b) if b else size - 1
                        else:
                            start = max(0, size - int(b))
                    except ValueError:
                        self.send_error(416)
                        return
                    if start >= size or end < start:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    end = min(end, size - 1)
                    status = 206
                length = end - start + 1
                self.send_response(status)
                self.send_header("Content-Type", MIME.get(os.path.splitext(path)[1].lower()) or mimetypes.guess_type(path)[0] or "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if head:
                    return
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(1 << 18, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def register(self, path):
        """Allow `path` to be streamed; returns its URL."""
        path = os.path.abspath(path)
        fid = hashlib.sha1(path.encode("utf-8", "surrogateescape")).hexdigest()[:20]
        with self._lock:
            self._files[fid] = path
        return f"http://127.0.0.1:{self.port}/media/{fid}?t={self.token}"

    def path_for(self, fid):
        with self._lock:
            return self._files.get(fid)

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
