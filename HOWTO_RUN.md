# PowerLayer — How to Run Everything

All commands are executed from the `powerlayer` root folder:
```bash
cd /home/hemanth/Ccp_CSBS/powerlayer
```

---

## Command Reference

PowerLayer provides a unified wrapper script (`./run_local.sh`) to run, test, and benchmark the system without modifying your system settings.

| Command | Action | Database Used |
| :--- | :--- | :--- |
| **`./run_local.sh test`** | Run the complete 111 unit test suite | In-Memory / Temporary |
| **`./run_local.sh e2e`** | Run quick 6-step model/pipeline test | `sandbox.db` (auto-seeds if missing) |
| **`./run_local.sh start`** | Start background daemon & tray (real system data) | `sandbox.db` (real-time data) |
| **`./run_local.sh start --demo`** | Launch simulated TUI dashboard (mentor demo) | `demo.db` (auto-seeds simulated events) |
| **`./run_local.sh stop`** | Stop background daemon & tray services | N/A |
| **`./run_local.sh restart`** | Restart background daemon & tray services | N/A |
| **`./run_local.sh benchmark`** | Run A/B battery savings benchmark (10m per phase) | `sandbox.db` (local) or `powerlayer.db` (prod) |
| **`./install.sh`** | Run cross-distro production systemd installer | `powerlayer.db` (system-wide) |

---

## 1. Running Unit & Integration Tests
Before testing the daemon, ensure all components (storage, collector, RF model, policy engine, cgroup/tc enforcers) are running correctly:
```bash
./run_local.sh test
```
* **Expected Output:** `Results: 111 passed, 0 failed`

---

## 2. Local Testing with Real Data
To test how PowerLayer throttles background applications using your actual system processes and real-time battery levels:

1. **Start the local daemon and tray**:
To test with real-time data
   ```bash
   ./run_local.sh start
   ```
   * *This launches the daemon in background and writes logs to `data/runtime/sandbox.log`.*
2. **Interact with the CLI**:
   * Show live tracking status: `./run_local.sh status`
   * Check battery savings report: `./run_local.sh report`
   * Add a case-insensitive override to force-throttle an app (e.g. Discord):
     ```bash
     ./run_local.sh override Discord --always-throttle
     ```
   * Explain current decision log for Discord: `./run_local.sh explain Discord`
3. **Stop background services**:
   ```bash
   ./run_local.sh stop
   ```

---

## 3. Mentor Demonstration (Simulated Dashboard)
To present the visual terminal visualizer to a mentor using simulated, fast-forwarded events:
```bash
./run_local.sh start --demo
```
* **What it does**: 
  1. Auto-seeds `demo.db` with realistic application events.
  2. Launches the fast-forwarded simulation visualizer (`tools/demo_live.py`).
  3. Displays CPU usage, battery drains, dynamic cgroup buffer flushes, and active ML-based throttling decisions in real-time.
* **To check demo statistics on the CLI**:
  * `./run_local.sh demo-status`
  * `./run_local.sh demo-report`
  * `./run_local.sh demo-explain dropbox`

---

## 4. Run the Battery Savings Benchmark (Phases A/B)
To verify the exact battery savings (% per hour and runtime extension) of your computer with and without PowerLayer:
```bash
# Run 10-minute baseline (PowerLayer OFF) + 10-minute active (PowerLayer ON)
./run_local.sh benchmark

# For a quicker test (5 minutes per phase)
./run_local.sh benchmark --duration 5
```
* *Unplug your laptop charger before running.*
* *It automatically detects if you are running locally or in production systemd, starting/stopping the correct daemon, and writes a markdown report comparing before vs after to `data/runtime/evaluation_report.md`.*

---

## 5. Production Service Installation (Boot Auto-Start)
To deploy PowerLayer globally on your machine as a system-wide service:
```bash
chmod +x install.sh
./install.sh
```
* **What it does**: 
  1. Installs package dependencies and registers `powerlayer.service` (daemon) + `powerlayer-tray.service` (tray indicator) in systemd.
  2. Sets up passwordless `sudo` rules for `tc` network shaping.
  3. Registers a global `/usr/local/bin/powerlayer` command link.
* **Production Commands**: Once installed, you can use the global CLI command directly:
  * `powerlayer status`
  * `powerlayer report`
  * `powerlayer explain <app>`
  * `powerlayer override <app> --always-allow`
  * `powerlayer benchmark`
