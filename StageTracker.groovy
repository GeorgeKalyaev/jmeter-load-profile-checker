/**
 * JSR223 Listener для отслеживания переходов между ступенями нагрузки.
 * 
 * Установка:
 * 1. Добавьте JSR223 Listener в Test Plan (на уровне Test Plan, чтобы работал для всех UTG)
 * 2. Language: groovy
 * 3. Script file: путь к этому файлу StageTracker.groovy
 * 4. Или вставьте код в Script text area
 * 
 * Требования:
 * - Переменная ${test_run} должна быть установлена в User Defined Variables
 * - InfluxDB URL и credentials можно задать через переменные (influx_url, influx_db, influx_user, influx_pass)
 *   или будут использованы значения по умолчанию
 * - Профиль должен быть заранее отправлен в InfluxDB через send_profile_to_influx.py
 */

import groovy.json.JsonSlurper
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.Base64

// Получаем test_run ID
String testRun = vars.get("test_run") ?: props.get("test_run") ?: "unknown"

// InfluxDB настройки
// Приоритет: 1) User Defined Variables в JMeter, 2) значения по умолчанию ниже
// ⚠️ ВАЖНО: Если меняете настройки, обновите также:
//   - influx_config.json (для Python скриптов)
//   - Backend Listener в JMX (параметр influxdbUrl)
// Для локального тестирования измените на: http://localhost:8086
String influxUrl = vars.get("influx_url") ?: props.get("influx_url") ?: "http://localhost:8086"
String influxDb = vars.get("influx_db") ?: props.get("influx_db") ?: "jmeter"
String influxUser = vars.get("influx_user") ?: props.get("influx_user") ?: "jmeter_user"
String influxPass = vars.get("influx_pass") ?: props.get("influx_pass") ?: "changeme"

// Загружаем профиль из InfluxDB один раз при инициализации
if (!props.get("stage_tracker_initialized")) {
    try {
        def profile = loadProfileFromInflux(testRun, influxUrl, influxDb, influxUser, influxPass)
        if (profile) {
            props.put("load_profile", new groovy.json.JsonBuilder(profile).toPrettyString())
            log.info("StageTracker: Профиль загружен из InfluxDB для test_run=${testRun}")
        } else {
            log.warn("StageTracker: Профиль не найден в InfluxDB для test_run=${testRun}. Убедитесь, что профиль был отправлен через send_profile_to_influx.py")
        }
    } catch (Exception e) {
        log.error("StageTracker: Ошибка загрузки профиля из InfluxDB: ${e.message}", e)
    }
    props.put("stage_tracker_initialized", "true")
}

// Получаем текущее время теста в секундах
// Устанавливаем TESTSTART.MS при первом вызове, если он еще не установлен
if (!props.get("TESTSTART.MS")) {
    props.put("TESTSTART.MS", String.valueOf(System.currentTimeMillis()))
}

long testStartTime = props.get("TESTSTART.MS").toLong()
long currentTime = System.currentTimeMillis()
long elapsedSeconds = (currentTime - testStartTime) / 1000

// Получаем имя текущей Thread Group
String currentTG = ctx.getThreadGroup().getName()

// Парсим профиль
def profileJson = props.get("load_profile")
if (!profileJson) {
    return  // Нет профиля - ничего не делаем
}

def profile = new JsonSlurper().parseText(profileJson)

// Ищем текущую Thread Group в профиле
def tg = profile.thread_groups.find { it.name == currentTG }
if (!tg) {
    return  // Thread Group не найдена в профиле
}

// Проверяем, не перешли ли мы на новую ступень
def stages = tg.stages ?: []
def currentStageKey = "${testRun}_${currentTG}_current_stage"
def lastKnownStageIdx = props.get(currentStageKey) ? props.get(currentStageKey).toInteger() : -1

