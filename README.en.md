# JMeter + InfluxDB load profile

[Russian](README.md)

Short guide: **what to run and in what order**.

### Influx settings — one separate file, effectively once

Connection settings for InfluxDB (`influx_url`, `influx_db`, `influx_user`, `influx_pass`, and optionally `aggregation_interval`) live in **a single JSON file**: copy `influx_config.example.json` to your own name (e.g. `influx_config.local.json`), fill it in **for your environment**, and keep it local (**do not commit** secrets). Then pass it in every command with `--config path/to/file.json`. You **do not need to edit the Python source** — scripts read these values from the JSON.

Use the **same** URL and credentials in the JMeter **Backend Listener** (and in Influx-related variables for **StageTracker** in the test plan, if applicable).

---

## How to use

**Important:** no script **starts JMeter for you**. You always run the plan yourself (GUI or `jmeter.bat -n -t ...`).

**InfluxDB:** provision InfluxDB 1.x, database, and credentials **yourself** (official Influx docs). This repo has **no** database bootstrap scripts — only JSON connection settings for Python and JMeter.

The examples use **`SimpleLoadTest.jmx`** and **`influx_config_localhost.json`**. Use your own JMX and JSON (copy of `influx_config.example.json`) for real environments. Run commands from the repository root.

### Main flow (`jmeter_load_pipeline.py`)

**Step 1 — prepare**

```text
python jmeter_load_pipeline.py prepare SimpleLoadTest.jmx --config influx_config_localhost.json
```

The tool runs, in order:

1. **`parse_jmx_profile.py`** → **`SimpleLoadTest.profile.json`** next to the plan. For **Ultimate Thread Group**, stages come from **schedule simulation** (`utg_schedule.py`); the profile includes `utg_schedule_mode`. If simulation yields no stages, fallback is one UTG row = one stage.
2. Generate **`test_run`**, write **`test_run_id.txt`** (one line).
3. **`send_profile_to_influx.py`** — profile to Influx (same JSON as `--config`).
4. Write **`test_run`** into **User Defined Variables** in the JMX file — no manual paste in JMeter if you open that same plan.

**Step 2 — load test in JMeter**

- **JSR223 Listener** + **`StageTracker.groovy`** under **Test Plan**.
- **Backend Listener** → same Influx as in the JSON (URL, DB, credentials).

**Step 3 — report**

```text
python jmeter_load_pipeline.py report --config influx_config_localhost.json
```

Reads **`test_run`** from **`test_run_id.txt`**, runs **`check_load_profile.py`**, writes **`load_profile_check_<test_run>.html`** and **`.json`**.

**Order:** **`prepare` → JMeter → `report`**.

### Manual mode (no orchestrator)

For debugging, run the same steps yourself. For **`send_profile_to_influx`** and **`check_load_profile`**, the Influx config path is the **last positional** argument (`jmeter_load_pipeline` uses **`--config`** instead).

1. `python parse_jmx_profile.py SimpleLoadTest.jmx` → **`SimpleLoadTest.profile.json`** (uses **`sampler_filter.json`**).
2. Choose a **`test_run`** and optionally one line in **`test_run_id.txt`** (for `report` without an explicit id).
3. `python send_profile_to_influx.py SimpleLoadTest.profile.json test_20260411_153045 influx_config_localhost.json`
4. In the JMX: **User Defined Variables** → **`test_run`** = same id (skip if you already used **`prepare`**, which patched the file).
5. Run JMeter.
6. `python check_load_profile.py test_20260411_153045 influx_config_localhost.json`

If **`test_run_id.txt`** holds the same id, step 6 can be:  
`python jmeter_load_pipeline.py report --config influx_config_localhost.json`.

---

## Things to watch

