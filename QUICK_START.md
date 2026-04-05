# Быстрый старт: Полный цикл работы

## ✅ Последовательность действий для запуска теста и получения отчета

### Шаг 1: Парсинг JMX
```powershell
cd C:\Users\kalya\JmeterReport
python parse_jmx_profile.py SimpleLoadTest.jmx
```
**Результат:** Создается файл `SimpleLoadTest.profile.json` с профилем нагрузок.

**Что происходит:**
- Парсер находит все Ultimate Thread Groups
- Извлекает ступени нагрузки (threads, ramp-up, hold, ramp-down)
- Находит все Transaction Controllers внутри каждой Thread Group
- Находит все Samplers, которые начинаются с префиксов из `sampler_filter.json` (по умолчанию "HTTP")
- Сохраняет все в JSON файл

---

### Шаг 2: Генерация test_run_id и отправка профиля в InfluxDB

**Генерируем уникальный ID теста:**
```powershell
$testRunId = "test_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
# Например: test_20260122_143022
```

**Отправляем профиль в InfluxDB:**
```powershell
python send_profile_to_influx.py SimpleLoadTest.profile.json $testRunId influx_config_localhost.json
```

**Результат:** Профиль загружен в InfluxDB с указанным `test_run_id`.

**Что происходит:**
- Читается `SimpleLoadTest.profile.json`
- Для каждой Thread Group и каждой ступени вычисляется целевой RPS
- Все данные отправляются в InfluxDB (measurement `load_profile`)
- Список Transaction Controllers сохраняется для каждой Thread Group
- Список Samplers отправляется в `load_profile_samplers`

---

### Шаг 3: Настройка JMeter и запуск теста

**3.1. Обновите переменную `test_run` в JMeter:**
- Откройте `SimpleLoadTest.jmx` в JMeter GUI
- Найдите "User Defined Variables"
- Установите `test_run` = тот же `test_run_id`, что использовали в шаге 2
  - Например: `test_20260122_143022`

**3.2. Запустите тест в JMeter:**
- Запустите тест через GUI или командной строкой
- Тест должен выполняться до завершения всех ступеней

**Что происходит во время теста:**
- Backend Listener отправляет метрики в InfluxDB (measurement `jmeter`)
- JSR223 Listener отслеживает переходы на ступени и отправляет события в `load_stage_change`
- Все данные связываются через `test_run_id`

---

### Шаг 4: Проверка результатов и создание HTML отчета

**После завершения теста:**
```powershell
python check_load_profile.py $testRunId influx_config_localhost.json
```

**Результат:** 
- Создается файл `load_profile_check_<test_run_id>.html` - HTML отчет
- Создается файл `load_profile_check_<test_run_id>.json` - JSON результаты

**Что происходит:**
1. Загружается профиль из InfluxDB по `test_run_id`
2. Загружаются события переходов на ступени
3. **Автоматически вычисляется время старта теста** из событий
4. Для каждого плато вычисляются абсолютные временные интервалы
5. Запрашиваются фактические метрики из InfluxDB за эти интервалы
6. Сравниваются фактические RPS с целевыми
7. Проверяются бизнес-критерии для Samplers (если заданы)
8. Генерируется HTML отчет

---

## 🔍 Что проверяется в отчете

### 1. Таблицы по каждой Thread Group
- Целевой RPS vs Фактический RPS
- Отклонение в процентах
- Количество успешных запросов и ошибок
- Процент ошибок
- Среднее, P95 и максимальное время отклика
- Статус (PASS/FAIL) для каждой ступени

### 2. Сводная статистика по всем Thread Groups
- Суммарные метрики всех Thread Groups вместе
- Итоговая строка с общими показателями

### 3. Проверка бизнес-критериев для Samplers
- Проверка времени отклика для каждого Sampler
- Сравнение P95 с критерием `max_response_time_ms`
- Статус (PASS/FAIL) для каждого Sampler на каждой ступени

---

## ⚠️ Важные моменты

1. **test_run_id должен совпадать везде:**
   - При отправке профиля (`send_profile_to_influx.py`)
   - В переменной `test_run` в JMeter
   - При проверке результатов (`check_load_profile.py`)

2. **Порядок действий важен:**
   - ✅ Сначала парсите JMX
   - ✅ Потом отправляйте профиль в InfluxDB
   - ✅ Потом обновляйте `test_run` в JMeter
   - ✅ Потом запускайте тест
   - ✅ После завершения теста проверяйте результаты

3. **Если изменили JMX:**
   - Нужно перепарсить: `python parse_jmx_profile.py SimpleLoadTest.jmx`
   - Нужно переотправить профиль: `python send_profile_to_influx.py ...`
   - Использовать новый `test_run_id` для нового теста

4. **Фильтр Samplers:**
   - По умолчанию учитываются только Samplers, начинающиеся с "HTTP"
   - Чтобы учитывать другие типы (JDBC, SOAP, FTP), отредактируйте `sampler_filter.json`

---

## 📝 Пример полного цикла

```powershell
# 1. Парсинг JMX
python parse_jmx_profile.py SimpleLoadTest.jmx

# 2. Генерация test_run_id
$testRunId = "test_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
Write-Host "Test Run ID: $testRunId"

# 3. Отправка профиля в InfluxDB
python send_profile_to_influx.py SimpleLoadTest.profile.json $testRunId influx_config_localhost.json

# 4. Обновите test_run в JMeter на значение $testRunId и запустите тест
# (вручную через JMeter GUI)

# 5. После завершения теста - проверка результатов
python check_load_profile.py $testRunId influx_config_localhost.json

# 6. Откройте HTML отчет
Start-Process "load_profile_check_$testRunId.html"
```

---

## ✅ Проверка работоспособности

После всех шагов вы должны увидеть:
- ✅ HTML отчет с таблицами по каждой Thread Group
- ✅ Сводную статистику по всем Thread Groups
- ✅ Проверку бизнес-критериев для Samplers (если заданы)
- ✅ Статусы PASS/FAIL для каждой ступени

Если что-то не работает, проверьте:
- Совпадает ли `test_run_id` везде
- Доступен ли InfluxDB
- Запущен ли тест до завершения
- Есть ли данные в InfluxDB (можно проверить через Grafana)
