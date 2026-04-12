# Профиль нагрузки JMeter + InfluxDB

[English](README.en.md)

Краткая инструкция: что запускать и в каком порядке.

### Настройка Influx — отдельный файл, по сути один раз

Подключение к InfluxDB (`influx_url`, `influx_db`, `influx_user`, `influx_pass`, при необходимости `aggregation_interval`) задаётся **в одном JSON-файле**: скопируйте `influx_config.example.json` под своим именем (например `influx_config.local.json`), заполните значения **под ваш стенд** и храните файл локально (**не коммитьте** пароли). Дальше во всех командах указываете его через `--config путь/к/файлу.json`. **Менять исходный код Python не требуется** — скрипты читают параметры из этого JSON.

Те же URL и учётные данные должны совпадать с тем, что в **Backend Listener** в JMX (и, если используете, с переменными Influx для **StageTracker** в плане).

---

## Вариант A и B: в чём разница

**Важно:** ни один скрипт **не запускает JMeter за вас**. Всегда вручную: открыть план в JMeter (или `jmeter.bat -n -t ...`) и дождаться конца теста.

- **Вариант A** — два раза вызываете **Python**: сначала `prepare`, после теста `report`. Между ними только JMeter.
- **Вариант B** — подготовку и отчёт делаете **отдельными командами** (или один `prepare_test.py` вместо цепочки parse → send).

В примерах ниже план — **`SimpleLoadTest.jmx`** (лежит в репозитории), конфиг — **`influx_config_localhost.json`** (локальный пример). Для своего стенда замените конфиг на свой JSON (например копию `influx_config.example.json` с вашими URL и паролем). Команды выполняйте из папки репозитория (`cd` туда).

### Вариант A — по шагам (`jmeter_load_pipeline.py`)

Ниже — что вводить руками и что происходит «само» внутри оркестратора.

**Шаг 0 (опционально, один раз на новой БД)**  
`python init_influxdb.py influx_config_localhost.json` — подготовка Influx 1.x под параметры из конфига (если БД/пользователь уже есть у админов, шаг можно пропустить).

**Шаг 1 — подготовка: одна команда, дальше цепочка Python**  
Из папки репозитория:

```text
python jmeter_load_pipeline.py prepare SimpleLoadTest.jmx --config influx_config_localhost.json
```

Вместо `SimpleLoadTest.jmx` и `influx_config_localhost.json` подставьте свой план и свой JSON, если работаете не с примером.

Что сделает скрипт **за вас** (последовательно вызываются другие `.py` из репозитория):

1. Разбор JMX → **`parse_jmx_profile.py`** → рядом появится **`SimpleLoadTest.profile.json`** (имя совпадает с планом).
2. Новый идентификатор прогона **`test_run`** (например `test_20260415_143022`) — запись в файл **`test_run_id.txt`** одной строкой.
3. Отправка профиля в Influx → **`send_profile_to_influx.py`** (подключение берётся из того же JSON по `--config`).
4. Запись того же **`test_run`** в **User Defined Variables** вашего **JMX** — переменная `test_run` обновляется в файле плана; **вручную вписывать ID в JMeter после prepare не нужно**, если открываете именно этот же файл.

**Шаг 2 — нагрузка в JMeter (только вы)**  
Запустите тест: GUI или `jmeter.bat -n -t SimpleLoadTest.jmx ...`. Скрипты JMeter **не стартуются** из Python.

Проверьте в плане:

- **JSR223 Listener** с файлом **`StageTracker.groovy`** стоит на уровне **Test Plan** (чтобы слушатель видел все Thread Group).
- **Backend Listener** пишет в тот же Influx, что в вашем JSON (URL, БД, учётка).

**Шаг 3 — отчёт после окончания теста**  

```text
python jmeter_load_pipeline.py report --config influx_config_localhost.json
```

Что произойдёт:

- из **`test_run_id.txt`** читается последний **`test_run`**;
- вызывается **`check_load_profile.py`**: сверка целевого профиля и фактических метрик в Influx;
- в каталоге репозитория появляются **`load_profile_check_<test_run>.html`** и **`load_profile_check_<test_run>.json`**.

**Итого порядок:** **`prepare` → JMeter → `report`**.

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
