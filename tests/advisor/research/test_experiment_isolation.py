from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import socket
import sys

import pytest

from advisor.research.artifacts import ArtifactStore
from advisor.research.experiments.candidates import seal_candidate
from advisor.research.experiments.isolation import (
    DarwinCandidateRunner, DarwinPythonRuntime, IsolationUnavailable, ProcessLimits,
)


@pytest.fixture(scope="module")
def runtime():
    if sys.platform != "darwin":
        pytest.skip("real local Darwin backend requires macOS")
    return DarwinPythonRuntime.discover()


@pytest.fixture
def runner(tmp_path, runtime):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "source"
    source.mkdir()
    for name in ("prompt.md", "requirements.lock", "io.json"):
        (source / name).write_text("fixture")

    def seal(program):
        (source / "main.py").write_text(program)
        package = seal_candidate(source, {
            "source_paths": ["main.py"], "prompt_paths": ["prompt.md"],
            "dependency_lock_paths": ["requirements.lock"], "contract_paths": ["io.json"],
            "entrypoint": "main.py", "input_contract": "phase@1", "output_contract": "actions@1",
            "allowed_config": {},
        }, artifacts=artifacts)
        return package.package_hash

    yield DarwinCandidateRunner(artifacts=artifacts, runtime=runtime), seal, source
    artifacts.close()


@pytest.fixture
def limits():
    return ProcessLimits(wall_seconds=5, cpu_seconds=2, cancel_grace_seconds=0.1,
                         poll_seconds=0.01, input_bytes=4096, output_bytes=8192,
                         file_bytes=16384, scratch_bytes=32768, scratch_entries=100,
                         open_files=32)


def test_runs_only_sealed_bytes_and_returns_reaped_usage(runner, limits):
    host, seal, source = runner
    package = seal("import json,sys,pathlib\n"
                   "data=json.load(sys.stdin)\n"
                   "pathlib.Path('saved').write_text('ok')\n"
                   "print(json.dumps(dict(data=data, cwd=str(pathlib.Path.cwd()))))\n")
    (source / "main.py").write_text("raise RuntimeError('changed source')")
    result = host.run(package, b'{"phase":"initial"}', limits=limits)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["data"] == {"phase": "initial"}
    assert not Path(data["cwd"]).exists()
    assert result.quiescent and not result.formal_ready
    assert result.peak_rss_bytes > 0 and result.user_cpu_seconds > 0
    with pytest.raises(ChildProcessError):
        os.waitpid(result.pid, os.WNOHANG)


def test_denies_outside_reads_writes_links_network_exec_and_env(runner, limits, tmp_path, monkeypatch):
    host, seal, _ = runner
    sentinel = tmp_path / "outside"
    sentinel.write_text("private fixture")
    inherited = os.open(sentinel, os.O_RDONLY)
    os.set_inheritable(inherited, True)
    monkeypatch.setenv("AHUNTER_FIXTURE_SECRET", "not for candidate")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    program = f'''
import json, os, pathlib, socket, subprocess, sys
outside = pathlib.Path({str(sentinel)!r})
denied = {{}}
def check(name, fn):
    try:
        fn()
        denied[name] = False
    except OSError:
        denied[name] = True
check('read', outside.read_bytes)
check('write', lambda: outside.write_text('bad'))
check('package_write', lambda: pathlib.Path(__file__).write_text('bad'))
check('package_chmod', lambda: os.chmod(__file__, 0o777))
check('fd', lambda: os.read({inherited}, 1))
check('root_listing', lambda: list(outside.parent.iterdir()))
pathlib.Path('link').symlink_to(outside)
check('symlink_read', lambda: pathlib.Path('link').read_bytes())
check('symlink_write', lambda: pathlib.Path('link').write_text('bad'))
check('hardlink', lambda: os.link(outside, 'hardlink'))
check('socket', lambda: socket.create_connection(('127.0.0.1', {listener.getsockname()[1]}), 0.1))
check('unix_socket', lambda: socket.socket(socket.AF_UNIX).bind('sock'))
check('shell', lambda: subprocess.run(['/bin/echo', 'escaped'], check=True))
check('python_child', lambda: subprocess.run([sys.executable, '-I', '-S', '-c', 'print(1)'], check=True))
check('fork', os.fork)
denied['env'] = 'AHUNTER_FIXTURE_SECRET' not in os.environ and 'PYTHONPATH' not in os.environ
print(json.dumps(denied))
'''
    try:
        result = host.run(seal(program), b"", limits=limits)
        assert result.returncode == 0, result.stderr
        assert all(json.loads(result.stdout).values()), result.stdout
        assert sentinel.read_text() == "private fixture"
        with pytest.raises(socket.timeout):
            listener.accept()
    finally:
        listener.close()
        os.close(inherited)


