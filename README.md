# Профиль нагрузки JMeter + InfluxDB

Краткая инструкция: что запускать на работе и в каком порядке. Рабочие URL и пароли держите в **локальном** JSON (скопируйте из `influx_config.example.json`), не коммитьте.

[English (short)](#english-short)

---

## Запустить на работе можно?

Да, если есть Python 3, JMeter 5.x с плагином **Ultimate Thread Group** (и всё, что требует ваш JMX), и доступ к **InfluxDB 1.x** с машины, где идут скрипты и откуда JMeter пишет в **Backend Listener**.

---

## Один скрипт или по шагам?

Логика одна и та же; отличается только обёртка.

### Вариант A — один вход (удобно на работе)

| Шаг | Действие |
|-----|----------|
| 1 | Скопировать `influx_config.example.json` → свой файл (например `influx_config.local.json`), подставить свои значения. Файл с паролем **не коммитить**. |
| 2 | Один раз при новой инсталляции Influx (если админы ещё не создали БД/пользователя): `python init_influxdb.py ваш_конфиг.json` |
| 3 | **Перед каждым прогоном:** `python jmeter_load_pipeline.py prepare ВашПлан.jmx --config ваш_конфиг.json` — парсинг JMX, профиль в Influx, новый `test_run`, запись `test_run` в JMX. |
| 4 | Запуск нагрузки в **JMeter**. Проверьте: JSR223 Listener с `StageTracker.groovy` на уровне **Test Plan**, Backend Listener смотрит в тот же Influx, что и конфиг. |
| 5 | **После теста:** `python jmeter_load_pipeline.py report --config ваш_конфиг.json` — HTML/JSON рядом со скриптами (`test_run` из `test_run_id.txt`). |

### Вариант B — те же шаги вручную

1. `python prepare_test.py ВашПлан.jmx ваш_конфиг.json --patch-jmx`  
   *(или: `parse_jmx_profile.py` → `send_profile_to_influx.py` с тем же `test_run_id`, что в `test_run_id.txt` и в JMeter)*  
2. Запуск JMeter.  
3. `python check_load_profile.py <test_run_id> ваш_конфиг.json`

---

## На что обратить внимание

- Один и тот же **`test_run`** при отправке профиля, в **User Defined Variables** в JMX и при отчёте.
- `aggregation_interval` в JSON должен совпадать с интервалом агрегации **Backend Listener** в JMeter (по умолчанию в примерах — 10 с).
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

1. Copy `influx_config.example.json` to a local file, fill host/DB/user/password — **do not commit** secrets.  
2. Once if needed: `python init_influxdb.py your_config.json`  
3. Before each run: `python jmeter_load_pipeline.py prepare your-plan.jmx --config your_config.json`  
4. Run the test in JMeter.  
5. After: `python jmeter_load_pipeline.py report --config your_config.json`
