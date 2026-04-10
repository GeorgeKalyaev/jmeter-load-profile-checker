# Профиль нагрузки JMeter + InfluxDB

Краткая инструкция: что запускать и в каком порядке. Рабочие URL и пароли держите в **локальном** JSON (скопируйте из `influx_config.example.json`), не коммитьте.

[English (short)](#english-short)

---

## Вариант A и B: в чём разница

**Важно:** ни один скрипт **не запускает JMeter за вас**. Всегда вручную: открыть план в JMeter (или `jmeter.bat -n -t ...`) и дождаться конца теста.

- **Вариант A** — два раза вызываете **Python**: сначала `prepare`, после теста `report`. Между ними только JMeter.
- **Вариант B** — подготовку и отчёт делаете **отдельными командами** (или один `prepare_test.py` вместо цепочки parse → send).

Подставляйте свои имена файлов: план **`МойПлан.jmx`**, конфиг Influx **`МойКонфиг.json`**. Команды выполняйте из папки репозитория (предварительно `cd` туда). Конфиг один раз скопируйте из `influx_config.example.json` и заполните; с секретами файл не коммитьте.

### Вариант A — `jmeter_load_pipeline.py` (2× Python + JMeter посередине)

| № | Команда / действие |
|---|---------------------|
| 0 | *(опционально, один раз)* `python init_influxdb.py МойКонфиг.json` |
| 1 | `python jmeter_load_pipeline.py prepare МойПлан.jmx --config МойКонфиг.json` — JMX → профиль в Influx → новый `test_run` → запись в `test_run_id.txt` и в UDV `test_run` в этом JMX. |
| 2 | Запуск **нагрузки в JMeter**. Backend Listener и `StageTracker.groovy` → тот же Influx, что в конфиге. |
| 3 | `python jmeter_load_pipeline.py report --config МойКонфиг.json` — читает `test_run` из `test_run_id.txt`, пишет `load_profile_check_<test_run>.html` и `.json`. |

Итого: **`prepare` → JMeter → `report`**.

### Вариант B — команды по порядку

**B1 (короче):** один скрипт на подготовку, потом JMeter, потом отчёт.

| № | Команда / действие |
|---|---------------------|
| 0 | *(опционально)* `python init_influxdb.py МойКонфиг.json` |
| 1 | `python prepare_test.py МойПлан.jmx МойКонфиг.json --patch-jmx` — в консоли будет **Test Run ID**; он же в `test_run_id.txt` и в JMX. |
| 2 | Запуск теста в **JMeter**. |
| 3 | `python check_load_profile.py test_YYYYMMDD_HHMMSS МойКонфиг.json` — подставьте **тот же** ID, что на шаге 1. |

**B2 (всё раздельно):** три разных скрипта + ручной `test_run` в JMeter (если не патчите JMX).

| № | Команда / действие |
|---|---------------------|
| 0 | *(опционально)* `python init_influxdb.py МойКонфиг.json` |
| 1 | `python parse_jmx_profile.py МойПлан.jmx` → появится **`МойПлан.profile.json`**. |
| 2 | Придумать ID, например `test_20260411_153045`, записать **одной строкой** в файл **`test_run_id.txt`** (удобно для контроля и для `report`, если решите им воспользоваться). |
| 3 | `python send_profile_to_influx.py МойПлан.profile.json test_20260411_153045 МойКонфиг.json` — второй аргумент = **тот же** ID. |
| 4 | В JMeter: **User Defined Variables** → **`test_run`** = тот же ID *(если не использовали `prepare_test.py --patch-jmx`).* |
| 5 | Запуск теста в **JMeter**. |
| 6 | `python check_load_profile.py test_20260411_153045 МойКонфиг.json` — снова **тот же** ID. |

В B2 отчёт — через **`check_load_profile.py`** с явным ID; либо, если `test_run_id.txt` не меняли, сработает и `python jmeter_load_pipeline.py report --config МойКонфиг.json`.

---

## На что обратить внимание

- Один и тот же **`test_run`** при отправке профиля, в **User Defined Variables** в JMX и при отчёте.
- `aggregation_interval` в JSON должен совпадать с **Sending interval** у **Backend Listener** в JMeter и с тем, как вы считаете RPS в Grafana (например `sum("count") / N` → `N`). В `SimpleLoadTest.jmx` интервал в JMX не задан явно — у Influx Backend Listener обычно **5 с** по умолчанию; в `influx_config_localhost.json` стоит `5.0`.
- Для локального Influx на `localhost` можно использовать `influx_config_localhost.json` (учётка/пароль по умолчанию только для dev).

---

## Состав репозитория

| Файл | Назначение |
|------|------------|
| `jmeter_load_pipeline.py` | Точка входа: `prepare` / `report` |
| `prepare_test.py` | Подготовка прогона + `--patch-jmx` |
| `parse_jmx_profile.py` | JMX → `*.profile.json` |
| `send_profile_to_influx.py` | Профиль в Influx |
| `check_load_profile.py` | Отчёт HTML/JSON по `test_run` |
| `init_influxdb.py` | Разовая инициализация БД/пользователя Influx 1.x |
| `StageTracker.groovy` | Ступени → события в Influx (JSR223 Listener) |
| `sampler_filter.json` | Префиксы имён сэмплеров для парсера (по умолчанию `HTTP`) |
| `influx_config.example.json` | Шаблон конфигурации |
| `influx_config_localhost.json` | Пример для локального Influx |
| `SimpleLoadTest.jmx` | Пример плана (3×UTG, Backend Listener, StageTracker) |

---

## English (short)

Python never starts JMeter. **Option A:** `jmeter_load_pipeline.py prepare` → run JMeter → `jmeter_load_pipeline.py report`. **Option B:** `prepare_test.py … --patch-jmx` → JMeter → `check_load_profile.py <id> config.json`, or full chain `parse_jmx_profile.py` → edit `test_run_id.txt` → `send_profile_to_influx.py` → set `test_run` in JMeter → JMeter → `check_load_profile.py`.

1. Copy `influx_config.example.json` to a local file — **do not commit** secrets.  
2. Once if needed: `python init_influxdb.py your_config.json`  
3. Option A: `python jmeter_load_pipeline.py prepare your-plan.jmx --config your_config.json` → JMeter → `python jmeter_load_pipeline.py report --config your_config.json`