def test_wall_timeout_kills_sigterm_ignoring_candidate(runner, limits):
    host, seal, _ = runner
    package = seal("import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(0.01)\n")
    result = host.run(package, b"", limits=replace(limits, wall_seconds=0.3))
    assert result.stop_reason == "wall_timeout"
    assert result.returncode == -signal.SIGKILL
    assert result.wall_seconds < 3 and result.quiescent


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_is_bounded_even_without_newlines(runner, limits, stream):
    host, seal, _ = runner
    package = seal(f"import os\nwhile True: os.write({1 if stream == 'stdout' else 2}, b'x'*4096)\n")
    result = host.run(package, b"", limits=replace(limits, output_bytes=128))
    assert result.stop_reason == "output_limit"
    assert len(result.stdout) + len(result.stderr) == 128
    assert result.quiescent


def test_kernel_cpu_limit_and_file_size_limit(runner, limits):
    host, seal, _ = runner
    result = host.run(seal("while True: pass\n"), b"", limits=replace(limits, cpu_seconds=1))
    assert result.returncode in {-signal.SIGXCPU, -signal.SIGKILL}
    assert result.user_cpu_seconds + result.system_cpu_seconds >= 0.8
    result = host.run(seal("import pathlib\npathlib.Path('large').write_bytes(b'x'*100000)\n"),
                      b"", limits=limits)
    assert result.returncode != 0


def test_ignored_cpu_signal_still_has_wall_deadline(runner, limits):
    host, seal, _ = runner
    result = host.run(seal("import signal\nsignal.signal(signal.SIGXCPU, signal.SIG_IGN)\nwhile True: pass\n"),
                      b"", limits=replace(limits, cpu_seconds=1, wall_seconds=1.4))
    assert result.returncode != 0 and result.quiescent
    assert result.wall_seconds < 3
    assert host.capabilities()["cpu_control"] == "rlimit_signal_and_host_wall_monitor"


def test_scratch_quota_and_uninspectable_directory_fail_closed(runner, limits):
    host, seal, _ = runner
    for program in ["import pathlib\nfor i in range(10): pathlib.Path(str(i)).write_bytes(b'x'*10000)\n",
                    "import os,time\nos.mkdir('hidden',0)\ntime.sleep(1)\n"]:
        result = host.run(seal(program), b"", limits=limits)
        assert result.stop_reason == "scratch_limit", result.stderr
        assert result.quiescent


def test_host_cancel_and_callback_failure_reap_process(runner, limits):
    host, seal, _ = runner
    package = seal("import time\ntime.sleep(20)\n")
    result = host.run(package, b"", limits=limits, cancelled=lambda: True)
    assert result.stop_reason == "cancelled" and result.quiescent

    def broken():
        raise RuntimeError("host callback failed")
    with pytest.raises(RuntimeError, match="host callback failed"):
        host.run(package, b"", limits=limits, cancelled=broken)


def test_bad_artifact_and_unsupported_backend_do_not_run(runner, limits, monkeypatch):
    host, seal, _ = runner
    package = seal("raise RuntimeError('should not execute')")
    with pytest.raises(ValueError, match="byte limit"):
        host.run(package, b"x" * 4097, limits=limits)
    with pytest.raises(FileNotFoundError):
        host.run("0" * 64, b"", limits=limits)
    monkeypatch.setattr(sys, "platform", "unsupported")
    with pytest.raises(IsolationUnavailable, match="unavailable"):
        DarwinPythonRuntime.discover()


@pytest.mark.parametrize("change", [{"wall_seconds": float("nan")}, {"cpu_seconds": True},
                                    {"open_files": 3}, {"poll_seconds": 6}, {"output_bytes": 0}])
def test_limits_have_no_hidden_or_unbounded_values(limits, change):
    with pytest.raises(ValueError):
        replace(limits, **change)
