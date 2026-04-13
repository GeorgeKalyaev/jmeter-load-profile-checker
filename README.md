# Профиль нагрузки JMeter + InfluxDB

[English](README.en.md)

Краткая инструкция: что запускать и в каком порядке.

### Настройка Influx — отдельный файл, по сути один раз

Подключение к InfluxDB (`influx_url`, `influx_db`, `influx_user`, `influx_pass`, при необходимости `aggregation_interval`) задаётся **в одном JSON-файле**: скопируйте `influx_config.example.json` под своим именем (например `influx_config.local.json`), заполните значения **под ваш стенд** и храните файл локально (**не коммитьте** пароли). Дальше во всех командах указываете его через `--config путь/к/файлу.json`. **Менять исходный код Python не требуется** — скрипты читают параметры из этого JSON.

Те же URL и учётные данные должны совпадать с тем, что в **Backend Listener** в JMX (и, если используете, с переменными Influx для **StageTracker** в плане).

---

## Как пользоваться

**Важно:** ни один скрипт **не запускает JMeter за вас**. План всегда запускается вручную (GUI или `jmeter.bat -n -t ...`).

**InfluxDB:** подготовьте экземпляр InfluxDB 1.x, базу и пользователя **самостоятельно** (по официальной документации Influx). В репозитории **нет** скриптов создания БД — только JSON-конфиг подключения для Python и JMeter.

В примерах ниже план — **`SimpleLoadTest.jmx`**, конфиг — **`influx_config_localhost.json`**. Для своего стенда подставьте свой JMX и свой JSON (копия `influx_config.example.json`). Команды выполняйте из корня репозитория.

### Основной сценарий (`jmeter_load_pipeline.py`)

**Шаг 1 — подготовка**

```text
python jmeter_load_pipeline.py prepare SimpleLoadTest.jmx --config influx_config_localhost.json
```

Последовательно выполняется:

1. **`parse_jmx_profile.py`** → рядом с планом **`SimpleLoadTest.profile.json`**. Для **Ultimate Thread Group** ступени строятся **симуляцией** суммарных потоков (`utg_schedule.py`); в профиле есть `utg_schedule_mode`. Если симуляция не дала ступеней — запасной режим «одна строка UTG = одна ступень».
2. Генерация **`test_run`**, запись в **`test_run_id.txt`** (одна строка).
3. **`send_profile_to_influx.py`** — профиль в Influx (тот же JSON, что в `--config`).
4. Подстановка **`test_run`** в **User Defined Variables** в файле JMX — вручную ID в JMeter вписывать не нужно, если открываете этот же JMX.

**Шаг 2 — нагрузка в JMeter**

- **JSR223 Listener** + **`StageTracker.groovy`** на уровне **Test Plan**.
- **Backend Listener** — тот же Influx, что в JSON (URL, БД, учётка).

**Шаг 3 — отчёт**

```text
python jmeter_load_pipeline.py report --config influx_config_localhost.json
```

Читается **`test_run`** из **`test_run_id.txt`**, вызывается **`check_load_profile.py`**, создаются **`load_profile_check_<test_run>.html`** и **`.json`**.

**Порядок:** **`prepare` → JMeter → `report`**.

### Ручной режим (без оркестратора)

Для отладки те же действия по отдельности. У **`send_profile_to_influx`** и **`check_load_profile`** путь к конфигу Influx — **последний позиционный** аргумент (у `jmeter_load_pipeline` конфиг задаётся через **`--config`**).

1. `python parse_jmx_profile.py SimpleLoadTest.jmx` → **`SimpleLoadTest.profile.json`** (учитывается **`sampler_filter.json`**).
2. Уникальный **`test_run`** и строка в **`test_run_id.txt`** (если хотите потом вызывать `report` без явного ID).
3. `python send_profile_to_influx.py SimpleLoadTest.profile.json test_20260411_153045 influx_config_localhost.json`
4. В JMX: **User Defined Variables** → **`test_run`** = тот же ID (если вы **не** пользовались `prepare`, который уже прописал переменную).
5. Запуск JMeter.
6. `python check_load_profile.py test_20260411_153045 influx_config_localhost.json`

Если **`test_run_id.txt`** содержит тот же ID, для шага 6 достаточно:  
`python jmeter_load_pipeline.py report --config influx_config_localhost.json`.

---

## На что обратить внимание

- Один и тот же **`test_run`** при отправке профиля, в **User Defined Variables** в JMX и при отчёте.
- `aggregation_interval` в JSON должен совпадать с **Sending interval** у **Backend Listener** в JMeter и с тем, как вы считаете RPS в Grafana (например `sum("count") / N` → `N`). В `SimpleLoadTest.jmx` интервал в JMX не задан явно — у Influx Backend Listener обычно **5 с** по умолчанию; в `influx_config_localhost.json` стоит `5.0`.
- Для локального Influx на `localhost` можно использовать `influx_config_localhost.json` (учётка/пароль по умолчанию только для dev).

