English | [Русский](README.ru.md)

## JMeter Load Profile Checker

Step-by-step instructions to set up and use automated target-vs-actual load profile verification.

## 🚦 Quick start (TL;DR)

1) One time — initialize InfluxDB 1.x
- Command:
```powershell
cd C:\Users\kalya\JmeterReport
python init_influxdb.py influx_config_localhost.json
```

2) Before each test — prepare profile and test_run_id
- Command:
```powershell
python prepare_test.py SimpleLoadTest.jmx influx_config_localhost.json
```
- Result: console prints `test_YYYYMMDD_HHMMSS`

3) In JMeter set the same test_run
- Where: Test Plan → User Defined Variables → `test_run`
- Value: the same `test_run_id` from step 2
- Ensure JSR223 Listener with `StageTracker.groovy` is added at Test Plan level

4) Run the test in JMeter
- Example (CLI):
```powershell
jmeter.bat -n -t SimpleLoadTest.jmx -l results.jtl
```

5) After the test — generate report
- Command:
```powershell
python check_load_profile.py <test_run_id> influx_config_localhost.json
```
- Output: `load_profile_check_<test_run_id>.html` and `.json`

Notes:
- For remote DB use `influx_config.json` instead of `influx_config_localhost.json`.
- Manual path without `prepare_test.py`: `python parse_jmx_profile.py SimpleLoadTest.jmx` → `python send_profile_to_influx.py SimpleLoadTest.profile.json <test_run_id> influx_config_localhost.json`.

### Sample report (end result)

The script writes an HTML report that compares **target vs actual** load (RPS by stage, thread groups, status). A real output from this repo is:

- [`load_profile_check_test_20260123_011440.html`](load_profile_check_test_20260123_011440.html)

**Viewing on GitHub:** the file is shown as source code, not as a rendered page. After cloning, open it locally (double-click or `Start-Process` in PowerShell). **Embedding HTML inside README is not supported** on GitHub (and most Markdown hosts) for security reasons.

**Preview without opening the file:** use a screenshot in the README (full-page capture of the HTML — scaled to half size for a smaller file; on GitHub click the image to view it at full resolution):

![Example: load profile check report (full page)](docs/images/load-profile-check-sample.png)

The PNG is a **real full-page screenshot** of the sample HTML report (same content as [`load_profile_check_test_20260123_011440.html`](load_profile_check_test_20260123_011440.html)). Replace it if you want the README to show another run.

## 📚 Detailed step-by-step guide

### Credentials (important)
- Examples use `jmeter_user` / `changeme`.
- Change `changeme` in:
  - `influx_config_localhost.json` / `influx_config.json`
  - Backend Listener URL in `SimpleLoadTest.jmx`
  - optional JSR223 defaults inside `SimpleLoadTest.jmx`

