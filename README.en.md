# JMeter + InfluxDB load profile

[Russian](README.md)

Short guide: **what to run and in what order**.

### Influx settings — one separate file, effectively once

Connection settings for InfluxDB (`influx_url`, `influx_db`, `influx_user`, `influx_pass`, and optionally `aggregation_interval`) live in **a single JSON file**: copy `influx_config.example.json` to your own name (e.g. `influx_config.local.json`), fill it in **for your environment**, and keep it local (**do not commit** secrets). Then pass it in every command with `--config path/to/file.json`. You **do not need to edit the Python source** — scripts read these values from the JSON.

Use the **same** URL and credentials in the JMeter **Backend Listener** (and in Influx-related variables for **StageTracker** in the test plan, if applicable).

---

## JMX layout and naming conventions

So **`parse_jmx_profile`**, Influx, and **`check_load_profile`** line up without hand-edits:

1. **`test_run`** — under **Test Plan → User Defined Variables**. After **`jmeter_load_pipeline.py prepare`** it is written into the JMX file; reopen the plan in the GUI if you edited the file externally.

2. **Thread Group `testname`** (e.g. `UC_01_Group_List`) — the logical group name in the report. Inside that TG, preferably wrap the flow in a **Transaction Controller** named **underscore + same name as the TG**:  
   TG `UC_01_Group_List` → Transaction Controller **`_UC_01_Group_List`**.  
   The Backend Listener often stores that label in the Influx tag **`transaction`**; the leading **`_`** is a common JMeter convention for the *transaction* sample vs individual HTTP samples.  
   If there is **no** Transaction Controller, the parser still adds both the TG name and **`_{TG_name}`** to the profile for `jmeter` queries (back-compat).

3. **HTTP Sampler names** — must start with one of the prefixes in **`sampler_filter.json`** (default **`HTTP`**, e.g. **`HTTP Request …`**). Otherwise the sampler is skipped in `*.profile.json` and in the report SLA table. For JDBC/SOAP/etc., add prefixes to **`allowed_sampler_prefixes`** in that JSON.

4. **StageTracker.groovy** — at **Test Plan** level (script from this repo; the JSR223 file path must resolve). **Backend Listener** (InfluxDB Backend Listener) — same Influx (URL, DB, credentials) as in the Python JSON. The **`test_run`** UDV must match profile send and report. To add the **`test_run`** **tag** on **`jmeter`** points (needed for **end-of-run time** and **SKIP/PARTIAL** when the test stops early), set **`eventTags`** on the listener, e.g. **`test_run=${test_run}`** (comma-separated for more tags). Without it, RPS is still computed from profile windows, but plateaus are **not** truncated to the real stop time.

5. **Ultimate Thread Group** — load stages in `*.profile.json` are collected **only** for **`kg.apc.jmeter.threads.UltimateThreadGroup`**. A plain **Thread Group** is **not** turned into profile stages by this parser (in `SimpleLoadTest.jmx` classic TGs are disabled). Use UTG for staircases; total-thread simulation lives in `utg_schedule.py`.

6. **Constant Throughput Timer** — target RPS in the report is computed as **(RPM × threads at that stage) / 60**, aligned with a per-thread style CTT (see comments in `send_profile_to_influx.py`). If your CTT `calcMode` differs, the displayed target may not match reality until you adjust the formula or plan.

7. **Per–thread-group RPS in `check_load_profile`** — Influx rows must use transaction names **`_UC*`** (Transaction Controller: child samplers roll up into that transaction’s counts). From profile `transaction_names`, only names **starting with `_UC`** are used in the query (**OR**). If there are none (only sampler names, etc.), the filter is **`transaction =~ /^_UC.*/`** for the whole time window (with several parallel TGs at once, all `_UC` rows may be summed). The parser still stores all TC names and Module Controller targets in the profile for consistency and other uses.

