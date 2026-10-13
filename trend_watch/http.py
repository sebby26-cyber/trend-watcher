"""Bounded urllib transport shared by adapters; injectable transport/clock for tests."""
import json
import time
import urllib.error
import urllib.request


class HTTPContext:
    def __init__(self, settings, origins=None, opener=None, sleep=time.sleep, clock=time.monotonic):
        self.settings = settings
        self.origins = origins or {}
        self.opener = opener or urllib.request.urlopen
        self.sleep, self.clock = sleep, clock
        self.last_request = None
        self.status = "ok"
        self.errors = []

    def owners(self, key, value):
        return self.origins.get(key, {}).get(str(value), [])

    def request(self, url, headers=None, data=None, retry_codes=None, before_attempt=None):
        headers = dict(headers or {})
        headers.setdefault("User-Agent", self.settings["user_agent"])
        for attempt in range(min(2, int(self.settings.get("retries", 2))) + 1):
            interval = self.settings.get("request_interval_s", 0)
            if self.last_request is not None:
                self.sleep(max(0, interval - (self.clock() - self.last_request)))
            if before_attempt:
                before_attempt()
            self.last_request = self.clock()
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                with self.opener(req, timeout=self.settings["timeout_s"]) as response:
                    return response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                retriable = exc.code in retry_codes if retry_codes is not None else exc.code == 429 or 500 <= exc.code <= 599
                if not retriable or attempt == min(2, int(self.settings.get("retries", 2))):
                    raise
                self.sleep(self.settings.get("backoff_s", 1) * 2 ** attempt)

    def get_text(self, url, headers=None):
        return self.request(url, headers)

    def get_json(self, url, headers=None):
        return json.loads(self.get_text(url, headers))

    def post_json(self, url, payload, key, before_attempt=None):
        return json.loads(self.request(url, {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                                       json.dumps(payload).encode("utf-8"), {429, 529}, before_attempt))

    def collect(self, values, fetch):
        """Keep successful origins even when another subreddit/feed fails."""
        result = []
        for value in values:
            try:
                result.extend(fetch(value))
            except Exception as exc:
                self.errors.append("{}: {}".format(value, type(exc).__name__))
        if self.errors:
            self.status = "partial" if result else "error"
        return result
