"""HTTP health endpoint for external monitors (uptime-kuma etc.).

Serves GET /health with a JSON status document. Returns 200 while both the
mic capture stream and the playback stream are active, 503 otherwise.
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, port: int, status_fn):
        """status_fn() -> (ok: bool, payload: dict)."""
        self._status_fn = status_fn

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ("/", "/health"):
                    self.send_error(404)
                    return
                ok, payload = self.server.status_fn()
                body = json.dumps(payload).encode()
                self.send_response(200 if ok else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                log.debug("health: " + fmt % args)

        self._server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self._server.status_fn = status_fn
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self):
        self._thread.start()
        log.info("health endpoint listening on 0.0.0.0:%d", self.port)

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
