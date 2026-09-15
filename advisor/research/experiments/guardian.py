"""Independent candidate owner and append-only local cleanup evidence.

The guardian survives a worker crash. Killing the guardian itself can still leave
unknown cleanup; a free lock/PID absence is never evidence that its child exited.
No database, gateway object, credential or inherited worker FD enters the candidate.
"""
import base64
from dataclasses import asdict, dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time
import uuid

from .isolation import DarwinCandidateRunner, DarwinPythonRuntime, ProcessLimits, ProcessResult
from .repository import Conflict
from .resolution import digest, encode


@dataclass(frozen=True)
class GuardianLimits:
    ipc_frame_bytes: int
    ipc_pending_bytes: int
    owner_timeout_seconds: float

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in (self.ipc_frame_bytes, self.ipc_pending_bytes)):
            raise ValueError("guardian IPC limits must be explicit positive integers")
        if (isinstance(self.owner_timeout_seconds, bool) or not math.isfinite(self.owner_timeout_seconds)
                or self.owner_timeout_seconds <= 0 or self.ipc_pending_bytes < self.ipc_frame_bytes):
            raise ValueError("invalid guardian timeout or pending limit")


class PeerFailure(RuntimeError):
    pass


class Peer:
    def __init__(self, connection, limits):
        self.socket, self.limits = connection, limits
        connection.setblocking(False)
        self.incoming, self.outgoing = bytearray(), bytearray()

    def send(self, value):
        data = encode(value).encode() + b"\n"
        if len(data) > self.limits.ipc_frame_bytes or len(self.outgoing) + len(data) > self.limits.ipc_pending_bytes:
            raise PeerFailure("guardian_ipc_limit")
        self.outgoing.extend(data)

    def poll(self):
        try:
            if self.outgoing:
                try:
                    sent = self.socket.send(self.outgoing)
                    del self.outgoing[:sent]
                except BlockingIOError:
                    pass
            try:
                data = self.socket.recv(min(65536, self.limits.ipc_pending_bytes + 1))
            except BlockingIOError:
                data = None
            if data == b"":
                raise PeerFailure("owner_disconnected")
            if data:
                self.incoming.extend(data)
            if len(self.incoming) > self.limits.ipc_pending_bytes:
                raise PeerFailure("guardian_ipc_limit")
            values = []
            while b"\n" in self.incoming:
                line, _, rest = self.incoming.partition(b"\n")
                self.incoming = bytearray(rest)
                if len(line) + 1 > self.limits.ipc_frame_bytes:
                    raise PeerFailure("guardian_ipc_limit")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise PeerFailure("guardian_ipc_invalid")
                values.append(value)
            if len(self.incoming) > self.limits.ipc_frame_bytes:
                raise PeerFailure("guardian_ipc_limit")
            return values
        except (OSError, ValueError) as error:
            raise PeerFailure("guardian_ipc_failure") from error


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise Conflict("guardian evidence must be a regular file")
        data = stream.read()
    value = json.loads(data)
    if encode(value).encode() != data:
        raise Conflict("guardian evidence is not canonical")
    return value


