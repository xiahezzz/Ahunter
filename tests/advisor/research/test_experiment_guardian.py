from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from advisor.research.experiments.guardian import GuardedCandidateRunner, GuardianLimits, PeerFailure, _publish, _read
from advisor.research.experiments.phase_process import PhaseCandidateProcesses, CandidateProcessRecovery
from advisor.research.experiments.clock import PhaseClock
from advisor.research.experiments.records import Fenced
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.resolution import digest, encode
from tests.advisor.research.test_experiment_isolation import runner, runtime, limits
from tests.advisor.research.test_experiment_phase_process import PRELUDE, channel_limits
from tests.advisor.research.test_experiment_gateway import gateway
from tests.advisor.research.test_experiment_orders import setup, registry, bundle


def guarded(artifacts, runtime, root):
    return GuardedCandidateRunner(artifacts=artifacts, runtime=runtime, evidence_root=root,
        guardian_limits=GuardianLimits(ipc_frame_bytes=131072, ipc_pending_bytes=262144, owner_timeout_seconds=2))


def until(check, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("specific guardian evidence did not arrive before deadline")


def test_real_guardian_returns_matching_durable_receipt_and_no_raw_output(runner, limits, tmp_path):
    host, seal, _ = runner
    api = guarded(host.artifacts, host.runtime, tmp_path / "guardians")
    package = seal("print('candidate-output')")
    handle = api.prepare(digest("normal"), package, b"", limits=limits)
    result = api.run(package, b"", limits=limits, process_handle=handle)
    assert result.returncode == 0 and result.stdout == b"candidate-output\n"
    receipt = api.evidence(handle)
    assert receipt["result"] == result.audit()
    assert "candidate-output" not in encode(receipt)
    assert api.reconcile(handle) == receipt
    with pytest.raises(Conflict):
        api.run(package, b"", limits=limits, process_handle=handle)


def test_recovery_tombstone_prevents_delayed_launch_and_does_not_guess_from_pid(runner, limits, tmp_path):
    host, seal, _ = runner
    api = guarded(host.artifacts, host.runtime, tmp_path / "guardians")
    package = seal("raise RuntimeError('must never start')")
    handle = api.prepare(digest("not-started"), package, b"", limits=limits)
    assert api.reconcile(handle)["state"] == "not_started"
    with pytest.raises(Conflict, match="cancelled"):
        api.run(package, b"", limits=limits, process_handle=handle)
    other = api.prepare(digest("unknown"), package, b"", limits=limits)
    directory, request = api._load(other)
    _publish(directory, "child-intent.json", {"request_hash": digest(request)})
    assert api.reconcile(other) is None
    assert not (directory / "receipt.json").exists()


def test_receipts_and_handles_cannot_be_rebound_or_contradict_launch(runner, limits, tmp_path):
    host, seal, _ = runner
    api = guarded(host.artifacts, host.runtime, tmp_path / "guardians")
    package = seal("pass")
    handle = api.prepare(digest("binding"), package, b"", limits=limits)
    with pytest.raises(Conflict, match="binding"):
        api.reconcile({**handle, "nonce": "another"})
    receipt = api.reconcile(handle)
    directory, request = api._load(handle)
    _publish(directory, "child-intent.json", {"request_hash": digest(request)})
    with pytest.raises(Conflict, match="contradicts"):
        api.evidence(handle)
    assert receipt["state"] == "not_started"


def test_recovery_fences_a_worker_paused_between_launch_claim_and_popen(runner, limits, tmp_path, monkeypatch):
    host, seal, _ = runner
    api = guarded(host.artifacts, host.runtime, tmp_path / "guardians")
    package = seal("raise RuntimeError('late worker must not execute')")
    handle = api.prepare(digest("delayed-launch"), package, b"", limits=limits)
    original = subprocess.Popen
    def delayed(*args, **kwargs):
        assert api.reconcile(handle)["state"] == "not_started"
        return original(*args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", delayed)
    with pytest.raises(PeerFailure):
        api.run(package, b"", limits=limits, process_handle=handle)
    assert not (api.evidence_root / handle["identity"] / "child-intent.json").exists()
    assert api.evidence(handle)["state"] == "not_started"


def test_candidate_cannot_access_guardian_journal_or_inherited_ipc(runner, limits, tmp_path):
    host, seal, _ = runner
    api = guarded(host.artifacts, host.runtime, tmp_path / "guardians")
    identity = digest("guardian-private-boundary")
    path = api.evidence_root / identity / "request.json"
    package = seal(f'''import os,pathlib,signal
denied=[]
for operation in [lambda:pathlib.Path({str(path)!r}).read_text(),
                  lambda:pathlib.Path({str(path)!r}).write_text('forged'),
                  lambda:os.kill(os.getppid(),signal.SIGUSR1)]:
    try:
        operation()
        denied.append(False)
    except OSError:
        denied.append(True)
assert all(denied)
for fd in range(3,32):
    try:
        os.fstat(fd)
    except OSError:
        continue
    raise AssertionError('inherited guardian descriptor')
print('isolated')
''')
    handle = api.prepare(identity, package, b"", limits=limits)
    result = api.run(package, b"", limits=limits, process_handle=handle)
    assert result.returncode == 0 and result.stdout == b"isolated\n"
    assert api.evidence(handle)["state"] == "reaped"


def test_corrupted_usage_receipt_is_not_a_cleanup_proof(runner, limits, tmp_path):
    host, seal, _ = runner
    api = guarded(host.artifacts, host.runtime, tmp_path / "guardians")
    package = seal("pass")
    handle = api.prepare(digest("corrupt-receipt"), package, b"", limits=limits)
    api.run(package, b"", limits=limits, process_handle=handle)
    receipt = api.evidence(handle)
    receipt["result"]["user_cpu_seconds"] = -1
    (api.evidence_root / handle["identity"] / "receipt.json").write_text(encode(receipt))
    with pytest.raises(Conflict, match="resource evidence"):
        api.reconcile(handle)


@pytest.mark.parametrize("registry", [PRELUDE + "assert call('observe',{},'observe')['status']=='available'\n"], indirect=True)
def test_guardian_transports_real_phase_tools_and_pins_receipt_artifact(setup, runtime, limits, tmp_path):
    client = gateway(setup)
    api = guarded(client.records.artifacts, runtime, tmp_path / "guardians")
    result = PhaseCandidateProcesses(client, api).run("guarded", limits=limits, channel_limits=channel_limits())
    assert result["status"] == "reaped" and result["returncode"] == 0, result
    assert client.records.artifacts.read_json(result["guardian_receipt_hash"])["result"]["pid"] == result["pid"]
    client.clock.begin_close("completed", action_id="closing")
    client.clock.finish_close(action_id="closed")


@pytest.mark.parametrize("registry", ["pass"], indirect=True)
@pytest.mark.parametrize("failure_point", ["before_event", "after_commit"])
def test_new_lease_resolves_uncertain_unstarted_claim_atomically(setup, runtime, limits, tmp_path, failure_point):
    client = gateway(setup)
    api = guarded(client.records.artifacts, runtime, tmp_path / "guardians")
    host = PhaseCandidateProcesses(client, api)
    def fault(point):
        if point == "after_commit":
            raise KeyboardInterrupt("claim response lost")
    client.records.fault = fault
    with pytest.raises(KeyboardInterrupt):
        host.run("uncertain", limits=limits, channel_limits=channel_limits())
    client.records.fault = lambda _: None
    client.clock.begin_close("completed", action_id="closing")
    identity = next(iter(client.records.projection(client.session.scope.test_id, "candidate_processes")["value"]["calls"]))
    setup[0][1][0] += timedelta(seconds=10001)
    lease = client.records.claim(client.session.scope.test_id, worker_id="replacement", lease_seconds=10000)
    recovery = CandidateProcessRecovery(client.records, lease, api)
    def fail_recovery(point):
        if point == failure_point:
            raise KeyboardInterrupt("recovery transaction interrupted")
    client.records.fault = fail_recovery
    with pytest.raises(KeyboardInterrupt):
        recovery.reconcile(identity)
    client.records.fault = lambda _: None
    persisted = client.records.projection(client.session.scope.test_id, "candidate_processes")["value"]["calls"][identity]
    assert persisted["quiescent"] == (failure_point == "after_commit")
    result = recovery.reconcile(identity)
    assert result["status"] == "not_started" and result["quiescent"]
    assert recovery.reconcile(identity) == result
    with pytest.raises(Fenced):
        CandidateProcessRecovery(client.records, client.clock.lease, api).reconcile(identity)
    PhaseClock(client.records, lease).finish_close(action_id="recovered")


WORKER = '''
import sys,json,sqlite3
from pathlib import Path
from datetime import datetime
sys.path.insert(0,sys.argv[1])
from advisor.research.artifacts import ArtifactStore
from advisor.research.experiments.records import ExperimentRecords,Lease
from advisor.research.experiments.clock import PhaseClock
from advisor.research.experiments.account import SimulatedAccount
from advisor.research.experiments.orders import StagePlans
from advisor.research.experiments.gateway import CandidateGateway
from advisor.research.experiments.guardian import GuardedCandidateRunner,GuardianLimits
from advisor.research.experiments.isolation import DarwinPythonRuntime,ProcessLimits
from advisor.research.experiments.phase_process import PhaseCandidateProcesses
from advisor.research.experiments.channel import ChannelLimits
from tests.advisor.research.test_experiment_contracts import SESSIONS
spec=json.loads(Path(sys.argv[2]).read_text())
artifacts=ArtifactStore(Path(spec['artifacts']))
records=ExperimentRecords(sqlite3.connect(spec['database']),artifacts=artifacts,clock=lambda:datetime.fromisoformat(spec['now']))
lease=Lease(**spec['lease'])
clock=PhaseClock(records,lease)
account=SimulatedAccount(records,lease,calendar_sessions=SESSIONS,calendar_hash=clock.sealed.tasks[0].calendar_hash)
plans=StagePlans(account,clock,main_actor_id='main')
gateway=CandidateGateway(clock.session('main'),clock,account,plans,products=(),plan_context=lambda *_:({},[]))
runner=GuardedCandidateRunner(artifacts=artifacts,runtime=DarwinPythonRuntime.discover(),evidence_root=Path(spec['evidence']),guardian_limits=GuardianLimits(**spec['guardian_limits']))
PhaseCandidateProcesses(gateway,runner).run('crash-worker',limits=ProcessLimits(**spec['limits']),channel_limits=ChannelLimits(**spec['channel_limits']))
'''


@pytest.mark.parametrize("registry", [PRELUDE + "import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ncall('observe',{},'observed')\nwhile True: time.sleep(0.01)\n"], indirect=True)
def test_killed_real_worker_leaves_guardian_to_reap_and_new_owner_recovers(setup, runtime, limits, tmp_path):
    client = gateway(setup)
    api = guarded(client.records.artifacts, runtime, tmp_path / "guardians")
    spec = {"database": str(tmp_path / "state.sqlite"), "artifacts": str(client.records.artifacts.root),
            "evidence": str(api.evidence_root), "now": client.records._now().isoformat(),
            "lease": asdict(client.clock.lease), "limits": asdict(limits),
            "guardian_limits": asdict(api.guardian_limits), "channel_limits": asdict(channel_limits())}
    config = tmp_path / "worker.json"
    config.write_text(json.dumps(spec))
    worker = subprocess.Popen([sys.executable, "-I", "-c", WORKER, str(Path(__file__).resolve().parents[3]), str(config)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, close_fds=True,
        env={"LANG": "C"}, start_new_session=True)
    try:
        def observed():
            assert worker.poll() is None, worker.stderr.read().decode()
            state = client.records.projection(client.session.scope.test_id, "candidate_processes")
            if state:
                identity = next(iter(state["value"]["calls"]))
                directory = api.evidence_root / identity
                if (directory / "child-started.json").exists():
                    return identity, state["value"]["calls"][identity]
        identity, call = until(observed)
        worker.kill()
        worker.wait(timeout=3)
        receipt = until(lambda: api.evidence(call["guardian"]))
        assert receipt["state"] == "reaped" and receipt["quiescent"]
        assert receipt["result"]["pid"] == _read(api.evidence_root / identity / "child-started.json")["pid"]
        client.clock.begin_close("completed", action_id="close-after-crash")
        with pytest.raises(Conflict, match="candidate processes"):
            client.clock.finish_close(action_id="no-db-receipt-yet")
        setup[0][1][0] += timedelta(seconds=10001)
        lease = client.records.claim(client.session.scope.test_id, worker_id="new-worker", lease_seconds=10000)
        result = CandidateProcessRecovery(client.records, lease, api).reconcile(identity)
        assert result["status"] == "reaped" and result["quiescent"]
        PhaseClock(client.records, lease).finish_close(action_id="recovered-after-real-kill")
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=3)
        worker.stderr.close()
