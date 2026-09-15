"""Experimental local macOS candidate process boundary; never an unsandboxed fallback.

This is a transport/backend, not a PhaseSession authorization or an Episode runner.
Only host-filtered bytes belong on stdin. Output is untrusted and needs gateway/schema
validation. The deprecated Apple backend and missing hard RSS/disk quotas explicitly
prevent formal readiness. No dependencies are installed and no model is invoked.
"""
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable

from .candidates import read_candidate


class IsolationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessLimits:
    wall_seconds: float
    cpu_seconds: int
    cancel_grace_seconds: float
    poll_seconds: float
    input_bytes: int
    output_bytes: int
    file_bytes: int
    scratch_bytes: int
    scratch_entries: int
    open_files: int

    def __post_init__(self):
        for name in ("wall_seconds", "cancel_grace_seconds", "poll_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("cpu_seconds", "input_bytes", "output_bytes", "file_bytes",
                     "scratch_bytes", "scratch_entries", "open_files"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.open_files < 16 or self.poll_seconds > self.wall_seconds:
            raise ValueError("invalid descriptor limit or polling interval")


@dataclass(frozen=True)
class ProcessResult:
    package_hash: str
    profile_hash: str
    pid: int
    returncode: int
    stop_reason: str
    stdout: bytes
    stderr: bytes
    user_cpu_seconds: float
    system_cpu_seconds: float
    peak_rss_bytes: int
    wall_seconds: float
    quiescent: bool = True  # Constructed only after wait4 reaped this exact child.
    formal_ready: bool = False

    def audit(self):
        return {"package_hash": self.package_hash, "profile_hash": self.profile_hash,
                "pid": self.pid, "returncode": self.returncode, "stop_reason": self.stop_reason,
                "stdout_hash": hashlib.sha256(self.stdout).hexdigest(),
                "stderr_hash": hashlib.sha256(self.stderr).hexdigest(),
                "user_cpu_seconds": self.user_cpu_seconds, "system_cpu_seconds": self.system_cpu_seconds,
                "peak_rss_bytes": self.peak_rss_bytes, "wall_seconds": self.wall_seconds,
                "quiescent": self.quiescent, "formal_ready": False}


def _literal(path: Path | str) -> str:
    # JSON escaping is used for SBPL string literals, never shell interpolation.
    return json.dumps(str(path), ensure_ascii=True)


@dataclass(frozen=True)
class DarwinPythonRuntime:
    executable: Path
    stdlib: Path
    native_files: tuple[Path, ...]

    @classmethod
    def discover(cls):
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            raise IsolationUnavailable("darwin_sandbox_unavailable")
        prefix = Path(sys.base_prefix).resolve(strict=True)
        # Framework's bin/python is a launcher that performs additional execs.
        # Pin the actual interpreter so arbitrary executable paths stay denied.
        executable = prefix / "Resources/Python.app/Contents/MacOS/Python"
        stdlib = prefix / f"lib/python{sys.version_info.major}.{sys.version_info.minor}"
        if not executable.is_file() or not (stdlib / "encodings/__init__.py").is_file():
            raise IsolationUnavailable("framework_python_unavailable")
        executable = executable.resolve(strict=True)
        stdlib = stdlib.resolve(strict=True)
        # Resolve the native dependency closure of the interpreter and stdlib
        # extensions. Read individual external libraries, never all /opt/local/lib.
        pending = [executable, *(stdlib / "lib-dynload").glob("*.so")]
        visited = set()
        while pending:
            file = pending.pop().resolve(strict=True)
            if file in visited:
                continue
            visited.add(file)
            result = subprocess.run(["/usr/bin/otool", "-L", str(file)], capture_output=True,
                                    text=True, timeout=10, check=True, close_fds=True,
                                    env={"LANG": "C"})
            for line in result.stdout.splitlines()[1:]:
                name = line.strip().split(" (compatibility version", 1)[0]
                if name.startswith(("/usr/lib/", "/System/Library/")):
                    continue  # dyld shared-cache paths need not exist on disk.
                if not name.startswith("/"):
                    raise IsolationUnavailable("unresolved_native_dependency")
                pending.append(Path(name))
        return cls(executable, stdlib, tuple(sorted(visited)))

    def profile(self, package: Path, scratch: Path) -> str:
        paths = " ".join(f"(literal {_literal(path)})" for path in self.native_files)
        return "\n".join([
            "(version 1)", "(deny default)",
            f"(allow process-exec (literal {_literal(self.executable)}))",
            # dyld needs to read the root directory itself, not its descendants.
            "(allow file-read* (literal \"/\") (subpath \"/System/Library\") (subpath \"/usr/lib\"))",
            f"(allow file-read* {paths} (subpath {_literal(self.stdlib)}) (subpath {_literal(package)}))",
            f"(deny file-read* (subpath {_literal(self.stdlib / 'site-packages')}))",
            f"(allow file-read* file-write* (subpath {_literal(scratch)}))",
            # A hard link could alias an otherwise forbidden inode into scratch.
            "(deny file-link)",
            "(allow file-read* (literal \"/dev/null\") (literal \"/dev/urandom\"))",
            "(allow file-write-data (literal \"/dev/null\"))",
        ])


# Executed before candidate imports, in the already kernel-sandboxed interpreter.
# No user Python code can run through PYTHONPATH, site, startup or inherited env.
_BOOTSTRAP = """
import resource, runpy, sys
cpu, size, files = map(int, sys.argv[1:4])
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
resource.setrlimit(resource.RLIMIT_NOFILE, (files, files))
package, entry = sys.argv[4:6]
sys.path.insert(0, package)
sys.argv = [entry]
runpy.run_path(entry, run_name='__main__')
"""


def _scratch_within_limits(root: Path, limits: ProcessLimits) -> bool:
    total = count = 0
    pending = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        pending.append(os.open(root, flags))
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        count += 1
                        if count > limits.scratch_entries:
                            return False
                        try:
                            item = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
                            if stat.S_ISDIR(item.st_mode):
                                # The candidate can rename/swap entries at any time.
                                # Open relative to an owned descriptor, never follow
                                # a replacement symlink into the host filesystem.
                                pending.append(os.open(entry.name, flags, dir_fd=directory))
                            elif stat.S_ISREG(item.st_mode):
                                total += item.st_size
                                if total > limits.scratch_bytes:
                                    return False
                        except FileNotFoundError:
                            continue
            finally:
                os.close(directory)
        return True
    except OSError:
        return False  # Uninspectable scratch is never presumed under quota.
    finally:
        for directory in pending:
            os.close(directory)


def _cleanup(root: Path):
    # Called only after the process is reaped. Restore private directory permissions
    # without following candidate symlinks before shutil's fd-safe removal.
    for directory, dirs, _ in os.walk(root, topdown=True, followlinks=False):
        os.chmod(directory, 0o700)
        for name in dirs:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chmod(path, 0o700)
    shutil.rmtree(root)


class DarwinCandidateRunner:
    """One bounded local process; callers must separately bind Test/phase/budget.

    Wall, scratch and output controls are host monitoring, with possible overshoot.
    CPU/file-size/descriptor settings are inherited kernel limits. The CPU signal
    is catchable on macOS, so it is not a proven hard compute bound. RSS is measured,
    not hard-limited. Never advertise this backend as formally accepted isolation.
    """

    def __init__(self, *, artifacts, runtime: DarwinPythonRuntime):
        self.artifacts = artifacts
        self.runtime = runtime

    @staticmethod
    def capabilities() -> dict:
        return {"backend": "darwin-sandbox-exec@1", "formal_ready": False,
                "policy_support": "apple_deprecated_third_party_unsupported",
                "dependencies": "stdlib_and_sealed_files_only",
                "runtime_integrity": "host_install_not_sealed",
                "cpu_control": "rlimit_signal_and_host_wall_monitor",
                "rss_control": "measured_at_exit_only",
                "scratch_control": "per_file_rlimit_and_aggregate_host_monitor",
                "phase_and_budget_integration": False,
                "host_crash_recovery": False}

    def run(self, package_hash: str, payload: bytes, *, limits: ProcessLimits,
            cancelled: Callable[[], bool] = lambda: False, channel=None, on_spawn=None) -> ProcessResult:
        if type(payload) is not bytes or len(payload) > limits.input_bytes:
            raise ValueError("candidate input exceeds byte limit")
        package = read_candidate(package_hash, artifacts=self.artifacts)
        root = Path(tempfile.mkdtemp(prefix="ahunter-candidate-")).resolve()
        proc = None
        reaped = False
        try:
            code, scratch = root / "package", root / "scratch"
            code.mkdir(mode=0o700)
            scratch.mkdir(mode=0o700)
            for item in package.files:
                target = code / item.path
                target.parent.mkdir(parents=True, exist_ok=True)
                # Case/Unicode aliases on the host filesystem must not overwrite
                # a different sealed manifest entry during materialization.
                with target.open("xb") as stream:
                    stream.write(self.artifacts.read_bytes(item.content_hash))
                target.chmod(0o444)
            for directory, _, _ in os.walk(code):
                os.chmod(directory, 0o555)
            profile = self.runtime.profile(code, scratch)
            started = time.monotonic()
            proc = subprocess.Popen([
                "/usr/bin/sandbox-exec", "-p", profile, str(self.runtime.executable),
                "-I", "-S", "-B", "-u", "-X", "utf8", "-c", _BOOTSTRAP,
                str(limits.cpu_seconds), str(limits.file_bytes), str(limits.open_files),
                str(code), str(code / package.entrypoint),
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=scratch, env={"LANG": "C", "TMPDIR": str(scratch)}, close_fds=True,
                pass_fds=(), start_new_session=True, bufsize=0)
            if on_spawn is not None:
                on_spawn(proc.pid)
            stdout, stderr = bytearray(), bytearray()
            stop_reason = "exited"
            stopping_at = None
            sent = 0
            sending_reply = False
            killed = False
            with selectors.DefaultSelector() as selector:
                for stream, events, name in ((proc.stdin, selectors.EVENT_WRITE, "stdin"),
                                             (proc.stdout, selectors.EVENT_READ, "stdout"),
                                             (proc.stderr, selectors.EVENT_READ, "stderr")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, events, name)
                while True:
                    pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
                    if pid:
                        reaped = True
                        proc.returncode = os.waitstatus_to_exitcode(status)
                        # No descendants are permitted; remaining pipe data is finite.
                        for stream, target in ((proc.stdout, stdout), (proc.stderr, stderr)):
                            if not stream.closed:
                                while True:
                                    chunk = os.read(stream.fileno(), min(65536, limits.output_bytes + 1))
                                    if not chunk:
                                        break
                                    room = limits.output_bytes - len(stdout) - len(stderr)
                                    target.extend(chunk[:max(0, room)])
                                    if channel is not None and len(chunk) > room:
                                        channel.abort("output_limit")
                                    if channel is not None and stream is proc.stdout:
                                        channel.receive(chunk)
                                    if len(chunk) > room:
                                        stop_reason = "output_limit" if stopping_at is None else stop_reason
                        if stop_reason == "exited" and not _scratch_within_limits(scratch, limits):
                            stop_reason = "scratch_limit"
                        if channel is not None:
                            channel.eof()
                            if channel.failure and stop_reason == "exited":
                                stop_reason = channel.failure
                        break
                    now = time.monotonic()
                    if stopping_at is None:
                        if channel is not None and channel.failure:
                            stop_reason = channel.failure
                        elif cancelled():
                            stop_reason = "cancelled"
                        elif now - started >= limits.wall_seconds:
                            stop_reason = "wall_timeout"
                        elif not _scratch_within_limits(scratch, limits):
                            stop_reason = "scratch_limit"
                        if stop_reason != "exited":
                            stopping_at = now
                            if channel is not None:
                                channel.abort(stop_reason)
                            os.killpg(proc.pid, signal.SIGTERM)
                    if stopping_at is not None and not proc.stdin.closed:
                        try:
                            selector.unregister(proc.stdin)
                        except KeyError:
                            pass
                        proc.stdin.close()
                    if stopping_at is not None and not killed and now - stopping_at >= limits.cancel_grace_seconds:
                        os.killpg(proc.pid, signal.SIGKILL)
                        killed = True
                    if (channel is not None and stopping_at is None and sent == len(payload)
                            and not proc.stdin.closed and proc.stdin.fileno() not in selector.get_map()):
                        reply = channel.response()
                        if reply is not None:
                            payload, sent, sending_reply = reply, 0, True
                            selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
                    for key, _ in selector.select(limits.poll_seconds):
                        stream = key.fileobj
                        if key.data == "stdin":
                            try:
                                sent += os.write(stream.fileno(), payload[sent:sent + 65536])
                            except BrokenPipeError:
                                sent = len(payload)
                            if sent == len(payload):
                                selector.unregister(stream)
                                if channel is None:
                                    stream.close()
                                elif sending_reply:
                                    channel.response_sent()
                                    sending_reply = False
                            continue
                        chunk = os.read(stream.fileno(), min(65536, limits.output_bytes + 1))
                        if not chunk:
                            if channel is not None and key.data == "stdout":
                                channel.eof()
                            selector.unregister(stream)
                            stream.close()
                            continue
                        room = limits.output_bytes - len(stdout) - len(stderr)
                        (stdout if key.data == "stdout" else stderr).extend(chunk[:max(0, room)])
                        if channel is not None and len(chunk) > room:
                            channel.abort("output_limit")
                        if channel is not None and key.data == "stdout":
                            channel.receive(chunk)
                        if len(chunk) > room and stopping_at is None:
                            stop_reason, stopping_at = "output_limit", time.monotonic()
                            os.killpg(proc.pid, signal.SIGTERM)
            return ProcessResult(package_hash, hashlib.sha256(profile.encode()).hexdigest(),
                                 proc.pid, proc.returncode, stop_reason, bytes(stdout), bytes(stderr),
                                 usage.ru_utime, usage.ru_stime, usage.ru_maxrss,
                                 time.monotonic() - started)
        finally:
            if proc is not None:
                if not reaped:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    # Cleanup exceptions/host cancellation cannot leave a live child.
                    _, status, _ = os.wait4(proc.pid, 0)
                    proc.returncode = os.waitstatus_to_exitcode(status)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()
            _cleanup(root)
