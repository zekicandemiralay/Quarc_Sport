"""A tiny stand-in for Strava's API, for tests: tokens, deauthorize and the activity list."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class FakeStrava:
    def __init__(self):
        self.activities: list[dict] = []          # any order; served newest first, like Strava
        self.fail_with: int | None = None         # make /athlete/activities answer this status
        self.usage = (2, 20)                      # X-RateLimit-Usage (15 min, day)
        self.limit = (100, 1000)
        self.requests: list[dict] = []            # every call: {"method", "path", "query", "auth", "form"}
        self.valid_codes = {"GOOD": {"id": 777, "firstname": "Ada", "lastname": "Runner"}}
        self.access_token = "access-1"
        self.refresh_token = "refresh-1"
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self.base = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()

    def calls(self, path_prefix: str) -> list[dict]:
        return [r for r in self.requests if r["path"].startswith(path_prefix)]

    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-RateLimit-Limit", ",".join(map(str, outer.limit)))
                self.send_header("X-RateLimit-Usage", ",".join(map(str, outer.usage)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self, form=None):
                u = urlparse(self.path)
                rec = {"method": self.command, "path": u.path, "query": {k: v[0] for k, v in parse_qs(u.query).items()},
                       "auth": self.headers.get("Authorization"), "form": form or {}}
                outer.requests.append(rec)
                return rec

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                self._record(form)
                path = urlparse(self.path).path
                if path.endswith("/oauth/token"):
                    if form.get("grant_type") == "authorization_code":
                        athlete = outer.valid_codes.get(form.get("code"))
                        if not athlete:
                            return self._send(400, {"message": "Bad Request"})
                        outer.access_token, outer.refresh_token = "access-1", "refresh-1"
                        return self._send(200, {"access_token": "access-1", "refresh_token": "refresh-1",
                                                "expires_at": int(time.time()) + 21600, "athlete": athlete})
                    if form.get("grant_type") == "refresh_token":
                        if form.get("refresh_token") != outer.refresh_token:
                            return self._send(400, {"message": "Bad Request"})
                        outer.access_token = f"access-{len(outer.calls('/oauth/token')) + 1}"
                        outer.refresh_token = f"refresh-{len(outer.calls('/oauth/token')) + 1}"
                        return self._send(200, {"access_token": outer.access_token, "refresh_token": outer.refresh_token,
                                                "expires_at": int(time.time()) + 21600})
                if path.endswith("/oauth/deauthorize"):
                    return self._send(200, {"access_token": form.get("access_token")})
                self._send(404, {})

            def do_GET(self):
                rec = self._record()
                path = urlparse(self.path).path
                if path.endswith("/athlete/activities"):
                    if outer.fail_with:
                        return self._send(outer.fail_with, {"message": "nope"})
                    if rec["auth"] != f"Bearer {outer.access_token}":
                        return self._send(401, {"message": "Authorization Error"})
                    q = rec["query"]
                    acts = sorted(outer.activities, key=lambda a: a["start_date"], reverse=True)
                    if "before" in q:
                        acts = [a for a in acts if _epoch(a["start_date"]) < int(q["before"])]
                    if "after" in q:
                        acts = [a for a in acts if _epoch(a["start_date"]) > int(q["after"])]
                    per, page = int(q.get("per_page", 30)), int(q.get("page", 1))
                    return self._send(200, acts[(page - 1) * per: page * per])
                self._send(404, {})

        return H


def _epoch(iso: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def activity(n: int, *, days_ago: int | None = None, **extra) -> dict:
    """A Strava SummaryActivity. Higher n = newer unless days_ago is given."""
    from datetime import datetime, timedelta, timezone
    start = datetime.now(timezone.utc) - timedelta(days=days_ago if days_ago is not None else 400 - n, hours=1)
    a = {
        "id": 1000 + n, "name": f"Run {n}", "sport_type": "Run", "type": "Run",
        "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "distance": 5000.0 + n, "moving_time": 1500, "elapsed_time": 1600,
        "total_elevation_gain": 40.0, "average_speed": 3.3, "average_heartrate": 150.4, "max_heartrate": 171.0,
        "start_latlng": [48.1, 11.5], "end_latlng": [48.2, 11.6],
        "map": {"summary_polyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"},
    }
    a.update(extra)
    return a
