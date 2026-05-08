"""
Post-run скрипт: проверяет попадание фактической нагрузки в профиль НТ.
Читает данные из InfluxDB за периоды плато и сравнивает с целевым профилем.

Использование:
    python check_load_profile.py <test_run_id> [config.json] [output_report.html] [tolerance_pct]
    
Пример:
    python check_load_profile.py 20250115_143022
    python check_load_profile.py 20250115_143022 influx_config.json report.html 10.0
"""
import json
import math
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from html import escape
from pathlib import Path


def load_influx_config(config_path: Optional[Path] = None) -> Dict[str, str]:
    """Загружает настройки InfluxDB из конфиг файла или использует значения по умолчанию."""
    default_config = {
        "influx_url": "http://localhost:8086",
        "influx_db": "jmeter",
        "influx_user": "jmeter_user",
        "influx_pass": "changeme",
    }
    
    if config_path is None:
        # Пытаемся найти конфиг рядом со скриптом
        script_dir = Path(__file__).parent
        config_path = script_dir / "influx_config.json"
    
    if config_path and config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                return {**default_config, **config}
        except Exception as e:
            print(f"Предупреждение: не удалось загрузить конфиг из {config_path}: {e}")
            print("Используются значения по умолчанию")
    
    return default_config


def load_sampler_filter_config(config_path: Optional[Path] = None) -> List[str]:
    """
    Загружает конфигурацию фильтра Samplers из файла sampler_filter.json.
    Возвращает список префиксов имен Samplers, которые должны учитываться.
    По умолчанию: ["HTTP"]
    """
    default_prefixes = ["HTTP"]
    
    if config_path is None:
        # Пытаемся найти конфиг рядом со скриптом
        script_dir = Path(__file__).parent
        config_path = script_dir / "sampler_filter.json"
    
    if config_path and config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                prefixes = config.get("allowed_sampler_prefixes", default_prefixes)
                if isinstance(prefixes, list):
                    return [str(p) for p in prefixes if p]
        except Exception as e:
            print(f"Предупреждение: не удалось загрузить конфиг фильтра из {config_path}: {e}")
            print("Используются значения по умолчанию: ['HTTP']")
    
    return default_prefixes


def _influx_quoted_tag_value(s: str) -> str:
    """Экранирование значения для InfluxQL в одинарных кавычках."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _jmeter_tag_filter_sql(
    test_run_id: str,
    runner_id: Optional[str] = None,
    *,
    require_test_run_tag: bool = True,
) -> str:
    """
    Фрагмент AND ... для запросов к measurement jmeter.
    Если require_test_run_tag=False (режим без тега runner в прогоне), тег test_run не фильтруем —
    обратная совместимость со старыми точками без тега.
    """
    parts: List[str] = []
    if require_test_run_tag and test_run_id:
        safe = _influx_quoted_tag_value(test_run_id)
        parts.append(f'"test_run" = \'{safe}\'')
    if runner_id is not None:
        rsafe = _influx_quoted_tag_value(runner_id)
        parts.append(f'"runner" = \'{rsafe}\'')
    if not parts:
        return ""
    return " AND " + " AND ".join(parts)


def list_jmeter_runner_ids(
    test_run_id: str,
    influx_url: str,
    db_name: str,
    username: str = None,
    password: str = None,
) -> List[str]:
    """Список значений тега runner в jmeter для данного test_run (пусто — один «логический» источник)."""
    safe = _influx_quoted_tag_value(test_run_id)
    query = (
        f'SHOW TAG VALUES FROM "jmeter" WITH KEY = "runner" '
        f'WHERE "test_run" = \'{safe}\''
    )
    series = query_influx(query, influx_url, db_name, username, password)
    found: List[str] = []
    for s in series:
        for row in s.get("values", []):
            if len(row) >= 2 and row[1] is not None and str(row[1]).strip() != "":
                found.append(str(row[1]).strip())
    return sorted(set(found))


def list_runner_ids_from_meta(
    test_run_id: str,
    influx_url: str,
    db_name: str,
    username: str = None,
    password: str = None,
) -> List[str]:
    """Fallback: список runner из measurement jmeter_runner_meta (JSR223 heartbeat)."""
    safe = _influx_quoted_tag_value(test_run_id)
    query = (
        f'SHOW TAG VALUES FROM "jmeter_runner_meta" WITH KEY = "runner" '
        f'WHERE "test_run" = \'{safe}\''
    )
    series = query_influx(query, influx_url, db_name, username, password)
    found: List[str] = []
    for s in series:
        for row in s.get("values", []):
            if len(row) >= 2 and row[1] is not None and str(row[1]).strip() != "":
                found.append(str(row[1]).strip())
    return sorted(set(found))


def _tg_jmeter_transaction_condition(transaction_names: Optional[List[str]]) -> str:
    """
    Фильтр по полю transaction для метрик одной Thread Group в get_actual_metrics.
    Учитываются только Transaction Controller с именами _UC* (семплеры внутри уже
    входят в count на уровне транзакции в Backend Listener).

    Если в профиле есть имена, начинающиеся с _UC — условие OR по ним (узко под эту TG).
    Иначе — общий шаблон /^_UC.*/ (все такие транзакции в окне времени).
    """
    names = transaction_names or []
    uc_names = [n for n in names if isinstance(n, str) and n.startswith("_UC")]
    if uc_names:
        parts = [f'"transaction" = \'{_influx_quoted_tag_value(n)}\'' for n in uc_names]
        return "(" + " OR ".join(parts) + ")"
    return '"transaction" =~ /^_UC.*/'


def _jmeter_transaction_filter_from_profile(transaction_names: List[str]) -> str:
    """
    Условие WHERE по полю transaction для поиска точек jmeter.
    Должно быть согласовано с get_actual_metrics: только /^_UC.*/ часто не находит
    первые сэмплы (они могут быть HTTP Request … или UC_* без ведущего подчёркивания).
    """
    names = [n for n in (transaction_names or []) if n]
    if not names:
        return '"transaction" =~ /^_UC.*/'
    parts = [f'"transaction" = \'{_influx_quoted_tag_value(n)}\'' for n in names]
    return "(" + " OR ".join(parts) + ")"


def is_sampler_allowed(sampler_name: str, allowed_prefixes: List[str]) -> bool:
    """
    Проверяет, должен ли Sampler учитываться на основе его имени.
    
    Args:
        sampler_name: Имя Sampler
        allowed_prefixes: Список префиксов, которые разрешены
    
    Returns:
        True если Sampler должен учитываться, False иначе
    """
    if not sampler_name or not allowed_prefixes:
        return False
    
    for prefix in allowed_prefixes:
        if sampler_name.startswith(prefix):
            return True
    
    return False


def query_influx(
    query: str,
    influx_url: str,
    db_name: str,
    username: str = None,
    password: str = None,
) -> List[Dict[str, Any]]:
    """Выполняет запрос к InfluxDB и возвращает результаты."""
    url = f"{influx_url}/query?db={db_name}&q={urllib.parse.quote(query)}"
    
    req = urllib.request.Request(url)
    
    if username and password:
        import base64
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {credentials}")
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
            if "results" in data and data["results"]:
                result = data["results"][0]
                if "series" in result:
                    return result["series"]
            return []
    except Exception as e:
        print(f"Ошибка запроса к InfluxDB: {e}")
        return []


def _influx_time_value_to_ns(time_value: Any) -> Optional[int]:
    """Преобразует поле time из ответа InfluxDB 1.x в наносекунды (UTC)."""
    if time_value is None:
        return None
    if isinstance(time_value, str):
        try:
            dt = datetime.fromisoformat(time_value.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000_000)
        except (ValueError, TypeError):
            return None
    if isinstance(time_value, (int, float)):
        time_ns = int(time_value)
        if time_ns < 1_000_000_000_000:
            if time_ns < 1_000_000_000:
                time_ns = time_ns * 1_000_000_000
            elif time_ns < 1_000_000_000_000:
                time_ns = time_ns * 1_000_000
            elif time_ns < 1_000_000_000_000_000:
                time_ns = time_ns * 1_000
        return time_ns
    return None


def get_test_end_time_ns(
    test_run_id: str,
    influx_url: str,
    db_name: str,
    username: str = None,
    password: str = None,
) -> Optional[int]:
    """
    Последняя точка jmeter с тегом test_run (если Backend Listener пишет тег).
    Без тега возвращает None — тогда отчёт не помечает ступени как SKIP/PARTIAL по времени.
    """
    safe = test_run_id.replace("\\", "\\\\").replace("'", "\\'")
    query = f'''
        SELECT time FROM "jmeter"
        WHERE "test_run" = '{safe}'
        ORDER BY time DESC
        LIMIT 1
    '''
    series = query_influx(query, influx_url, db_name, username, password)
    if not series or len(series) == 0:
        return None
    values = series[0].get("values") or []
    if not values or len(values[0]) == 0:
        return None
    return _influx_time_value_to_ns(values[0][0])


def classify_plateau_evaluation(
    plateau_start_s: int,
    plateau_end_s: int,
    test_start_time_ns: Optional[int],
    test_end_time_ns: Optional[int],
) -> Tuple[str, int, int, Optional[str]]:
    """
    Оценка покрытия окна плато фактическим прогоном.

    Returns:
        kind: 'full' | 'partial' | 'skip'
        effective_start_s, effective_end_s — границы для запросов метрик (сек от старта теста)
        skip_reason — текст для отчёта (только для skip)
    """
    if (
        test_start_time_ns is None
        or test_end_time_ns is None
        or plateau_end_s <= plateau_start_s
    ):
        return "full", plateau_start_s, plateau_end_s, None

    plateau_start_ns = test_start_time_ns + plateau_start_s * 1_000_000_000
    plateau_end_ns = test_start_time_ns + plateau_end_s * 1_000_000_000

    if test_end_time_ns <= plateau_start_ns:
        return (
            "skip",
            plateau_start_s,
            plateau_end_s,
            "Тест завершился до начала окна плато; ступень не достигнута, оценка не выполнялась.",
        )

    if test_end_time_ns >= plateau_end_ns:
        return "full", plateau_start_s, plateau_end_s, None

    rel_end_s = (test_end_time_ns - test_start_time_ns) / 1_000_000_000.0
    cand_end = min(
        plateau_end_s,
        max(plateau_start_s + 1, int(math.ceil(rel_end_s))),
    )
    if cand_end >= plateau_end_s:
        return "full", plateau_start_s, plateau_end_s, None
    if cand_end <= plateau_start_s:
        return (
            "skip",
            plateau_start_s,
            plateau_end_s,
            "Нет наблюдаемого окна плато (остановка на границе ступени).",
        )
    return "partial", plateau_start_s, cand_end, None


def get_profile_from_influx(
    test_run_id: str,
    influx_url: str,
    db_name: str,
    username: str = None,
    password: str = None,
) -> Dict[str, Any]:
    """Получает профиль нагрузки из InfluxDB."""
    query = f'SELECT * FROM "load_profile" WHERE "test_run" = \'{test_run_id}\' ORDER BY time'
    series = query_influx(query, influx_url, db_name, username, password)
    
    profile = {"thread_groups": {}}
    
    # Используем множество для отслеживания уникальных комбинаций (thread_group, stage_idx)
    # чтобы избежать дубликатов, если профиль был отправлен несколько раз
    seen_stages = set()
    
    for s in series:
        tags = s.get("tags", {})
        values = s.get("values", [])
        columns = s.get("columns", [])
        
        for row in values:
            row_dict = dict(zip(columns, row))
            
            # thread_group может быть в тегах или в полях (если InfluxDB сохранил его как поле)
            tg_name = tags.get("thread_group", "") or row_dict.get("thread_group", "")
            
            if tg_name not in profile["thread_groups"]:
                profile["thread_groups"][tg_name] = {"stages": [], "transaction_names": []}
            
            # Загружаем список транзакций (если есть) - обычно только в первой ступени
            transaction_names_str = row_dict.get("transaction_names", "")
            if transaction_names_str and not profile["thread_groups"][tg_name].get("transaction_names"):
                # Парсим строку с разделителями в список
                profile["thread_groups"][tg_name]["transaction_names"] = [
                    name.strip() for name in transaction_names_str.split(",") if name.strip()
                ]
            
            stage_idx = int(row_dict.get("stage_idx", 0))
            
            # Проверяем, не добавляли ли мы уже эту ступень
            stage_key = (tg_name, stage_idx)
            if stage_key in seen_stages:
                continue  # Пропускаем дубликаты
            
            seen_stages.add(stage_key)
            profile["thread_groups"][tg_name]["stages"].append({
                "stage_idx": stage_idx,
                "plateau_start_s": int(row_dict.get("plateau_start_s", 0)),
                "plateau_end_s": int(row_dict.get("plateau_end_s", 0)),
                "hold_s": int(row_dict.get("hold_s", 0)),
                "threads": int(row_dict.get("threads", 0)),
                "target_rps": float(row_dict.get("target_rps", 0.0)),
            })
    
    return profile


def get_stage_events(
    test_run_id: str,
    influx_url: str,
    db_name: str,
    username: str = None,
    password: str = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Получает события переходов на ступени."""
    query = f'SELECT * FROM "load_stage_change" WHERE "test_run" = \'{test_run_id}\' ORDER BY time'
    series = query_influx(query, influx_url, db_name, username, password)
    
    events = {}
    for s in series:
        tags = s.get("tags", {})
        tg_name = tags.get("thread_group", "")
        if tg_name not in events:
            events[tg_name] = []
        
        values = s.get("values", [])
        columns = s.get("columns", [])
        
        for row in values:
            row_dict = dict(zip(columns, row))
            # Время из InfluxDB может быть в разных форматах
            # В колонке "time" обычно строка RFC3339 или число (наносекунды или микросекунды)
            time_value = row_dict.get("time", "")
            time_ns = None
            
            # Пробуем получить время как число
            if isinstance(time_value, (int, float)):
                time_ns = int(time_value)
                # Определяем формат времени по масштабу числа
                # InfluxDB 1.x обычно возвращает наносекунды (больше 10^12 для современных дат)
                # Но может вернуть и микросекунды (10^12 - 10^15) или миллисекунды (10^9 - 10^12)
                # Для событий из load_stage_change: если меньше 10^12, это может быть микросекунды
                if time_ns < 1_000_000_000_000:
                    # Если меньше 10^9 - это секунды (маловероятно для timestamp)
                    if time_ns < 1_000_000_000:
                        time_ns = time_ns * 1_000_000_000
                    # Если между 10^9 и 10^12 - это миллисекунды, конвертируем в наносекунды
                    elif time_ns < 1_000_000_000_000:
                        time_ns = time_ns * 1_000_000
                # Если между 10^12 и 10^15 - это микросекунды, конвертируем в наносекунды
                elif time_ns < 1_000_000_000_000_000:
                    time_ns = time_ns * 1_000
                # Если больше 10^15 - уже наносекунды, оставляем как есть
            elif isinstance(time_value, str):
                # Если строка содержит только цифры
                if time_value.replace('.', '').replace('-', '').isdigit():
                    time_ns = int(float(time_value))
                    # Аналогичная проверка масштаба
                    if time_ns < 1_000_000_000_000:
                        if time_ns < 1_000_000_000:
                            time_ns = time_ns * 1_000_000_000
                        elif time_ns < 1_000_000_000_000:
                            time_ns = time_ns * 1_000_000
                    elif time_ns < 1_000_000_000_000_000:
                        time_ns = time_ns * 1_000
                else:
                    # Если это RFC3339 строка, конвертируем в наносекунды
                    try:
                        from datetime import datetime
                        # Парсим RFC3339 формат (например: "2025-01-15T14:30:00Z")
                        dt = datetime.fromisoformat(time_value.replace('Z', '+00:00'))
                        time_ns = int(dt.timestamp() * 1_000_000_000)
                    except:
                        pass
            
            events[tg_name].append({
                "stage_idx": int(row_dict.get("stage_idx", 0)),
                "time": time_value,
                "time_ns": time_ns,  # Добавляем время в наносекундах для удобства
                "plateau_start_s": int(row_dict.get("plateau_start_s", 0)),
            })
    
    return events


