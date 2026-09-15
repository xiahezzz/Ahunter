from __future__ import annotations

import stat
from pathlib import Path

import pytest

from advisor.mx import chrome_launcher as launcher_module
from advisor.mx.chrome_launcher import ChromeLaunchError, DedicatedChromeLauncher


def _executable(tmp_path: Path) -> Path:
    executable = tmp_path / "Google Chrome"
    executable.write_bytes(b"fixture")
    executable.chmod(0o700)
    return executable


def test_start_is_a_noop_when_the_dedicated_chrome_is_already_ready(tmp_path: Path):
    launched: list[tuple[str, ...]] = []
    launcher = DedicatedChromeLauncher(
        chrome_executable=tmp_path / "missing",
        profile_directory=tmp_path / "profile",
        endpoint_probe=lambda: launcher_module._EndpointState.READY,
        process_launcher=launched.append,
    )

    result = launcher.start()

    assert result.changed is False
    assert result.ready is True
    assert launched == []


def test_start_uses_the_fixed_isolated_arguments_without_a_page_url(tmp_path: Path):
    executable = _executable(tmp_path)
    profile = tmp_path / "profile"
    launched: list[tuple[str, ...]] = []
    states = iter([
        launcher_module._EndpointState.ABSENT,
        launcher_module._EndpointState.ABSENT,
        launcher_module._EndpointState.READY,
    ])
    launcher = DedicatedChromeLauncher(
        chrome_executable=executable,
        profile_directory=profile,
        endpoint_probe=lambda: next(states),
        process_launcher=launched.append,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
        startup_timeout_seconds=1.0,
    )

    result = launcher.start()

    assert result.changed is True
    assert result.ready is True
    assert launched == [(
        str(executable),
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=9333",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    )]
    assert all(not argument.startswith(("http://", "https://")) for argument in launched[0])
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700


def test_start_rejects_an_occupied_non_chrome_endpoint_before_launch(tmp_path: Path):
    launched: list[tuple[str, ...]] = []
    launcher = DedicatedChromeLauncher(
        chrome_executable=_executable(tmp_path),
        profile_directory=tmp_path / "profile",
        endpoint_probe=lambda: launcher_module._EndpointState.OCCUPIED,
        process_launcher=launched.append,
    )

    with pytest.raises(ChromeLaunchError) as caught:
        launcher.start()

    assert caught.value.code == "port_in_use"
    assert str(caught.value) == "port_in_use"
    assert launched == []


def test_start_rejects_a_symlink_profile_without_exposing_its_path(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    profile = tmp_path / "profile"
    profile.symlink_to(target, target_is_directory=True)
    launcher = DedicatedChromeLauncher(
        chrome_executable=_executable(tmp_path),
        profile_directory=profile,
        endpoint_probe=lambda: launcher_module._EndpointState.ABSENT,
        process_launcher=lambda _arguments: None,
    )

    with pytest.raises(ChromeLaunchError) as caught:
        launcher.start()

    assert caught.value.code == "profile_unavailable"
    assert str(profile) not in str(caught.value)


def test_start_returns_pending_after_a_bounded_readiness_wait(tmp_path: Path):
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    launcher = DedicatedChromeLauncher(
        chrome_executable=_executable(tmp_path),
        profile_directory=tmp_path / "profile",
        endpoint_probe=lambda: launcher_module._EndpointState.ABSENT,
        process_launcher=lambda _arguments: None,
        monotonic=lambda: now[0],
        sleep=sleep,
        startup_timeout_seconds=0.5,
        poll_interval_seconds=0.2,
    )

    result = launcher.start()

    assert result.changed is True
    assert result.ready is False
    assert now[0] == pytest.approx(0.5)