8. **Module Controller** inside a UTG (reference to another subtree, including a TC in a **disabled** “library” Thread Group) — the parser appends the **last segment** of `node_path` to `transaction_names` (e.g. **`_UC_01_Check_List`**). The parser does not execute the plan: the path is static in the JMX, so the target transaction name is picked up even when the script lives under a disabled group. Otherwise an “empty” UTG tree would only get `UC_01_Group_List` / `_UC_01_Group_List` in the profile while Influx uses another `transaction` → **0 requests** and a false deviation.

**Data flow (short):**  
`prepare` → Influx **`load_profile`** + **`load_profile_samplers`** (expected profile). JMeter run → **`jmeter`** (sample metrics) + **`load_stage_change`** lines from **StageTracker** (stage transitions).  
`report` reads the profile from Influx and compares to **`jmeter`** by time and **`transaction`** (per TG, see item 7). The same **`test_run`** must be used for the profile and UDV; a **`test_run`** tag on **`jmeter`** points is recommended for early-stop logic (see item 4).

### Multiple pods / injectors (`runner` tag in Influx)

Same **`test_run`**, several JMeter instances (e.g. Kubernetes pods): set the **`runner`** tag in the **Backend Listener** (`eventTags`) and in **`load_stage_change`** lines from **StageTracker** — see **`StageTracker.groovy`** and **`SimpleLoadTest.jmx`** (pod hostname via `HOSTNAME` / `runner`).

**`check_load_profile.py`** discovers every distinct **`runner`** for that `test_run` in **`jmeter`** and renders **one table block per runner**, then a **cluster summary**: target RPS = single-instance profile × **N**, actual = summed metrics across pods. **N** is not hard-coded (2, 3, 4, … — whatever distinct `runner` values exist).

**Not implemented yet (future):** per-runner **`test_start_time_ns`**; today one global start time is used for all runners. If pods start far apart, RPS deviation at stage boundaries may be slightly worse for the lagging pod.

If **`jmeter`** has no **`runner`** tag, the report behaves like a **single** source (backward compatible).

---

## Options A and B: what is the difference

**Common:** no script **starts JMeter**. InfluxDB 1.x, database, and user are **your** ops concern (official Influx docs); this repo only ships JSON for connections.

| | **Option A** | **Option B** |
|---|----------------|---------------|
| **Idea** | Single entry point: **`jmeter_load_pipeline.py`** (`prepare` / `report`) | Same work as **separate** commands: `parse_jmx_profile` → `send_profile_to_influx` → `check_load_profile` |
| **Influx config** | Always **`--config path.json`** | For `send_profile_to_influx` and `check_load_profile`, config is the **last positional** argument (no `--config`) |
| **`test_run` in JMX** | **`prepare`** writes it **automatically** | You set **User Defined Variables** **by hand** (unless you reuse a JMX already patched by A) |
| **When it fits** | Normal day-to-day use | Debugging one step, custom automation without the orchestrator |

Examples below use **`SimpleLoadTest.jmx`** and **`influx_config_localhost.json`**. Run commands from the repository root.

---

### Option A — step by step (`jmeter_load_pipeline.py`)

**Step 1 — prepare**

```text
python jmeter_load_pipeline.py prepare SimpleLoadTest.jmx --config influx_config_localhost.json
```

Runs, in order:

1. **`parse_jmx_profile.py`** → **`SimpleLoadTest.profile.json`**. For UTG, stages from simulation (`utg_schedule.py`), field `utg_schedule_mode`; else fallback one UTG row = one stage.
2. New **`test_run`** → **`test_run_id.txt`** (one line).
3. Write **`test_run`** into **User Defined Variables** in the JMX on disk — open that file in JMeter; no manual id entry.
4. **`send_profile_to_influx.py`** — profile to Influx (same JSON as `--config`).

**Step 2 — load test in JMeter (you only)**

Run the plan (GUI or `jmeter.bat -n -t ...`). Ensure:

