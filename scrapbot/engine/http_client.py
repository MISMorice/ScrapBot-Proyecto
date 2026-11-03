"""Cliente HTTP "educado" (Tabla 5 fila 3; mitigaciones R2 y R5).

* Identificación explícita mediante la cabecera User-Agent.
* Pausa aleatoria (2-5 s por defecto) entre peticiones al mismo host.
* Timeout de 10 s y reintentos con espera exponencial ante red/5xx/429.
* Respeta robots.txt (RFC 9309) y se mantiene en el dominio autorizado.
* No hay ninguna técnica de evasión: un 403/401 simplemente se registra.
"""
from __future__ import annotations

import hashlib
import random
import time
import urllib.robotparser
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

import requests


class FetchError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class RobotsDisallowed(FetchError):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    text: str
    sha256: str
    elapsed_ms: int
    attempts: int


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class PoliteHttpClient:
    def __init__(self, *, user_agent: str, timeout: float = 10, max_retries: int = 3,
                 backoff_base: float = 2.0, delay_range: tuple = (2.0, 5.0),
                 respect_robots: bool = True, max_bytes: int = 5 * 1024 * 1024,
                 on_attempt: Callable | None = None, session: requests.Session | None = None,
                 sleep: Callable = time.sleep, uniform: Callable = random.uniform):
        self.user_agent = user_agent
        self.agent_token = user_agent.split("/")[0].split()[0]
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.delay_range = delay_range
        self.respect_robots = respect_robots
        self.max_bytes = max_bytes
        self.on_attempt = on_attempt or (lambda **kw: None)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"})
        self._sleep = sleep
        self._uniform = uniform
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._crawl_delay: dict[str, float] = {}

    # --- Control de frecuencia -----------------------------------------
    def _pause(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = max(self._uniform(*self.delay_range), self._crawl_delay.get(host, 0))
            remaining = wait - (time.monotonic() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request[host] = time.monotonic()

    # --- Una petición (sin reintentos) ---------------------------------
    def _send(self, url: str):
        """Devuelve (response|None, body|None, error|None, elapsed_ms)."""
        started = time.monotonic()
        try:
            resp = self.session.get(url, timeout=(self.timeout, self.timeout), stream=True,
                                    allow_redirects=True)
            chunks, size = [], 0
            try:
                for chunk in resp.iter_content(65536):
                    size += len(chunk)
                    if size > self.max_bytes:
                        resp.close()
                        ms = int((time.monotonic() - started) * 1000)
                        return resp, None, f"Respuesta mayor a {self.max_bytes} bytes", ms
                    chunks.append(chunk)
            finally:
                resp.close()
            return resp, b"".join(chunks), None, int((time.monotonic() - started) * 1000)
        except requests.exceptions.Timeout:
            return None, None, f"Timeout de {self.timeout:g} s", int((time.monotonic() - started) * 1000)
        except requests.exceptions.RequestException as exc:
            return None, None, f"Error de red: {type(exc).__name__}", int((time.monotonic() - started) * 1000)

    @staticmethod
    def _decode(resp, body: bytes) -> str:
        ctype = resp.headers.get("Content-Type", "").lower()
        if "charset=" in ctype:
            try:
                return body.decode(resp.encoding or "utf-8", errors="replace")
            except LookupError:
                pass
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            return body.decode("latin-1")

    # --- robots.txt ------------------------------------------------------
    def _robots_for(self, url: str):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        robots_url = origin + "/robots.txt"
        self._pause(_host(url))
        resp, body, error, ms = self._send(robots_url)
        status = resp.status_code if resp is not None else None
        self.on_attempt(url=robots_url, attempt=1, status_code=status, response_ms=ms,
                        sha256=hashlib.sha256(body).hexdigest() if body else None, error=error)
        parser: urllib.robotparser.RobotFileParser | None
        if resp is not None and status == 200 and body is not None:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(self._decode(resp, body).splitlines())
            delay = parser.crawl_delay(self.agent_token)
            if delay:
                self._crawl_delay[_host(url)] = float(delay)
        elif status is not None and 400 <= status < 500:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse([])                       # sin robots.txt: todo permitido
        else:
            parser = None                          # inaccesible (5xx/red): se asume bloqueado
        self._robots[origin] = parser
        return parser

    def _check_robots(self, url: str) -> None:
        parser = self._robots_for(url)
        if parser is None:
            msg = "robots.txt inaccesible: se omite la URL por precaución"
            self.on_attempt(url=url, attempt=0, status_code=None, response_ms=0, sha256=None, error=msg)
            raise RobotsDisallowed(msg)
        if not parser.can_fetch(self.agent_token, url):
            msg = "Bloqueado por robots.txt de la fuente"
            self.on_attempt(url=url, attempt=0, status_code=None, response_ms=0, sha256=None, error=msg)
            raise RobotsDisallowed(msg)

    # --- API pública -----------------------------------------------------
    def get(self, url: str) -> FetchResult:
        if urlsplit(url).scheme not in ("http", "https"):
            raise FetchError(f"Esquema no permitido: {url}")
        if self.respect_robots:
            self._check_robots(url)
        host = _host(url)
        last_error, last_status = "Sin intentos", None
        for attempt in range(1, self.max_retries + 2):
            self._pause(host)
            resp, body, error, ms = self._send(url)
            status = resp.status_code if resp is not None else None
            sha = hashlib.sha256(body).hexdigest() if body is not None else None

            if resp is not None and error is None and _host(resp.url) != host:
                error = f"Redirección a un dominio no autorizado: {_host(resp.url)}"
                self.on_attempt(url=url, attempt=attempt, status_code=status, response_ms=ms, sha256=sha, error=error)
                raise FetchError(error, status)
            if resp is not None and error is None and status is not None and 200 <= status < 300:
                ctype = resp.headers.get("Content-Type", "").lower()
                if ctype and "html" not in ctype and "xml" not in ctype:
                    error = f"Tipo de contenido no soportado: {ctype}"
                    self.on_attempt(url=url, attempt=attempt, status_code=status, response_ms=ms, sha256=sha, error=error)
                    raise FetchError(error, status)
                self.on_attempt(url=url, attempt=attempt, status_code=status, response_ms=ms, sha256=sha, error=None)
                return FetchResult(url, resp.url, status, self._decode(resp, body), sha, ms, attempt)

            if error is None:
                error = f"HTTP {status}"
            self.on_attempt(url=url, attempt=attempt, status_code=status, response_ms=ms, sha256=sha, error=error)
            last_error, last_status = error, status

            retryable = status is None or status in RETRYABLE_STATUS
            if not retryable or attempt > self.max_retries:
                break
            wait = self.backoff_base ** (attempt - 1)
            if status == 429 and resp is not None:
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = max(wait, min(float(retry_after), 60))
            self._sleep(wait)
        raise FetchError(f"{last_error} ({url})", last_status)