// Находим текущую ступень на основе elapsedSeconds
def currentStage = null
for (stage in stages) {
    int plateauStart = stage.plateau_start_s ?: 0
    int plateauEnd = stage.plateau_end_s ?: Integer.MAX_VALUE
    
    // Проверяем, вошли ли мы в плато этой ступени
    if (elapsedSeconds >= plateauStart && elapsedSeconds < plateauEnd) {
        currentStage = stage
        break
    }
}

// Если нашли текущую ступень и она отличается от последней известной, отправляем событие
if (currentStage != null) {
    int currentStageIdx = currentStage.stage_idx ?: 0
    
    // Проверяем, изменилась ли ступень
    if (currentStageIdx != lastKnownStageIdx) {
        String stageKey = "${testRun}_${currentTG}_${currentStageIdx}"
        
        // Используем синхронизацию для предотвращения множественных отправок от разных потоков
        synchronized(this.getClass()) {
            // Двойная проверка: возможно, другой поток уже отправил событие
            if (currentStageIdx != (props.get(currentStageKey) ? props.get(currentStageKey).toInteger() : -1)) {
                // Отправляем событие перехода на ступень в InfluxDB
                sendStageEvent(testRun, currentTG, currentStage, tg.target_rps, influxUrl, influxDb, influxUser, influxPass)
                props.put(stageKey, "sent")
                props.put(currentStageKey, currentStageIdx.toString())
                log.info("StageTracker: Отправлено событие перехода на ступень ${currentStageIdx} для ${currentTG} (t=${elapsedSeconds}s, plateau=${currentStage.plateau_start_s}-${currentStage.plateau_end_s}s)")
            }
        }
    }
} else {
    // Если не нашли текущую ступень, логируем для отладки (только периодически, чтобы не засорять лог)
    def debugKey = "${testRun}_${currentTG}_debug"
    def lastDebugTime = props.get(debugKey) ? props.get(debugKey).toLong() : 0
    if (currentTime - lastDebugTime > 60000) {  // Раз в минуту
        log.debug("StageTracker: Не найдена текущая ступень для ${currentTG} при elapsedSeconds=${elapsedSeconds}s. Доступные ступени: ${stages.collect { "${it.stage_idx}: ${it.plateau_start_s}-${it.plateau_end_s}s" }.join(', ')}")
        props.put(debugKey, currentTime.toString())
    }
}

/**
 * Загружает профиль нагрузки из InfluxDB по test_run_id
 */
