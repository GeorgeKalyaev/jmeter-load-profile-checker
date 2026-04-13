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

1. Разбор JMX → **`parse_jmx_profile.py`** → рядом появится **`SimpleLoadTest.profile.json`** (имя совпадает с планом). Для **Ultimate Thread Group** ступени профиля строятся **симуляцией** расписания (сумма добавляемых потоков по строкам, интервалы без ramp), чтобы окна плато совпадали с «ровными» участками на графике TG; в JSON у группы есть поле `utg_schedule_mode`. Если симуляция не дала ступеней, используется запасной вариант «одна строка = одна ступень».
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

### Вариант B — по шагам (без `jmeter_load_pipeline`, каждую команду вводите сами)

Здесь нет оркестратора: вы сами запускаете нужные `.py` по очереди. Конфиг Influx по-прежнему **один JSON** (`--config` нет у всех скриптов — путь к конфигу передаётся **вторым/последним аргументом**, см. ниже).

---

#### B1 — короче: один скрипт на подготовку, потом JMeter, потом отчёт

**Шаг 0 (опционально)** — то же, что в варианте A:  
`python init_influxdb.py influx_config_localhost.json`

**Шаг 1 — подготовка одной командой**  

```text
python prepare_test.py SimpleLoadTest.jmx influx_config_localhost.json --patch-jmx
```

Что сделает скрипт **за вас** (внутри — те же действия, что и у `prepare`, но без `jmeter_load_pipeline`):

1. Запуск **`parse_jmx_profile.py`** → рядом с JMX появится **`SimpleLoadTest.profile.json`**.
2. Генерация **`test_run`**, запись в **`test_run_id.txt`** и вывод ID в консоль.
3. Запуск **`send_profile_to_influx.py`** с этим ID и вашим JSON-конфигом.
4. С флагом **`--patch-jmx`**: подстановка того же **`test_run`** в **User Defined Variables** в файле **JMX** (вручную ID вписывать не нужно).

Без **`--patch-jmx`** профиль и Influx всё равно будут готовы, но переменную **`test_run`** в JMeter нужно будет выставить **вручную** (как в B2, шаг 4).

**Шаг 2 — нагрузка в JMeter (только вы)**  
Запуск плана, как в варианте A: **StageTracker** на уровне **Test Plan**, **Backend Listener** → тот же Influx, что в JSON.

**Шаг 3 — отчёт**  

```text
python check_load_profile.py test_YYYYMMDD_HHMMSS influx_config_localhost.json
```

Подставьте **реальный** ID с шага 1 (тот, что в консоли и в `test_run_id.txt`). На выходе — **`load_profile_check_<test_run>.html`** и **`.json`**.

*Альтернатива:* если **`test_run_id.txt`** вы не трогали после шага 1, можно вместо явного ID вызвать  
`python jmeter_load_pipeline.py report --config influx_config_localhost.json` — он прочитает ID из файла.

---

#### B2 — всё раздельно: каждый скрипт по очереди, ID в JMeter вручную

Используйте, если хотите полностью контролировать каждый вызов (или отлаживать по одному шагу).

**Шаг 0 (опционально)**  
`python init_influxdb.py influx_config_localhost.json`

**Шаг 1 — только разбор JMX в профиль**  

```text
python parse_jmx_profile.py SimpleLoadTest.jmx
```

Результат: файл **`SimpleLoadTest.profile.json`** (имя = имя JMX с суффиксом `.profile.json`). Учитывается **`sampler_filter.json`** рядом со скриптом.

**Шаг 2 — ID прогона и файл `test_run_id.txt` (вручную)**  
Придумайте уникальный ID, например `test_20260411_153045`, и сохраните его **одной строкой** в файле **`test_run_id.txt`** в папке репозитория (удобно для контроля и для `jmeter_load_pipeline report`, если позже им воспользуетесь).

**Шаг 3 — отправка профиля в Influx**  

```text
python send_profile_to_influx.py SimpleLoadTest.profile.json test_20260411_153045 influx_config_localhost.json
```

Аргументы по порядку: **файл профиля**, **`test_run`**, **JSON-конфиг Influx**. Второй аргумент должен **совпадать** с тем, что в `test_run_id.txt` (если используете оба).

**Шаг 4 — подстановка `test_run` в JMeter (вручную)**  
Откройте **`SimpleLoadTest.jmx`** в JMeter: **Test Plan** → **User Defined Variables** → переменная **`test_run`** = **тот же** ID (например `test_20260411_153045`). Сохраните JMX.  
Иначе StageTracker и отчёт не сойдутся с данными в Influx.

**Шаг 5 — нагрузка в JMeter (только вы)**  
Запуск теста; проверки те же: StageTracker на **Test Plan**, Backend Listener.

**Шаг 6 — отчёт**  

```text
python check_load_profile.py test_20260411_153045 influx_config_localhost.json
```

Первый аргумент — снова **тот же** `test_run`. Файлы отчёта — как в B1.

*Альтернатива:* если **`test_run_id.txt`** содержит тот же ID и вы его не меняли — сработает  
`python jmeter_load_pipeline.py report --config influx_config_localhost.json`.

---

## На что обратить внимание

- Один и тот же **`test_run`** при отправке профиля, в **User Defined Variables** в JMX и при отчёте.
- `aggregation_interval` в JSON должен совпадать с **Sending interval** у **Backend Listener** в JMeter и с тем, как вы считаете RPS в Grafana (например `sum("count") / N` → `N`). В `SimpleLoadTest.jmx` интервал в JMX не задан явно — у Influx Backend Listener обычно **5 с** по умолчанию; в `influx_config_localhost.json` стоит `5.0`.
- Для локального Influx на `localhost` можно использовать `influx_config_localhost.json` (учётка/пароль по умолчанию только для dev).

---

## Логика проверки на GitHub: что считается «плато» и что сознательно отбрасывается

Этот раздел описывает **как устроен отчёт** `check_load_profile` и почему длительность «ступени» в HTML **не обязана** совпадать с колонкой **Hold** в Ultimate Thread Group построчно.

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

Файлы с реальными URL стендов, паролями Influx и токенами держите локально (например, копия `influx_config.example.json` под именем вроде `influx_config.local.json` и занесённая в `.gitignore`). В репозитории остаются только **`influx_config.example.json`** и пример **`influx_config_localhost.json`** для локальной разработки.

---

## Состав репозитория

| Файл | Назначение |
|------|------------|
| `jmeter_load_pipeline.py` | Точка входа: `prepare` / `report` |
| `prepare_test.py` | Подготовка прогона + `--patch-jmx` |
| `parse_jmx_profile.py` | JMX → `*.profile.json` |
| `utg_schedule.py` | Симуляция UTG: интервалы «ровного» суммарного числа потоков |
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
