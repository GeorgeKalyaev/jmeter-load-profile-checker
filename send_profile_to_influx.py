"""
Pre-run script: отправляет профиль нагрузки в InfluxDB перед запуском теста.
Использование:
    python send_profile_to_influx.py <profile.json> <test_run_id> [config.json]
    
Пример:
    python send_profile_to_influx.py SimpleLoadTest.profile.json test_20260123_011440
    python send_profile_to_influx.py SimpleLoadTest.profile.json test_20260123_011440 influx_config_localhost.json
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Any, List, Optional


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
                # Объединяем с дефолтными значениями на случай отсутствия некоторых ключей
                return {**default_config, **config}
        except Exception as e:
            print(f"Предупреждение: не удалось загрузить конфиг из {config_path}: {e}")
            print("Используются значения по умолчанию")
    
    return default_config


def send_to_influx(
    data_points: List[str],
    influx_url: str,
    db_name: str,
    username: str = None,
    password: str = None,
) -> bool:
    """Отправляет точки данных в InfluxDB через HTTP API."""
    url = f"{influx_url}/write?db={db_name}"
    
    data = "\n".join(data_points)
    req = urllib.request.Request(url, data=data.encode("utf-8"))
    
    if username and password:
        import base64
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {credentials}")
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status == 204
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"Ошибка отправки в InfluxDB: HTTP {e.code}: {e.reason}")
        if body.strip():
            print(f"Ответ сервера: {body.strip()}")
        return False
    except Exception as e:
        print(f"Ошибка отправки в InfluxDB: {e}")
        return False


def escape_influx_tag_value(value: str) -> str:
    """Нормализует значение тега для InfluxDB Line Protocol (теги без запятых/пробелов/=)."""
    s = str(value)
    for ch in ", =":
        s = s.replace(ch, "_")
    # Частые символы из URL/имён сэмплеров, недопустимые или неудобные в тегах
    for ch in "/?&%:#|*\"'\\<>[]{}":
        s = s.replace(ch, "_")
    s = s.replace(".", "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_") or "empty"


def escape_influx_field_string(value: str) -> str:
    """Экранирование строкового поля по Line Protocol InfluxDB 1.x."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def format_field_pair(key: str, value: Any) -> str:
    """Одно поле: целые — с суффиксом i, float — как есть, строки — в кавычках с escape."""
    if isinstance(value, bool):
        return f"{key}={'true' if value else 'false'}"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{key}={value}i"
    if isinstance(value, float):
        return f"{key}={value}"
    return f'{key}="{escape_influx_field_string(str(value))}"'


def format_influx_line(
    measurement: str,
    tags: Dict[str, str],
    fields: Dict[str, Any],
    timestamp_ns: Optional[int] = None,
) -> str:
    """Форматирует строку в формате InfluxDB Line Protocol (совместимо с InfluxDB 1.x)."""
    tag_str = ",".join(
        f"{k}={escape_influx_tag_value(str(v))}" for k, v in sorted(tags.items()) if v is not None and str(v) != ""
    )
    field_str = ",".join(format_field_pair(k, v) for k, v in sorted(fields.items()))
    
    line = f"{measurement}"
    if tag_str:
        line += f",{tag_str}"
    line += f" {field_str}"
    if timestamp_ns is not None:
        line += f" {timestamp_ns}"
    
    return line


