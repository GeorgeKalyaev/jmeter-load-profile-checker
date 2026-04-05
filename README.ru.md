# Автоматическая проверка профиля нагрузки НТ

[English](README.md) | Русский

Пошаговая инструкция по настройке и использованию системы проверки попадания фактической нагрузки в профиль НТ.

## 🚦 Очередность запуска (TL;DR)

1) Один раз (инициализация БД InfluxDB 1.x)
- Команда:
```powershell
cd C:\Users\kalya\JmeterReport
python init_influxdb.py influx_config_localhost.json
```

2) Перед каждым тестом — подготовка профиля и test_run_id
- Команда:
```powershell
python prepare_test.py SimpleLoadTest.jmx influx_config_localhost.json
```
- Результат: в консоли будет выведен test_run_id вида `test_YYYYMMDD_HHMMSS`

3) В JMeter проставить такой же test_run
- Где: Test Plan → User Defined Variables → `test_run`
- Значение: тот же `test_run_id`, что напечатан на шаге 2
- Убедитесь, что JSR223 Listener с содержимым `StageTracker.groovy` добавлен на уровень Test Plan

4) Запустить тест в JMeter
- Пример (CLI):
```powershell
jmeter.bat -n -t SimpleLoadTest.jmx -l results.jtl
```

5) После завершения теста — сгенерировать отчет
- Команда:
```powershell
python check_load_profile.py <test_run_id> influx_config_localhost.json
```
- Результат: `load_profile_check_<test_run_id>.html` и `.json` в папке проекта

Примечание:
- Для удалённой БД используйте `influx_config.json` вместо `influx_config_localhost.json`.
- Ручной путь без `prepare_test.py`: `python parse_jmx_profile.py SimpleLoadTest.jmx` → `python send_profile_to_influx.py SimpleLoadTest.profile.json <test_run_id> influx_config_localhost.json`.

### Пример итогового отчёта

В конце работы скрипт создаёт HTML со **сравнением целевого и фактического** профиля (RPS по ступеням, thread group, статусы). Готовый пример из этого репозитория:

- [`load_profile_check_test_20260123_011440.html`](load_profile_check_test_20260123_011440.html)

**Просмотр на GitHub:** файл откроется как исходный код, а не как страница в браузере. Удобнее открыть локально после клонирования (двойной щелчок или `Start-Process` в PowerShell). **Вставить живой HTML в README нельзя** — так устроены GitHub и большинство рендеров Markdown (безопасность).

**Картинка «сразу в README»** — скриншот **всей страницы** отчёта в масштабе окна браузера (~1387×3731 px; на GitHub нажмите на картинку для увеличения):

![Пример: полный отчёт о сверке профиля](docs/images/load-profile-check-sample.png)

Файл PNG — полноразмерный скриншот того же примера, что и [`load_profile_check_test_20260123_011440.html`](load_profile_check_test_20260123_011440.html). При необходимости замените его скриншотом другого прогона.

## 📚 Подробная пошаговая инструкция

### Учетные данные (важно)
- В примерах используется пользователь `jmeter_user` и пароль `changeme`.
- Поменяйте `changeme` в:
  - `influx_config_localhost.json` / `influx_config.json`
  - Backend Listener URL в `SimpleLoadTest.jmx`
  - (опц.) дефолт в JSR223 Listener внутри `SimpleLoadTest.jmx`

### 0) Предварительные условия (один раз)
- Установлен Python 3
- InfluxDB 1.x запущен и доступен
- Установлен Apache JMeter 5.x ([скачать](https://jmeter.apache.org/download_jmeter.cgi)); `jmeter.bat` в `PATH` или полный путь к `bin\jmeter.bat`
- В файле `influx_config_localhost.json` указаны:
  - `influx_url`: `http://localhost:8086`
  - `influx_db`: `jmeter`
  - `influx_user`: `jmeter_user`
  - `influx_pass`: `jmeter123`

Проверка конфигурации:
```powershell
type influx_config_localhost.json
```

## ⚙️ Шаг 1: Настройка InfluxDB

### 1.1 Настройте `influx_config.json`
Откройте `influx_config.json` (или используйте `influx_config_localhost.json`) и укажите параметры подключения к InfluxDB. Те же значения должны быть в Backend Listener вашего JMX.

### 1.2 Создайте БД/пользователя/политику хранения (InfluxDB 1.x)
InfluxDB 1.x — без схем, измерения создаются при первой записи. Рекомендуется заранее создать БД/пользователя/RP:
```powershell
cd C:\Users\kalya\JmeterReport
python init_influxdb.py influx_config_localhost.json
```
Скрипт гарантирует:
- Базу `jmeter`
- Пользователя `jmeter_user` с паролем `changeme` и права
- Политику хранения `autogen` (по умолчанию)
- «Прогрев» измерений: `load_profile`, `load_profile_thread_group_info`, `load_stage_change`, `jmeter`

Альтернатива: выполнить аналогичные команды через curl при включённой авторизации.

### 1.3 Проверьте Backend Listener в JMX
Пример URL:
```
http://jmeter_user:changeme@localhost:8086/write?db=jmeter
```

### 1.4 (опц.) Значения по умолчанию в `StageTracker.groovy`
При необходимости скорректируйте дефолты `influxUrl/db/user/pass`.

## 📝 Шаг 2: Настройка JMeter
Добавьте один JSR223 Listener на уровне Test Plan, задайте `test_run` в User Defined Variables и укажите корректный Backend Listener URL. Убедитесь, что StageTracker только один (на уровне Test Plan).

## 🚀 Шаг 3: Полный цикл
1) Распарсить JMX и отправить профиль в InfluxDB (`prepare_test.py` или вручную)  
2) Проставить такой же `test_run` в JMeter  
3) Запустить тест (GUI/CLI)  
4) Сгенерировать HTML-отчёт: `python check_load_profile.py <test_run_id> [config]`

Скрипт автоматически:
- читает целевой профиль из `load_profile`,
- читает события ступеней из `load_stage_change`,
- вычисляет абсолютные интервалы,
- получает фактические метрики из `jmeter`,
- сравнивает фактический и целевой RPS и формирует HTML/JSON.

## 📊 Модель данных InfluxDB
- `load_profile`: целевые ступени по Thread Group (теги: `test_run`, `thread_group`; поля: `stage_idx`, `plateau_start_s`, `plateau_end_s`, `hold_s`, `threads`, `target_rps`)
- `load_stage_change`: событие входа в плато (теги: `test_run`, `thread_group`; поля: `stage_idx`, `threads`, `target_rps`, `plateau_start_s`, `hold_s`)
- `jmeter`: фактические метрики Backend Listener (теги: `application`, `transaction`; поля включают `count`, процентили и т. п.)

## 🔍 Диагностика
- Везде используйте один и тот же `test_run_id` (отправка профиля, переменная в JMeter, генерация отчёта)
- Убедитесь, что активен только один StageTracker (на Test Plan)
- Проверьте достижимость InfluxDB и корректность Backend Listener URL
- При больших отклонениях: проверьте Constant Throughput Timer и при необходимости увеличьте допуск

## 📈 Grafana (опционально)
- Создайте переменную дашборда `test_run` (Text box или Query)
- Добавьте панели для целевого профиля, фактического RPS и аннотаций переходов ступеней, используя измерения выше

