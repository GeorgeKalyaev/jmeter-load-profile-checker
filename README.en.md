English | [Русский](README.ru.md)

## JMeter Load Profile Checker

End-to-end toolkit to:
- parse a JMX into a structured load profile,
- send the profile to InfluxDB 1.x,
- run the test with JMeter (JSR223 stage tracker + Backend Listener),
- generate an HTML report comparing target vs actual RPS per stage.

### Prerequisites
- Python 3
- InfluxDB 1.x running and reachable
- Apache JMeter 5.5 installed (folder `apache-jmeter-5.5`)

### Quick start (TL;DR)
1) Initialize InfluxDB (one time)
```powershell
cd C:\Users\kalya\JmeterReport
python init_influxdb.py influx_config_localhost.json
```

2) Prepare profile and test_run_id (each test)
```powershell
python prepare_test.py SimpleLoadTest.jmx influx_config_localhost.json
```
Copy the printed `test_YYYYMMDD_HHMMSS` value.

3) JMeter setup
- Test Plan → User Defined Variables → `test_run` = the same ID
- Ensure JSR223 Listener has `StageTracker.groovy`
- Backend Listener URL example:
  - `http://jmeter_user:changeme@localhost:8086/write?db=jmeter`

4) Run the test (CLI example)
```powershell
"C:\Users\kalya\JmeterReport\apache-jmeter-5.5\bin\jmeter.bat" -n -t SimpleLoadTest.jmx -l results.jtl
```

5) Generate HTML report
```powershell
python check_load_profile.py <test_run_id> influx_config_localhost.json
```
Artifacts: `load_profile_check_<test_run_id>.html` and `.json`.

### Credentials
- Default credentials used for examples:
  - user: `jmeter_user`
  - pass: `changeme`
- Change them in:
  - `influx_config_localhost.json` / `influx_config.json`
  - JMeter Backend Listener URL
  - optional JSR223 defaults inside `SimpleLoadTest.jmx`

### Files overview
- `parse_jmx_profile.py` — parses JMX to load profile
- `send_profile_to_influx.py` — writes profile to InfluxDB
- `check_load_profile.py` — builds HTML report (target vs actual RPS)
- `prepare_test.py` — convenience: parse + generate `test_run_id` + send profile
- `init_influxdb.py` — creates DB/user/RP and warms up measurements
- `SimpleLoadTest.jmx` — example plan (UTG + stage tracker + backend listener)
- `StageTracker.groovy` — JSR223 Listener script (stage change events)
- `influx_config_localhost.json` — local config (with `changeme`)
- `influx_config.json` — template for remote envs
- `sampler_filter.example.json` — how to include sampler types

### Troubleshooting
- Always keep the same `test_run_id` across:
  - profile sending,
  - JMeter `test_run` variable,
  - report generation.
- Ensure only one StageTracker on Test Plan level.
- Backend Listener must point to the correct InfluxDB URL/db.