def get_actual_metrics(
    test_run_id: str,
    start_time_s: int,
    end_time_s: int,
    influx_url: str,
    db_name: str,
    username: str = None,
    password: str = None,
    test_start_time_ns: int = None,
    aggregation_interval: float = 10.0,
    thread_group_name: str = None,
    transaction_names: List[str] = None,
    runner_id: Optional[str] = None,
    use_test_run_tag: bool = True,
) -> Dict[str, Any]:
    """
    Получает фактические метрики из InfluxDB за период плато.

    Для thread_group_name учитываются только точки с transaction, совпадающими с
    именами _UC* из transaction_names (OR), либо общий фильтр /^_UC.*/ если таких
    имён в списке нет — см. _tg_jmeter_transaction_condition.
    
    Args:
        start_time_s: начало плато в секундах от старта теста
        end_time_s: конец плато в секундах от старта теста
        test_start_time_ns: абсолютное время старта теста в наносекундах (если None, используется относительное время)
        runner_id: если задан — только точки jmeter с этим тегом runner (под/хост)
        use_test_run_tag: если False — не фильтровать по test_run (старые прогоны без тега)
    """
    tag_sql = _jmeter_tag_filter_sql(
        test_run_id, runner_id, require_test_run_tag=use_test_run_tag
    )
    if test_start_time_ns:
        # Используем абсолютное время: время старта теста + относительное время плато
        start_ns = test_start_time_ns + (start_time_s * 1_000_000_000)
        end_ns = test_start_time_ns + (end_time_s * 1_000_000_000)
    else:
        # Используем относительное время (для обратной совместимости)
        # В этом случае start_time_s и end_time_s должны быть абсолютными наносекундами
        start_ns = start_time_s * 1_000_000_000
        end_ns = end_time_s * 1_000_000_000
    
    # Запрос для получения среднего RPS за период
    # ВАЖНО: Поле "count" в JMeter Backend Listener хранит количество запросов за интервал агрегации.
    # По умолчанию интервал = 10 секунд.
    # 
    # ПРАВИЛЬНЫЙ ПОДХОД (как в Grafana):
    # В Grafana правильно вычисляют RPS для каждого момента времени: count / 10
    # Затем агрегируют эти RPS значения (mean или sum).
    # 
    # В InfluxDB 1.x нельзя напрямую сделать SELECT mean("count" / 10),
    # но можно использовать математику в SELECT: mean("count") / 10
    # 
    # Однако, если мы агрегируем за большой период (например, 300 секунд),
    # то mean(count) дает среднее значение count за все интервалы в этом периоде.
    # Это правильно, если count - это количество за каждый интервал (10 секунд).
    # 
    # Но если в Grafana они видят правильные данные, значит они используют другой подход:
    # - Возможно, они не агрегируют за большой период, а показывают данные по интервалам
    # - Или используют другой метод агрегации
    # 
    # ПРАВИЛЬНЫЙ МЕТОД для получения среднего RPS за период:
    # mean(count) / interval - это среднее количество запросов за интервал, деленное на длительность интервала
    # Это дает средний RPS за период, что правильно.
    #
    # Но если результаты не совпадают, возможно проблема в том, что:
    # 1. Интервал агрегации не 10 секунд
    # 2. Или нужно использовать другой метод (например, учитывать только полные интервалы)
    
    # ПРАВИЛЬНЫЙ ЗАПРОС для вычисления среднего RPS за период:
    # 
    # Стандартный метод (без подстройки):
    # mean("count") / aggregation_interval
    # 
    # Где:
    # - mean("count") - среднее количество запросов за интервал агрегации
    # - aggregation_interval - интервал агрегации JMeter Backend Listener (по умолчанию 10 секунд)
    # 
    # Это дает средний RPS за период плато.
    # 
    # ВАЖНО: Не подстраиваемся под результаты! Используем стандартный метод.
    # Если результаты не совпадают, это может быть из-за:
    # 1. Нестандартной настройки интервала агрегации в JMeter
    # 2. Дополнительных запросов (внутренние запросы JMeter, health checks и т.д.)
    # 3. Особенностей работы Constant Throughput Timer
    
    # Запрос для вычисления среднего RPS за период
    # Если указан thread_group_name, считаем RPS только для этой Thread Group
    # Иначе считаем общий RPS для всех групп
    interval_seconds = int(aggregation_interval)
    
    if thread_group_name:
        # Только Transaction Controller _UC_* (семплеры внутри — в count транзакции)
        tg_tx_cond = _tg_jmeter_transaction_condition(transaction_names)
        query = f'''
            SELECT sum("count") as count_per_interval
            FROM "jmeter"
            WHERE time >= {start_ns} AND time <= {end_ns}
            AND "statut" = 'ok'
            AND {tg_tx_cond}{tag_sql}
            GROUP BY time({interval_seconds}s)
        '''
    else:
        # Общий RPS для всех групп
        query = f'''
            SELECT sum("count") as count_per_interval
            FROM "jmeter"
            WHERE time >= {start_ns} AND time <= {end_ns}
            AND "statut" = 'ok'
            AND "transaction" =~ /^_UC.*/{tag_sql}
            GROUP BY time({interval_seconds}s)
        '''
    
    series = query_influx(query, influx_url, db_name, username, password)
    
    metrics = {}
    
    # Собираем RPS для каждого интервала (как в Grafana)
    rps_values = []
    if series and len(series) > 0:
        values = series[0].get("values", [])
        for row in values:
            if len(row) > 1 and row[1] is not None:
                try:
                    count = float(row[1])
                    rps = count / aggregation_interval  # RPS для этого интервала
                    rps_values.append(rps)
                except (ValueError, TypeError):
                    continue
    
    # Запрашиваем общее количество успешных запросов за весь период
    # Если указан thread_group_name, считаем только для этой Thread Group
    if thread_group_name:
        tg_tx_cond = _tg_jmeter_transaction_condition(transaction_names)
        query_total_requests = f'''
            SELECT sum("count") as total_requests
            FROM "jmeter"
            WHERE time >= {start_ns} AND time <= {end_ns}
            AND "statut" = 'ok'
            AND {tg_tx_cond}{tag_sql}
        '''
    else:
        query_total_requests = f'''
            SELECT sum("count") as total_requests
            FROM "jmeter"
            WHERE time >= {start_ns} AND time <= {end_ns}
            AND "statut" = 'ok'
            AND "transaction" =~ /^_UC.*/{tag_sql}
        '''
    
    series_total = query_influx(query_total_requests, influx_url, db_name, username, password)
    total_requests = 0
    if series_total and len(series_total) > 0 and series_total[0].get("values"):
        for row in series_total[0]["values"]:
            if len(row) > 1 and row[1] is not None:
                try:
                    total_requests += int(float(row[1]))
                except (ValueError, TypeError):
                    continue
    
    # Запрашиваем общее количество ошибок за весь период
    # Если указан thread_group_name, считаем ошибки только для этой Thread Group
    # Иначе считаем общие ошибки для всех групп
    if thread_group_name:
        tg_tx_cond = _tg_jmeter_transaction_condition(transaction_names)
        query_total_errors = f'''
            SELECT sum("count") as total_errors
            FROM "jmeter"
            WHERE time >= {start_ns} AND time <= {end_ns}
            AND "statut" = 'ko'
            AND {tg_tx_cond}{tag_sql}
        '''
    else:
        # Общие ошибки для всех групп: используем statut='all' и transaction='all'
        query_total_errors = f'''
            SELECT sum("countError") as total_errors
            FROM "jmeter"
            WHERE time >= {start_ns} AND time <= {end_ns}
            AND "statut" = 'all'
            AND "transaction" = 'all'{tag_sql}
        '''
    
    series_errors = query_influx(query_total_errors, influx_url, db_name, username, password)
    total_errors = 0
    if series_errors and len(series_errors) > 0 and series_errors[0].get("values"):
        for row in series_errors[0]["values"]:
            if len(row) > 1 and row[1] is not None:
                try:
                    total_errors += int(float(row[1]))
                except (ValueError, TypeError):
                    continue

    # Фактический RPS для проверки профиля:
    # учитываем все запросы (успешные + с ошибками), чтобы интенсивность
    # отражала реальную поданную нагрузку, а не только quality-срез.
    duration_seconds = end_time_s - start_time_s
    total_requests_including_errors = total_requests + total_errors
    if duration_seconds > 0:
        actual_rps_all = total_requests_including_errors / duration_seconds
        actual_rps_ok = total_requests / duration_seconds
    else:
        actual_rps_all = 0.0
        actual_rps_ok = 0.0

    # Сохраняем оба значения для совместимости
    metrics["all"] = {
        "mean_rps": actual_rps_all,
        "mean_rps_all": actual_rps_all,
        "mean_rps_ok": actual_rps_ok,
        "total_requests": total_requests,
        "total_requests_including_errors": total_requests_including_errors,
    }

    # Также сохраняем среднее по интервалам для справки (если нужно)
    if rps_values:
        mean_rps_by_intervals = sum(rps_values) / len(rps_values)
        metrics["all"]["mean_rps_by_intervals"] = mean_rps_by_intervals
    else:
        metrics["all"]["mean_rps_by_intervals"] = 0.0
    
    # Запрашиваем метрики времени отклика за весь период
    # Если указан thread_group_name, считаем только для этой Thread Group
    if thread_group_name:
        tg_tx_cond = _tg_jmeter_transaction_condition(transaction_names)
        query_response_times = f'''
            SELECT mean("avg") as avg_response_time,
                   mean("pct95.0") as pct95_response_time,
                   max("max") as max_response_time
            FROM "jmeter"
            WHERE time >= {start_ns} AND time <= {end_ns}
            AND "statut" = 'ok'
            AND {tg_tx_cond}{tag_sql}
        '''
    else:
        # Общие метрики для всех групп
        query_response_times = f'''
            SELECT mean("avg") as avg_response_time,
                   mean("pct95.0") as pct95_response_time,
                   max("max") as max_response_time
            FROM "jmeter"
            WHERE time >= {start_ns} AND time <= {end_ns}
            AND "statut" = 'ok'
            AND ("transaction" = 'all' OR "transaction" =~ /^_UC.*/){tag_sql}
        '''
    
    series_response_times = query_influx(query_response_times, influx_url, db_name, username, password)
    avg_response_time = 0.0
    pct95_response_time = 0.0
    max_response_time = 0.0
    
    # Если есть несколько серий (например, одна для 'all' и несколько для отдельных транзакций),
    # используем данные из 'all', если они есть, иначе усредняем по всем транзакциям
    if series_response_times and len(series_response_times) > 0:
        # Ищем серию с transaction='all'
        all_series = None
        other_series = []
        for s in series_response_times:
            tags = s.get("tags", {})
            if tags.get("transaction") == "all":
                all_series = s
            else:
                other_series.append(s)
        
        # Используем данные из 'all', если есть
        if all_series and all_series.get("values"):
            row = all_series["values"][0]
            columns = all_series.get("columns", [])
            row_dict = dict(zip(columns, row))
            
            if row_dict.get("avg_response_time") is not None:
                avg_response_time = float(row_dict["avg_response_time"])
            if row_dict.get("pct95_response_time") is not None:
                pct95_response_time = float(row_dict["pct95_response_time"])
            if row_dict.get("max_response_time") is not None:
                max_response_time = float(row_dict["max_response_time"])
        elif other_series:
            # Если нет 'all', усредняем по всем транзакциям
            avg_values = []
            pct95_values = []
            max_values = []
            
            for s in other_series:
                if s.get("values"):
                    row = s["values"][0]
                    columns = s.get("columns", [])
                    row_dict = dict(zip(columns, row))
                    
                    if row_dict.get("avg_response_time") is not None:
                        avg_values.append(float(row_dict["avg_response_time"]))
                    if row_dict.get("pct95_response_time") is not None:
                        pct95_values.append(float(row_dict["pct95_response_time"]))
                    if row_dict.get("max_response_time") is not None:
                        max_values.append(float(row_dict["max_response_time"]))
            
            if avg_values:
                avg_response_time = sum(avg_values) / len(avg_values)
            if pct95_values:
                pct95_response_time = sum(pct95_values) / len(pct95_values)
            if max_values:
                max_response_time = max(max_values)
    
    # Добавляем все метрики
    metrics["all"]["total_requests"] = total_requests
    metrics["all"]["total_errors"] = total_errors
    metrics["all"]["avg_response_time_ms"] = avg_response_time
    metrics["all"]["pct95_response_time_ms"] = pct95_response_time
    metrics["all"]["max_response_time_ms"] = max_response_time
    
    return metrics


def get_sampler_response_times(
    test_run_id: str,
    start_time_s: int,
    end_time_s: int,
    influx_url: str,
    db_name: str,
    username: str = None,
    password: str = None,
    test_start_time_ns: int = None,
) -> Dict[str, Dict[str, float]]:
    """
    Получает метрики времени отклика для samplers за период плато.
    Возвращает словарь: {sampler_name: {"mean": float, "pct95": float, "max": float}}
    """
    if test_start_time_ns is None:
        return {}
    
    start_ns = test_start_time_ns + (start_time_s * 1_000_000_000)
    end_ns = test_start_time_ns + (end_time_s * 1_000_000_000)
    
    # Получаем среднее время отклика, 95-й процентиль и максимум для каждого sampler
    # В InfluxDB поля называются: avg (среднее), pct95.0 (95-й процентиль), max (максимум)
    # Транзакции могут называться "HTTP Request Yandex", "JDBC Request", "SOAP Request" и т.д.
    # или "_UC_01_Yandex", "_UC_02_Google", "_UC_03_GitHub" (имена Transaction Controller)
    # Также нужно фильтровать по test_run, если он есть в тегах
    
    # Формируем условие для фильтрации по префиксам из конфига
    allowed_prefixes = load_sampler_filter_config()
    transaction_patterns = []
    for prefix in allowed_prefixes:
        # Экранируем специальные символы для регулярного выражения
        # Заменяем пробелы и точки на экранированные версии
        escaped_prefix = prefix.replace(" ", "\\ ").replace(".", "\\.")
        transaction_patterns.append(f'"transaction" =~ /^{escaped_prefix}.*/')
    
    # Также добавляем паттерн для Transaction Controllers (начинаются с _UC)
    transaction_patterns.append('"transaction" =~ /^_UC.*/')
    
    transaction_condition = " OR ".join(transaction_patterns)
    
    query = f'''
        SELECT mean("avg") as mean_response_time,
               mean("pct95.0") as pct95_response_time,
               max("max") as max_response_time
        FROM "jmeter"
        WHERE time >= {start_ns} AND time <= {end_ns}
        AND "statut" = 'ok'
        AND ({transaction_condition})
        GROUP BY "transaction"
    '''
    
    series = query_influx(query, influx_url, db_name, username, password)
    
    sampler_metrics = {}
    allowed_prefixes = load_sampler_filter_config()
    for s in series:
        tags = s.get("tags", {})
        sampler_name = tags.get("transaction", "unknown")
        
        # Учитываем только Samplers, которые соответствуют конфигурации фильтра
        if not is_sampler_allowed(sampler_name, allowed_prefixes):
            continue
        
        values = s.get("values", [])
        
        if values and len(values[0]) > 1:
            # values[0] = [time, mean_response_time, pct95_response_time, max_response_time]
            mean_rt = float(values[0][1]) if values[0][1] is not None else 0.0
            pct95_rt = float(values[0][2]) if len(values[0]) > 2 and values[0][2] is not None else 0.0
            max_rt = float(values[0][3]) if len(values[0]) > 3 and values[0][3] is not None else 0.0
            
            sampler_metrics[sampler_name] = {
                "mean": mean_rt,
                "pct95": pct95_rt,
                "max": max_rt,
            }
    
    return sampler_metrics