---

## Логика отчёта: что считается «плато» и что сознательно отбрасывается

Ниже — **как устроен** `check_load_profile` и почему длительность «ступени» в HTML **не обязана** совпадать с колонкой **Hold** в Ultimate Thread Group построчно.

### 1. Ultimate Thread Group и несколько строк

В типичной «лесенке» каждая **строка** UTG **добавляет** потоки к уже запущенным. Итоговая нагрузка по времени — это **сумма** вкладов всех строк (см. модуль `utg_schedule.py`). **Бизнес-ступень** профиля — это интервал, где **суммарное** число активных потоков **не меняется** (нет ramp-up/ramp-down по сумме), а не «одна строка таблицы = одна ступень».

### 2. Окно плато `[plateau_start_s, plateau_end_s)`

Для каждой такой ступени в `*.profile.json` задаётся полуинтервал времени **в секундах от старта теста**:

- **Начало** — когда суммарная нагрузка уже вышла на **ровный** участок после ramp-up **к этой** сумме потоков.
- **Конец** — момент **до** начала следующего изменения суммы (например, до старта ramp-up следующей строки). Интервал в отчёте обычно трактуется как **`[start, end)`** — правая граница **не включается**.

**Интервалы ramp-up / ramp-down между ступенями в это окно не попадают.** Например, 20 с разгона ко второй «волне» — отдельный отрезок; он не считается ни плато предыдущей ступени, ни плато следующей.

### 3. Что считает `check_load_profile.py` внутри плато

Для каждой Thread Group и каждой ступени из профиля в Influx (measurement `jmeter`, теги вроде `test_run`, `transaction` / имя сэмплера) за **только этот** временной отрезок:

| Величина | Смысл |
|----------|--------|
| **Целевой RPS** | Из JMX: `(Constant Throughput Timer в RPM × число потоков этой TG) / 60` — ожидание **для этой TG**. |
| **Фактический RPS** | `число успешных запросов (statut = 'ok') / длительность плато в секундах`. Ошибки и не-ok в числитель RPS **не входят**. |
| **Отклонение %** | `|факт − цель| / цель × 100%` по **этой TG**; порог PASS/FAIL в отчёте обычно 10%. |
| **Ожидаемое число запросов** | `целевой RPS × (plateau_end_s − plateau_start_s)` — только плато, без рамп между ступенями. |

Длительность плато в секундах — это **`end − start`**, а не сырой Hold из одной строки UTG, если расписание из нескольких строк даёт другую геометрию (см. выше).

### 4. События ступеней в Influx

`StageTracker.groovy` пишет служебные события (например, смена ступени) для привязки времени; отчёт может использовать их для уточнения **начала** теста. **Границы плато для сверки RPS** берутся из **профиля** (распарсенный JMX + симуляция UTG), а не «нарезаются по глазу» из графика.

### 5. Что не коммитить в публичный репозиторий

Файлы с реальными URL стендов, паролями Influx и токенами держите локально (например, копия `influx_config.example.json` под именем вроде `influx_config.local.json` в `.gitignore`). В репозитории в качестве примеров — **`influx_config.example.json`** (шаблон) и **`influx_config_localhost.json`** (локальный dev).

---

## Состав репозитория

| Файл | Назначение |
|------|------------|
| `jmeter_load_pipeline.py` | Точка входа: `prepare` / `report` (в т.ч. запись `test_run` в JMX) |
| `parse_jmx_profile.py` | JMX → `*.profile.json` |
| `utg_schedule.py` | Симуляция UTG: интервалы «ровного» суммарного числа потоков |
| `send_profile_to_influx.py` | Профиль в Influx |
| `check_load_profile.py` | Отчёт HTML/JSON по `test_run` |
| `StageTracker.groovy` | Ступени → события в Influx (JSR223 Listener) |
| `sampler_filter.json` | Префиксы имён сэмплеров для парсера (по умолчанию `HTTP`) |
| `influx_config.example.json` | Шаблон конфигурации |
| `influx_config_localhost.json` | Пример для локального Influx |
| `SimpleLoadTest.jmx` | Пример плана (3×UTG, Backend Listener, StageTracker) |
| `docs/images/load-profile-check-sample.png` | Иллюстрация для README |

### Пример HTML-отчёта

Так может выглядеть итоговый отчёт `check_load_profile` (скрин из репозитория):

![Пример отчёта проверки профиля нагрузки](docs/images/load-profile-check-sample.png)
