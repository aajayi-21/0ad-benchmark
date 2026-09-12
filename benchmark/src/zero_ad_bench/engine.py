"""Own one headless engine process and its private benchmark transport."""

import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib import error, request

from zero_ad_bench import PROTOCOL_VERSION


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENGINE = Path(os.environ.get("ZERO_AD_ENGINE", ROOT / "binaries/system/pyrogenesis"))


class EngineError(Exception):
    """A transport, protocol, or engine failure that invalidates the current episode."""

    def __init__(self, kind, code, message, status=None, detail=None):
        super().__init__(f"{kind}/{code}: {message}")
        self.kind = kind
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail

    def record(self):
        return {
            "kind": self.kind,
            "code": self.code,
            "message": self.message,
            "http_status": self.status,
            "detail": self.detail,
        }


class EngineProcess:
    """Start `pyrogenesis --benchmark-interface` with an isolated profile and a process deadline.

    `mod_sources` maps additional mod names to directories copied into the profile before start.
    The runner token never leaves this object and must not be given to a model adapter.
    """

    def __init__(
        self,
        directory,
        *,
        engine=DEFAULT_ENGINE,
        mods=("agent_benchmark",),
        mod_sources=None,
        port=None,
        process_deadline_s=1800,
        request_timeout_s=45,
    ):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.engine = Path(engine)
        self.request_timeout_s = request_timeout_s
        self.token = secrets.token_hex(24)
        self.opener = request.build_opener(request.ProxyHandler({}))
        if port is None:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.env = os.environ.copy()
        for key in ("DISPLAY", "WAYLAND_DISPLAY"):
            self.env.pop(key, None)
        for key in ("DATA", "CONFIG", "CACHE", "STATE"):
            self.env[f"XDG_{key}_HOME"] = str(self.directory / key.lower())
        self.env["ZERO_AD_BENCHMARK_TOKEN"] = self.token
        self.mod_sources = {name: Path(path) for name, path in (mod_sources or {}).items()}
        for name, source in self.mod_sources.items():
            shutil.copytree(source, self.directory / "data/0ad/mods" / name)
        self.mods = list(mods)
        args = [
            str(self.engine),
            "--autostart-nonvisual",
            f"--benchmark-interface={self.url[7:]}",
            "--mod=public",
            *[f"--mod={name}" for name in self.mods],
        ]
        self.log_path = self.directory / "engine.log"
        self.log = self.log_path.open("w", encoding="utf-8")
        # --foreground keeps the engine in the runner's process group, so killing that group
        # (an operator or supervisor action) takes the engine down with the runner.
        self.process = subprocess.Popen(
            ["timeout", "--foreground", "--kill-after=5s", f"{int(process_deadline_s)}s", *args],
            cwd=self.directory,
            env=self.env,
            stdout=self.log,
            stderr=self.log,
        )

    def call(self, operation, body=None, request_id=None):
        """Perform one HTTP request and return `(status, decoded JSON)`; transport errors raise."""
        headers = {
            "Content-Type": "application/json",
            "X-Request-ID": request_id or secrets.token_hex(8),
            "Authorization": "Bearer " + self.token,
        }
        method = "GET" if operation == "health" else "POST"
        data = None if method == "GET" else json.dumps(body or {}).encode()
        # The URL is always the numeric loopback address chosen above.
        req = request.Request(  # noqa: S310
            f"{self.url}/benchmark/v1/{operation}", data=data, headers=headers, method=method
        )
        try:
            response = self.opener.open(req, timeout=self.request_timeout_s)
        except error.HTTPError as exc:
            response = exc
        except (OSError, error.URLError) as exc:
            raise EngineError("transport", "unreachable", str(exc)) from exc
        with response:
            status = response.code
            content = response.read().decode()
        if response.headers.get_content_type() != "application/json":
            raise EngineError("transport", "non_json", content[:200], status)
        return status, json.loads(content)

    def request(self, operation, body=None, request_id=None):
        """Perform a request and return the envelope, raising `EngineError` on failure."""
        status, envelope = self.call(operation, body, request_id)
        if not envelope.get("ok"):
            failure = envelope.get("error") or {}
            raise EngineError(
                "engine_error",
                failure.get("code", "unknown"),
                failure.get("message", ""),
                status,
                {"state": envelope.get("state"), "turn": envelope.get("turn")},
            )
        if envelope.get("protocol_version") != PROTOCOL_VERSION:
            raise EngineError(
                "protocol", "version_mismatch", str(envelope.get("protocol_version")), status
            )
        return envelope

    def mutate(self, operation, body, request_id, attempts=3):
        """Submit a reset/advance once.

        Retries reuse the request ID and byte-identical content, so the engine returns the
        original receipt instead of executing twice. When the outcome is still unknown after
        the retries, the episode is unusable and must be discarded.
        """
        last = None
        for _ in range(attempts):
            try:
                return self.request(operation, body, request_id)
            except EngineError as exc:
                if exc.kind != "transport":
                    raise
                last = exc
                time.sleep(0.2)
        raise EngineError("ambiguous", "unknown_outcome", str(last), detail=last.record())

    def ready(self, timeout_s=30):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise EngineError(
                    "engine_exit", "startup_failed", self.log_path.read_text()[-2000:]
                )
            try:
                status, envelope = self.call("health")
            except EngineError:
                time.sleep(0.1)
                continue
            if status == 200:
                return envelope["data"]
            time.sleep(0.1)
        raise EngineError("transport", "startup_timeout", "Engine did not start in time")

    def alive(self):
        return self.process.poll() is None

    def close(self):
        if self.process.poll() is None:
            try:
                self.call("shutdown")
                self.process.wait(timeout=10)
            except (EngineError, subprocess.TimeoutExpired, ValueError):
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        self.log.close()
