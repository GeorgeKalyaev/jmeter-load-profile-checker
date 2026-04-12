# JMeter + InfluxDB load profile

[Russian](README.md)

Short guide: **what to run and in what order**.

### Influx settings — one separate file, effectively once

Connection settings for InfluxDB (`influx_url`, `influx_db`, `influx_user`, `influx_pass`, and optionally `aggregation_interval`) live in **a single JSON file**: copy `influx_config.example.json` to your own name (e.g. `influx_config.local.json`), fill it in **for your environment**, and keep it local (**do not commit** secrets). Then pass it in every command with `--config path/to/file.json`. You **do not need to edit the Python source** — scripts read these values from the JSON.

Use the **same** URL and credentials in the JMeter **Backend Listener** (and in Influx-related variables for **StageTracker** in the test plan, if applicable).

---

## Options A and B: what is the difference

**Important:** no script **starts JMeter for you**. You always run the test plan yourself in JMeter (or `jmeter.bat -n -t ...`) and wait until it finishes.

- **Option A** — you run **Python twice**: first `prepare`, then after the test `report`. Only JMeter runs in between.
- **Option B** — you run **individual commands** for preparation and report (or a single `prepare_test.py` instead of parse → send).

The examples below use plan **`SimpleLoadTest.jmx`** (included in the repo) and config **`influx_config_localhost.json`** (local example). For your environment, point commands at your own JSON (e.g. a copy of `influx_config.example.json` with your URL and password). Run commands from the repository folder (`cd` there).

### Option A — `jmeter_load_pipeline.py` (2× Python + JMeter in the middle)

| # | Command / action |
|---|------------------|
| 0 | *(optional, once)* `python init_influxdb.py influx_config_localhost.json` |
| 1 | `python jmeter_load_pipeline.py prepare SimpleLoadTest.jmx --config influx_config_localhost.json` — JMX → profile in Influx → new `test_run` → write to `test_run_id.txt` and to UDV `test_run` in this JMX. |
| 2 | Run the **load test in JMeter**. Backend Listener and `StageTracker.groovy` must target the same Influx as in the config. |
| 3 | `python jmeter_load_pipeline.py report --config influx_config_localhost.json` — reads `test_run` from `test_run_id.txt`, writes `load_profile_check_<test_run>.html` and `.json`. |

Summary: **`prepare` → JMeter → `report`**.

### Option B — commands in order

**B1 (shorter):** one script for preparation, then JMeter, then the report.

| # | Command / action |
|---|------------------|
| 0 | *(optional)* `python init_influxdb.py influx_config_localhost.json` |
| 1 | `python prepare_test.py SimpleLoadTest.jmx influx_config_localhost.json --patch-jmx` — console shows **Test Run ID**; same value in `test_run_id.txt` and in the JMX. |
| 2 | Run the test in **JMeter**. |
| 3 | `python check_load_profile.py test_YYYYMMDD_HHMMSS influx_config_localhost.json` — use the **same** ID as in step 1. |

**B2 (fully manual):** three separate scripts + manual `test_run` in JMeter (if you do not patch the JMX).

| # | Command / action |
|---|------------------|
| 0 | *(optional)* `python init_influxdb.py influx_config_localhost.json` |
| 1 | `python parse_jmx_profile.py SimpleLoadTest.jmx` → creates **`SimpleLoadTest.profile.json`**. |
| 2 | Choose an ID, e.g. `test_20260411_153045`, write it as **a single line** in **`test_run_id.txt`** (helps tracking and `report` if you use it). |
| 3 | `python send_profile_to_influx.py SimpleLoadTest.profile.json test_20260411_153045 influx_config_localhost.json` — second argument = **the same** ID. |
| 4 | In JMeter: **User Defined Variables** → **`test_run`** = same ID *(if you did not use `prepare_test.py --patch-jmx`).* |
| 5 | Run the test in **JMeter**. |
| 6 | `python check_load_profile.py test_20260411_153045 influx_config_localhost.json` — again **the same** ID. |

In B2, build the report with **`check_load_profile.py`** and an explicit ID; or, if you did not change `test_run_id.txt`, `python jmeter_load_pipeline.py report --config influx_config_localhost.json` also works.

---

## Things to watch

- The same **`test_run`** when sending the profile, in **User Defined Variables** in the JMX, and when generating the report.
- `aggregation_interval` in JSON should match the **Sending interval** of the **Backend Listener** in JMeter and how you compute RPS in Grafana (e.g. `sum("count") / N` → use `N`). In `SimpleLoadTest.jmx` the interval is not set explicitly — the Influx Backend Listener often defaults to **5 s**; `influx_config_localhost.json` uses **`5.0`**.
- For local Influx on `localhost`, you can use `influx_config_localhost.json` (default user/password are for dev only).

---

## Repository layout

| File | Purpose |
|------|---------|
| `jmeter_load_pipeline.py` | Entry point: `prepare` / `report` |
| `prepare_test.py` | Run preparation + `--patch-jmx` |
| `parse_jmx_profile.py` | JMX → `*.profile.json` |
| `send_profile_to_influx.py` | Send profile to Influx |
| `check_load_profile.py` | HTML/JSON report for `test_run` |
| `init_influxdb.py` | One-time InfluxDB 1.x DB/user setup |
| `StageTracker.groovy` | Stages → events in Influx (JSR223 Listener) |
| `sampler_filter.json` | Sampler name prefixes for the parser (default `HTTP`) |
| `influx_config.example.json` | Config template |
| `influx_config_localhost.json` | Example for local Influx |
| `SimpleLoadTest.jmx` | Sample plan (3× UTG, Backend Listener, StageTracker) |

### Sample HTML report

What the `check_load_profile` output can look like (screenshot from this repo):

![Sample load profile check report](docs/images/load-profile-check-sample.png)
