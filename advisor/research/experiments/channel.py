"""Bounded, one-request-at-a-time transport shared by host and process monitor."""
from dataclasses import dataclass
import json
from threading import Lock


@dataclass(frozen=True)
class ChannelLimits:
    frame_bytes: int
    response_bytes: int
    total_response_bytes: int
    max_requests: int

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in vars(self).values()):
            raise ValueError("channel limits must be explicit positive integers")


class ToolChannel:
    def __init__(self, limits: ChannelLimits):
        self.limits = limits
        self._lock = Lock()
        self._buffer = bytearray()
        self._request = self._response = None
        self._outstanding = False
        self._count = self._sent = 0
        self._failure = None

    @property
    def failure(self):
        with self._lock:
            return self._failure

    def receive(self, chunk):
        with self._lock:
            if self._failure:
                return
            if self._outstanding:
                self._failure = "request_pipelining"
                return
            self._buffer.extend(chunk)
            if len(self._buffer) > self.limits.frame_bytes:
                self._failure = "frame_limit"
                self._buffer.clear()
                return
            if b"\n" not in self._buffer:
                return
            line, rest = bytes(self._buffer).split(b"\n", 1)
            self._buffer.clear()
            self._count += 1
            if rest:
                self._failure = "request_pipelining"
            elif self._count > self.limits.max_requests:
                self._failure = "request_limit"
            else:
                try:
                    # Reject duplicate keys, non-JSON numbers and invalid UTF-8.
                    def pairs(items):
                        value = {}
                        for key, item in items:
                            if key in value:
                                raise ValueError()
                            value[key] = item
                        return value
                    value = json.loads(line.decode("utf-8"), object_pairs_hook=pairs,
                                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if not isinstance(value, dict):
                        raise ValueError()
                except (ValueError, RecursionError):
                    self._failure = "invalid_frame"
                else:
                    self._request, self._outstanding = value, True

    def request(self):
        with self._lock:
            value, self._request = self._request, None
            return value

    def answer(self, response):
        data = (json.dumps(response, ensure_ascii=True, allow_nan=False,
                           separators=(",", ":")) + "\n").encode()
        with self._lock:
            if self._failure:
                return
            if not self._outstanding or self._response is not None:
                raise RuntimeError("no outstanding channel request")
            if (len(data) > self.limits.response_bytes
                    or self._sent + len(data) > self.limits.total_response_bytes):
                self._failure = "response_limit"
                return
            self._sent += len(data)
            self._response = data

    def response(self):
        with self._lock:
            value, self._response = self._response, None
            return value

    def response_sent(self):
        with self._lock:
            self._outstanding = False

    def eof(self):
        with self._lock:
            if self._buffer and not self._failure:
                self._failure = "incomplete_frame"

    def abort(self, reason):
        with self._lock:
            if not self._failure:
                self._failure = reason
            self._request = self._response = None
            self._buffer.clear()