- **JSR223 Listener** + **`StageTracker.groovy`** on **Test Plan**.
- **Backend Listener** → same Influx as in the JSON (URL, DB, credentials).

**Step 3 — report after the test**

```text
python jmeter_load_pipeline.py report --config influx_config_localhost.json
```

Reads **`test_run`** from **`test_run_id.txt`**, runs **`check_load_profile.py`**, writes **`load_profile_check_<test_run>.html`** and **`.json`**.

**Overall:** **`prepare` → JMeter → `report`**.

---

### Option B — step by step (no orchestrator)

Useful to run a single script or wire steps yourself.

**Step 1** — JMX → profile:

```text
python parse_jmx_profile.py SimpleLoadTest.jmx
```

Output: **`SimpleLoadTest.profile.json`** (uses **`sampler_filter.json`**).

**Step 2** — choose **`test_run`**: pick an id (e.g. `test_20260411_153045`) and optionally write it as **one line** in **`test_run_id.txt`** (for step 5a below).

**Step 3** — send profile to Influx:

```text
python send_profile_to_influx.py SimpleLoadTest.profile.json test_20260411_153045 influx_config_localhost.json
```

Arguments: **profile file**, **`test_run`**, **Influx JSON** (last argument is the config path).

**Step 4** — in JMeter: **User Defined Variables** → **`test_run`** = **same** id. Save the JMX. (Skip if you already ran **option A `prepare`** on this file.)

**Step 5** — run the load test in JMeter (same as option A).

**Step 6** — report, **either**:

- Explicit id:

```text
python check_load_profile.py test_20260411_153045 influx_config_localhost.json report.html 10.0
```

(Third argument: HTML path; fourth: **RPS deviation tolerance %**, default **10**; you can omit the tail for defaults.)

- Or, if **`test_run_id.txt`** contains the same id:

```text
python jmeter_load_pipeline.py report --config influx_config_localhost.json
```

---

## Things to watch

- If Influx returns **`partial write: points beyond retention policy dropped`** on **`send_profile`**, the server likely enforces retention: older script versions used scenario-second timestamps (epoch 1970). Current **`send_profile_to_influx.py`** uses timestamps near “now”.
- If you see **`field type conflict`** (e.g. `hold_s` integer vs float), the DB already locked field types from older writes; the current script sends those numeric fields as **float** to match common existing schemas.
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
| **Deviation %** | `|actual − target| / target × 100%` **per TG**; PASS/FAIL threshold is a **`check_load_profile`** argument (default **10%**), shown explicitly in the HTML. |
| **Expected requests** | `target RPS × evaluated plateau duration` — full stage uses `(plateau_end_s − plateau_start_s)`; **PARTIAL** uses a shorter window (below). |

Plateau duration is **`end − start`**, not necessarily the raw **Hold** of one UTG row when several rows overlap.

### 4. Early stop (SKIP / PARTIAL)

If the run **ends before** the full profile (manual Stop, etc.) and **`jmeter`** has a **last point tagged `test_run`**, the report can:

- **SKIP** stages whose plateau was never reached (they do **not** drive overall FAIL);
- **PARTIAL** a stage stopped **inside** plateau: RPS and expected requests use a **truncated** interval; within tolerance → **PARTIAL**, otherwise **FAIL**.

Without **`test_run`** on **`jmeter`** points, end time is unknown — every stage uses the **full** profile window (legacy behavior). Configure **`eventTags`** on the Backend Listener (item 4).

### 5. Stage events in Influx

`StageTracker.groovy` writes auxiliary events (e.g. stage changes) for time alignment; the report may use them to refine **test start**. **Plateau boundaries used for RPS** come from the **parsed profile** (JMX + UTG simulation), not from eyeballing a chart. When end-of-run time is known from **`jmeter`**, those windows are **further** clipped to the actual run (see **§4 Early stop** above).

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
