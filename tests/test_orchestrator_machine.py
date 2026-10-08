"""Static safety contracts for remote orchestration cleanup commands."""

from scripts.orchestrator.config import MachineConfig
from scripts.orchestrator.machine import Machine


def _capture_clean_command(os_name: str) -> str:
    machine = Machine(
        MachineConfig(
            name="fixture",
            ssh_target="fixture",
            os=os_name,
            repo="/repo" if os_name != "windows" else "C:/repo",
        )
    )
    captured = []

    def fake_ssh(command, timeout=None):
        captured.append(command)
        return 0, "ARCHIVED"

    machine.ssh = fake_ssh
    assert machine.clean_autosaves() == "ARCHIVED"
    assert len(captured) == 1
    return captured[0]


def test_windows_cleanup_archives_without_delete_command():
    command = _capture_clean_command("windows")
    assert "_abandoned_mcp" in command
    assert "Move-Item" in command
    assert "del /Q" not in command


def test_linux_cleanup_archives_without_rm_command():
    command = _capture_clean_command("linux")
    assert "_abandoned_mcp" in command
    assert "mv --" in command
    assert "rm -f" not in command


def _capture_telemetry_command(os_name: str) -> str:
    machine = Machine(
        MachineConfig(
            name="fixture",
            ssh_target="fixture",
            os=os_name,
            repo="/repo" if os_name != "windows" else "C:/repo",
        )
    )
    captured = []

    def fake_ssh(command, timeout=None):
        captured.append(command)
        return 0, "ARCHIVED"

    machine.ssh = fake_ssh
    assert machine.clear_local_telemetry() == "ARCHIVED"
    assert len(captured) == 1
    return captured[0]


def test_windows_telemetry_cleanup_archives_without_recursive_delete():
    command = _capture_telemetry_command("windows")
    assert "telemetry_" in command
    assert "Move-Item" in command
    assert "Remove-Item" not in command


def test_linux_telemetry_cleanup_archives_without_recursive_delete():
    command = _capture_telemetry_command("linux")
    assert "telemetry_" in command
    assert "mv --" in command
    assert "rm -rf" not in command
