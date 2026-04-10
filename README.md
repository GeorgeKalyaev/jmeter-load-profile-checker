# Профиль нагрузки JMeter + InfluxDB

[English](README.en.md)

Краткая инструкция: что запускать и в каком порядке. Рабочие URL и пароли держите в **локальном** JSON (скопируйте из `influx_config.example.json`), не коммитьте.

---

## Вариант A и B: в чём разница

**Важно:** ни один скрипт **не запускает JMeter за вас**. Всегда вручную: открыть план в JMeter (или `jmeter.bat -n -t ...`) и дождаться конца теста.

- **Вариант A** — два раза вызываете **Python**: сначала `prepare`, после теста `report`. Между ними только JMeter.
- **Вариант B** — подготовку и отчёт делаете **отдельными командами** (или один `prepare_test.py` вместо цепочки parse → send).

В примерах ниже план — **`SimpleLoadTest.jmx`** (лежит в репозитории), конфиг — **`influx_config_localhost.json`** (локальный пример). Для своего стенда замените конфиг на свой JSON (например копию `influx_config.example.json` с вашими URL и паролем). Команды выполняйте из папки репозитория (`cd` туда).

### Вариант A — `jmeter_load_pipeline.py` (2× Python + JMeter посередине)

| № | Команда / действие |
|---|---------------------|
| 0 | *(опционально, один раз)* `python init_influxdb.py influx_config_localhost.json` |
| 1 | `python jmeter_load_pipeline.py prepare SimpleLoadTest.jmx --config influx_config_localhost.json` — JMX → профиль в Influx → новый `test_run` → запись в `test_run_id.txt` и в UDV `test_run` в этом JMX. |
| 2 | Запуск **нагрузки в JMeter**. Backend Listener и `StageTracker.groovy` → тот же Influx, что в конфиге. |
| 3 | `python jmeter_load_pipeline.py report --config influx_config_localhost.json` — читает `test_run` из `test_run_id.txt`, пишет `load_profile_check_<test_run>.html` и `.json`. |

Итого: **`prepare` → JMeter → `report`**.

### Вариант B — команды по порядку

**B1 (короче):** один скрипт на подготовку, потом JMeter, потом отчёт.

| № | Команда / действие |
|---|---------------------|
| 0 | *(опционально)* `python init_influxdb.py influx_config_localhost.json` |
| 1 | `python prepare_test.py SimpleLoadTest.jmx influx_config_localhost.json --patch-jmx` — в консоли будет **Test Run ID**; он же в `test_run_id.txt` и в JMX. |
| 2 | Запуск теста в **JMeter**. |
| 3 | `python check_load_profile.py test_YYYYMMDD_HHMMSS influx_config_localhost.json` — подставьте **тот же** ID, что на шаге 1. |

**B2 (всё раздельно):** три разных скрипта + ручной `test_run` в JMeter (если не патчите JMX).

| № | Команда / действие |
|---|---------------------|
| 0 | *(опционально)* `python init_influxdb.py influx_config_localhost.json` |
| 1 | `python parse_jmx_profile.py SimpleLoadTest.jmx` → появится **`SimpleLoadTest.profile.json`**. |
| 2 | Придумать ID, например `test_20260411_153045`, записать **одной строкой** в файл **`test_run_id.txt`** (удобно для контроля и для `report`, если решите им воспользоваться). |
| 3 | `python send_profile_to_influx.py SimpleLoadTest.profile.json test_20260411_153045 influx_config_localhost.json` — второй аргумент = **тот же** ID. |
| 4 | В JMeter: **User Defined Variables** → **`test_run`** = тот же ID *(если не использовали `prepare_test.py --patch-jmx`).* |
| 5 | Запуск теста в **JMeter**. |
| 6 | `python check_load_profile.py test_20260411_153045 influx_config_localhost.json` — снова **тот же** ID. |

В B2 отчёт — через **`check_load_profile.py`** с явным ID; либо, если `test_run_id.txt` не меняли, сработает и `python jmeter_load_pipeline.py report --config influx_config_localhost.json`.

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

### Пример HTML-отчёта

Так может выглядеть итоговый отчёт `check_load_profile` (скрин из репозитория):

![Пример отчёта проверки профиля нагрузки](docs/images/load-profile-check-sample.png)