def _publish(directory, name, value, *, exclusive=False):
    data = encode(value).encode()
    temporary = directory / (".staging-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, directory / name)
        except FileExistsError:
            if exclusive or _read(directory / name) != value:
                raise Conflict("guardian evidence already exists") from None
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def _lock(directory):
    return os.open(directory / "guardian.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)


def _receipt(request, state, result=None):
    return {"version": 1, "identity": request["identity"], "nonce": request["nonce"],
            "request_hash": digest(request), "state": state, "quiescent": True,
            "result": result, "formal_ready": False}


class GuardedCandidateRunner:
    def __init__(self, *, artifacts, runtime, evidence_root: Path, guardian_limits: GuardianLimits):
        self.artifacts, self.runtime, self.guardian_limits = artifacts, runtime, guardian_limits
        self.evidence_root = evidence_root.resolve()
        self.evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def capabilities():
        return {**DarwinCandidateRunner.capabilities(), "transport": "candidate-guardian@1",
                "worker_loss_recovery": True, "guardian_loss_recovery": False,
                "guardian_compute_metering": False, "formal_ready": False}

    def prepare(self, identity, package_hash, payload, *, limits):
        if type(payload) is not bytes or len(payload) > limits.input_bytes:
            raise ValueError("guardian input exceeds process byte limit")
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("guardian identity must be a digest")
        request = {"version": 1, "identity": identity, "package_hash": package_hash,
                   "input_hash": digest(base64.b64encode(payload).decode()), "limits": asdict(limits),
                   "guardian_limits": asdict(self.guardian_limits), "artifacts_root": str(self.artifacts.root),
                   "runtime": {"executable": str(self.runtime.executable), "stdlib": str(self.runtime.stdlib),
                               "native_files": [str(p) for p in self.runtime.native_files]}}
        # Reserve enough space for base64 output plus the bounded metadata in the
        # final message before any dispatch. No silent truncation of IPC evidence.
        if self.guardian_limits.ipc_frame_bytes < 2 * max(limits.output_bytes, limits.input_bytes) + 4096:
            raise ValueError("guardian frame cannot hold configured process evidence")
        directory = self.evidence_root / identity
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if directory.is_symlink():
                raise Conflict("guardian directory cannot be a symlink")
            old = _read(directory / "request.json")
            if {k: v for k, v in old.items() if k != "nonce"} != request:
                raise Conflict("guardian preparation input changed")
            request = old
        else:
            request["nonce"] = uuid.uuid4().hex
            _publish(directory, "request.json", request)
        return {"backend": "candidate-guardian@1", "identity": identity, "nonce": request["nonce"],
                "request_hash": digest(request)}

    def _load(self, handle):
        identity = handle["identity"]
        if (handle.get("backend") != "candidate-guardian@1" or len(identity) != 64
                or any(c not in "0123456789abcdef" for c in identity)):
            raise Conflict("invalid guardian handle")
        directory = self.evidence_root / identity
        if directory.is_symlink():
            raise Conflict("guardian directory cannot be a symlink")
        request = _read(directory / "request.json")
        if request["identity"] != identity or request["nonce"] != handle["nonce"] or digest(request) != handle["request_hash"]:
            raise Conflict("guardian handle binding mismatch")
        return directory, request

    def evidence(self, handle):
        directory, request = self._load(handle)
        try:
            receipt = _read(directory / "receipt.json")
        except FileNotFoundError:
            return None
        if (receipt.get("version") != 1 or receipt.get("identity") != handle["identity"]
                or receipt.get("nonce") != handle["nonce"] or receipt.get("request_hash") != handle["request_hash"]
                or receipt.get("quiescent") is not True or receipt.get("formal_ready") is not False):
            raise Conflict("guardian receipt binding mismatch")
        if receipt["state"] == "reaped":
            result = receipt["result"]
            if (result["package_hash"] != request["package_hash"] or result["quiescent"] is not True
                    or result.get("formal_ready") is not False or not (directory / "child-intent.json").is_file()
                    or type(result.get("pid")) is not int or result["pid"] <= 0
                    or type(result.get("returncode")) is not int):
                raise Conflict("guardian result binding mismatch")
            for name in ("user_cpu_seconds", "system_cpu_seconds", "peak_rss_bytes", "wall_seconds"):
                value = result[name]
                if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                    raise Conflict("invalid guardian resource evidence")
            for name in ("stdout_hash", "stderr_hash", "profile_hash"):
                value = result[name]
                if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                    raise Conflict("invalid guardian output evidence")
        elif receipt["state"] != "not_started" or receipt["result"] is not None:
            raise Conflict("invalid guardian cleanup receipt")
        elif (directory / "child-intent.json").exists():
            raise Conflict("not-started evidence contradicts child launch intent")
        return receipt

    def reconcile(self, handle):
        """Irreversibly prevent future launch, then seek proof; never kill a guessed PID."""
        directory, request = self._load(handle)
        _publish(directory, "cancel.json", {"request_hash": digest(request)})
        receipt = self.evidence(handle)
        if receipt is not None:
            return receipt
        fd = _lock(directory)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            receipt = self.evidence(handle)
            if receipt is not None:
                return receipt
            if (directory / "child-intent.json").exists():
                return None  # Guardian may have died with a live child; lock freedom proves nothing.
            receipt = _receipt(request, "not_started")
            _publish(directory, "receipt.json", receipt)
            return receipt  # Future guardians check cancellation while holding this same lock.
        finally:
            os.close(fd)

    def run(self, package_hash, payload, *, limits, channel=None, process_handle=None):
        if process_handle is None:
            raise Conflict("guardian requires a durable prepared identity")
        directory, request = self._load(process_handle)
        if (package_hash != request["package_hash"] or asdict(limits) != request["limits"]
                or digest(base64.b64encode(payload).decode()) != request["input_hash"]):
            raise Conflict("guardian dispatch differs from preparation")
        if (directory / "cancel.json").exists():
            raise Conflict("guardian launch was irreversibly cancelled")
        _publish(directory, "launch.json", {"request_hash": digest(request)}, exclusive=True)
        parent, child = socket.socketpair()
        proc = None
        peer = Peer(parent, self.guardian_limits)
        try:
            command = "import sys;sys.path.insert(0,sys.argv.pop(1));from advisor.research.experiments.guardian import guardian_main;guardian_main()"
            proc = subprocess.Popen([sys.executable, "-I", "-c", command, str(Path(__file__).resolve().parents[3]),
                                     str(child.fileno()), str(directory)], pass_fds=(child.fileno(),), close_fds=True,
                                    start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env={"LANG": "C"})
            child.close()
            peer.send({"op": "start", "payload": base64.b64encode(payload).decode(), "interactive": channel is not None})
            result = None
            while result is None:
                peer.send({"op": "heartbeat"})
                if channel is not None:
                    if channel.failure:
                        peer.send({"op": "abort", "reason": channel.failure})
                    response = channel.response()
                    if response is not None:
                        peer.send({"op": "response", "data": base64.b64encode(response).decode()})
                for message in peer.poll():
                    op = message.get("op")
                    if op == "result":
                        data = message["value"]
                        data["stdout"] = base64.b64decode(data["stdout"], validate=True)
                        data["stderr"] = base64.b64decode(data["stderr"], validate=True)
                        result = ProcessResult(**data)
                    elif channel is not None and op == "stdout":
                        channel.receive(base64.b64decode(message["data"], validate=True))
                    elif channel is not None and op == "sent":
                        channel.response_sent()
                    elif channel is not None and op == "eof":
                        channel.eof()
                    elif channel is not None and op == "aborted":
                        channel.abort(message["reason"])
                    else:
                        raise PeerFailure("guardian_protocol_error")
                if result is None:
                    time.sleep(limits.poll_seconds)
            receipt = self.evidence(process_handle)
            if receipt is None or receipt["state"] != "reaped" or receipt["result"] != result.audit():
                raise Conflict("guardian wire result lacks matching durable receipt")
            return result
        finally:
            parent.close()
            child.close()
            if proc is not None:
                # Closing the private socket triggers guardian cleanup. Never kill
                # the guardian while it may still be responsible for a live child.
                proc.wait()


class GuardianChannel:
    def __init__(self, peer, directory, limits):
        self.peer, self.directory, self.limits = peer, directory, limits
        self.last_owner = time.monotonic()
        self._failure = None
        self._response = None

    def pump(self):
        if self._failure:
            return []
        try:
            messages = self.peer.poll()
            if messages:
                self.last_owner = time.monotonic()
            if (self.directory / "cancel.json").exists():
                self._failure = "owner_cancelled"
            elif time.monotonic() - self.last_owner >= self.limits.owner_timeout_seconds:
                self._failure = "owner_unresponsive"
            for message in messages:
                if message.get("op") == "abort":
                    self._failure = message["reason"]
                elif message.get("op") == "response":
                    if self._response is not None:
                        raise PeerFailure("guardian_response_pipelining")
                    self._response = base64.b64decode(message["data"], validate=True)
                elif message.get("op") not in {"start", "heartbeat"}:
                    raise PeerFailure("guardian_protocol_error")
            return messages
        except (PeerFailure, ValueError):
            self._failure = "owner_disconnected"
            return []

    @property
    def failure(self):
        self.pump()
        return self._failure

    def emit(self, message):
        if self._failure:
            return
        try:
            self.peer.send(message)
        except PeerFailure:
            self._failure = "guardian_ipc_limit"

    def receive(self, data):
        self.emit({"op": "stdout", "data": base64.b64encode(data).decode()})

    def response(self):
        self.pump()
        data, self._response = self._response, None
        return data if not self._failure else None

    def response_sent(self):
        self.emit({"op": "sent"})

    def eof(self):
        self.emit({"op": "eof"})

    def abort(self, reason):
        self.emit({"op": "aborted", "reason": reason})
        self._failure = reason


def guardian_main():
    from advisor.research.artifacts import ArtifactStore
    directory = Path(sys.argv[2])
    request = _read(directory / "request.json")
    limits = ProcessLimits(**request["limits"])
    owner_limits = GuardianLimits(**request["guardian_limits"])
    connection = socket.socket(fileno=int(sys.argv[1]))
    peer = Peer(connection, owner_limits)
    channel = GuardianChannel(peer, directory, owner_limits)
    fd = _lock(directory)
    artifacts = None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (directory / "receipt.json").exists():
            return
        if (directory / "cancel.json").exists():
            _publish(directory, "receipt.json", _receipt(request, "not_started"))
            return
        start = None
        while start is None and not channel._failure:
            for message in channel.pump():
                if message.get("op") == "start":
                    if start is not None:
                        raise PeerFailure("duplicate_guardian_start")
                    start = message
            if start is None:
                time.sleep(limits.poll_seconds)
        if channel._failure:
            _publish(directory, "receipt.json", _receipt(request, "not_started"))
            return
        payload = base64.b64decode(start["payload"], validate=True)
        if digest(base64.b64encode(payload).decode()) != request["input_hash"] or len(payload) > limits.input_bytes:
            raise Conflict("guardian start payload differs")
        runtime = request["runtime"]
        runtime = DarwinPythonRuntime(Path(runtime["executable"]), Path(runtime["stdlib"]),
                                      tuple(Path(p) for p in runtime["native_files"]))
        artifacts = ArtifactStore(Path(request["artifacts_root"]))
        runner = DarwinCandidateRunner(artifacts=artifacts, runtime=runtime)
        _publish(directory, "child-intent.json", {"request_hash": digest(request)}, exclusive=True)
        # Cancellation is polled by the monitor even for noninteractive programs.
        result = runner.run(request["package_hash"], payload, limits=limits,
                            channel=channel if start["interactive"] else None,
                            cancelled=lambda: bool(channel.failure),
                            on_spawn=lambda pid: _publish(directory, "child-started.json",
                                {"request_hash": digest(request), "pid": pid, "guardian_pid": os.getpid()}, exclusive=True))
        _publish(directory, "receipt.json", _receipt(request, "reaped", result.audit()))
        wire = asdict(result)
        wire["stdout"] = base64.b64encode(result.stdout).decode()
        wire["stderr"] = base64.b64encode(result.stderr).decode()
        try:
            peer.send({"op": "result", "value": wire})
            until = time.monotonic() + owner_limits.owner_timeout_seconds
            while peer.outgoing and time.monotonic() < until:
                peer.poll()
                time.sleep(limits.poll_seconds)
        except PeerFailure:
            pass  # Durable receipt is authoritative even when the worker is gone.
    except BaseException as error:
        _publish(directory, "failure.json", {"request_hash": digest(request), "error_type": type(error).__name__})
        raise
    finally:
        if artifacts is not None:
            artifacts.close()
        os.close(fd)
        connection.close()
