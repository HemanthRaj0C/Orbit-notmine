"""
tests/test_collector.py
───────────────────────
Acceptance tests for the collector layer.

Tests are designed to be hermetic — they don't depend on real battery/PSI
hardware being present and degrade gracefully when running in a VM or CI.

Run with:  python tests/test_collector.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector.proc_reader import (
    snapshot_processes,
    get_system_cpu_pct,
    get_system_memory,
    is_user_active,
    get_idle_ms,
)
from collector.psi_reader import (
    read_cpu_pressure,
    read_memory_pressure,
    read_all_pressure,
    psi_available,
    check_cgroup_v2,
)
from collector.monitor import (
    detect_battery_path,
    detect_network_interface,
    read_battery_pct,
    read_net_bytes,
)


# ─────────────────────────────────────────────────────────────────────────────
# proc_reader tests
# ─────────────────────────────────────────────────────────────────────────────

def test_snapshot_returns_list():
    """snapshot_processes() must return a list (even if empty)."""
    result = snapshot_processes()
    assert isinstance(result, list), f"Expected list, got {type(result)}"
    print(f"  ✓ snapshot_processes() returned {len(result)} processes")


def test_snapshot_row_schema():
    """Every row must have the required keys."""
    required_keys = {
        "pid", "name", "status", "cpu_pct",
        "num_threads", "io_read_bytes", "io_write_bytes",
        "net_bytes", "is_foreground", "event_type",
    }
    # First call — cpu_percent may return 0 for all; that's fine
    time.sleep(0.1)   # allow psutil to warm up
    procs = snapshot_processes()
    for row in procs:
        missing = required_keys - set(row.keys())
        assert not missing, f"Row missing keys: {missing} — row: {row}"
    print(f"  ✓ All {len(procs)} process rows have required schema keys")


def test_snapshot_cpu_pct_type():
    """cpu_pct must be a float >= 0."""
    procs = snapshot_processes()
    for row in procs:
        assert isinstance(row["cpu_pct"], float), \
            f"cpu_pct is not float: {row['cpu_pct']!r}"
        assert row["cpu_pct"] >= 0, f"Negative cpu_pct: {row['cpu_pct']}"
    print(f"  ✓ All cpu_pct values are non-negative floats")


def test_snapshot_event_type_values():
    """event_type must be one of the allowed values."""
    allowed = {"foreground", "idle", "sync", "network", "wake"}
    procs = snapshot_processes()
    for row in procs:
        assert row["event_type"] in allowed, \
            f"Unknown event_type: {row['event_type']!r}"
    print(f"  ✓ All event_type values are valid")


def test_system_cpu_pct():
    """System CPU % should be a float between 0 and 100."""
    # Warm up
    get_system_cpu_pct()
    time.sleep(0.2)
    pct = get_system_cpu_pct()
    assert isinstance(pct, float), f"Expected float, got {type(pct)}"
    assert 0.0 <= pct <= 100.0, f"CPU % out of range: {pct}"
    print(f"  ✓ System CPU: {pct:.1f}%")


def test_system_memory():
    """Memory stats should have expected keys and sane values."""
    mem = get_system_memory()
    assert "total" in mem and "available" in mem and "used" in mem
    assert mem["total"] > 0
    assert 0 <= mem["used"] <= mem["total"]
    print(f"  ✓ System RAM: {mem['total']//1024//1024} MB total, "
          f"{mem['available']//1024//1024} MB available")


def test_idle_detection_returns_int():
    """get_idle_ms() must return a non-negative integer."""
    idle_ms = get_idle_ms()
    assert isinstance(idle_ms, int), f"Expected int, got {type(idle_ms)}"
    assert idle_ms >= 0, f"Negative idle_ms: {idle_ms}"
    print(f"  ✓ Idle time: {idle_ms}ms  (is_active={is_user_active()})")


# ─────────────────────────────────────────────────────────────────────────────
# psi_reader tests
# ─────────────────────────────────────────────────────────────────────────────

def test_psi_available_check():
    """psi_available() must return a bool without crashing."""
    result = psi_available()
    assert isinstance(result, bool)
    print(f"  ✓ PSI available: {result}")


def test_cgroup_v2_check():
    """check_cgroup_v2() must return a bool without crashing."""
    result = check_cgroup_v2()
    assert isinstance(result, bool)
    print(f"  ✓ cgroup v2 detected: {result}")


def test_psi_returns_none_or_dict():
    """PSI readers must return None or a dict with avg10/avg60/avg300."""
    cpu_psi = read_cpu_pressure()
    if cpu_psi is None:
        print("  ✓ PSI not available on this system (graceful None returned)")
        return
    assert isinstance(cpu_psi, dict), f"Expected dict or None, got {type(cpu_psi)}"
    for key in ("avg10", "avg60", "avg300"):
        assert key in cpu_psi, f"Missing key '{key}' in PSI data: {cpu_psi}"
        assert isinstance(cpu_psi[key], float)
    print(f"  ✓ CPU PSI: avg10={cpu_psi['avg10']:.2f}  avg60={cpu_psi['avg60']:.2f}")


def test_psi_all_keys():
    """read_all_pressure() must return dict with cpu/memory/io keys."""
    result = read_all_pressure()
    assert "cpu" in result
    assert "memory" in result
    assert "io" in result
    print(f"  ✓ read_all_pressure() has correct keys")


# ─────────────────────────────────────────────────────────────────────────────
# monitor helpers tests
# ─────────────────────────────────────────────────────────────────────────────

def test_detect_battery_path():
    """Battery detection should return Path or None (never crash)."""
    result = detect_battery_path()
    if result is None:
        print("  ✓ No battery detected (desktop/VM — expected)")
    else:
        assert isinstance(result, Path)
        assert result.exists()
        cap = result / "capacity"
        assert cap.exists(), f"capacity file missing in {result}"
        pct = read_battery_pct(result)
        assert pct is not None
        assert 0.0 <= pct <= 100.0
        print(f"  ✓ Battery found: {result.name}  capacity={pct:.1f}%")


def test_detect_network_interface():
    """Network interface detection should return string or None."""
    result = detect_network_interface()
    if result is None:
        print("  ✓ No active network interface detected")
    else:
        assert isinstance(result, str)
        assert len(result) > 0
        print(f"  ✓ Network interface: {result}")


def test_read_net_bytes():
    """read_net_bytes() should return a tuple of two non-negative ints."""
    iface = detect_network_interface()
    if iface is None:
        print("  ✓ Skipped (no interface)")
        return
    rx, tx = read_net_bytes(iface)
    assert isinstance(rx, int) and rx >= 0
    assert isinstance(tx, int) and tx >= 0
    print(f"  ✓ {iface}: rx={rx:,} bytes  tx={tx:,} bytes")


def test_net_bytes_increase_over_time():
    """Rx+Tx bytes should not decrease between reads (counters are cumulative)."""
    iface = detect_network_interface()
    if iface is None:
        print("  ✓ Skipped (no interface)")
        return
    rx1, tx1 = read_net_bytes(iface)
    time.sleep(0.5)
    rx2, tx2 = read_net_bytes(iface)
    assert rx2 >= rx1, f"rx decreased: {rx1} → {rx2}"
    assert tx2 >= tx1, f"tx decreased: {tx1} → {tx2}"
    print(f"  ✓ Net counters monotonically increasing  Δrx={rx2-rx1}  Δtx={tx2-tx1}")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        # proc_reader
        test_snapshot_returns_list,
        test_snapshot_row_schema,
        test_snapshot_cpu_pct_type,
        test_snapshot_event_type_values,
        test_system_cpu_pct,
        test_system_memory,
        test_idle_detection_returns_int,
        # psi_reader
        test_psi_available_check,
        test_cgroup_v2_check,
        test_psi_returns_none_or_dict,
        test_psi_all_keys,
        # monitor helpers
        test_detect_battery_path,
        test_detect_network_interface,
        test_read_net_bytes,
        test_net_bytes_increase_over_time,
    ]

    print("\n═══════════════════════════════════════════")
    print("  PowerLayer — Collector Tests")
    print("═══════════════════════════════════════════\n")

    passed = failed = 0
    for test in tests:
        print(f"▶ {test.__name__}")
        try:
            test()
            passed += 1
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")
            import traceback; traceback.print_exc()
            failed += 1
        print()

    print("═══════════════════════════════════════════")
    print(f"  Results: {passed} passed, {failed} failed")
    print("═══════════════════════════════════════════\n")
    sys.exit(0 if failed == 0 else 1)
