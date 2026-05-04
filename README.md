# Профиль нагрузки JMeter + InfluxDB

[English](README.en.md)

Краткая инструкция: что запускать и в каком порядке.

### Настройка Influx — отдельный файл, по сути один раз

Подключение к InfluxDB (`influx_url`, `influx_db`, `influx_user`, `influx_pass`, при необходимости `aggregation_interval`) задаётся **в одном JSON-файле**: скопируйте `influx_config.example.json` под своим именем (например `influx_config.local.json`), заполните значения **под ваш стенд** и храните файл локально (**не коммитьте** пароли). Дальше во всех командах указываете его через `--config путь/к/файлу.json`. **Менять исходный код Python не требуется** — скрипты читают параметры из этого JSON.

Те же URL и учётные данные должны совпадать с тем, что в **Backend Listener** в JMX (и, если используете, с переменными Influx для **StageTracker** в плане).

---

## Структура JMX и соглашения по именам

Чтобы **`parse_jmx_profile`**, Influx и **`check_load_profile`** сходились без ручной правки:

1. **`test_run`** — в **Test Plan → User Defined Variables**. После **`jmeter_load_pipeline.py prepare`** значение пишется в файл JMX автоматически; в GUI откройте план заново, если правили его снаружи.

2. **Имя Thread Group** (`testname`, например `UC_01_Group_List`) — логическое имя группы в отчёте. Внутри этой TG по возможности оберните сценарий в **Transaction Controller**, имя которого = **подчёркивание + то же имя, что у TG**:  
   TG `UC_01_Group_List` → Transaction Controller **`_UC_01_Group_List`**.  
   В Influx у Backend Listener тег **`transaction`** часто совпадает с именем транзакции; ведущий **`_`** — типичное соглашение JMeter для «сэмпла транзакции», чтобы отличать от отдельных HTTP-запросов.  
   Если Transaction Controller **нет**, парсер для совместимости всё равно добавит в профиль и имя TG, и вариант **`_{имя_TG}`** для запросов к `jmeter`.

3. **Имена HTTP Sampler** — должны начинаться с одного из префиксов из **`sampler_filter.json`** (по умолчанию только **`HTTP`**, например **`HTTP Request …`**). Иначе сэмплер не попадёт в `*.profile.json` и в сводку по SLA в отчёте. Другие типы (JDBC и т.д.) — добавьте префикс в **`allowed_sampler_prefixes`** в том же JSON.

4. **StageTracker.groovy** — на уровне **Test Plan** (скрипт из репозитория, путь к файлу в JSR223 должен быть доступен). **Backend Listener** (InfluxDB Backend Listener) — тот же Influx (URL, БД, учётка), что в JSON конфиге Python. Тег **`test_run`** в listener должен совпадать с переменной **`${test_run}`** в плане (после `prepare` она уже в JMX).

5. **Ultimate Thread Group** — ступени нагрузки в `*.profile.json` сейчас собираются **только** для **`kg.apc.jmeter.threads.UltimateThreadGroup`**. Обычный **Thread Group** этим парсером в профиль **не** превращается (в примере `SimpleLoadTest.jmx` классические TG отключены). Для «лесенки» используйте UTG; симуляция суммарных потоков — в `utg_schedule.py`.

6. **Constant Throughput Timer** — целевой RPS в отчёте считается как **(RPM × число потоков на ступени) / 60**, в духе режима «на поток» (см. комментарии в `send_profile_to_influx.py`). Если у вас другой calcMode CTT, цифра «целевой RPS» может не совпасть с фактом — тогда править формулу или план.

7. **Несколько Transaction Controller** в одной TG — парсер собирает **все** их имена в `transaction_names`; отчёт строит фильтр по Influx по этому списку. Имена должны быть согласованы с тем, что реально попадает в тег **`transaction`** у Backend Listener (часто это пункт2 с префиксом **`_`**).

8. **Module Controller** внутри UTG (ссылка на фрагмент плана) — в `transaction_names` добавляется **последний сегмент** `node_path` (например **`_UC_01_Check_List`**). Иначе при пустом дереве UTG парсер оставлял бы только `UC_01_Group_List` / `_UC_01_Group_List`, а в Influx были бы сэмплы с другим `transaction` → в отчёте **0 запросов** и 100% отклонение.

**Цепочка данных (кратко):**  
`prepare` → в Influx уходят **`load_profile`** и **`load_profile_samplers`** (ожидаемый профиль). Запуск JMeter → **`jmeter`** (метрики сэмплов) и строки **`load_stage_change`** из **StageTracker** (смена ступеней).  
`report` читает профиль из Influx и сверяет с **`jmeter`** по `test_run` и `transaction` / имени TG.

### Несколько подов (тег `runner` в Influx)

Один **`test_run`**, несколько экземпляров JMeter (например Pod в Kubernetes): задайте тег **`runner`** в **Backend Listener** (`eventTags`) и в строках **`load_stage_change`** из **StageTracker** — см. **`StageTracker.groovy`** и пример **`SimpleLoadTest.jmx`** (hostname пода через `HOSTNAME` / свойство `runner`).

**`check_load_profile.py`** сам находит все значения **`runner`** для данного `test_run` в **`jmeter`** и строит **отдельный блок таблиц на каждый под**, затем **«Сводка кластера»**: целевой RPS = профиль одного инстанса × **N**, факт — сумма метрик по всем подам. Число **N** не фиксировано (2, 3, 4, … — сколько уникальных `runner` в данных).

**Пока не делаем (на будущее):** своё время старта теста **на каждый** `runner`; сейчас для всех подов используется **одно** общее `test_start_time_ns`. При сильном рассинхроне старта у отстающего пода отклонение RPS на границах ступеней может быть чуть выше.

Если в **`jmeter`** нет тега **`runner`**, отчёт ведёт себя как для **одного** источника (как раньше).

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
3. Запись **`test_run`** в **User Defined Variables** в JMX на диске — открываете этот же файл в JMeter, руками ID не вводите.
4. **`send_profile_to_influx.py`** — профиль в Influx (тот же JSON, что в `--config`).

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

- Если Influx при **`send_profile`** отвечает **`partial write: points beyond retention policy dropped`**, на стенде обычно жёсткий retention: старые версии записывали метки времени как «секунды сценария» (1970 год). Актуальный **`send_profile_to_influx.py`** ставит точкам время около текущего момента.
- Если видите **`field type conflict`** (например `hold_s` integer vs float) — в БД уже зафиксирован тип поля от старых записей; актуальный скрипт шлёт те же поля как **float**, чтобы совпасть с типичным существующим схемой.
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