def get_sampler_criteria(
    test_run_id: str,
    influx_url: str,
    db_name: str,
    username: str = None,
    password: str = None,
) -> Dict[str, Dict[str, float]]:
    """
    Получает бизнес-критерии для samplers из InfluxDB.
    Возвращает словарь: {sampler_name: {"max_response_time_ms": float}}
    """
    query = f'''
        SELECT * FROM "load_profile_samplers"
        WHERE "test_run" = '{test_run_id}'
    '''
    
    series = query_influx(query, influx_url, db_name, username, password)
    
    criteria = {}
    for s in series:
        # Теперь sampler_name может быть в тегах (если был добавлен в теги)
        tags = s.get("tags", {})
        sampler_name_from_tag = tags.get("sampler_name", "")
        
        values = s.get("values", [])
        columns = s.get("columns", [])
        
        for row in values:
            row_map = {}
            for idx, col in enumerate(columns):
                if idx < len(row):
                    row_map[col] = row[idx]
            
            # Приоритет: сначала из тегов, затем из полей
            sampler_name = sampler_name_from_tag or row_map.get("sampler_name", "")
            # Пока поддерживаем только max_response_time_ms
            # В будущем можно добавить другие критерии
            max_rt_ms = row_map.get("max_response_time_ms")
            if sampler_name and max_rt_ms is not None:
                # Заменяем подчеркивания обратно на пробелы (если были экранированы)
                sampler_name = sampler_name.replace("_", " ")
                # Проверяем, должен ли Sampler учитываться на основе конфигурации
                allowed_prefixes = load_sampler_filter_config()
                if is_sampler_allowed(sampler_name, allowed_prefixes):
                    criteria[sampler_name] = {
                        "max_response_time_ms": float(max_rt_ms),
                    }
    
    return criteria