def loadProfileFromInflux(String testRun, String url, String db, String user, String pass) {
    try {
        // Используем GROUP BY для получения всех Thread Groups и всех ступеней
        String query = "SELECT * FROM \"load_profile\" WHERE \"test_run\" = '${testRun}' GROUP BY \"thread_group\", \"stage_idx\" ORDER BY time"
        String queryUrl = "${url}/query?db=${db}&q=${URLEncoder.encode(query, 'UTF-8')}"
        
        URL obj = new URL(queryUrl)
        HttpURLConnection conn = (HttpURLConnection) obj.openConnection()
        conn.setRequestMethod("GET")
        conn.setConnectTimeout(5000)
        conn.setReadTimeout(5000)
        
        // Basic Auth
        String auth = "${user}:${pass}"
        String encodedAuth = Base64.getEncoder().encodeToString(auth.getBytes())
        conn.setRequestProperty("Authorization", "Basic ${encodedAuth}")
        
        int responseCode = conn.getResponseCode()
        if (responseCode != 200) {
            log.warn("StageTracker: Неожиданный код ответа InfluxDB при загрузке профиля: ${responseCode}")
            return null
        }
        
        // Читаем ответ
        def response = new groovy.json.JsonSlurper().parseText(conn.inputStream.text)
        conn.disconnect()
        
        if (!response.results || !response.results[0].series) {
            return null
        }
        
        // Преобразуем данные из InfluxDB в формат профиля
        def profile = [test_name: testRun, thread_groups: []]
        def tgMap = [:]
        
        response.results[0].series.each { series ->
            def tags = series.tags
            String tgName = tags.thread_group
            if (!tgMap[tgName]) {
                tgMap[tgName] = [name: tgName, stages: [], target_rps: null]
            }
            
            def values = series.values
            def columns = series.columns
            
            values.each { row ->
                def rowMap = [:]
                columns.eachWithIndex { col, idx ->
                    rowMap[col] = row[idx]
                }
                
                // Извлекаем данные ступени
                def stage = [
                    stage_idx: (int)(rowMap.stage_idx ?: 0),
                    threads: (int)(rowMap.threads ?: 0),
                    plateau_start_s: (int)(rowMap.plateau_start_s ?: 0),
                    plateau_end_s: (int)(rowMap.plateau_end_s ?: 0),
                    hold_s: (int)(rowMap.hold_s ?: 0),
                ]
                
                tgMap[tgName].stages.add(stage)
                
                // Сохраняем target_rps из первой записи
                if (tgMap[tgName].target_rps == null && rowMap.target_rps != null) {
                    tgMap[tgName].target_rps = (double)rowMap.target_rps
                }
            }
        }
        
        profile.thread_groups = tgMap.values().toList()
        
        // Сортируем ступени по stage_idx для каждой TG
        profile.thread_groups.each { tg ->
            tg.stages = tg.stages.sort { it.stage_idx }
        }
        
        // Логируем загруженные Thread Groups для отладки
        def tgNames = profile.thread_groups.collect { it.name }
        log.info("StageTracker: Загружено ${profile.thread_groups.size()} Thread Groups из InfluxDB: ${tgNames}")
        
        return profile
        
    } catch (Exception e) {
        log.error("StageTracker: Ошибка загрузки профиля из InfluxDB: ${e.message}", e)
        return null
    }
}

/**
 * Отправляет событие перехода на ступень в InfluxDB
 */
def sendStageEvent(String testRun, String tgName, def stage, def targetRps, String url, String db, String user, String pass) {
    try {
        long timestampNs = System.currentTimeMillis() * 1_000_000  // миллисекунды * 1_000_000 = микросекунды (для InfluxDB 1.x это корректно)
        
        // Формируем строку в формате InfluxDB Line Protocol
        String line = String.format(
            "load_stage_change,test_run=%s,thread_group=%s stage_idx=%d,threads=%d,target_rps=%.2f,plateau_start_s=%d,hold_s=%d %d",
            testRun.replace(" ", "\\ "),
            tgName.replace(" ", "\\ ").replace(",", "\\,"),
            stage.stage_idx ?: 0,
            stage.threads ?: 0,
            (targetRps ?: 0.0) as double,
            stage.plateau_start_s ?: 0,
            stage.hold_s ?: 0,
            timestampNs
        )
        
        String writeUrl = "${url}/write?db=${db}"
        URL obj = new URL(writeUrl)
        HttpURLConnection conn = (HttpURLConnection) obj.openConnection()
        conn.setRequestMethod("POST")
        conn.setDoOutput(true)
        conn.setConnectTimeout(5000)
        conn.setReadTimeout(5000)
        
        // Basic Auth
        String auth = "${user}:${pass}"
        String encodedAuth = Base64.getEncoder().encodeToString(auth.getBytes())
        conn.setRequestProperty("Authorization", "Basic ${encodedAuth}")
        
        // Отправляем данные
        conn.getOutputStream().write(line.getBytes("UTF-8"))
        
        int responseCode = conn.getResponseCode()
        if (responseCode == 204) {
            log.debug("StageTracker: Событие успешно отправлено в InfluxDB")
        } else {
            log.warn("StageTracker: Неожиданный код ответа InfluxDB: ${responseCode}")
        }
        
        conn.disconnect()
    } catch (Exception e) {
        log.error("StageTracker: Ошибка отправки события в InfluxDB: ${e.message}", e)
    }
}
