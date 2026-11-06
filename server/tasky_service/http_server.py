from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hmac import compare_digest
import json
import re
from urllib.parse import parse_qs, urlsplit

from .clients import ApiError, NotConfigured, ProviderError


ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class TaskyHandler(BaseHTTPRequestHandler):
    service = None
    device_token = ""

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._send(200, {"ok": True})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if parsed.path == "/api/v1/today":
            query = parse_qs(parsed.query)
            source = query.get("source", [None])[0]
            if source not in (None, "todoist", "trello"):
                self._send(400, {"error": "invalid_source"})
                return
            try:
                offset = max(0, int(query.get("offset", ["0"])[0]))
                limit = int(query.get("limit", ["5"])[0]) if source else None
            except ValueError:
                self._send(400, {"error": "invalid_pagination"})
                return
            if limit is not None and not 1 <= limit <= 5:
                self._send(400, {"error": "invalid_pagination"})
                return
            force = query.get("refresh", ["0"])[0] == "1"
            self._send(200, self.service.get_today(force=force, source=source, offset=offset, limit=limit))
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        match = re.fullmatch(r"/api/v1/(tasks|cards)/([^/]+)/(complete|archive)", urlsplit(self.path).path)
        if not match:
            self._send(404, {"error": "not_found"})
            return
        resource, item_id, action = match.groups()
        if not ID_PATTERN.fullmatch(item_id):
            self._send(400, {"error": "invalid_id"})
            return
        try:
            if resource == "tasks" and action == "complete":
                self.service.complete_todoist(item_id)
            elif resource == "cards" and action == "archive":
                self.service.archive_trello(item_id)
            else:
                self._send(404, {"error": "not_found"})
                return
        except NotConfigured:
            self._send(503, {"error": "provider_not_configured"})
            return
        except ApiError as error:
            self._send(error.status if error.status and error.status in (401, 403, 404, 429) else 502,
                        {"error": "provider_request_failed"})
            return
        except ProviderError:
            self._send(502, {"error": "provider_request_failed"})
            return
        self._send(200, {"ok": True})

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        prefix = "Bearer "
        return bool(self.device_token) and supplied.startswith(prefix) and compare_digest(
            supplied[len(prefix):], self.device_token
        )

    def _send(self, status, payload):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        return


def make_server(settings, service):
    handler = type("ConfiguredTaskyHandler", (TaskyHandler,), {
        "service": service,
        "device_token": settings.device_token,
    })
    return ThreadingHTTPServer((settings.host, settings.port), handler)