### 0) Prerequisites (one time)
- Python 3
- InfluxDB 1.x is running and reachable
- Apache JMeter 5.x installed ([download](https://jmeter.apache.org/download_jmeter.cgi)); ensure `jmeter.bat` is on `PATH` or use the full path to `bin\jmeter.bat`
- In `influx_config_localhost.json`:
  - `influx_url`: `http://localhost:8086`
  - `influx_db`: `jmeter`
  - `influx_user`: `jmeter_user`
  - `influx_pass`: `jmeter123`

Verify config:
```powershell
type influx_config_localhost.json
```

### 1) Initialize InfluxDB (one time per machine/DB)
- Goal: create DB/user/retention policy and warm up measurements
```powershell
cd C:\Users\kalya\JmeterReport
python init_influxdb.py influx_config_localhost.json
```
If OK — proceed. Re-running is safe.

### 2) Prepare profile and test_run_id (each test)
- Recommended automation:
```powershell
python prepare_test.py SimpleLoadTest.jmx influx_config_localhost.json
```
- Output shows:
```
Test Run ID: test_20260123_011440
```
- Use this ID in JMeter (next step).

Verify profile is stored:
```powershell
python check_load_profile.py test_20260123_011440 influx_config_localhost.json 2>$null | findstr /C:"Загрузка профиля"
```

### 3) JMeter setup
- Open `SimpleLoadTest.jmx` in JMeter GUI
- Test Plan → User Defined Variables → `test_run` = your ID (e.g., `test_20260123_011440`)
- Ensure:
  - JSR223 Listener at Test Plan level with `StageTracker.groovy`
  - Backend Listener points to Influx:
    - `http://jmeter_user:changeme@localhost:8086/write?db=jmeter`
  - Needed Thread Groups/elements are enabled

### 4) Run the test
- Via GUI — as usual
- Via CLI:
```powershell
jmeter.bat -n -t SimpleLoadTest.jmx -l results.jtl
```

Check JMeter logs:
- Expect lines like:
  - `StageTracker: Профиль загружен из InfluxDB для test_run=...`
  - `StageTracker: Отправлено событие перехода на ступень ...`

### 5) Generate report (after test)
- Run:
```powershell
python check_load_profile.py test_20260123_011440 influx_config_localhost.json
```
- Check output:
  - `JSON результаты сохранены: load_profile_check_test_20260123_011440.json`
  - `HTML отчёт сохранён: load_profile_check_test_20260123_011440.html`

Open report:
```powershell
Start-Process "load_profile_check_test_20260123_011440.html"
```

### 6) Common checks and pitfalls
- Same `test_run_id`:
  - when sending profile
  - in JMeter (User Defined Variables)
  - when generating report
- Use a fresh `test_run_id` each time
- Filter by `test_run` in InfluxDB queries
- Only one StageTracker at Test Plan level

### 7) Where data lives
- In InfluxDB (single DB `jmeter`):
  - `load_profile` — target profile per stage (`send_profile_to_influx.py`)
  - `load_profile_thread_group_info` — TG info (transactions list)
  - `load_profile_samplers` — samplers + criteria (optional)
  - `load_stage_change` — stage change events (JSR223 Listener)
  - `jmeter` — actual metrics (Backend Listener)

## 📋 Project structure

**Required files:**
- `parse_jmx_profile.py` — parse JMX to profile
- `send_profile_to_influx.py` — send profile to InfluxDB
- `StageTracker.groovy` — stage change tracker (JSR223 Listener)
- `check_load_profile.py` — post-test profile compliance check
- `prepare_test.py` — quick prep: parse → gen test_run_id → send profile
- `influx_config.json` / `influx_config_localhost.json` — InfluxDB config
- `sampler_filter.json` — sampler prefix filtering (default: `["HTTP"]`)
- `grafana_dashboard_default.json` — Grafana dashboard (optional)
- `init_influxdb.py` — InfluxDB 1.x init (DB/user/RP) and warm-up

## ⚙️ Step 1: InfluxDB setup

### 1.1 Configure `influx_config.json`
Open `influx_config.json` (or use `influx_config_localhost.json`) and set your InfluxDB connection parameters. The same values must be reflected in the JMX Backend Listener.

### 1.2 Create DB/user/retention policy (InfluxDB 1.x)
InfluxDB 1.x is schemaless; measurements are created on first write. Still, it’s better to create DB/user/RP up front:
```powershell
cd C:\Users\kalya\JmeterReport
python init_influxdb.py influx_config_localhost.json
```
This script ensures:
- Database `jmeter`
- User `jmeter_user` with password `changeme` and grants
- Default RP `autogen`
- Warm-up writes for: `load_profile`, `load_profile_thread_group_info`, `load_stage_change`, `jmeter`

Alternatively, use curl with auth enabled.

### 1.3 Verify Backend Listener URL in JMX
Example format:
```
http://jmeter_user:changeme@localhost:8086/write?db=jmeter
```

### 1.4 Optional defaults in `StageTracker.groovy`
Adjust default `influxUrl/db/user/pass` if needed.

## 📝 Step 2: JMeter setup
Add a JSR223 Listener at Test Plan level, set `test_run` in User Defined Variables, and point the Backend Listener to your InfluxDB. Make sure only one StageTracker exists at Test Plan level.

## 🚀 Step 3: Full workflow
1) Parse JMX to profile and send it to InfluxDB (use `prepare_test.py` or do it manually)  
2) Set the same `test_run` in JMeter  
3) Run the test (GUI/CLI)  
4) Generate the HTML report with `check_load_profile.py <test_run_id> [config]`

The script automatically:
- reads the target profile from `load_profile`,
- reads stage-change events from `load_stage_change`,
- determines absolute time ranges,
- fetches actual metrics from `jmeter`,
- compares actual vs target RPS and produces HTML/JSON.

## 📊 InfluxDB data model
- `load_profile`: target stages per Thread Group (tags: `test_run`, `thread_group`; fields: `stage_idx`, `plateau_start_s`, `plateau_end_s`, `hold_s`, `threads`, `target_rps`)
- `load_stage_change`: one event per stage when plateau starts (tags: `test_run`, `thread_group`; fields: `stage_idx`, `threads`, `target_rps`, `plateau_start_s`, `hold_s`)
- `jmeter`: actual metrics from Backend Listener (tags: `application`, `transaction`; fields include `count`, percentiles, etc.)

## 🔍 Troubleshooting
- Ensure the same `test_run_id` is used for profile sending, JMeter variable, and report generation
- Verify only one StageTracker is active (at Test Plan)
- Check InfluxDB reachability and Backend Listener URL
- If deviations are large: verify Constant Throughput Timer and increase tolerance if needed

## 📈 Grafana (optional)
- Create a `test_run` dashboard variable (Text box or Query)
- Build panels for target profile, actual RPS, and stage-change annotations using the measurements above
