"""Fake-HTTP für Adaptertests: Routen mit Regex, Aufzeichnung aller Anfragen, Weiterleitungen."""

from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from schulsync.errors import NetworkError
from schulsync.http import HttpResponse


@dataclass
class RecordedRequest:
    method: str
    url: str
    params: Mapping[str, Any] | None
    data: Any
    json: Any
    headers: Mapping[str, str] | None


def json_response(data: Any, status: int = 200, headers: Mapping[str, str] | None = None) -> HttpResponse:
    h = {"content-type": "application/json"}
    h.update({k.lower(): v for k, v in (headers or {}).items()})
    return HttpResponse(status=status, url="", headers=h, text=_json.dumps(data))


def text_response(text: str, status: int = 200, headers: Mapping[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status=status, url="", headers={k.lower(): v for k, v in (headers or {}).items()}, text=text)


def redirect(location: str, status: int = 302) -> HttpResponse:
    return HttpResponse(status=status, url="", headers={"location": location}, text="")


Handler = Callable[[RecordedRequest], HttpResponse]


@dataclass
class _Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler | HttpResponse | list


@dataclass
class FakeHttp:
    """Erste passende Route gewinnt. Handler: HttpResponse, Callable oder Liste (nacheinander)."""

    routes: list[_Route] = field(default_factory=list)
    requests: list[RecordedRequest] = field(default_factory=list)
    cookies: dict[str, str] = field(default_factory=dict)
    fail_network: bool = False

    def add(self, method: str, url_regex: str, handler: Handler | HttpResponse | list) -> "FakeHttp":
        self.routes.append(_Route(method.upper(), re.compile(url_regex), handler))
        return self

    def get_cookie(self, name: str) -> str | None:
        return self.cookies.get(name)

    def calls(self, url_regex: str, method: str | None = None) -> list[RecordedRequest]:
        pat = re.compile(url_regex)
        return [r for r in self.requests if pat.search(r.url) and (method is None or r.method == method.upper())]

    def request(self, method, url, *, params=None, data=None, json=None, headers=None, allow_redirects=True):
        if self.fail_network:
            raise NetworkError("Keine Verbindung (Fake).")
        history: list[tuple[int, str]] = []
        method = method.upper()
        for _ in range(10):
            full = url + (("?" + urlencode(params)) if params else "")
            req = RecordedRequest(method, full, params, data, json, headers)
            self.requests.append(req)
            resp = self._dispatch(req)
            resp = HttpResponse(resp.status, resp.url or url, resp.headers, resp.text, tuple(history))
            loc = resp.headers.get("location")
            if allow_redirects and resp.status in (301, 302, 303, 307, 308) and loc:
                history.append((resp.status, loc))
                url = loc if loc.startswith("http") else re.sub(r"^(https://[^/]+).*$", r"\1", url) + loc
                method, params, data, json = "GET", None, None, None
                continue
            return resp
        raise AssertionError("zu viele Weiterleitungen im Fake")

    def _dispatch(self, req: RecordedRequest) -> HttpResponse:
        for route in self.routes:
            if route.method == req.method and route.pattern.search(req.url):
                h = route.handler
                if isinstance(h, list):
                    return h.pop(0) if len(h) > 1 else h[0]
                if callable(h):
                    return h(req)
                return h
        raise AssertionError(f"Keine Fake-Route für {req.method} {req.url}")