def send_profile(profile_path: Path, test_run_id: str, influx_url: str, db_name: str, username: str = None, password: str = None) -> None:
    """Читает профиль и отправляет его в InfluxDB."""
    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)
    
    data_points: List[str] = []
    # Время точек — около «сейчас» (нс), с монотонным seq. Иначе plateau_start_s*1e9 даёт 1970 год и Influx 1.x
    # с ограниченным retention возвращает: partial write: points beyond retention policy dropped=...
    # Различие серий обеспечивают теги (test_run, thread_group, stage_idx); длительности плато — в полях.
    batch_base_ns = int(time.time() * 1_000_000_000)
    seq = 0

    # Отправляем информацию о каждой ступени каждой thread group
    for tg in profile.get("thread_groups", []):
        tg_name = tg.get("name", "")
        target_rpm = tg.get("target_rps")  # ВАЖНО: теперь это RPM, а не RPS!
        transaction_names = tg.get("transaction_names", [])  # Список всех Transaction Controllers внутри Thread Group
        
        # Сохраняем список транзакций как строку с разделителями (для InfluxDB)
        transaction_names_str = ",".join(transaction_names) if transaction_names else ""
        
        for stage in tg.get("stages", []):
            threads = stage.get("threads", 0)
            
            # ВАЖНО: Constant Throughput Timer с calcMode=0 = "This thread only".
            # 10 samples per minute — на ОДНОГО пользователя, не на всех.
            # Каждый поток независимо: 10 RPM/поток → 10*threads RPM всего.
            # Общий RPS = (RPM * threads) / 60.0
            # Пример: CTT=10 RPM, threads=10 → 100 RPM → 100/60 ≈ 1.67 RPS
            if target_rpm is not None and target_rpm > 0:
                # Предполагаем calcMode=0 (наиболее распространенный случай)
                # Если calcMode=1 или 2, то target_rps = target_rpm / 60.0 (без умножения на threads)
                target_rps = (target_rpm * threads) / 60.0
            else:
                target_rps = 0.0
            
            # Отправляем информацию о плато (hold период)
            # stage_idx в тегах — отдельная серия на ступень
            # Числа как float (не integer с суффиксом i): на многих стендах measurement load_profile
            # уже создан с float-полями; иначе Influx 1.x: field type conflict dropped=...
            fields = {
                "plateau_start_s": float(stage["plateau_start_s"]),
                "plateau_end_s": float(stage["plateau_end_s"]),
                "hold_s": float(stage["hold_s"]),
                "threads": float(threads),
                "target_rps": float(target_rps),
            }
            
            # Добавляем список транзакций в поля (только для первой ступени, чтобы не дублировать)
            if stage.get("stage_idx", 1) == 1 and transaction_names_str:
                fields["transaction_names"] = transaction_names_str
            
            timestamp_ns = batch_base_ns + seq
            seq += 1
            
            line = format_influx_line(
                measurement="load_profile",
                tags={
                    "test_run": test_run_id,
                    "thread_group": tg_name,
                    "stage_idx": str(stage["stage_idx"]),
                },
                fields=fields,
                timestamp_ns=timestamp_ns,
            )
            data_points.append(line)
    
    # Список samplers: только тип и SLA; имя — в теге (нормализованное).
    for sampler in profile.get("samplers", []):
        max_response_time_ms = sampler.get("max_response_time_ms")
        raw_name = sampler.get("name", "") or "unknown"
        tag_sampler = escape_influx_tag_value(raw_name)

        fields: Dict[str, Any] = {
            "sampler_type": sampler.get("type", ""),
        }
        if max_response_time_ms is not None:
            fields["max_response_time_ms"] = float(max_response_time_ms)

        line = format_influx_line(
            measurement="load_profile_samplers",
            tags={
                "test_run": test_run_id,
                "sampler_name": tag_sampler,
            },
            fields=fields,
            timestamp_ns=batch_base_ns + seq,
        )
        data_points.append(line)
        seq += 1
    
    if not data_points:
        print("Нет данных для отправки")
        return
    
    print(f"Отправка {len(data_points)} точек данных в InfluxDB...")
    if send_to_influx(data_points, influx_url, db_name, username, password):
        print(f"[OK] Профиль успешно отправлен в InfluxDB (test_run={test_run_id})")
    else:
        print("[ERROR] Ошибка отправки профиля")
        sys.exit(1)


def main(argv: List[str]) -> None:
    if len(argv) < 3:
        print("Usage: python send_profile_to_influx.py <profile.json> <test_run_id> [config.json]")
        print("\nПример:")
        print('  python send_profile_to_influx.py SimpleLoadTest.profile.json test_20260123_011440')
        print('  python send_profile_to_influx.py profile.json test_20260123_011440 influx_config_localhost.json')
        print("\nНастройки InfluxDB берутся из influx_config.json (если есть) или используются значения по умолчанию")
        sys.exit(1)
    
    profile_path = Path(argv[1])
    test_run_id = argv[2]
    config_path = Path(argv[3]) if len(argv) > 3 else None
    
    # Загружаем настройки InfluxDB из конфига
    config = load_influx_config(config_path)
    influx_url = config["influx_url"]
    db_name = config["influx_db"]
    username = config.get("influx_user")
    password = config.get("influx_pass")
    
    if not profile_path.exists():
        print(f"Файл профиля не найден: {profile_path}")
        sys.exit(1)
    
    print(f"Используются настройки InfluxDB: {influx_url}, db={db_name}")
    send_profile(profile_path, test_run_id, influx_url, db_name, username, password)


if __name__ == "__main__":
    main(sys.argv)
