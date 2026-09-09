"""Small process harness for black-box ContextMesh evaluations.

The harness deliberately talks to the compiled binary over HTTP and uses the
PostgreSQL URLs supplied by the caller.  It is useful from unittest suites as
well as from a benchmark runner::

    with Service(mock=True) as cm:
        event_id = cm.insert("The release is Friday", "release-1")
        cm.await_idle()
        packet = cm.request("/v1/context", {"task": "release"})

Each instance gets fresh random tenant ids.  No rows are deleted on teardown;
this makes a failed run inspectable while preventing two concurrent runs from
seeing one another's data.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


ROOT = Path(__file__).resolve().parents[1]


class RequestError(RuntimeError):
    """An HTTP response did not match ``request``'s expected status."""

    def __init__(self, status: int, path: str, body: Any):
        self.status = status
        self.path = path
        self.body = body
        super().__init__(f"ContextMesh {path} returned HTTP {status}: {body!r}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_response(response: Any) -> Any:
    data = response.read()
    if not data:
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return data.decode("utf-8", errors="replace")


class Service:
    """Run one real ContextMesh API and worker against an isolated tenant.

    ``mock`` is an opt-in conformance mode using the deterministic gateway in
    ``tests/e2e.py``. The default has no model environment and cannot spend
    provider tokens.
    """

    def __init__(
        self,
        *,
        binary: str | os.PathLike[str] | None = None,
        database_url: str | None = None,
        migration_database_url: str | None = None,
        mock: bool = False,
        mock_curation: bool | None = None,
        workers: int = 2,
        startup_timeout: float = 30.0,
        preserve_logs: bool | None = None,
    ) -> None:
        if mock_curation is not None:
            mock = mock_curation
        self.binary = str(Path(binary or os.environ.get("CONTEXTMESH_BINARY", ROOT / "target/debug/contextmesh")).resolve())
        self.database_url = database_url or os.environ.get("DATABASE_URL")
        self.migration_database_url = migration_database_url or os.environ.get("MIGRATION_DATABASE_URL")
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required for evals.Service")
        if not self.migration_database_url:
            raise RuntimeError("MIGRATION_DATABASE_URL is required for evals.Service")
        if workers < 1 or workers > 64:
            raise ValueError("workers must be between 1 and 64")
        self.mock = mock
        self.workers_count = workers
        self.startup_timeout = startup_timeout
        self.preserve_logs = preserve_logs if preserve_logs is not None else bool(os.environ.get("CONTEXTMESH_TEST_LOG_DIR"))
        self.tenant_id = str(uuid.uuid4())
        self.other_tenant_id = str(uuid.uuid4())
        self.namespace = secrets.token_hex(10)
        self.owner = "eval-owner-" + secrets.token_urlsafe(18)
        self.user = "eval-user-" + secrets.token_urlsafe(18)
        self.guest = "eval-guest-" + secrets.token_urlsafe(18)
        self.finance = "eval-finance-" + secrets.token_urlsafe(18)
        self.outsider = "eval-outsider-" + secrets.token_urlsafe(18)
        # Friendly aliases used by benchmark adapters.
        self.owner_token = self.owner
        self.user_token = self.user
        self.guest_token = self.guest
        self.finance_token = self.finance
        self.outsider_token = self.outsider
        self.url: str | None = None
        self._temp: tempfile.TemporaryDirectory[str] | None = None
        self._logs: list[Any] = []
        self._servers: list[subprocess.Popen[bytes]] = []
        self._gateway: ThreadingHTTPServer | None = None
        self._gateway_thread: threading.Thread | None = None
        self._env: dict[str, str] | None = None

    def __enter__(self) -> "Service":
        try:
            return self._start()
        except BaseException:
            # Python does not call __exit__ when __enter__ fails.  Stop any
            # process/gateway already started and remove temporary credentials.
            self.__exit__(None, None, None)
            raise

    def _start(self) -> "Service":
        if not Path(self.binary).is_file():
            raise FileNotFoundError(f"compiled ContextMesh binary not found: {self.binary}")
        self._temp = tempfile.TemporaryDirectory(prefix="contextmesh-eval-")
        directory = Path(self._temp.name)
        env = os.environ.copy()
        env["RUST_LOG"] = env.get("RUST_LOG", "contextmesh=info")
        if not self.mock:
            # Capture and context remain available without an inference gateway.
            for key in ("CONTEXTMESH_MODEL_URL", "CONTEXTMESH_MODEL", "CONTEXTMESH_MODEL_KEY"):
                env.pop(key, None)
        else:
            self._start_gateway()
            assert self._gateway is not None
            model_url = f"http://127.0.0.1:{self._gateway.server_port}/v1"
            env.update(CONTEXTMESH_MODEL_URL=model_url, CONTEXTMESH_MODEL="mock-curator", CONTEXTMESH_MODEL_KEY="eval-only")
        config = {
            "tenants": [
                {"id": self.tenant_id, "name": "Evaluation tenant"},
                {"id": self.other_tenant_id, "name": "Evaluation outsider"},
            ],
            "dev_tokens": [
                {"token": self.owner, "tenant_id": self.tenant_id, "subject": "owner", "groups": ["contextmesh-admins", "engineering", "finance"]},
                {"token": self.user, "tenant_id": self.tenant_id, "subject": "user", "groups": ["engineering"]},
                {"token": self.guest, "tenant_id": self.tenant_id, "subject": "guest", "groups": []},
                {"token": self.finance, "tenant_id": self.tenant_id, "subject": "finance", "groups": ["finance"]},
                {"token": self.outsider, "tenant_id": self.other_tenant_id, "subject": "outsider", "groups": []},
            ],
        }
        config_path = directory / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.config_path = config_path
        self._env = env
        subprocess.run([self.binary, "migrate", "--database-url", self.migration_database_url], env=env, check=True)
        self.url = f"http://127.0.0.1:{_free_port()}"
        listen = self.url.removeprefix("http://")
        self._start_process("serve", directory / "serve.log", config_path, listen, env)
        self._start_process("worker", directory / "worker.log", config_path, None, env)
        self._wait_for_health()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        for process in self._servers:
            if process.poll() is None:
                process.terminate()
        for process in self._servers:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self._gateway is not None:
            self._gateway.shutdown()
            self._gateway.server_close()
        for log in self._logs:
            log.flush()
            log.close()
        if self._temp is not None and self.preserve_logs:
            destination = os.environ.get("CONTEXTMESH_TEST_LOG_DIR")
            if destination:
                destination_path = Path(destination)
                destination_path.mkdir(parents=True, exist_ok=True)
                for log in Path(self._temp.name).glob("*.log"):
                    shutil.copy2(log, destination_path / f"{self.namespace}-{log.name}")
        if self._temp is not None:
            self._temp.cleanup()

    def _start_gateway(self) -> None:
        path = ROOT / "tests" / "e2e.py"
        spec = importlib.util.spec_from_file_location("contextmesh_e2e_gateway", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot import deterministic gateway from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        gateway_class = module.Gateway
        gateway_class.counts = {}
        gateway_class.gates = {}
        gateway_class.entered = {}
        gateway_class.healed = set()
        gateway_class.jwks = {"keys": []}
        self._gateway = ThreadingHTTPServer(("127.0.0.1", 0), gateway_class)
        self._gateway_thread = threading.Thread(target=self._gateway.serve_forever, daemon=True)
        self._gateway_thread.start()

    def _start_process(self, mode: str, log_path: Path, config: Path, listen: str | None, env: dict[str, str]) -> None:
        args = [self.binary, mode, "--database-url", self.database_url, "--config", str(config), "--allow-dev-auth"]
        if listen is not None:
            args += ["--listen", listen]
        if mode == "worker":
            args += ["--workers", str(self.workers_count)]
        log = log_path.open("wb")
        self._logs.append(log)
        self._servers.append(subprocess.Popen(args, env=env, stdout=log, stderr=log))

    def _wait_for_health(self) -> None:
        assert self.url is not None
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in self._servers):
                logs = "\n".join(Path(log.name).read_text(errors="replace")[-2000:] for log in self._logs)
                raise RuntimeError(f"ContextMesh process exited during startup\n{logs}")
            try:
                status, _ = self.request_raw("/health", token=self.owner)
                if status == 200:
                    return
            except (OSError, URLError, HTTPError):
                pass
            time.sleep(0.1)
        raise TimeoutError("ContextMesh API did not become healthy")

    def request_raw(self, path: str, body: Any = None, token: str | None = None) -> tuple[int, Any]:
        """Return ``(status, decoded_body)`` without raising on HTTP errors."""
        if self.url is None:
            raise RuntimeError("Service is not running")
        if not path.startswith("/") or ".." in path or "#" in path:
            raise ValueError("path must be an absolute API path without '..' or '#'")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(self.url + path, data=data, headers=headers, method="POST" if body is not None else "GET")
        try:
            with urlopen(request, timeout=240) as response:
                return response.status, _json_response(response)
        except HTTPError as error:
            return error.code, _json_response(error)

    def request(
        self,
        path: str,
        body: Any = None,
        token: str | None = None,
        *,
        expected: int | Iterable[int] | None = 200,
    ) -> Any:
        """Call the API, raising ``RequestError`` unless status is expected.

        Set ``expected=None`` to accept any status and return the decoded body;
        use :meth:`request_raw` when the status itself is part of the result.
        """
        status, result = self.request_raw(path, body, token or self.user)
        if expected is not None:
            allowed = {expected} if isinstance(expected, int) else set(expected)
            if status not in allowed:
                raise RequestError(status, path, result)
        return result

    def insert(
        self, text: str, external_id: str, revision: int = 1, context: dict[str, Any] | None = None,
        classification: str = "internal", read_groups: list[str] | None = None, token: str | None = None,
        inputs: list[str] | None = None, supersedes: list[str] | None = None,
    ) -> str:
        """Append a stable record and return its UUID (external_id is deterministic UUID input)."""
        import uuid
        try: rid = str(uuid.UUID(external_id))
        except ValueError: rid = str(uuid.uuid5(uuid.NAMESPACE_URL, external_id))
        scope = {"visibility": classification, "groups": read_groups or []}
        if context and isinstance(context.get("scope"), dict): scope.update(context["scope"])
        response = self.request("/v1/records", {"records": [{"id": rid, "content": text, "scope": scope,
            "inputs": inputs or [], "supersedes": supersedes or [], "metadata": context or {}}]}, token)
        return str(response.get("records", [{}])[0].get("id", rid))

    def insert_response(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        text, external_id = args[:2] if len(args) >= 2 else (kwargs.pop("text"), kwargs.pop("external_id"))
        token = kwargs.pop("token", None)
        return {"records": [{"id": self.insert(text, external_id, token=token)}]}

    def query(self, query: str, *, token: str | None = None, **options: Any) -> dict[str, Any]:
        packet = self.request("/v1/context", {"task": query, **options}, token)
        packet["memories"] = packet.get("records", [])
        return packet

    def find(self, query: str, *, token: str | None = None, **options: Any) -> dict[str, Any]:
        return self.query(query, token=token, **options)


    def await_idle(self, timeout: float = 60.0, poll_interval: float = 0.2) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.request("/v1/status", token=self.owner)
            jobs = status.get("jobs", {})
            if isinstance(jobs, Mapping):
                if int(jobs.get("failed", 0) or 0): raise RuntimeError("ContextMesh curation failed")
                if not any(int(jobs.get(k, 0) or 0) for k in ("pending", "running")): return
            elif isinstance(jobs, list) and not any(x.get("state") in ("pending", "running") for x in jobs if isinstance(x, Mapping)): return
            time.sleep(poll_interval)
        raise TimeoutError("ContextMesh did not become idle")

__all__ = ["RequestError", "Service"]
