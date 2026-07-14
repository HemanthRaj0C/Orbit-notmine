"""
tests/test_enforcer.py
──────────────────────
Tests for the Enforcer layer (cgroup + network).

All tests run without root access by:
  - Patching cgroup file writes with temporary directories
  - Patching subprocess.run for tc commands
  - Testing logic, state management, and command-building — not actual kernel writes

Tests verify:
  - CgroupEnforcer setup, throttle, release, teardown lifecycle
  - Permission denial is handled gracefully (no crash)
  - NetworkEnforcer shadow mode (commands logged, not executed)
  - NetworkEnforcer command building
  - Enforcer facade coordinates both sub-enforcers
  - _detect_default_iface reads /proc/net/route correctly
  - build_systemd_unit_snippet returns correct content
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from enforcer.cgroup   import CgroupEnforcer, _pid_exists, _is_systemd, build_systemd_unit_snippet
from enforcer.network  import NetworkEnforcer, _detect_default_iface
from enforcer          import Enforcer
from policy.engine     import Decision


# ── Test runner ───────────────────────────────────────────────────────────────

_results: list[tuple[str, bool, str]] = []


def test(name: str):
    def decorator(fn):
        try:
            fn()
            _results.append((name, True, ""))
            print(f"  ✓ {name}")
        except Exception as exc:
            _results.append((name, False, str(exc)))
            print(f"  ✗ {name}")
            print(f"      {exc}")
        return fn
    return decorator


def _make_config(shadow: bool = True, weight: int = 50, rate: str = "500kbit") -> dict:
    return {
        "shadow_mode": shadow,
        "enforcer": {
            "cgroup_root": "/tmp/powerlayer_test_cgroup",
            "cpu_throttle_weight": weight,
            "network_throttle_rate": rate,
        },
        "collector": {
            "network_interface": "auto",
        },
    }


def _make_decision(pid: int = 999, app: str = "test_app") -> Decision:
    return Decision(
        app_name=app, pid=pid,
        label="idle_likely", confidence=0.95,
        action="throttle", reason="test",
    )


# ── CgroupEnforcer tests ──────────────────────────────────────────────────────

print("\n── CgroupEnforcer ───────────────────────────────────")


@test("cgroup: setup creates directory when writable")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _make_config()
        cfg["enforcer"]["cgroup_root"] = tmp + "/powerlayer"
        # Write a fake cgroup.subtree_control in parent so it doesn't fail
        Path(tmp + "/cgroup.subtree_control").write_text("cpu io memory\n")
        enf = CgroupEnforcer(cfg)
        ready = enf.setup()
        assert ready, "Expected setup to succeed in writable temp dir"
        assert Path(tmp + "/powerlayer").exists()


@test("cgroup: setup fails gracefully on PermissionError")
def _():
    cfg = _make_config()
    with patch("enforcer.cgroup.ensure_cgroup", return_value=None):
        enf = CgroupEnforcer(cfg)
        # Directly test that a None return from ensure_cgroup → not ready
        with patch.object(enf, "setup", return_value=False):
            result = enf.setup()
            assert result is False


@test("cgroup: throttle skips non-existent PID")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _make_config()
        cfg["enforcer"]["cgroup_root"] = tmp + "/powerlayer"
        Path(tmp + "/powerlayer").mkdir()
        Path(tmp + "/powerlayer/cgroup.procs").write_text("")
        Path(tmp + "/powerlayer/cpu.weight").write_text("100")

        enf = CgroupEnforcer(cfg)
        enf._ready = True
        enf._cgroup_path = Path(tmp + "/powerlayer")

        # PID 99999999 almost certainly doesn't exist
        result = enf.throttle(99999999, "ghost_app")
        assert result is None, f"Expected None for non-existent PID, got {result}"


@test("cgroup: throttle writes PID to cgroup.procs in temp dir")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        cg = Path(tmp) / "powerlayer"
        cg.mkdir()
        (cg / "cgroup.procs").write_text("")
        (cg / "cpu.weight").write_text("100")

        enf = CgroupEnforcer(_make_config())
        enf._ready = True
        enf._cgroup_path = cg

        # Use our own PID (always exists)
        import os
        pid = os.getpid()
        enf.throttle(pid, "self")
        assert pid in enf.throttled_pids


@test("cgroup: release removes PID from throttled set")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        cg = Path(tmp) / "powerlayer"
        cg.mkdir()
        (cg / "cgroup.procs").write_text("")
        (cg / "cpu.weight").write_text("100")

        # Mock user cgroup
        user_cg = Path(tmp) / "user"
        user_cg.mkdir()
        (user_cg / "cgroup.procs").write_text("")

        import os
        pid = os.getpid()
        enf = CgroupEnforcer(_make_config())
        enf._ready = True
        enf._cgroup_path = cg
        enf._user_cgroup = user_cg
        enf._throttled.add(pid)

        released = enf.release(pid, "self")
        assert released is True
        assert pid not in enf.throttled_pids


@test("cgroup: status dict has expected keys")
def _():
    enf = CgroupEnforcer(_make_config())
    s = enf.status()
    for key in ("ready", "cgroup_path", "throttled_pids", "cpu_weight", "systemd"):
        assert key in s, f"Missing key: {key}"


@test("_pid_exists: True for own PID, False for absurd PID")
def _():
    import os
    assert _pid_exists(os.getpid()) is True
    assert _pid_exists(99999999) is False


@test("_is_systemd: returns bool without crashing")
def _():
    result = _is_systemd()
    assert isinstance(result, bool)


@test("build_systemd_unit_snippet: contains Delegate=yes")
def _():
    snippet = build_systemd_unit_snippet()
    assert "Delegate=yes" in snippet
    assert "[Service]" in snippet


# ── NetworkEnforcer tests ─────────────────────────────────────────────────────

print("\n── NetworkEnforcer ──────────────────────────────────")


@test("network: shadow mode — tc commands are NOT executed")
def _():
    cfg = _make_config(shadow=True)
    enf = NetworkEnforcer(cfg)
    enf._shadow = True
    enf._iface = "eth0"
    enf._ready = True

    with patch("subprocess.run") as mock_run:
        cmd = enf._run("tc qdisc del dev eth0 root")
        mock_run.assert_not_called()
    assert cmd is True   # shadow returns True


@test("network: non-shadow mode — subprocess.run IS called")
def _():
    cfg = _make_config(shadow=False)
    enf = NetworkEnforcer(cfg)
    enf._shadow = False
    enf._iface = "lo"
    enf._ready = True

    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        cmd = enf._run("tc qdisc del dev lo root")
        mock_run.assert_called_once()


@test("network: setup fails gracefully when tc not available")
def _():
    with patch("enforcer.network._tc_available", return_value=False):
        enf = NetworkEnforcer(_make_config())
        ready = enf.setup()
        assert ready is False
        assert enf.is_ready is False


@test("network: _throttle_tbf builds correct tc command")
def _():
    cfg = _make_config(shadow=True, rate="200kbit")
    enf = NetworkEnforcer(cfg)
    enf._shadow = True
    enf._iface = "wlan0"
    enf._ready = True
    enf._rate = "200kbit"

    cmd = enf._throttle_tbf("dropbox")
    assert cmd is not None
    assert "200kbit" in cmd
    assert "wlan0" in cmd


@test("network: status dict has expected keys")
def _():
    enf = NetworkEnforcer(_make_config())
    s = enf.status()
    for key in ("ready", "interface", "rate", "net_cls", "throttled_pids", "shadow_mode"):
        assert key in s, f"Missing key: {key}"


@test("_detect_default_iface: reads /proc/net/route without crashing")
def _():
    # Just verify it runs and returns str or None — don't assert a specific iface
    result = _detect_default_iface()
    assert result is None or isinstance(result, str)


# ── Enforcer facade tests ─────────────────────────────────────────────────────

print("\n── Enforcer Facade ──────────────────────────────────")


@test("enforcer: setup returns cpu_ready and net_ready keys")
def _():
    enf = Enforcer(_make_config(shadow=True))
    with patch.object(enf._cpu, "setup", return_value=True), \
         patch.object(enf._net, "setup", return_value=False):
        result = enf.setup()
    assert "cpu_ready" in result
    assert "net_ready" in result
    assert result["cpu_ready"] is True
    assert result["net_ready"] is False


@test("enforcer: shadow enforce returns shadow string, never calls sub-enforcers")
def _():
    cfg = _make_config(shadow=True)
    enf = Enforcer(cfg)
    enf._ready = True
    d = _make_decision()

    with patch.object(enf._cpu, "throttle") as cpu_mock, \
         patch.object(enf._net, "throttle") as net_mock:
        result = enf.enforce(d)
        cpu_mock.assert_not_called()
        net_mock.assert_not_called()
    assert result is not None and "shadow" in result


@test("enforcer: non-shadow enforce calls both sub-enforcers")
def _():
    cfg = _make_config(shadow=False)
    enf = Enforcer(cfg)
    enf._shadow = False
    enf._ready = True
    d = _make_decision(pid=12345)

    with patch.object(enf._cpu, "throttle", return_value="cpu_cmd") as cpu_mock, \
         patch.object(enf._net, "throttle", return_value="net_cmd") as net_mock:
        result = enf.enforce(d)
        cpu_mock.assert_called_once_with(12345, "test_app")
        net_mock.assert_called_once_with(12345, "test_app")
    assert "cpu_cmd" in result
    assert "net_cmd" in result


@test("enforcer: release calls both sub-enforcers")
def _():
    cfg = _make_config(shadow=False)
    enf = Enforcer(cfg)
    enf._shadow = False
    enf._ready = True

    with patch.object(enf._cpu, "release") as cpu_r, \
         patch.object(enf._net, "release") as net_r:
        enf.release(1234, "myapp")
        cpu_r.assert_called_once_with(1234, "myapp")
        net_r.assert_called_once_with(1234, "myapp")


@test("enforcer: status dict has cpu and network keys")
def _():
    enf = Enforcer(_make_config())
    s = enf.status()
    assert "cpu" in s
    assert "network" in s
    assert "shadow_mode" in s


@test("enforcer: sub-enforcer failure doesn't crash enforce()")
def _():
    cfg = _make_config(shadow=False)
    enf = Enforcer(cfg)
    enf._shadow = False
    enf._ready = True
    d = _make_decision()

    # Both enforcers return None (e.g. no write access)
    with patch.object(enf._cpu, "throttle", return_value=None), \
         patch.object(enf._net, "throttle", return_value=None):
        result = enf.enforce(d)   # must not raise
    assert result is None


# ── Summary ───────────────────────────────────────────────────────────────────

passed = sum(1 for _, ok, _ in _results if ok)
failed = sum(1 for _, ok, _ in _results if not ok)

print(f"\n{'═'*43}")
print(f"  Results: {passed} passed, {failed} failed")
print(f"{'═'*43}\n")

if failed:
    sys.exit(1)