- The same **`test_run`** when sending the profile, in **User Defined Variables** in the JMX, and when generating the report.
- `aggregation_interval` in JSON should match the **Sending interval** of the **Backend Listener** in JMeter and how you compute RPS in Grafana (e.g. `sum("count") / N` → use `N`). In `SimpleLoadTest.jmx` the interval is not set explicitly — the Influx Backend Listener often defaults to **5 s**; `influx_config_localhost.json` uses **`5.0`**.
- For local Influx on `localhost`, you can use `influx_config_localhost.json` (default user/password are for dev only).

---

## How the report works: plateaus vs ramps

This section explains what the **`check_load_profile`** report measures and why a “stage” duration in HTML **does not have to match** the **Hold** column of a single Ultimate Thread Group row.

### 1. Ultimate Thread Group with multiple rows

In a typical “staircase” plan, each UTG row **adds** threads on top of those already running. Load over time is the **sum** of all rows (see **`utg_schedule.py`**). A **business stage** in the profile is an interval where the **total** active thread count is **flat** (no ramp on the aggregate), not “one table row = one stage”.

### 2. Plateau window `[plateau_start_s, plateau_end_s)`

For each stage, `*.profile.json` defines a half-open time window **in seconds from test start**:

- **Start** — after ramp-up to a **flat** segment at that total thread count.
- **End** — **before** the next change in total load (e.g. before the next row’s ramp-up starts). The report uses **`[start, end)`** — the right bound is **exclusive**.

**Ramp-up / ramp-down between stages are not inside that window.** For example, a 20 s ramp to the next wave is a separate slice; it is neither the previous plateau nor the next plateau.

### 3. What `check_load_profile.py` computes inside the plateau

For each thread group and each stage, using Influx (`jmeter` measurement, tags such as `test_run`, `transaction` / sampler name) **only over that time slice**:

| Metric | Meaning |
|--------|--------|
| **Target RPS** | From JMX: `(CTT in RPM × threads in this TG) / 60` — expected **for this TG**. |
| **Actual RPS** | `successful requests (statut = 'ok') / plateau duration in seconds`. Errors and non-ok are **not** in the numerator. |
| **Deviation %** | `|actual − target| / target × 100%` **per TG**; PASS/FAIL threshold in the report is typically 10%. |
| **Expected requests** | `target RPS × (plateau_end_s − plateau_start_s)` — plateau only, no ramps between stages. |

Plateau duration is **`end − start`**, not necessarily the raw **Hold** of one UTG row when several rows overlap.

### 4. Stage events in Influx

`StageTracker.groovy` writes auxiliary events (e.g. stage changes) for time alignment; the report may use them to refine **test start**. **Plateau boundaries used for RPS** come from the **parsed profile** (JMX + UTG simulation), not from eyeballing a chart.

### 5. What not to commit

Keep real URLs, Influx passwords, and tokens in local files only (e.g. a copy of `influx_config.example.json` named like `influx_config.local.json` in `.gitignore`). This repo ships **`influx_config.example.json`** (template) and **`influx_config_localhost.json`** (local dev sample).

---

## Repository layout

| File | Purpose |
|------|---------|
| `jmeter_load_pipeline.py` | Entry point: `prepare` / `report` (also writes `test_run` into JMX) |
| `parse_jmx_profile.py` | JMX → `*.profile.json` |
| `utg_schedule.py` | UTG schedule simulation: flat total-thread segments |
| `send_profile_to_influx.py` | Send profile to Influx |
| `check_load_profile.py` | HTML/JSON report for `test_run` |
| `StageTracker.groovy` | Stages → events in Influx (JSR223 Listener) |
| `sampler_filter.json` | Sampler name prefixes for the parser (default `HTTP`) |
| `influx_config.example.json` | Config template |
| `influx_config_localhost.json` | Example for local Influx |
| `SimpleLoadTest.jmx` | Sample plan (3× UTG, Backend Listener, StageTracker) |
| `docs/images/load-profile-check-sample.png` | README screenshot |

### Sample HTML report

What the `check_load_profile` output can look like (screenshot from this repo):

![Sample load profile check report](docs/images/load-profile-check-sample.png)
