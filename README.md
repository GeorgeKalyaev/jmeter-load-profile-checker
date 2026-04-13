# Профиль нагрузки JMeter + InfluxDB

[English](README.en.md)

Краткая инструкция: что запускать и в каком порядке.

### Настройка Influx — отдельный файл, по сути один раз

Подключение к InfluxDB (`influx_url`, `influx_db`, `influx_user`, `influx_pass`, при необходимости `aggregation_interval`) задаётся **в одном JSON-файле**: скопируйте `influx_config.example.json` под своим именем (например `influx_config.local.json`), заполните значения **под ваш стенд** и храните файл локально (**не коммитьте** пароли). Дальше во всех командах указываете его через `--config путь/к/файлу.json`. **Менять исходный код Python не требуется** — скрипты читают параметры из этого JSON.

Те же URL и учётные данные должны совпадать с тем, что в **Backend Listener** в JMX (и, если используете, с переменными Influx для **StageTracker** в плане).

---

## Вариант A и B: в чём разница

**Общее:** ни один скрипт **не запускает JMeter**. InfluxDB 1.x, база и пользователь — **ваша** инфраструктура (по документации Influx); в репозитории только JSON для подключения.

| | **Вариант A** | **Вариант B** |
|---|----------------|---------------|
| **Суть** | Один вход: **`jmeter_load_pipeline.py`** (`prepare` / `report`) | Те же шаги **отдельными** командами: `parse_jmx_profile` → `send_profile_to_influx` → `check_load_profile` |
| **Конфиг Influx** | Везде **`--config путь.json`** | У `send_profile_to_influx` и `check_load_profile` конфиг — **последний позиционный** аргумент (без `--config`) |
| **`test_run` в JMX** | После **`prepare`** подставляется **автоматически** | Задаёте **вручную** в User Defined Variables (если не копируете готовый JMX после A) |
| **Когда удобен** | Обычная работа | Отладка отдельного шага, свой CI без оркестратора |

В примерах: план **`SimpleLoadTest.jmx`**, конфиг **`influx_config_localhost.json`**. Команды — из корня репозитория.

---

### Вариант A — по шагам (`jmeter_load_pipeline.py`)

**Шаг 1 — подготовка прогона**

```text
python jmeter_load_pipeline.py prepare SimpleLoadTest.jmx --config influx_config_localhost.json
```

Внутри по порядку:

1. **`parse_jmx_profile.py`** → **`SimpleLoadTest.profile.json`**. Для UTG ступени из симуляции (`utg_schedule.py`), поле `utg_schedule_mode`; иначе запасной режим «одна строка UTG = одна ступень».
2. Новый **`test_run`** → файл **`test_run_id.txt`** (одна строка).
3. **`send_profile_to_influx.py`** — профиль в Influx (тот же JSON, что в `--config`).
4. Запись **`test_run`** в **User Defined Variables** в JMX на диске — открываете этот же файл в JMeter, руками ID не вводите.

**Шаг 2 — нагрузочный тест в JMeter (только вы)**

Запуск плана (GUI или `jmeter.bat -n -t ...`). Убедитесь:

- **JSR223 Listener** + **`StageTracker.groovy`** на **Test Plan**.
- **Backend Listener** → тот же Influx (URL, БД, учётка), что в JSON.

**Шаг 3 — отчёт после теста**

```text
python jmeter_load_pipeline.py report --config influx_config_localhost.json
```

Берётся **`test_run`** из **`test_run_id.txt`**, вызывается **`check_load_profile.py`**, появляются **`load_profile_check_<test_run>.html`** и **`.json`**.

**Итого:** **`prepare` → JMeter → `report`**.

---

### Вариант B — по шагам (без оркестратора)

Имеет смысл, если нужно вызвать только один скрипт или собрать цепочку в своём окружении.

**Шаг 1** — JMX → профиль:

```text
python parse_jmx_profile.py SimpleLoadTest.jmx
```

Результат: **`SimpleLoadTest.profile.json`** (рядом **`sampler_filter.json`**).

**Шаг 2** — задать **`test_run`**: придумайте ID (например `test_20260411_153045`) и при необходимости запишите **одной строкой** в **`test_run_id.txt`** (удобно для шага 5a ниже).

**Шаг 3** — отправка профиля в Influx:

```text
python send_profile_to_influx.py SimpleLoadTest.profile.json test_20260411_153045 influx_config_localhost.json
```

Аргументы: **файл профиля**, **`test_run`**, **JSON-конфиг** (последний — путь к Influx).

**Шаг 4** — в JMeter: **User Defined Variables** → переменная **`test_run`** = **тот же** ID. Сохраните JMX. (Если перед этим уже делали **вариант A `prepare`** для этого файла — шаг можно пропустить.)

**Шаг 5** — запуск нагрузки в JMeter (как в варианте A).

**Шаг 6** — отчёт, **один из двух способов**:

- Явный ID:

```text
python check_load_profile.py test_20260411_153045 influx_config_localhost.json
```

- Или, если в **`test_run_id.txt`** лежит тот же ID:

```text
python jmeter_load_pipeline.py report --config influx_config_localhost.json
```

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
