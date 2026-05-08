# JMeter Load Profile Checker

[English](README.en.md) · [Короткая версия](README.ru.md)

Проект сравнивает **ожидаемый профиль нагрузки** (из JMX/UTG) и **фактические метрики** в InfluxDB.

## Главное правило

Отчёт считает только **чистые плато**:

- учитываются интервалы, где суммарная нагрузка стабильна;
- **ramp-up** и **ramp-down** в расчёт ступени не входят;
- отклонения считаются только внутри окна `[plateau_start_s, plateau_end_s)`.

Это ключевая логика проекта: сравниваем "профиль vs факт" только на стабильной нагрузке.

---

## Быстрый старт (рекомендуемый путь)

### 0) Подготовьте конфиг Influx

Скопируйте `influx_config.example.json` в локальный файл, например `influx_config.local.json`, заполните:
`influx_url`, `influx_db`, `influx_user`, `influx_pass` (опционально `aggregation_interval`).

Используйте этот файл во всех командах через `--config`.

### 1) Подготовка прогона

```text
python jmeter_load_pipeline.py prepare SimpleLoadTest.jmx --config influx_config_localhost.json
```

Что делает `prepare`:

1. строит `SimpleLoadTest.profile.json` из JMX;
2. генерирует `test_run` и пишет в `test_run_id.txt`;
3. подставляет `test_run` в JMX (UDV);
4. отправляет профиль в Influx (`load_profile`, `load_profile_samplers`).

### 2) Запуск нагрузки

Запустите JMeter план вручную (GUI или `jmeter.bat -n -t ...`).

Проверьте в плане:

- `StageTracker.groovy` подключен на уровне Test Plan;
- Backend Listener пишет в тот же Influx, что указан в JSON.

### 3) Построение отчёта

```text
python jmeter_load_pipeline.py report --config influx_config_localhost.json
```

Результат: `load_profile_check_<test_run>.html` и `.json`.

---

## Как устроен расчёт в отчёте

Для каждой Thread Group и каждой ступени:

- **Target RPS**: `(CTT RPM * threads_on_stage) / 60`;
- **Fact. RPS OK**: `ok_requests / plateau_duration_seconds`;
- **Fact. RPS ALL**: `(ok_requests + ko_requests) / plateau_duration_seconds`;
- **Deviation OK %**: `abs(actual_ok - target) / target * 100`;
- **Deviation ALL %**: `abs(actual_all - target) / target * 100`.

Где:

- `ok_requests` — запросы с `statut='ok'`;
- `ko_requests` — запросы с `statut='ko'`.

Статус ступени `PASS/FAIL` для проверки попадания в профиль считается по **Deviation ALL %**.
Дополнительно в таблице есть информационный `Статус запросов` по отклонению количества запросов
(ожидаемое vs фактическое): `PASS <= 5%`, `WARN <= 10%`, `FAIL > 10%`.

### Ранний стоп теста

Если тест остановился раньше и в `jmeter` есть тег `test_run`, отчёт умеет:

- помечать недостигнутые ступени как `SKIP`;
- считать оборванную ступень как `PARTIAL` (по укороченному окну).

Без `test_run` в `jmeter` ранний стоп корректно учесть нельзя.

---

## Multi-pod / multi-runner

Один `test_run`, несколько инжекторов (pod/VM):

- для per-runner аналитики нужен тег `runner` в `jmeter`;
- отчёт строит блок таблиц по каждому `runner`, затем `Cluster Summary`.

### Fallback режим

Если в `jmeter` нет тега `runner`, используется `jmeter_runner_meta` (heartbeat из `StageTracker.groovy`):

- определяется число раннеров `N`;
- target RPS масштабируется на `N`;
- per-runner таблицы недоступны, доступна только агрегированная сводка.

---

## Обязательные соглашения по JMX

Чтобы данные сходились без ручных правок:

1. `test_run` в UDV должен совпадать между prepare / запуском / отчётом.
2. Для бизнес-метрики используйте Transaction Controller вида `_UC_*`.
3. В `sampler_filter.json` должны быть нужные префиксы sampler-имен.
4. Для ступеней используется только `UltimateThreadGroup`.
5. `aggregation_interval` в JSON должен соответствовать интервалу Backend Listener.

---

## Компоненты репозитория

| Файл | Назначение |
|---|---|
| `jmeter_load_pipeline.py` | Оркестратор `prepare` / `report` |
| `parse_jmx_profile.py` | JMX -> `*.profile.json` |
| `utg_schedule.py` | Выделение чистых плато UTG |
| `send_profile_to_influx.py` | Отправка профиля в Influx |
| `check_load_profile.py` | Генерация HTML/JSON отчёта |
| `StageTracker.groovy` | Stage events + runner heartbeat |
| `SimpleLoadTest.jmx` | Эталонный пример плана |

---

## Диагностика частых проблем

- `partial write: points beyond retention policy dropped`:
  скрипт/данные используют некорректные timestamps для вашего retention.
- `field type conflict`:
  в measurement ранее зафиксирован другой тип поля.
- "Отклонение 100% при 2+ подах":
  обычно нет тега `runner` в `jmeter`, проверьте fallback и event tags.
- "Ступени разбились странно":
  убедитесь, что анализируется UTG и сравнение идёт именно по плато.

---

## Пример отчёта

<a href="docs/images/load-profile-check-full.png"><img src="docs/images/load-profile-check-full.png" alt="Пример отчёта проверки профиля нагрузки" width="1200" /></a>

