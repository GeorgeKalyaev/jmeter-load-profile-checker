# JMeter Load Profile Checker

[Русская версия](README.md) · [Short RU pointer](README.ru.md)

Compares **expected load profile** (from JMX/UTG) with **actual metrics** in InfluxDB.

## Core principle

The report evaluates only **clean plateaus**:

- intervals with stable total load;
- ramp-up and ramp-down are excluded from stage comparison;
- deviations are computed only in `[plateau_start_s, plateau_end_s)`.

This is the main design rule of the project.

---

## Quick start (recommended flow)

### 0) Prepare Influx config

Copy `influx_config.example.json` to a local file (for example `influx_config.local.json`) and set:
`influx_url`, `influx_db`, `influx_user`, `influx_pass` (optionally `aggregation_interval`).

Pass this file to commands with `--config`.

### 1) Prepare a run

```text
python jmeter_load_pipeline.py prepare SimpleLoadTest.jmx --config influx_config_localhost.json
```

`prepare` does:

1. builds `SimpleLoadTest.profile.json` from JMX;
2. generates `test_run` and writes it to `test_run_id.txt`;
3. injects `test_run` into JMX UDV;
4. sends profile to Influx (`load_profile`, `load_profile_samplers`).

### 2) Run load in JMeter

Run the plan manually (GUI or `jmeter.bat -n -t ...`).

Check:

- `StageTracker.groovy` is attached at Test Plan level;
- Backend Listener points to the same Influx as your JSON config.

### 3) Build report

```text
python jmeter_load_pipeline.py report --config influx_config_localhost.json
```

Output: `load_profile_check_<test_run>.html` and `.json`.

---

## Report math

For each thread group and each stage:

- **Target RPS**: `(CTT RPM * threads_on_stage) / 60`;
- **Fact. RPS OK**: `ok_requests / plateau_duration_seconds`;
- **Fact. RPS ALL**: `(ok_requests + ko_requests) / plateau_duration_seconds`;
- **Deviation OK %**: `abs(actual_ok - target) / target * 100`;
- **Deviation ALL %**: `abs(actual_all - target) / target * 100`.

Where:

- `ok_requests` are rows with `statut='ok'`;
- `ko_requests` are rows with `statut='ko'`.

Stage `PASS/FAIL` for profile compliance is based on **Deviation ALL %**.
The report also shows an informational `Requests status` (expected vs actual request count):
`PASS <= 5%`, `WARN <= 10%`, `FAIL > 10%`.

### Early stop support

If run ends early and `jmeter` points have `test_run` tag, report can:

- mark unreached stages as `SKIP`;
- mark interrupted stage as `PARTIAL` (truncated interval).

Without `test_run` tag in `jmeter`, early-stop trimming is not available.

---

## Multi-pod / multi-runner behavior

With one `test_run` and multiple injectors:

- per-runner analysis requires `runner` tag in `jmeter`;
- report renders one block per runner and then a cluster summary.

### Fallback mode

If `runner` is missing in `jmeter`, script reads `jmeter_runner_meta` (heartbeat from `StageTracker.groovy`):

- detects runner count `N`;
- scales target RPS by `N`;
- shows cluster-only summary (no per-runner tables).

---

## Required JMX conventions

To keep profile and metrics aligned:

1. `test_run` must match across prepare / execution / report.
2. Use Transaction Controller names like `_UC_*` for business-level RPS.
3. Ensure sampler prefixes are configured in `sampler_filter.json`.
4. Stage extraction currently supports `UltimateThreadGroup`.
5. Keep `aggregation_interval` aligned with Backend Listener sending interval.

---

## Repository map

| File | Purpose |
|---|---|
| `jmeter_load_pipeline.py` | Orchestrates `prepare` / `report` |
| `parse_jmx_profile.py` | JMX -> `*.profile.json` |
| `utg_schedule.py` | Finds UTG clean plateaus |
| `send_profile_to_influx.py` | Sends expected profile to Influx |
| `check_load_profile.py` | Produces HTML/JSON report |
| `StageTracker.groovy` | Stage events + runner heartbeat |
| `SimpleLoadTest.jmx` | Reference test plan |

---

## Troubleshooting

- `partial write: points beyond retention policy dropped`:
  timestamps conflict with retention policy.
- `field type conflict`:
  field type was fixed differently in existing measurement.
- "100% deviation with multiple pods":
  usually missing `runner` tag in `jmeter`.
- "Unexpected extra stages":
  verify UTG-based plateau parsing is used (not ramp intervals).

---

## Example report

<a href="docs/images/load-profile-check-full.png">
  <img src="docs/images/load-profile-check-full.png" alt="Sample load profile check report" width="1200"/>
</a>

Click the image to open the full-size version.