def check_sampler_criteria(
    test_run_id: str,
    influx_url: str,
    db_name: str,
    username: str = None,
    password: str = None,
    test_start_time_ns: int = None,
    profile: Dict[str, Any] = None,
    test_end_time_ns: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Проверяет соответствие времени отклика samplers бизнес-критериям.
    Возвращает результаты проверки для каждого sampler на каждой ступени.
    """
    if not profile:
        return {}
    
    criteria = get_sampler_criteria(test_run_id, influx_url, db_name, username, password)
    
    if not criteria:
        print("[INFO] Бизнес-критерии для samplers не найдены в InfluxDB")
        return {}
    
    results = {
        "samplers": {},
    }
    
    # Получаем все ступени из профиля
    # Профиль возвращается в формате {"thread_groups": {"TG_Name": {"stages": [...]}}}
    all_stages = []
    thread_groups = profile.get("thread_groups", {})
    
    # thread_groups - это словарь, где ключи - имена thread groups
    for tg_name, tg_data in thread_groups.items():
        if not isinstance(tg_data, dict):
            continue
        stages = tg_data.get("stages", [])
        
        for stage in stages:
            stage_idx = stage.get("stage_idx", 0)
            plateau_start = stage.get("plateau_start_s", 0)
            plateau_end = stage.get("plateau_end_s", 0)
            
            # Проверяем, не добавили ли мы уже эту ступень
            if not any(s["stage_idx"] == stage_idx and s["plateau_start_s"] == plateau_start 
                      for s in all_stages):
                all_stages.append({
                    "stage_idx": stage_idx,
                    "plateau_start_s": plateau_start,
                    "plateau_end_s": plateau_end,
                })
    
    # Проверяем каждый sampler на каждой ступени
    # Учитываем только Samplers, которые соответствуют конфигурации фильтра
    allowed_prefixes = load_sampler_filter_config()
    for sampler_name, sampler_criteria in criteria.items():
        # Пропускаем samplers, которые не соответствуют фильтру
        if not is_sampler_allowed(sampler_name, allowed_prefixes):
            continue
        sampler_results = {
            "name": sampler_name,
            "criteria": sampler_criteria,
            "stages": [],
        }
        
        max_rt_ms = sampler_criteria.get("max_response_time_ms", 0)
        
        for stage in all_stages:
            stage_idx = stage["stage_idx"]
            plateau_start = stage["plateau_start_s"]
            plateau_end = stage["plateau_end_s"]
            
            ev_kind, m_start, m_end, skip_reason = classify_plateau_evaluation(
                plateau_start,
                plateau_end,
                test_start_time_ns,
                test_end_time_ns,
            )
            
            if ev_kind == "skip":
                sampler_results["stages"].append(
                    {
                        "stage_idx": stage_idx,
                        "plateau_start_s": plateau_start,
                        "plateau_end_s": plateau_end,
                        "mean_response_time_ms": 0.0,
                        "pct95_response_time_ms": 0.0,
                        "max_response_time_ms": 0.0,
                        "criteria_max_ms": max_rt_ms,
                        "status": "SKIP",
                        "evaluation": "skip",
                        "skip_reason": skip_reason,
                    }
                )
                continue
            
            response_times = get_sampler_response_times(
                test_run_id,
                m_start,
                m_end,
                influx_url,
                db_name,
                username,
                password,
                test_start_time_ns,
            )
            
            # Пытаемся найти метрики для этого sampler
            # Имена могут не совпадать точно: "HTTP Request Yandex" vs "_UC_01_Yandex"
            # Приоритет: сначала ищем точное совпадение "HTTP Request X", затем "_UC_XX_X"
            sampler_metrics = {}
            if sampler_name in response_times:
                # Точное совпадение - используем его
                sampler_metrics = response_times[sampler_name]
            else:
                # Пробуем найти по части имени (например, "Yandex" в "HTTP Request Yandex" или "_UC_01_Yandex")
                # Извлекаем ключевое слово из имени sampler (последнее слово, например "Yandex")
                sampler_keyword = sampler_name.split()[-1] if " " in sampler_name else sampler_name
                
                # Сначала ищем среди "HTTP Request X" (приоритет)
                http_request_match = None
                uc_match = None
                
                for tx_name, metrics in response_times.items():
                    if sampler_keyword in tx_name:
                        if tx_name.startswith("HTTP Request"):
                            http_request_match = (tx_name, metrics)
                            break
                        elif tx_name.startswith("_UC"):
                            uc_match = (tx_name, metrics)
                
                # Используем HTTP Request, если есть, иначе _UC
                if http_request_match:
                    sampler_metrics = http_request_match[1]
                elif uc_match:
                    sampler_metrics = uc_match[1]
            
            mean_rt = sampler_metrics.get("mean", 0.0)
            pct95_rt = sampler_metrics.get("pct95", 0.0)
            max_rt = sampler_metrics.get("max", 0.0)
            
            rt_ok = pct95_rt <= max_rt_ms
            if ev_kind == "partial":
                status = "PARTIAL" if rt_ok else "FAIL"
            else:
                status = "PASS" if rt_ok else "FAIL"
            
            stage_result = {
                "stage_idx": stage_idx,
                "plateau_start_s": plateau_start,
                "plateau_end_s": plateau_end,
                "mean_response_time_ms": mean_rt,
                "pct95_response_time_ms": pct95_rt,
                "max_response_time_ms": max_rt,
                "criteria_max_ms": max_rt_ms,
                "status": status,
                "evaluation": ev_kind,
            }
            
            sampler_results["stages"].append(stage_result)
        
        results["samplers"][sampler_name] = sampler_results
    
    return results


def _compute_thread_group_results(
    profile: Dict[str, Any],
    test_run_id: str,
    test_start_time_ns: Optional[int],
    test_end_time_ns: Optional[int],
    influx_url: str,
    db_name: str,
    username: Optional[str],
    password: Optional[str],
    tolerance_pct: float,
    aggregation_interval: float,
    runner_id: Optional[str],
    use_test_run_tag: bool,
    target_rps_multiplier: float,
    log_label: str,
) -> Dict[str, Any]:
    """
    Считает thread_groups + overall_status для одного режима:
    один runner (multiplier=1), или кластер (runner_id=None, multiplier=N, цели × N).
    """
    thread_groups: Dict[str, Any] = {}
    overall_status = "PASS"

    for tg_name, tg_data in profile.get("thread_groups", {}).items():
        tg_results: Dict[str, Any] = {
            "name": tg_name,
            "stages": [],
            "status": "PASS",
        }

        sorted_stages = sorted(tg_data.get("stages", []), key=lambda x: x.get("stage_idx", 0))
        for stage in sorted_stages:
            stage_idx = stage["stage_idx"]
            plateau_start = stage["plateau_start_s"]
            plateau_end = stage["plateau_end_s"]
            base_target_rps = float(stage.get("target_rps", 0.0))
            threads_inst = int(stage.get("threads", 0))
            effective_target_rps = base_target_rps * target_rps_multiplier
            threads_display = int(round(threads_inst * target_rps_multiplier))

            print(
                f"Проверка [{log_label}] {tg_name}, ступень {stage_idx} "
                f"(плато t={plateau_start}-{plateau_end}s, без ramp)..."
            )

            transaction_names = tg_data.get("transaction_names", [])
            ev_kind, metrics_start_s, metrics_end_s, skip_reason = classify_plateau_evaluation(
                plateau_start,
                plateau_end,
                test_start_time_ns,
                test_end_time_ns,
            )

            if ev_kind == "skip":
                print(f"  [SKIP] {skip_reason}")
                tg_results["stages"].append(
                    {
                        "stage_idx": stage_idx,
                        "plateau_start_s": plateau_start,
                        "plateau_end_s": plateau_end,
                        "plateau_duration_s": 0,
                        "target_rps": effective_target_rps,
                        "target_rps_one_instance": base_target_rps,
                        "total_target_rps": effective_target_rps,
                        "actual_rps": 0.0,
                        "actual_rps_all": 0.0,
                        "actual_rps_ok": 0.0,
                        "deviation_pct": 0.0,
                        "deviation_pct_all": 0.0,
                        "deviation_pct_ok": 0.0,
                        "threads": threads_display,
                        "threads_one_instance": threads_inst,
                        "status": "SKIP",
                        "evaluation": "skip",
                        "skip_reason": skip_reason,
                        "samplers": {
                            "all": {
                                "mean_rps": 0.0,
                                "mean_rps_by_intervals": 0.0,
                                "total_requests": 0,
                                "total_errors": 0,
                                "avg_response_time_ms": 0.0,
                                "pct95_response_time_ms": 0.0,
                                "max_response_time_ms": 0.0,
                            }
                        },
                        "total_requests": 0,
                        "total_errors": 0,
                        "error_percentage": 0.0,
                        "avg_response_time_ms": 0.0,
                        "pct95_response_time_ms": 0.0,
                        "max_response_time_ms": 0.0,
                        "expected_requests": 0,
                        "actual_all_requests": 0,
                    }
                )
                continue

            actual_metrics = get_actual_metrics(
                test_run_id,
                metrics_start_s,
                metrics_end_s,
                influx_url,
                db_name,
                username,
                password,
                test_start_time_ns,
                aggregation_interval,
                thread_group_name=tg_name,
                transaction_names=transaction_names,
                runner_id=runner_id,
                use_test_run_tag=use_test_run_tag,
            )

            if "all" in actual_metrics:
                actual_rps_all_this_tg = actual_metrics["all"].get(
                    "mean_rps_all",
                    actual_metrics["all"].get("mean_rps", 0.0),
                )
                actual_rps_ok_this_tg = actual_metrics["all"].get("mean_rps_ok", 0.0)
            else:
                actual_rps_all_this_tg = 0.0
                actual_rps_ok_this_tg = 0.0

            deviation_pct_all = 0.0
            deviation_pct_ok = 0.0
            if effective_target_rps > 0:
                deviation_pct_all = abs(
                    (actual_rps_all_this_tg - effective_target_rps) / effective_target_rps * 100.0
                )
                deviation_pct_ok = abs(
                    (actual_rps_ok_this_tg - effective_target_rps) / effective_target_rps * 100.0
                )

            rps_ok = deviation_pct_all <= tolerance_pct
            if ev_kind == "partial":
                stage_status = "PARTIAL" if rps_ok else "FAIL"
            else:
                stage_status = "PASS" if rps_ok else "FAIL"

            if stage_status == "FAIL":
                tg_results["status"] = "FAIL"
                overall_status = "FAIL"

            all_metrics = actual_metrics.get("all", {})
            total_requests = all_metrics.get("total_requests", 0)
            total_errors = all_metrics.get("total_errors", 0)
            avg_response_time_ms = all_metrics.get("avg_response_time_ms", 0.0)
            pct95_response_time_ms = all_metrics.get("pct95_response_time_ms", 0.0)
            max_response_time_ms = all_metrics.get("max_response_time_ms", 0.0)

            total_all_requests = total_requests + total_errors
            error_percentage = 0.0
            success_percentage = 0.0
            if total_all_requests > 0:
                error_percentage = (total_errors / total_all_requests) * 100.0
                success_percentage = (total_requests / total_all_requests) * 100.0

            plateau_duration_s = metrics_end_s - metrics_start_s
            expected_requests_this_tg = int(effective_target_rps * plateau_duration_s)
            actual_all_requests = total_requests + total_errors

            stage_result = {
                "stage_idx": stage_idx,
                "plateau_start_s": plateau_start,
                "plateau_end_s": plateau_end,
                "metrics_window_start_s": metrics_start_s,
                "metrics_window_end_s": metrics_end_s,
                "plateau_duration_s": plateau_duration_s,
                "target_rps": effective_target_rps,
                "target_rps_one_instance": base_target_rps,
                "total_target_rps": effective_target_rps,
                "actual_rps": actual_rps_all_this_tg,
                "actual_rps_all": actual_rps_all_this_tg,
                "actual_rps_ok": actual_rps_ok_this_tg,
                "deviation_pct": deviation_pct_all,
                "deviation_pct_all": deviation_pct_all,
                "deviation_pct_ok": deviation_pct_ok,
                "threads": threads_display,
                "threads_one_instance": threads_inst,
                "status": stage_status,
                "evaluation": ev_kind,
                "samplers": actual_metrics,
                "total_requests": total_requests,
                "total_errors": total_errors,
                "success_percentage": success_percentage,
                "error_percentage": error_percentage,
                "avg_response_time_ms": avg_response_time_ms,
                "pct95_response_time_ms": pct95_response_time_ms,
                "max_response_time_ms": max_response_time_ms,
                "expected_requests": expected_requests_this_tg,
                "actual_all_requests": actual_all_requests,
            }
            if runner_id is not None:
                stage_result["runner_id"] = runner_id

            tg_results["stages"].append(stage_result)

        thread_groups[tg_name] = tg_results

    return {"thread_groups": thread_groups, "overall_status": overall_status}


def _any_skip_or_partial(thread_groups: Dict[str, Any]) -> bool:
    return any(
        s.get("status") in ("SKIP", "PARTIAL")
        for tg in thread_groups.values()
        for s in tg.get("stages", [])
    )


def check_profile_compliance(
    test_run_id: str,
    influx_url: str,
    db_name: str,
    username: str = None,
    password: str = None,
    tolerance_pct: float = 10.0,
    aggregation_interval: float = 10.0,
) -> Dict[str, Any]:
    """Проверяет соответствие фактической нагрузки профилю."""
    print(f"Загрузка профиля для test_run={test_run_id}...")
    profile = get_profile_from_influx(test_run_id, influx_url, db_name, username, password)
    
    print(f"Загрузка событий переходов на ступени...")
    events = get_stage_events(test_run_id, influx_url, db_name, username, password)

    jmeter_runner_ids = list_jmeter_runner_ids(
        test_run_id, influx_url, db_name, username, password
    )
    runner_ids = list(jmeter_runner_ids)
    runner_source = "jmeter"
    if runner_ids:
        print(
            f"Раннеры (тег runner в jmeter): {len(runner_ids)} шт. — {', '.join(runner_ids)}"
        )
    else:
        meta_runner_ids = list_runner_ids_from_meta(
            test_run_id, influx_url, db_name, username, password
        )
        if meta_runner_ids:
            runner_ids = meta_runner_ids
            runner_source = "jmeter_runner_meta"
            print(
                "[INFO] В measurement jmeter нет runner/test_run тегов; "
                f"runner берём из jmeter_runner_meta: {len(runner_ids)} шт. — {', '.join(runner_ids)}"
            )
    has_jmeter_runner_tags = len(jmeter_runner_ids) > 0
    use_test_run_tag = has_jmeter_runner_tags
    
    # Собираем все transaction_names из профиля для поиска данных
    all_transaction_names = []
    for tg_name, tg_data in profile.get("thread_groups", {}).items():
        transaction_names = tg_data.get("transaction_names", [])
        all_transaction_names.extend(transaction_names)
    all_transaction_names = list(set(all_transaction_names))  # Убираем дубликаты

    earliest_tag_sql = _jmeter_tag_filter_sql(
        test_run_id, None, require_test_run_tag=use_test_run_tag
    )
    
    # Определяем время старта теста (одно на весь прогон для всех runner).
    # FUTURE (не реализовано): при нескольких подах можно ввести test_start_time_ns на каждый runner,
    # чтобы окна плато по метрикам jmeter совпадали со сдвигом старта каждого пода.
    # ВАЖНО: События load_stage_change приходят при входе в окно плато ступени (после ramp-up по профилю);
    # сами метрики jmeter могут появляться раньше (Backend Listener). Сравнение RPS/RT в отчёте — только
    # по интервалу [plateau_start_s, plateau_end_s), т.е. по чистому hold, без ramp-up/ramp-down между ступенями.
    # Поэтому используем самое раннее время из данных jmeter в диапазоне вокруг первого события.
    test_start_time_ns = None
    
    if events:
        # Находим самое раннее событие
        earliest_event_time_ns = None
        earliest_plateau_start_s = None
        
        for tg_events in events.values():
            for event in tg_events:
                event_time_ns = event.get("time_ns")
                if event_time_ns:
                    plateau_start_s = event.get("plateau_start_s", 0)
                    if earliest_event_time_ns is None or event_time_ns < earliest_event_time_ns:
                        earliest_event_time_ns = event_time_ns
                        earliest_plateau_start_s = plateau_start_s
        
        if earliest_event_time_ns and earliest_plateau_start_s is not None:
            # Пытаемся найти самое раннее время данных jmeter в очень широком диапазоне вокруг первого события
            # (например, за 86400 секунд = 24 часа до первого события и до первого события)
            # Это нужно, потому что события отправляются с задержкой, а данные начинают записываться раньше
            # Используем широкий диапазон, чтобы найти самое раннее время данных для этого теста
            try:
                # Используем очень широкий диапазон: за 30 дней до события и до события + 1 час
                # Это гарантирует, что мы найдем самое раннее время данных для этого теста
                # Данные могут начинать записываться намного раньше события (например, за 20+ часов)
                search_start_ns = earliest_event_time_ns - (30 * 24 * 3600 * 1_000_000_000)  # 30 дней назад
                search_end_ns = earliest_event_time_ns + (3600 * 1_000_000_000)  # 1 час вперед
                
                # Имена transaction — как в профиле (HTTP Request …, _UC_*, TG name и т.д.); узкий /^_UC.*/
                # часто не совпадает с первыми точками в Influx → тогда «не нашли данные» при живых метриках.
                tx_filter = _jmeter_transaction_filter_from_profile(all_transaction_names)
                query_earliest = f'''
                    SELECT time
                    FROM "jmeter"
                    WHERE time >= {search_start_ns} AND time <= {search_end_ns}
                    AND "statut" = 'ok'
                    AND {tx_filter}{earliest_tag_sql}
                    ORDER BY time ASC
                    LIMIT 1
                '''
                series_earliest = query_influx(query_earliest, influx_url, db_name, username, password)
                
                if not series_earliest or len(series_earliest) == 0:
                    print(
                        "[INFO] Не удалось найти самую раннюю точку jmeter по именам транзакций из профиля "
                        f"в окне вокруг первого события ступени; используется время старта из load_stage_change. "
                        f"(диапазон поиска: {search_start_ns} … {search_end_ns})"
                    )
                
                if series_earliest and len(series_earliest) > 0:
                    values_earliest = series_earliest[0].get("values", [])
                    if values_earliest and len(values_earliest) > 0:
                        time_value_earliest = values_earliest[0][0]
                        
                        # Конвертируем время в наносекунды
                        if isinstance(time_value_earliest, str):
                            dt = datetime.fromisoformat(time_value_earliest.replace('Z', '+00:00'))
                            earliest_data_time_ns = int(dt.timestamp() * 1_000_000_000)
                        elif isinstance(time_value_earliest, (int, float)):
                            earliest_data_time_ns = int(time_value_earliest)
                            # Проверяем масштаб времени
                            if earliest_data_time_ns < 1_000_000_000_000:
                                if earliest_data_time_ns < 1_000_000_000:
                                    earliest_data_time_ns = earliest_data_time_ns * 1_000_000_000
                                elif earliest_data_time_ns < 1_000_000_000_000:
                                    earliest_data_time_ns = earliest_data_time_ns * 1_000_000
                                elif earliest_data_time_ns < 1_000_000_000_000_000:
                                    earliest_data_time_ns = earliest_data_time_ns * 1_000
                        else:
                            earliest_data_time_ns = None
                        
                        if earliest_data_time_ns:
                            # Вычисляем время старта теста: самое раннее время данных - относительное время плато
                            # Если plateau_start_s = 0, то время данных уже является временем старта
                            test_start_time_ns = earliest_data_time_ns - (earliest_plateau_start_s * 1_000_000_000)
                            print(f"Определено время старта теста из данных jmeter: {test_start_time_ns} (из данных в {earliest_data_time_ns}, plateau_start_s={earliest_plateau_start_s}s, разница с событием: {(earliest_event_time_ns - earliest_data_time_ns) / 1_000_000_000:.2f} секунд)")
            except Exception as e:
                print(f"[WARN] Не удалось определить время старта из данных jmeter: {e}")
            
            # Если не удалось определить из данных jmeter, используем события (старый способ)
            # НО: если plateau_start_s = 0, то время события уже является временем старта теста
            if test_start_time_ns is None:
                if earliest_plateau_start_s == 0:
                    # Если plateau_start_s = 0, то событие отправляется в момент старта теста
                    # Но данные могут начать записываться раньше, поэтому ищем самое раннее время данных
                    # в широком диапазоне до события
                    try:
                        search_start_ns = earliest_event_time_ns - (86400 * 1_000_000_000)  # 24 часа назад
                        search_end_ns = earliest_event_time_ns
                        
                        query_earliest = f'''
                            SELECT time
                            FROM "jmeter"
                            WHERE time >= {search_start_ns} AND time <= {search_end_ns}
                            AND "statut" = 'ok'{earliest_tag_sql}
                            ORDER BY time ASC
                            LIMIT 1
                        '''
                        series_earliest = query_influx(query_earliest, influx_url, db_name, username, password)
                        
                        if series_earliest and len(series_earliest) > 0:
                            values_earliest = series_earliest[0].get("values", [])
                            if values_earliest and len(values_earliest) > 0:
                                time_value_earliest = values_earliest[0][0]
                                
                                # Конвертируем время в наносекунды
                                if isinstance(time_value_earliest, str):
                                    dt = datetime.fromisoformat(time_value_earliest.replace('Z', '+00:00'))
                                    earliest_data_time_ns = int(dt.timestamp() * 1_000_000_000)
                                elif isinstance(time_value_earliest, (int, float)):
                                    earliest_data_time_ns = int(time_value_earliest)
                                    if earliest_data_time_ns < 1_000_000_000_000:
                                        if earliest_data_time_ns < 1_000_000_000:
                                            earliest_data_time_ns = earliest_data_time_ns * 1_000_000_000
                                        elif earliest_data_time_ns < 1_000_000_000_000:
                                            earliest_data_time_ns = earliest_data_time_ns * 1_000_000
                                        elif earliest_data_time_ns < 1_000_000_000_000_000:
                                            earliest_data_time_ns = earliest_data_time_ns * 1_000
                                
                                if earliest_data_time_ns:
                                    # Если plateau_start_s = 0, то самое раннее время данных уже является временем старта теста
                                    test_start_time_ns = earliest_data_time_ns
                                    print(f"Определено время старта теста из данных jmeter (plateau_start_s=0): {test_start_time_ns} (из данных в {earliest_data_time_ns}, разница с событием: {(earliest_event_time_ns - earliest_data_time_ns) / 1_000_000_000:.2f} секунд)")
                    except Exception as e:
                        print(f"[WARN] Не удалось определить время старта из данных jmeter: {e}")
                
                # Если все еще не определено, используем события
                if test_start_time_ns is None:
                    # Вычисляем время старта теста: время события - относительное время плато
                    test_start_time_ns = earliest_event_time_ns - (earliest_plateau_start_s * 1_000_000_000)
                    print(f"Определено время старта теста из событий: {test_start_time_ns} (из события в {earliest_event_time_ns}, plateau_start_s={earliest_plateau_start_s}s)")
        else:
            print("[WARN] Предупреждение: не удалось определить время старта теста из событий. Используется относительное время.")
    else:
        print("[WARN] Предупреждение: события переходов на ступени не найдены. Используется относительное время.")
    
    test_end_time_ns: Optional[int] = None
    if test_start_time_ns is not None:
        test_end_time_ns = get_test_end_time_ns(
            test_run_id, influx_url, db_name, username, password
        )
        if test_end_time_ns is not None:
            print(
                f"Определено время окончания теста (последняя точка jmeter с тегом test_run): {test_end_time_ns}"
            )
        else:
            print(
                "[INFO] Не удалось получить время окончания по тегу test_run в jmeter; "
                "ступени не помечаются как SKIP/PARTIAL по факту ранней остановки."
            )
    
    results: Dict[str, Any] = {
        "test_run": test_run_id,
        "check_time": datetime.now().isoformat(),
        "thread_groups": {},
        "overall_status": "PASS",
        "tolerance_pct": tolerance_pct,
    }

    if runner_ids and has_jmeter_runner_tags:
        n_run = float(len(runner_ids))
        results["runner_ids"] = runner_ids
        results["runner_count"] = len(runner_ids)
        results["runner_source"] = runner_source
        results["runners"] = {}
        any_runner_fail = False
        for rid in sorted(runner_ids):
            comp = _compute_thread_group_results(
                profile,
                test_run_id,
                test_start_time_ns,
                test_end_time_ns,
                influx_url,
                db_name,
                username,
                password,
                tolerance_pct,
                aggregation_interval,
                runner_id=rid,
                use_test_run_tag=True,
                target_rps_multiplier=1.0,
                log_label=f"runner={rid}",
            )
            results["runners"][rid] = {
                "thread_groups": comp["thread_groups"],
                "overall_status": comp["overall_status"],
            }
            if comp["overall_status"] != "PASS":
                any_runner_fail = True

        agg = _compute_thread_group_results(
            profile,
            test_run_id,
            test_start_time_ns,
            test_end_time_ns,
            influx_url,
            db_name,
            username,
            password,
            tolerance_pct,
            aggregation_interval,
            runner_id=None,
            use_test_run_tag=True,
            target_rps_multiplier=n_run,
            log_label="кластер (все поды, цель = N × профиль инстанса)",
        )
        results["thread_groups"] = agg["thread_groups"]
        cluster_ok = agg["overall_status"] == "PASS"
        results["overall_status"] = (
            "PASS" if (not any_runner_fail and cluster_ok) else "FAIL"
        )
        results["aggregate_cluster"] = {
            "overall_status": agg["overall_status"],
            "runner_count": len(runner_ids),
            "description": "Суммарный факт jmeter по test_run сравнивается с целевым RPS × N (одинаковый JMX на N подах).",
        }
    elif runner_ids and not has_jmeter_runner_tags:
        # Fallback для distributed-режима: N берём из jmeter_runner_meta (JSR223), сравнение только кластерное.
        n_run = float(len(runner_ids))
        comp = _compute_thread_group_results(
            profile,
            test_run_id,
            test_start_time_ns,
            test_end_time_ns,
            influx_url,
            db_name,
            username,
            password,
            tolerance_pct,
            aggregation_interval,
            runner_id=None,
            use_test_run_tag=False,
            target_rps_multiplier=n_run,
            log_label="кластер fallback (N из jmeter_runner_meta, без runner тегов в jmeter)",
        )
        results["thread_groups"] = comp["thread_groups"]
        results["overall_status"] = comp["overall_status"]
        results["runner_ids"] = runner_ids
        results["runner_count"] = len(runner_ids)
        results["runner_source"] = runner_source
        results["aggregate_cluster"] = {
            "overall_status": comp["overall_status"],
            "runner_count": len(runner_ids),
            "description": (
                "Fallback cluster-mode: в measurement jmeter нет тегов runner/test_run; "
                "число раннеров взято из jmeter_runner_meta, целевой RPS масштабируется на N."
            ),
        }
    else:
        comp = _compute_thread_group_results(
            profile,
            test_run_id,
            test_start_time_ns,
            test_end_time_ns,
            influx_url,
            db_name,
            username,
            password,
            tolerance_pct,
            aggregation_interval,
            runner_id=None,
            use_test_run_tag=False,
            target_rps_multiplier=1.0,
            log_label="единый источник (без тега runner)",
        )
        results["thread_groups"] = comp["thread_groups"]
        results["overall_status"] = comp["overall_status"]

    # Сохраняем время старта теста и профиль для использования в проверке критериев
    results["test_start_time_ns"] = test_start_time_ns
    results["test_end_time_ns"] = test_end_time_ns
    results["profile"] = profile

    results["has_skip_or_partial_stages"] = _any_skip_or_partial(results["thread_groups"])
    if results.get("runners"):
        results["has_skip_or_partial_stages"] = results["has_skip_or_partial_stages"] or any(
            _any_skip_or_partial(r["thread_groups"]) for r in results["runners"].values()
        )

    return results


def _format_ns_utc(ns: Optional[int]) -> str:
    if ns is None:
        return "—"
    try:
        return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OSError, OverflowError, ValueError):
        return str(ns)


def _emit_thread_group_tables_html(thread_groups: Dict[str, Any], tol: float) -> str:
    """Фрагмент HTML: таблицы по всем Thread Groups (один блок отчёта)."""
    frags: List[str] = []
    sorted_tg_items = sorted(
        thread_groups.items(),
        key=lambda x: (x[1].get("status") != "FAIL", x[0]),
    )
    for tg_name, tg_data in sorted_tg_items:
        frags.append(f"""
    <h3>{tg_name} - <span class="status-{tg_data['status']}">{tg_data['status']}</span></h3>
    <table>
        <tr>
            <th>Ступень</th>
            <th>Время (сек)</th>
            <th class="col-rps-ok" title="Ожидаемый RPS для ЭТОЙ Thread Group = (CTT в RPM × потоки ЭТОЙ группы) / 60">Целевой RPS<br/>(эта Thread Group)</th>
            <th class="col-rps-ok" title="Реальный RPS по успешным запросам (statut='ok') для ЭТОЙ Thread Group за период плато">Факт. RPS OK<br/>(эта TG)</th>
            <th class="col-rps-ok" title="Отклонение по успешным запросам: |Факт. RPS OK - Целевой RPS| / Целевой RPS × 100%">Отклонение OK %</th>
            <th class="col-rps-all" title="Реальный RPS по всем запросам (успешные + ошибки) для ЭТОЙ Thread Group за период плато">Факт. RPS ALL<br/>(эта TG)</th>
            <th class="col-rps-all" title="Отклонение по всем запросам: |Факт. RPS ALL - Целевой RPS| / Целевой RPS × 100%">Отклонение ALL %</th>
            <th>Потоков</th>
            <th title="Длительность плато в секундах">Длительность<br/>(сек)</th>
            <th class="col-req" title="Общее количество успешных запросов за период плато">Запросов<br/>(успешных)</th>
            <th class="col-req" title="Общее количество запросов с ошибками за период плато">Запросов<br/>(с ошибками)</th>
            <th class="col-quality" title="Процент успешных = (успешные / (успешные + ошибки)) × 100%">% Успешных</th>
            <th class="col-quality" title="Процент неуспешных = (ошибки / (успешные + ошибки)) × 100%">% Неуспешных</th>
            <th class="col-req" title="Ожидаемое число запросов = целевой RPS × длительность оцениваемого плато (полный hold или укороченный интервал при PARTIAL)">Ожидаемое<br/>запросов</th>
            <th class="col-req" title="Фактическое количество запросов = Успешные + Ошибки">Фактическое<br/>запросов</th>
            <th class="col-req" title="Отклонение по количеству запросов = |Фактическое - Ожидаемое| / Ожидаемое × 100%">Откл. запросов %</th>
            <th class="col-req" title="Статус по количеству запросов (информационный): PASS ≤ 5%, WARN ≤ 10%, FAIL > 10%">Статус<br/>запросов</th>
            <th title="Среднее время отклика всех запросов за плато (мс)">Avg RT<br/>(мс)</th>
            <th title="95-й перцентиль времени отклика (мс)">P95 RT<br/>(мс)</th>
            <th title="Максимальное время отклика за плато (мс). Может содержать выбросы (outliers)">Max RT<br/>(мс)</th>
            <th title="PASS/FAIL по RPS: порог {tol:g}%. Также PARTIAL (успех на укороченном плато) и SKIP (ступень не достигнута) — см. пояснения вверху отчёта.">Статус</th>
        </tr>
""")
        # Сортируем ступени по stage_idx для правильного отображения
        sorted_stages = sorted(tg_data.get("stages", []), key=lambda x: x.get("stage_idx", 0))
        # Группируем по stage_idx, чтобы показывать каждую ступень только один раз
        stages_by_idx = {}
        for stage in sorted_stages:
            stage_idx = stage.get("stage_idx", 0)
            if stage_idx not in stages_by_idx:
                stages_by_idx[stage_idx] = stage
        
        # Показываем каждую ступень только один раз
        for stage_idx in sorted(stages_by_idx.keys()):
            stage = stages_by_idx[stage_idx]
            row_status = stage.get("status", "PASS")
            total_requests = stage.get("total_requests", 0)
            total_errors = stage.get("total_errors", 0)
            success_percentage = stage.get("success_percentage", 0.0)
            error_percentage = stage.get("error_percentage", 0.0)
            deviation_ok_pct = stage.get("deviation_pct_ok", 0.0)
            deviation_all_pct = stage.get("deviation_pct_all", stage.get("deviation_pct", 0.0))
            actual_rps_ok = stage.get("actual_rps_ok", 0.0)
            actual_rps_all = stage.get("actual_rps_all", stage.get("actual_rps", 0.0))
            plateau_duration_s = stage.get("plateau_duration_s", 0)
            expected_requests = stage.get("expected_requests", 0)
            actual_all_requests = stage.get("actual_all_requests", 0)
            avg_response_time_ms = stage.get("avg_response_time_ms", 0.0)
            pct95_response_time_ms = stage.get("pct95_response_time_ms", 0.0)
            max_response_time_ms = stage.get("max_response_time_ms", 0.0)
            
            skip_reason = stage.get("skip_reason") or ""
            status_title = f' title="{skip_reason}"' if skip_reason else ""
            
            if row_status == "SKIP":
                deviation_class = ""
                deviation_ok_cell = "—"
                actual_rps_ok_cell = "—"
                deviation_all_cell = "—"
                actual_rps_all_cell = "—"
                plateau_dur_cell = "—"
                req_ok_cell = "—"
                req_err_cell = "—"
                succ_pct_cell = "—"
                err_pct_cell = "—"
                exp_req_cell = "—"
                act_all_cell = "—"
                req_dev_cell = "—"
                req_status_cell = "—"
                avg_rt_cell = "—"
                p95_cell = "—"
                max_rt_cell = "—"
                requests_diff_class = ""
                req_status_class = ""
                errors_class = ""
                success_percentage_class = ""
                error_percentage_class = ""
                row_class = "row-skip"
                status_icon = "[—]"
            else:
                deviation_class = "deviation-good"
                if deviation_all_pct > 20:
                    deviation_class = "deviation-bad"
                elif deviation_all_pct > 10:
                    deviation_class = "deviation-warning"
                deviation_ok_cell = f"{deviation_ok_pct:.2f}%"
                actual_rps_ok_cell = f"{actual_rps_ok:.2f}"
                deviation_all_cell = f"{deviation_all_pct:.2f}%"
                actual_rps_all_cell = f"{actual_rps_all:.2f}"
                plateau_dur_cell = str(plateau_duration_s)
                req_ok_cell = f"<strong>{total_requests:,}</strong>"
                errors_class = "deviation-bad" if total_errors > 0 else ""
                req_err_cell = f"<strong>{total_errors:,}</strong>"

                success_percentage_class = "deviation-good"
                if success_percentage < 95.0:
                    success_percentage_class = "deviation-bad"
                elif success_percentage < 99.0:
                    success_percentage_class = "deviation-warning"
                succ_pct_cell = f"<strong>{success_percentage:.2f}%</strong>"

                error_percentage_class = "deviation-good"
                if error_percentage > 5.0:
                    error_percentage_class = "deviation-bad"
                elif error_percentage > 1.0:
                    error_percentage_class = "deviation-warning"
                err_pct_cell = f"<strong>{error_percentage:.2f}%</strong>"
                exp_req_cell = f"{expected_requests:,}"
                
                requests_diff = actual_all_requests - expected_requests
                requests_diff_class = "deviation-good"
                if expected_requests > 0:
                    requests_diff_pct = abs(requests_diff / expected_requests * 100.0)
                    if requests_diff_pct > 10.0:
                        requests_diff_class = "deviation-bad"
                    elif requests_diff_pct > 5.0:
                        requests_diff_class = "deviation-warning"
                else:
                    requests_diff_pct = 0.0
                act_all_cell = f"{actual_all_requests:,}"
                req_dev_cell = f"{requests_diff_pct:.2f}%"

                req_status = "PASS"
                req_status_class = "status-PASS"
                if requests_diff_pct > 10.0:
                    req_status = "FAIL"
                    req_status_class = "status-FAIL"
                elif requests_diff_pct > 5.0:
                    req_status = "WARN"
                    req_status_class = "status-PARTIAL"
                req_status_cell = f"{req_status}"
                
                avg_rt_class = "deviation-good"
                if avg_response_time_ms > 3000:
                    avg_rt_class = "deviation-bad"
                elif avg_response_time_ms > 1000:
                    avg_rt_class = "deviation-warning"
                avg_rt_cell = f'<span class="{avg_rt_class}">{avg_response_time_ms:.0f}</span>'
                
                pct95_rt_class = "deviation-good"
                if pct95_response_time_ms > 3000:
                    pct95_rt_class = "deviation-bad"
                elif pct95_response_time_ms > 1000:
                    pct95_rt_class = "deviation-warning"
                p95_cell = f'<span class="{pct95_rt_class}">{pct95_response_time_ms:.0f}</span>'
                
                max_rt_class = "deviation-good"
                if max_response_time_ms > 5000:
                    max_rt_class = "deviation-bad"
                elif max_response_time_ms > 3000:
                    max_rt_class = "deviation-warning"
                max_rt_cell = f'<span class="{max_rt_class}">{max_response_time_ms:.0f}</span>'
                
                if row_status == "PARTIAL":
                    row_class = "row-partial"
                    status_icon = "[~]"
                elif row_status == "PASS":
                    row_class = "row-pass"
                    status_icon = "[OK]"
                else:
                    row_class = "row-fail"
                    status_icon = "[FAIL]"
            
            frags.append(f"""
        <tr class="{row_class}">
            <td>{stage['stage_idx']}</td>
            <td>{stage['plateau_start_s']}-{stage['plateau_end_s']}</td>
            <td class="col-rps-ok"><strong>{stage['target_rps']:.2f}</strong></td>
            <td class="col-rps-ok">{actual_rps_ok_cell}</td>
            <td class="col-rps-ok {deviation_class}">{deviation_ok_cell}</td>
            <td class="col-rps-all">{actual_rps_all_cell}</td>
            <td class="col-rps-all {deviation_class}">{deviation_all_cell}</td>
            <td>{stage['threads']}</td>
            <td>{plateau_dur_cell}</td>
            <td class="col-req">{req_ok_cell}</td>
            <td class="col-req {errors_class}">{req_err_cell}</td>
            <td class="col-quality {success_percentage_class}">{succ_pct_cell}</td>
            <td class="col-quality {error_percentage_class}">{err_pct_cell}</td>
            <td class="col-req">{exp_req_cell}</td>
            <td class="col-req {requests_diff_class}">{act_all_cell}</td>
            <td class="col-req {requests_diff_class}">{req_dev_cell}</td>
            <td class="col-req {req_status_class}">{req_status_cell}</td>
            <td>{avg_rt_cell}</td>
            <td>{p95_cell}</td>
            <td>{max_rt_cell}</td>
            <td class="status-{stage['status']}"{status_title}>{status_icon} {stage['status']}</td>
        </tr>
""")
        
        # Добавляем итоговую строку для Thread Group (SKIP не входят в суммы и средние)
        if tg_data.get("stages"):
            eval_stages = [s for s in tg_data.get("stages", []) if s.get("status") != "SKIP"]
            total_target_rps = sum(s.get("target_rps", 0.0) for s in eval_stages)
            total_actual_rps_ok = sum(s.get("actual_rps_ok", 0.0) for s in eval_stages)
            total_actual_rps_all = sum(
                s.get("actual_rps_all", s.get("actual_rps", 0.0)) for s in eval_stages
            )
            total_requests = sum(s.get("total_requests", 0) for s in eval_stages)
            total_errors = sum(s.get("total_errors", 0) for s in eval_stages)
            total_expected = sum(s.get("expected_requests", 0) for s in eval_stages)
            total_actual_all = sum(s.get("actual_all_requests", 0) for s in eval_stages)
            stages_count = len(eval_stages)
            
            if stages_count > 0:
                avg_deviation_ok = (
                    sum(s.get("deviation_pct_ok", 0.0) for s in eval_stages) / stages_count
                )
                avg_deviation_all = (
                    sum(
                        s.get("deviation_pct_all", s.get("deviation_pct", 0.0))
                        for s in eval_stages
                    )
                    / stages_count
                )
                avg_rt = sum(s.get("avg_response_time_ms", 0.0) for s in eval_stages) / stages_count
            else:
                avg_deviation_ok = 0.0
                avg_deviation_all = 0.0
                avg_rt = 0.0
            
            max_pct95_rt = max((s.get("pct95_response_time_ms", 0.0) for s in eval_stages), default=0.0)
            max_max_rt = max((s.get("max_response_time_ms", 0.0) for s in eval_stages), default=0.0)
            
            total_all_requests = total_requests + total_errors
            success_percentage = (total_requests / total_all_requests * 100.0) if total_all_requests > 0 else 0.0
            error_percentage = (total_errors / total_all_requests * 100.0) if total_all_requests > 0 else 0.0
            
            summary_deviation_class = "deviation-good"
            if avg_deviation_all > 20:
                summary_deviation_class = "deviation-bad"
            elif avg_deviation_all > 10:
                summary_deviation_class = "deviation-warning"
            
            summary_success_class = "deviation-good"
            if success_percentage < 95.0:
                summary_success_class = "deviation-bad"
            elif success_percentage < 99.0:
                summary_success_class = "deviation-warning"

            summary_error_class = "deviation-good"
            if error_percentage > 5.0:
                summary_error_class = "deviation-bad"
            elif error_percentage > 1.0:
                summary_error_class = "deviation-warning"
            
            summary_status_icon = "[OK]" if tg_data['status'] == "PASS" else "[FAIL]"
            requests_diff_total = total_actual_all - total_expected
            requests_diff_total_pct = (
                abs(requests_diff_total / total_expected * 100.0) if total_expected > 0 else 0.0
            )
            req_total_class = "deviation-good"
            req_total_status = "PASS"
            req_total_status_class = "status-PASS"
            if requests_diff_total_pct > 10.0:
                req_total_class = "deviation-bad"
                req_total_status = "FAIL"
                req_total_status_class = "status-FAIL"
            elif requests_diff_total_pct > 5.0:
                req_total_class = "deviation-warning"
                req_total_status = "WARN"
                req_total_status_class = "status-PARTIAL"
            
            frags.append(f"""
        <tr class="summary-row">
            <td><strong>Итого</strong></td>
            <td>-</td>
            <td class="col-rps-ok"><strong>{total_target_rps:.2f}</strong></td>
            <td class="col-rps-ok"><strong>{total_actual_rps_ok:.2f}</strong></td>
            <td class="col-rps-ok {summary_deviation_class}"><strong>{avg_deviation_ok:.2f}%</strong></td>
            <td class="col-rps-all"><strong>{total_actual_rps_all:.2f}</strong></td>
            <td class="col-rps-all {summary_deviation_class}"><strong>{avg_deviation_all:.2f}%</strong></td>
            <td>-</td>
            <td>-</td>
            <td class="col-req"><strong>{total_requests:,}</strong></td>
            <td class="col-req {'deviation-bad' if total_errors > 0 else ''}"><strong>{total_errors:,}</strong></td>
            <td class="col-quality {summary_success_class}"><strong>{success_percentage:.2f}%</strong></td>
            <td class="col-quality {summary_error_class}"><strong>{error_percentage:.2f}%</strong></td>
            <td class="col-req"><strong>{total_expected:,}</strong></td>
            <td class="col-req {req_total_class}"><strong>{total_actual_all:,}</strong></td>
            <td class="col-req {req_total_class}"><strong>{requests_diff_total_pct:.2f}%</strong></td>
            <td class="col-req {req_total_status_class}"><strong>{req_total_status}</strong></td>
            <td><strong>{avg_rt:.0f}</strong></td>
            <td><strong>{max_pct95_rt:.0f}</strong></td>
            <td><strong>{max_max_rt:.0f}</strong></td>
            <td class="status-{tg_data['status']}"><strong>{summary_status_icon} {tg_data['status']}</strong></td>
        </tr>
""")
        
        frags.append("""
    </table>
""")
    return "".join(frags)


def generate_html_report(results: Dict[str, Any], output_path: Path) -> None:
    """Генерирует HTML отчёт."""
    tol = float(results.get("tolerance_pct", 10.0))
    coverage_warn = ""
    if results.get("has_skip_or_partial_stages"):
        coverage_warn = """
    <div class="coverage-warn">
        <strong>Внимание (ранняя остановка теста):</strong>
        В таблицах ниже есть ступени <span class="status-SKIP">SKIP</span> (плато физически не достигнуто)
        и/или <span class="status-PARTIAL">PARTIAL</span> (сравнение RPS и запросов выполнено только по обрезанному окну плато).
        Это не то же самое, что <span class="status-FAIL">FAIL</span> из‑за превышения допустимого отклонения на полной ступени.
        Общий статус относится только к ступеням, где проверка выполнялась.
    </div>"""

    test_start_disp = _format_ns_utc(results.get("test_start_time_ns"))
    test_end_disp = _format_ns_utc(results.get("test_end_time_ns"))
    if results.get("test_end_time_ns") is None:
        end_line = (
            "<strong>Конец теста по Influx:</strong> не определён — в <code>jmeter</code> нет последней точки с тегом "
            "<code>test_run</code>, совпадающим с этим прогоном (ступени не помечаются SKIP/PARTIAL по времени остановки)."
        )
    else:
        end_line = (
            f"<strong>Конец теста по Influx</strong> (последняя точка <code>jmeter</code> с <code>test_run</code>): "
            f"<code>{test_end_disp}</code>"
        )
    timing_lines = f"""    <p><strong>Порог отклонения RPS (эта проверка):</strong> {tol:g}%</p>
    <p style="font-size:0.95em; color:#444;"><strong>Оценка старта теста:</strong> {test_start_disp}</p>
    <p style="font-size:0.95em; color:#444;">{end_line}</p>
"""

    multi_note = ""
    if results.get("runners"):
        n = len(results["runners"])
        multi_note = f"""
        <div style="margin: 15px 0; padding: 15px; background-color: #e3f2fd; border-left: 3px solid #1976d2; border-radius: 3px;">
            <h5 style="margin-top: 0; color: #0d47a1;">5. Несколько подов (тег <code>runner</code> в Influx)</h5>
            <p style="margin: 5px 0;">Обнаружено подов: <strong>{n}</strong>. Ниже сначала таблицы <strong>по каждому поду</strong> (целевой RPS как в профиле для одного JMX), затем блок <strong>«Сводка кластера»</strong>: целевой RPS × {n}, факт — сумма метрик по всем подам (фильтр только <code>test_run</code>).</p>
            <p style="margin: 5px 0;">Общий статус <strong>PASS</strong>, только если каждый под и сводка кластера укладываются в порог <strong>{tol:g}%</strong> по RPS.</p>
        </div>
"""
    elif results.get("aggregate_cluster") and results.get("runner_count", 0) > 1:
        n = int(results.get("runner_count", 0))
        src = escape(str(results.get("runner_source", "unknown")))
        multi_note = f"""
        <div style="margin: 15px 0; padding: 15px; background-color: #fff3e0; border-left: 3px solid #ef6c00; border-radius: 3px;">
            <h5 style="margin-top: 0; color: #e65100;">5. Кластерный fallback-режим</h5>
            <p style="margin: 5px 0;">В measurement <code>jmeter</code> не обнаружены теги <code>runner/test_run</code>, поэтому сравнение выполнено только на уровне кластера: целевой RPS × <strong>{n}</strong>, фактический RPS — суммарно по всем источникам.</p>
            <p style="margin: 5px 0;">Число раннеров получено из measurement <code>{src}</code>. Разрез «по каждому поду» в этом режиме недоступен.</p>
        </div>
"""

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Проверка профиля нагрузки - {results['test_run']}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        h1 {{ color: #333; }}
        .status-PASS {{ color: green; font-weight: bold; }}
        .status-FAIL {{ color: red; font-weight: bold; }}
        .status-SKIP {{ color: #616161; font-weight: bold; }}
        .status-PARTIAL {{ color: #e65100; font-weight: bold; }}
        .coverage-warn {{
            background-color: #fff8e1;
            border-left: 4px solid #ff9800;
            padding: 12px 16px;
            margin: 16px 0;
            border-radius: 4px;
        }}
        table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f2f2f2; cursor: help; }}
        th:hover {{ background-color: #e0e0e0; }}
        /* Группы колонок для более понятного визуального сравнения */
        .col-rps-ok {{ background-color: #eaf4ff; }}
        .col-rps-all {{ background-color: #f1ecff; }}
        .col-req {{ background-color: #fff8e8; }}
        .col-quality {{ background-color: #edf8ef; }}
        th.col-rps-ok {{ background-color: #d8eaff; }}
        th.col-rps-all {{ background-color: #e3dbff; }}
        th.col-req {{ background-color: #ffefc9; }}
        th.col-quality {{ background-color: #d9efdd; }}
        /* В сводной таблице также применяем цветовые группы с приоритетом */
        .summary-table-header th {{
            color: #1f2937 !important;
            font-weight: bold !important;
        }}
        .summary-table-header th.col-rps-ok {{ background-color: #cfe3fb !important; }}
        .summary-table-header th.col-rps-all {{ background-color: #d9cdfd !important; }}
        .summary-table-header th.col-req {{ background-color: #ffe5ad !important; }}
        .summary-table-header th.col-quality {{ background-color: #cfe8d4 !important; }}
        /* Тонкие вертикальные разделители между блоками */
        table tr th:nth-child(5), table tr td:nth-child(5),
        table tr th:nth-child(7), table tr td:nth-child(7),
        table tr th:nth-child(13), table tr td:nth-child(13),
        table tr th:nth-child(17), table tr td:nth-child(17) {{
            border-right: 2px solid #c7ced6 !important;
        }}
        /* Decision columns чуть ярче внутри блоков */
        table tr th:nth-child(5), table tr td:nth-child(5) {{ background-color: #dfefff !important; }}
        table tr th:nth-child(7), table tr td:nth-child(7) {{ background-color: #e8ddff !important; }}
        table tr th:nth-child(16), table tr td:nth-child(16) {{ background-color: #ffeec7 !important; }}
        /* RT-блок приглушаем */
        table tr th:nth-child(18), table tr td:nth-child(18),
        table tr th:nth-child(19), table tr td:nth-child(19),
        table tr th:nth-child(20), table tr td:nth-child(20) {{ background-color: #f6f7f9 !important; }}
        /* Фоновая подсветка статуса запросов */
        td:nth-child(17).status-PASS {{ background-color: #e6f4ea !important; color: #1b5e20; }}
        td:nth-child(17).status-PARTIAL {{ background-color: #fff4e5 !important; color: #8a4b00; }}
        td:nth-child(17).status-FAIL {{ background-color: #fdecea !important; color: #b71c1c; }}
        .deviation-good {{ color: green; }}
        .deviation-warning {{ color: orange; }}
        .deviation-bad {{ color: red; }}
        tr:nth-child(even) {{ background-color: #f9f9f9; }}
        tr.row-pass {{ background-color: #e8f5e9; }}
        tr.row-fail {{ background-color: #ffebee; }}
        tr.row-skip {{ background-color: #f5f5f5; color: #424242; }}
        tr.row-partial {{ background-color: #fffde7; }}
        tr.summary-row {{ background-color: #e3f2fd; font-weight: bold; border-top: 2px solid #2196F3; border-bottom: 2px solid #2196F3; }}
        .status-icon {{ font-size: 1.2em; margin-right: 5px; }}
        .compact-number {{ font-size: 0.9em; }}
    </style>
</head>
<body>
    <h1>Проверка профиля нагрузки</h1>
    <p><strong>Test Run ID:</strong> {results['test_run']}</p>
    <p><strong>Время проверки:</strong> {results['check_time']}</p>
{timing_lines}
    <p><strong>Общий статус:</strong> <span class="status-{results['overall_status']}">{results['overall_status']}</span></p>
{coverage_warn}
    <h2>Результаты по Thread Groups</h2>
    <div style="background-color: #f0f0f0; padding: 20px; margin: 20px 0; border-left: 4px solid #4CAF50; border-radius: 5px;">
        <h4 style="margin-top: 0; color: #2c3e50;">Пояснения по расчетам RPS:</h4>
        
        <div style="margin: 15px 0; padding: 15px; background-color: #ffffff; border-radius: 3px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
            <h5 style="margin-top: 0; color: #2196F3;">1. Целевой RPS (эта Thread Group)</h5>
            <p style="margin: 5px 0;"><strong>Когда рассчитывается:</strong> ДО теста (при парсинге JMX)</p>
            <p style="margin: 5px 0;"><strong>Что показывает:</strong> Ожидаемый RPS для ЭТОЙ конкретной Thread Group</p>
            <p style="margin: 5px 0;"><strong>Формула:</strong> <code style="background-color: #f5f5f5; padding: 2px 6px; border-radius: 3px;">(Constant Throughput Timer в RPM × количество потоков ЭТОЙ группы) / 60</code></p>
            <p style="margin: 5px 0;"><strong>Пример:</strong> CTT = 10 RPM, потоков = 10 → (10 × 10) / 60 = <strong>1.67 RPS</strong></p>
            <p style="margin: 5px 0; color: #666; font-style: italic;">Это уже сумма всех потоков ЭТОЙ Thread Group. Если у вас 10 потоков, каждый делает 10 RPM, то всего эта Thread Group должна выдавать 100 RPM = 1.67 RPS.</p>
        </div>
        
        <div style="margin: 15px 0; padding: 15px; background-color: #ffffff; border-radius: 3px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
            <h5 style="margin-top: 0; color: #2196F3;">2. Фактический RPS (эта Thread Group)</h5>
            <p style="margin: 5px 0;"><strong>Когда рассчитывается:</strong> ПОСЛЕ теста (из InfluxDB)</p>
            <p style="margin: 5px 0;"><strong>Что показывает:</strong> Реальный RPS, который был достигнут ЭТОЙ Thread Group за <strong>оцениваемое окно плато</strong> (полное из профиля или укороченное при ранней остановке и статусе PARTIAL).</p>
            <p style="margin: 5px 0;"><strong>Источник данных:</strong> InfluxDB, measurement <code style="background-color: #f5f5f5; padding: 2px 6px; border-radius: 3px;">jmeter</code> (JMeter Backend Listener). Для каждой Thread Group считаются только серии с именем транзакции <strong><code>_UC*</code></strong> (Transaction Controller): семплеры внутри уже входят в count родительской транзакции. Если в профиле (<code>load_profile</code>) для TG перечислены такие имена — в запросе OR по ним; если в профиле нет имён с префиксом <code>_UC</code>, используется условие <code>transaction =~ /^_UC.*/</code> в окне времени (при параллельных TG возможен суммарный учёт всех <code>_UC</code> в этот момент).</p>
            <p style="margin: 5px 0;"><strong>Формула:</strong> <code style="background-color: #f5f5f5; padding: 2px 6px; border-radius: 3px;">(Успешные запросы + Запросы с ошибками) / Длительность оцениваемого плато (сек)</code></p>
            <p style="margin: 5px 0; color: #666; font-style: italic;">Для попадания в профиль учитывается вся поданная интенсивность: <code style="background-color: #f5f5f5; padding: 2px 6px; border-radius: 3px;">statut = 'ok'</code> и <code style="background-color: #f5f5f5; padding: 2px 6px; border-radius: 3px;">statut = 'ko'</code>. Качество остаётся в отдельных колонках ошибок и % ошибок.</p>
        </div>
        
        <div style="margin: 15px 0; padding: 15px; background-color: #e8f4fd; border-left: 3px solid #2196F3; border-radius: 3px;">
            <h5 style="margin-top: 0; color: #1565c0;">3. Статусы ступени (ранний стоп)</h5>
            <p style="margin: 5px 0;"><strong>PASS / FAIL</strong> — полное окно плато по профилю; FAIL, если отклонение RPS &gt; {tol:g}%.</p>
            <p style="margin: 5px 0;"><strong>PARTIAL</strong> — тест закончился внутри плато; RPS и «ожидаемые запросы» считаются по <strong>укороченной</strong> длительности; PARTIAL = по этому куску отклонение ≤ {tol:g}% (не путать с FAIL «не выдержали профиль» на полной ступени).</p>
            <p style="margin: 5px 0;"><strong>SKIP</strong> — до плато этой ступени тест не дошёл; метрики не считаются, в общий FAIL не входит.</p>
            <p style="margin: 5px 0; color: #666; font-style: italic;">Нужны время старта из данных и последняя точка <code>jmeter</code> с тегом <code>test_run</code> (см. блок выше). Иначе все ступени считаются по полному окну из профиля.</p>
        </div>
        
        <div style="margin: 15px 0; padding: 15px; background-color: #fff3cd; border-left: 3px solid #ffc107; border-radius: 3px;">
            <h5 style="margin-top: 0; color: #856404;">4. Отклонение % и порог</h5>
            <p style="margin: 5px 0;"><strong>ВАЖНО:</strong> Отклонение считается для <strong>каждой Thread Group отдельно</strong>, а не для суммы всех групп!</p>
            <p style="margin: 5px 0;"><strong>Формулы:</strong></p>
            <p style="margin: 5px 0;"><code style="background-color: #fff8dc; padding: 2px 6px; border-radius: 3px;">Отклонение OK % = |Факт. RPS OK - Целевой RPS| / Целевой RPS × 100%</code></p>
            <p style="margin: 5px 0;"><code style="background-color: #fff8dc; padding: 2px 6px; border-radius: 3px;">Отклонение ALL % = |Факт. RPS ALL - Целевой RPS| / Целевой RPS × 100%</code></p>
            <p style="margin: 5px 0;"><strong>Порог для PASS / FAIL по RPS:</strong> используется <strong>Отклонение ALL %</strong> (попадание в профиль по всей поданной интенсивности, включая ошибки). PASS если ≤ <strong>{tol:g}%</strong>, FAIL если &gt; <strong>{tol:g}%</strong>.</p>
            <p style="margin: 5px 0; color: #666; font-style: italic;">Статус по количеству запросов — отдельный информационный индикатор и не влияет на общий PASS/FAIL по профилю.</p>
        </div>
{multi_note}
    </div>
"""

    if results.get("runners"):
        for rid in sorted(results["runners"].keys()):
            rinfo = results["runners"][rid]
            html += (
                f'    <h2>Под (runner): {escape(rid)} — '
                f'<span class="status-{rinfo["overall_status"]}">{rinfo["overall_status"]}</span></h2>\n'
                f'    <p style="color:#555;font-size:0.95em;">Целевой RPS — из профиля для <strong>одного</strong> экземпляра JMX; факт — только точки <code>jmeter</code> с этим <code>runner</code>.</p>\n'
            )
            html += _emit_thread_group_tables_html(rinfo["thread_groups"], tol)
        nclus = len(results["runners"])
        html += (
            f'    <h2>Сводка кластера (все поды, N = {nclus})</h2>\n'
            f'    <p style="color:#555;font-size:0.95em;">Фактический RPS — сумма по всем подам (фильтр <code>test_run</code>); целевой — значение из профиля × {nclus} (одинаковый план на каждом поде).</p>\n'
        )
    elif results.get("aggregate_cluster") and results.get("runner_count", 0) > 1:
        nclus = int(results.get("runner_count", 0))
        html += (
            f'    <h2>Сводка кластера (fallback, N = {nclus})</h2>\n'
            f'    <p style="color:#555;font-size:0.95em;">Пер-под таблицы недоступны: теги <code>runner</code> в measurement <code>jmeter</code> отсутствуют.</p>\n'
        )

    html += _emit_thread_group_tables_html(results["thread_groups"], tol)
    
    # Добавляем сводную таблицу по всем Thread Groups
    html += """
    <hr style="margin: 40px 0; border: 2px solid #333;" />
    <div style="background-color: #e8f5e9; padding: 20px; margin: 20px 0; border-left: 5px solid #4CAF50; border-radius: 5px;">
        <h2 style="margin-top: 0; color: #2e7d32;">Сводная статистика по всем Thread Groups</h2>
        <p style="color: #666; font-style: italic; margin-bottom: 0;">Суммарные метрики всех Thread Groups вместе для каждой ступени</p>
    </div>
    <table style="box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
        <tr class="summary-table-header">
            <th title="Номер ступени нагрузки">Ступень</th>
            <th title="Временной интервал плато (секунды от начала теста)">Время (сек)</th>
            <th class="col-rps-ok" title="Сумма целевых RPS всех Thread Groups на этой ступени">Целевой RPS<br/>(сумма всех групп)</th>
            <th class="col-rps-ok" title="Сумма фактических RPS по успешным запросам (statut='ok')">Факт. RPS OK<br/>(сумма групп)</th>
            <th class="col-rps-ok" title="Отклонение по успешным запросам: |Факт. RPS OK - Целевой RPS| / Целевой RPS × 100%">Отклонение OK %</th>
            <th class="col-rps-all" title="Сумма фактических RPS по всем запросам (успешные + ошибки)">Факт. RPS ALL<br/>(сумма групп)</th>
            <th class="col-rps-all" title="Отклонение по всем запросам: |Факт. RPS ALL - Целевой RPS| / Целевой RPS × 100%">Отклонение ALL %</th>
            <th title="Сумма потоков всех Thread Groups">Всего<br/>потоков</th>
            <th title="Длительность плато в секундах">Длительность<br/>(сек)</th>
            <th class="col-req" title="Сумма успешных запросов всех Thread Groups">Запросов<br/>(успешных)</th>
            <th class="col-req" title="Сумма ошибок всех Thread Groups">Запросов<br/>(с ошибками)</th>
            <th class="col-quality" title="Общий процент успешных = (сумма успешных / (сумма успешных + сумма ошибок)) × 100%">% Успешных</th>
            <th class="col-quality" title="Общий процент неуспешных = (сумма ошибок / (сумма успешных + сумма ошибок)) × 100%">% Неуспешных</th>
            <th class="col-req" title="Сумма ожидаемых запросов всех Thread Groups">Ожидаемое<br/>запросов</th>
            <th class="col-req" title="Сумма фактических запросов всех Thread Groups">Фактическое<br/>запросов</th>
            <th class="col-req" title="Отклонение по количеству запросов = |Фактическое - Ожидаемое| / Ожидаемое × 100%">Откл. запросов %</th>
            <th class="col-req" title="Статус по количеству запросов (информационный): PASS ≤ 5%, WARN ≤ 10%, FAIL > 10%">Статус<br/>запросов</th>
            <th title="Среднее время отклика по всем Thread Groups (взвешенное)">Avg RT<br/>(мс)</th>
            <th title="Максимальное P95 время отклика среди всех Thread Groups">P95 RT<br/>(мс)</th>
            <th title="Максимальное время отклика среди всех Thread Groups">Max RT<br/>(мс)</th>
            <th title="Статус: PASS/FAIL по отклонению RPS с порогом {tol:g}%; SKIP/PARTIAL — см. пояснения.">Статус</th>
        </tr>
"""
    
    # Собираем данные по всем Thread Groups для каждой ступени
    all_stages_summary = {}  # {stage_idx: {метрики}}
    tolerance_pct = tol
    
    for tg_name, tg_data in results.get("thread_groups", {}).items():
        for stage in tg_data.get("stages", []):
            stage_idx = stage.get("stage_idx", 0)
            st = stage.get("status", "PASS")
            
            if stage_idx not in all_stages_summary:
                all_stages_summary[stage_idx] = {
                    "stage_idx": stage_idx,
                    "plateau_start_s": stage.get("plateau_start_s", 0),
                    "plateau_end_s": stage.get("plateau_end_s", 0),
                    "plateau_duration_s": 0,
                    "total_target_rps": 0.0,
                    "total_actual_rps_ok": 0.0,
                    "total_actual_rps_all": 0.0,
                    "total_threads": 0,
                    "total_requests": 0,
                    "total_errors": 0,
                    "total_expected_requests": 0,
                    "total_actual_all_requests": 0,
                    "weighted_avg_rt": 0.0,
                    "total_requests_for_avg": 0,
                    "max_pct95_rt": 0.0,
                    "max_max_rt": 0.0,
                    "skip_only": True,
                    "any_partial": False,
                }
            
            summary = all_stages_summary[stage_idx]
            if st == "SKIP":
                continue
            
            summary["skip_only"] = False
            if st == "PARTIAL":
                summary["any_partial"] = True
            
            summary["total_target_rps"] += stage.get("target_rps", 0.0)
            summary["total_actual_rps_ok"] += stage.get("actual_rps_ok", 0.0)
            summary["total_actual_rps_all"] += stage.get(
                "actual_rps_all",
                stage.get("actual_rps", 0.0),
            )
            summary["total_threads"] += stage.get("threads", 0)
            summary["total_requests"] += stage.get("total_requests", 0)
            summary["total_errors"] += stage.get("total_errors", 0)
            summary["total_expected_requests"] += stage.get("expected_requests", 0)
            summary["total_actual_all_requests"] += stage.get("actual_all_requests", 0)
            pdur = stage.get("plateau_duration_s", 0)
            summary["plateau_duration_s"] = max(summary["plateau_duration_s"], pdur)
            
            tg_requests = stage.get("total_requests", 0)
            tg_avg_rt = stage.get("avg_response_time_ms", 0.0)
            if tg_requests > 0:
                summary["weighted_avg_rt"] += tg_avg_rt * tg_requests
                summary["total_requests_for_avg"] += tg_requests
            
            summary["max_pct95_rt"] = max(summary["max_pct95_rt"], stage.get("pct95_response_time_ms", 0.0))
            summary["max_max_rt"] = max(summary["max_max_rt"], stage.get("max_response_time_ms", 0.0))
    
    # Вычисляем взвешенное среднее время отклика
    for stage_idx, summary in all_stages_summary.items():
        if summary["total_requests_for_avg"] > 0:
            summary["weighted_avg_rt"] = summary["weighted_avg_rt"] / summary["total_requests_for_avg"]
    
    # Сортируем ступени и выводим сводную таблицу
    for stage_idx in sorted(all_stages_summary.keys()):
        summary = all_stages_summary[stage_idx]
        
        deviation_ok_pct = 0.0
        deviation_all_pct = 0.0
        if summary.get("skip_only"):
            status = "SKIP"
            deviation_ok_display = "—"
            deviation_all_display = "—"
            deviation_class = ""
            summary_row_class = "row-skip"
            summary_status_icon = "[—]"
            total_all_requests = 0
            success_percentage = 0.0
            error_percentage = 0.0
            success_percentage_class = ""
            error_percentage_class = ""
            requests_diff_class = ""
            requests_diff_pct = 0.0
            req_status = "—"
            req_status_class = ""
        else:
            if summary["total_target_rps"] > 0:
                deviation_ok_pct = abs(
                    (summary["total_actual_rps_ok"] - summary["total_target_rps"])
                    / summary["total_target_rps"]
                    * 100.0
                )
                deviation_all_pct = abs(
                    (summary["total_actual_rps_all"] - summary["total_target_rps"])
                    / summary["total_target_rps"]
                    * 100.0
                )
            status = "PASS" if deviation_all_pct <= tolerance_pct else "FAIL"
            if summary.get("any_partial") and status == "PASS":
                status = "PARTIAL"
            deviation_ok_display = f"{deviation_ok_pct:.2f}%"
            deviation_all_display = f"{deviation_all_pct:.2f}%"
            deviation_class = "deviation-good"
            if deviation_all_pct > 20:
                deviation_class = "deviation-bad"
            elif deviation_all_pct > 10:
                deviation_class = "deviation-warning"
            if status == "PARTIAL":
                summary_row_class = "row-partial"
                summary_status_icon = "[~]"
            elif status == "FAIL":
                summary_row_class = "row-fail"
                summary_status_icon = "[FAIL]"
            else:
                summary_row_class = "row-pass"
                summary_status_icon = "[OK]"
            
            total_all_requests = summary["total_requests"] + summary["total_errors"]
            success_percentage = 0.0
            error_percentage = 0.0
            if total_all_requests > 0:
                success_percentage = (summary["total_requests"] / total_all_requests) * 100.0
                error_percentage = (summary["total_errors"] / total_all_requests) * 100.0

            success_percentage_class = "deviation-good"
            if success_percentage < 95.0:
                success_percentage_class = "deviation-bad"
            elif success_percentage < 99.0:
                success_percentage_class = "deviation-warning"

            error_percentage_class = "deviation-good"
            if error_percentage > 5.0:
                error_percentage_class = "deviation-bad"
            elif error_percentage > 1.0:
                error_percentage_class = "deviation-warning"
            
            requests_diff = summary["total_actual_all_requests"] - summary["total_expected_requests"]
            requests_diff_class = "deviation-good"
            requests_diff_pct = 0.0
            req_status = "PASS"
            req_status_class = "status-PASS"
            if summary["total_expected_requests"] > 0:
                requests_diff_pct = abs(requests_diff / summary["total_expected_requests"] * 100.0)
                if requests_diff_pct > 10.0:
                    requests_diff_class = "deviation-bad"
                    req_status = "FAIL"
                    req_status_class = "status-FAIL"
                elif requests_diff_pct > 5.0:
                    requests_diff_class = "deviation-warning"
                    req_status = "WARN"
                    req_status_class = "status-PARTIAL"
        
        html += f"""
        <tr class="{summary_row_class}">
            <td><strong>{summary['stage_idx']}</strong></td>
            <td>{summary['plateau_start_s']}-{summary['plateau_end_s']}</td>
            <td class="col-rps-ok"><strong>{summary['total_target_rps']:.2f}</strong></td>
            <td class="col-rps-ok"><strong>{summary['total_actual_rps_ok']:.2f}</strong></td>
            <td class="col-rps-ok {deviation_class}"><strong>{deviation_ok_display}</strong></td>
            <td class="col-rps-all"><strong>{summary['total_actual_rps_all']:.2f}</strong></td>
            <td class="col-rps-all {deviation_class}"><strong>{deviation_all_display}</strong></td>
            <td><strong>{summary['total_threads']}</strong></td>
            <td>{summary['plateau_duration_s'] if not summary.get('skip_only') else '—'}</td>
            <td class="col-req"><strong>{summary['total_requests']:,}</strong></td>
            <td class="col-req {'deviation-bad' if summary['total_errors'] > 0 else ''}"><strong>{summary['total_errors']:,}</strong></td>
            <td class="col-quality {success_percentage_class}"><strong>{success_percentage:.2f}%</strong></td>
            <td class="col-quality {error_percentage_class}"><strong>{error_percentage:.2f}%</strong></td>
            <td class="col-req">{summary['total_expected_requests']:,}</td>
            <td class="col-req {requests_diff_class}"><strong>{summary['total_actual_all_requests']:,}</strong></td>
            <td class="col-req {requests_diff_class}"><strong>{requests_diff_pct:.2f}%</strong></td>
            <td class="col-req {req_status_class}"><strong>{req_status}</strong></td>
            <td>{summary['weighted_avg_rt']:.0f}</td>
            <td>{summary['max_pct95_rt']:.0f}</td>
            <td>{summary['max_max_rt']:.0f}</td>
            <td class="status-{status}"><strong>{summary_status_icon} {status}</strong></td>
        </tr>
"""
    
    # Добавляем итоговую строку для сводной таблицы
    if all_stages_summary:
        total_all_target_rps = sum(s["total_target_rps"] for s in all_stages_summary.values())
        total_all_actual_rps_ok = sum(s["total_actual_rps_ok"] for s in all_stages_summary.values())
        total_all_actual_rps_all = sum(s["total_actual_rps_all"] for s in all_stages_summary.values())
        total_all_requests = sum(s["total_requests"] for s in all_stages_summary.values())
        total_all_errors = sum(s["total_errors"] for s in all_stages_summary.values())
        total_all_expected = sum(s["total_expected_requests"] for s in all_stages_summary.values())
        total_all_actual_all = sum(s["total_actual_all_requests"] for s in all_stages_summary.values())
        stages_count = len(all_stages_summary)
        
        if stages_count > 0:
            deviations = []
            for s in all_stages_summary.values():
                if s.get("skip_only"):
                    continue
                if s["total_target_rps"] > 0:
                    dev_ok = abs((s["total_actual_rps_ok"] - s["total_target_rps"]) / s["total_target_rps"] * 100.0)
                    dev_all = abs((s["total_actual_rps_all"] - s["total_target_rps"]) / s["total_target_rps"] * 100.0)
                    deviations.append((dev_ok, dev_all))
            avg_all_deviation_ok = (
                sum(d[0] for d in deviations) / len(deviations) if deviations else 0.0
            )
            avg_all_deviation_all = (
                sum(d[1] for d in deviations) / len(deviations) if deviations else 0.0
            )
            evaluated_summaries = [s for s in all_stages_summary.values() if not s.get("skip_only")]
            avg_all_rt = (
                sum(s["weighted_avg_rt"] for s in evaluated_summaries) / len(evaluated_summaries)
                if evaluated_summaries
                else 0.0
            )
        else:
            avg_all_deviation_ok = 0.0
            avg_all_deviation_all = 0.0
            avg_all_rt = 0.0
        
        max_all_pct95_rt = max((s["max_pct95_rt"] for s in all_stages_summary.values()), default=0.0)
        max_all_max_rt = max((s["max_max_rt"] for s in all_stages_summary.values()), default=0.0)
        
        total_all_all_requests = total_all_requests + total_all_errors
        success_percentage_all = (total_all_requests / total_all_all_requests * 100.0) if total_all_all_requests > 0 else 0.0
        error_percentage_all = (total_all_errors / total_all_all_requests * 100.0) if total_all_all_requests > 0 else 0.0
        
        summary_all_deviation_class = "deviation-good"
        if avg_all_deviation_all > 20:
            summary_all_deviation_class = "deviation-bad"
        elif avg_all_deviation_all > 10:
            summary_all_deviation_class = "deviation-warning"
        
        summary_all_success_class = "deviation-good"
        if success_percentage_all < 95.0:
            summary_all_success_class = "deviation-bad"
        elif success_percentage_all < 99.0:
            summary_all_success_class = "deviation-warning"

        summary_all_error_class = "deviation-good"
        if error_percentage_all > 5.0:
            summary_all_error_class = "deviation-bad"
        elif error_percentage_all > 1.0:
            summary_all_error_class = "deviation-warning"
        
        overall_status = "PASS" if avg_all_deviation_all <= 10.0 else "FAIL"
        overall_status_icon = "[OK]" if overall_status == "PASS" else "[FAIL]"
        req_overall_diff = total_all_actual_all - total_all_expected
        req_overall_diff_pct = (
            abs(req_overall_diff / total_all_expected * 100.0) if total_all_expected > 0 else 0.0
        )
        req_overall_class = "deviation-good"
        req_overall_status = "PASS"
        req_overall_status_class = "status-PASS"
        if req_overall_diff_pct > 10.0:
            req_overall_class = "deviation-bad"
            req_overall_status = "FAIL"
            req_overall_status_class = "status-FAIL"
        elif req_overall_diff_pct > 5.0:
            req_overall_class = "deviation-warning"
            req_overall_status = "WARN"
            req_overall_status_class = "status-PARTIAL"
        
        html += f"""
        <tr class="summary-row">
            <td><strong>Итого</strong></td>
            <td>-</td>
            <td class="col-rps-ok"><strong>{total_all_target_rps:.2f}</strong></td>
            <td class="col-rps-ok"><strong>{total_all_actual_rps_ok:.2f}</strong></td>
            <td class="col-rps-ok {summary_all_deviation_class}"><strong>{avg_all_deviation_ok:.2f}%</strong></td>
            <td class="col-rps-all"><strong>{total_all_actual_rps_all:.2f}</strong></td>
            <td class="col-rps-all {summary_all_deviation_class}"><strong>{avg_all_deviation_all:.2f}%</strong></td>
            <td>-</td>
            <td>-</td>
            <td class="col-req"><strong>{total_all_requests:,}</strong></td>
            <td class="col-req {'deviation-bad' if total_all_errors > 0 else ''}"><strong>{total_all_errors:,}</strong></td>
            <td class="col-quality {summary_all_success_class}"><strong>{success_percentage_all:.2f}%</strong></td>
            <td class="col-quality {summary_all_error_class}"><strong>{error_percentage_all:.2f}%</strong></td>
            <td class="col-req"><strong>{total_all_expected:,}</strong></td>
            <td class="col-req {req_overall_class}"><strong>{total_all_actual_all:,}</strong></td>
            <td class="col-req {req_overall_class}"><strong>{req_overall_diff_pct:.2f}%</strong></td>
            <td class="col-req {req_overall_status_class}"><strong>{req_overall_status}</strong></td>
            <td><strong>{avg_all_rt:.0f}</strong></td>
            <td><strong>{max_all_pct95_rt:.0f}</strong></td>
            <td><strong>{max_all_max_rt:.0f}</strong></td>
            <td class="status-{overall_status}"><strong>{overall_status_icon} {overall_status}</strong></td>
        </tr>
"""
    
    html += """
    </table>
    <div style="background-color: #f0f0f0; padding: 15px; margin: 20px 0; border-left: 4px solid #4CAF50;">
        <h4 style="margin-top: 0;">Пояснения по сводной таблице:</h4>
        <ul style="margin-bottom: 0;">
            <li><strong>Целевой RPS (сумма всех групп):</strong> Сумма целевых RPS всех Thread Groups, работающих на этой ступени</li>
            <li><strong>Факт. RPS OK / Отклонение OK %:</strong> Расчёт только по успешным запросам (<code>statut='ok'</code>)</li>
            <li><strong>Факт. RPS ALL / Отклонение ALL %:</strong> Расчёт по всем запросам (успешные + ошибки), используется для проверки попадания в профиль</li>
            <li><strong>Статус запросов:</strong> информационный индикатор по отклонению количества запросов (PASS ≤ 5%, WARN ≤ 10%, FAIL > 10%), не влияет на общий PASS/FAIL по профилю</li>
            <li><strong>Avg RT:</strong> Взвешенное среднее время отклика (учитывает количество запросов каждой Thread Group)</li>
            <li><strong>P95 RT:</strong> Максимальное P95 время отклика среди всех Thread Groups</li>
            <li><strong>Max RT:</strong> Максимальное время отклика среди всех Thread Groups</li>
        </ul>
    </div>
"""
    
    # Добавляем таблицу проверки бизнес-критериев для samplers
    sampler_criteria = results.get("sampler_criteria", {})
    if sampler_criteria.get("samplers"):
        html += """
    <h2>Проверка бизнес-критериев для эндпоинтов (Samplers)</h2>
    <div style="background-color: #f0f0f0; padding: 15px; margin: 20px 0; border-left: 4px solid #2196F3;">
        <h4 style="margin-top: 0;">Пояснения:</h4>
        <ul style="margin-bottom: 0;">
            <li><strong>Среднее время отклика (Mean):</strong> Среднее время ответа сервера за период плато</li>
            <li><strong>95-й процентиль (P95):</strong> Время, ниже которого 95% запросов получили ответ</li>
            <li><strong>Максимальное время отклика (Max):</strong> Максимальное время ответа за период плато</li>
            <li><strong>Критерий (Max Response Time):</strong> Бизнес-требование: P95 должен быть ≤ этого значения</li>
            <li><strong>Статус:</strong> PASS если P95 ≤ критерий, иначе FAIL</li>
        </ul>
    </div>
"""
        
        for sampler_name, sampler_data in sampler_criteria.get("samplers", {}).items():
            criteria_max_ms = sampler_data.get("criteria", {}).get("max_response_time_ms", 0)
            html += f"""
    <h3>{sampler_name} - Критерий: P95 ≤ {criteria_max_ms:.0f} мс</h3>
    <table>
        <tr>
            <th>Ступень</th>
            <th>Время (сек)</th>
            <th>Среднее время отклика (мс)</th>
            <th>P95 время отклика (мс)</th>
            <th>Макс. время отклика (мс)</th>
            <th>Критерий (мс)</th>
            <th>Статус</th>
        </tr>
"""
            for stage in sampler_data.get("stages", []):
                st = stage.get("status", "PASS")
                if st == "PASS":
                    status_class = "status-PASS"
                elif st == "FAIL":
                    status_class = "status-FAIL"
                elif st == "SKIP":
                    status_class = "status-SKIP"
                elif st == "PARTIAL":
                    status_class = "status-PARTIAL"
                else:
                    status_class = "status-FAIL"
                if st == "SKIP":
                    mean_cell = "—"
                    p95_cell = "—"
                    max_cell = "—"
                    pct95_class = ""
                else:
                    mean_cell = f"{stage['mean_response_time_ms']:.2f}"
                    p95_cell = f"<strong>{stage['pct95_response_time_ms']:.2f}</strong>"
                    max_cell = f"{stage['max_response_time_ms']:.2f}"
                    pct95_class = "deviation-good" if stage["pct95_response_time_ms"] <= criteria_max_ms else "deviation-bad"
                skip_title = ""
                if stage.get("skip_reason"):
                    skip_title = f' title="{stage["skip_reason"]}"'
                
                html += f"""
        <tr>
            <td>{stage['stage_idx']}</td>
            <td>{stage['plateau_start_s']}-{stage['plateau_end_s']}</td>
            <td>{mean_cell}</td>
            <td class="{pct95_class}">{p95_cell}</td>
            <td>{max_cell}</td>
            <td>{stage['criteria_max_ms']:.0f}</td>
            <td class="{status_class}"{skip_title}>{st}</td>
        </tr>
"""
            html += """
    </table>
"""
    
    html += """
</body>
</html>
"""
    
    output_path.write_text(html, encoding="utf-8")
    print(f"[OK] HTML отчет сохранен: {output_path}")


def main(argv: List[str]) -> None:
    if len(argv) < 2:
        print("Usage: python check_load_profile.py <test_run_id> [config.json] [output_report.html] [tolerance_pct]")
        print("\nПример:")
        print('  python check_load_profile.py 20250115_143022')
        print('  python check_load_profile.py 20250115_143022 influx_config.json report.html 10.0')
        print("\nНастройки InfluxDB берутся из influx_config.json (если есть) или используются значения по умолчанию")
        sys.exit(1)
    
    test_run_id = argv[1]
    
    # Парсим аргументы: [config.json] [output.html] [tolerance]
    config_path = None
    output_path = Path(f"load_profile_check_{test_run_id}.html")
    tolerance_pct = 10.0
    
    for arg in argv[2:]:
        if arg.endswith('.json') or (Path(arg).exists() and Path(arg).suffix == '.json'):
            config_path = Path(arg)
        elif arg.endswith('.html'):
            output_path = Path(arg)
        else:
            try:
                tolerance_pct = float(arg)
            except ValueError:
                pass  # Игнорируем неизвестные аргументы
    
    # Загружаем настройки InfluxDB из конфига
    config = load_influx_config(config_path)
    influx_url = config["influx_url"]
    db_name = config["influx_db"]
    username = config.get("influx_user")
    password = config.get("influx_pass")
    aggregation_interval = config.get("aggregation_interval", 10.0)  # Интервал агрегации из конфига или 10 по умолчанию
    
    print(f"Проверка профиля нагрузки для test_run={test_run_id}...")
    print(f"Используются настройки InfluxDB: {influx_url}, db={db_name}")
    print(f"Интервал агрегации: {aggregation_interval} секунд")
    results = check_profile_compliance(
        test_run_id,
        influx_url,
        db_name,
        username,
        password,
        tolerance_pct,
        aggregation_interval,
    )
    
    # Проверяем бизнес-критерии для samplers
    print("\nПроверка бизнес-критериев для samplers...")
    sampler_criteria_results = check_sampler_criteria(
        test_run_id,
        influx_url,
        db_name,
        username,
        password,
        results.get("test_start_time_ns"),
        results.get("profile"),
        results.get("test_end_time_ns"),
    )
    
    if sampler_criteria_results:
        results["sampler_criteria"] = sampler_criteria_results
        print(f"[OK] Проверено {len(sampler_criteria_results.get('samplers', {}))} samplers")
    else:
        print("[INFO] Бизнес-критерии для samplers не найдены")
    
    # Сохраняем JSON результаты
    json_path = output_path.with_suffix(".json")
    json_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] JSON результаты сохранены: {json_path}")
    
    # Генерируем HTML отчёт
    generate_html_report(results, output_path)
    
    # Выводим краткую сводку
    print("\n" + "="*60)
    print(f"Общий статус: {results['overall_status']}")
    print("="*60)
    if results.get("runners"):
        for rid, rinfo in sorted(results["runners"].items()):
            print(f"\n--- Под (runner): {rid} — {rinfo['overall_status']} ---")
            for tg_name, tg_data in rinfo.get("thread_groups", {}).items():
                print(f"\n{tg_name}: {tg_data['status']}")
                sorted_stages = sorted(tg_data.get("stages", []), key=lambda x: x.get("stage_idx", 0))
                stages_by_idx = {}
                for stage in sorted_stages:
                    sidx = stage.get("stage_idx", 0)
                    if sidx not in stages_by_idx:
                        stages_by_idx[sidx] = stage
                for sidx in sorted(stages_by_idx.keys()):
                    stage = stages_by_idx[sidx]
                    print(
                        f"  Ступень {stage['stage_idx']} [{stage.get('status', '?')}]: "
                        f"целевой RPS = {stage['target_rps']:.2f}, "
                        f"факт OK = {stage.get('actual_rps_ok', 0.0):.2f}, "
                        f"факт ALL = {stage.get('actual_rps_all', stage.get('actual_rps', 0.0)):.2f}, "
                        f"отклонение OK = {stage.get('deviation_pct_ok', 0.0):.2f}%, "
                        f"отклонение ALL = {stage.get('deviation_pct_all', stage.get('deviation_pct', 0.0)):.2f}%"
                    )
        print(f"\n--- Сводка кластера (N={len(results['runners'])}) — {results.get('aggregate_cluster', {}).get('overall_status', '?')} ---")
    elif results.get("aggregate_cluster") and results.get("runner_count", 0) > 1:
        print(
            f"\n--- Сводка кластера fallback "
            f"(N={results.get('runner_count')}, source={results.get('runner_source', '?')}) — "
            f"{results.get('aggregate_cluster', {}).get('overall_status', '?')} ---"
        )
    for tg_name, tg_data in results.get("thread_groups", {}).items():
        print(f"\n{tg_name}: {tg_data['status']}")
        # Сортируем ступени по stage_idx для правильного отображения
        sorted_stages = sorted(tg_data.get("stages", []), key=lambda x: x.get("stage_idx", 0))
        # Группируем по stage_idx, чтобы показывать каждую ступень только один раз
        stages_by_idx = {}
        for stage in sorted_stages:
            stage_idx = stage.get("stage_idx", 0)
            if stage_idx not in stages_by_idx:
                stages_by_idx[stage_idx] = stage
        
        # Показываем каждую ступень только один раз
        for stage_idx in sorted(stages_by_idx.keys()):
            stage = stages_by_idx[stage_idx]
            total_target_rps = stage.get("total_target_rps", stage["target_rps"])
            total_requests = stage.get("total_requests", 0)
            total_errors = stage.get("total_errors", 0)
            error_percentage = stage.get("error_percentage", 0.0)
            avg_rt = stage.get("avg_response_time_ms", 0.0)
            pct95_rt = stage.get("pct95_response_time_ms", 0.0)
            max_rt = stage.get("max_response_time_ms", 0.0)
            expected_requests = stage.get("expected_requests", 0)
            actual_all_requests = stage.get("actual_all_requests", 0)
            plateau_duration_s = stage.get("plateau_duration_s", 0)
            print(
                f"  Ступень {stage['stage_idx']} [{stage.get('status', '?')}]: целевой RPS (эта Thread Group) = {stage['target_rps']:.2f}, "
                f"фактический RPS OK (эта Thread Group) = {stage.get('actual_rps_ok', 0.0):.2f}, "
                f"фактический RPS ALL (эта Thread Group) = {stage.get('actual_rps_all', stage.get('actual_rps', 0.0)):.2f}, "
                f"отклонение OK = {stage.get('deviation_pct_ok', 0.0):.2f}%, "
                f"отклонение ALL = {stage.get('deviation_pct_all', stage.get('deviation_pct', 0.0)):.2f}%, "
                f"запросов (успешных) = {total_requests:,}, запросов (с ошибками) = {total_errors:,}, % ошибок = {error_percentage:.2f}%, "
                f"Avg RT = {avg_rt:.0f}мс, P95 RT = {pct95_rt:.0f}мс, Max RT = {max_rt:.0f}мс, ожидаемое запросов = {expected_requests:,}, "
                f"фактическое запросов = {actual_all_requests:,}, длительность = {plateau_duration_s}с"
            )
    
    sys.exit(0 if results["overall_status"] == "PASS" else 1)


if __name__ == "__main__":
    main(sys.argv)
