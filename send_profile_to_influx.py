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
import urllib.request
import urllib.parse
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
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status == 204
    except Exception as e:
        print(f"Ошибка отправки в InfluxDB: {e}")
        return False


def escape_influx_tag_value(value: str) -> str:
    """Экранирует специальные символы в значениях тегов InfluxDB."""
    # В InfluxDB теги не могут содержать запятые, пробелы, знаки равенства
    # Заменяем пробелы на подчеркивания, удаляем запятые и знаки равенства
    return value.replace(",", "_").replace(" ", "_").replace("=", "_")

def format_influx_line(
    measurement: str,
    tags: Dict[str, str],
    fields: Dict[str, Any],
    timestamp_ns: int = None,
) -> str:
    """Форматирует строку в формате InfluxDB Line Protocol."""
    tag_str = ",".join(f"{k}={escape_influx_tag_value(str(v))}" for k, v in sorted(tags.items()) if v)
    field_str = ",".join(
        f"{k}={v}" if isinstance(v, (int, float)) else f'{k}="{v}"'
        for k, v in sorted(fields.items())
    )
    
    line = f"{measurement}"
    if tag_str:
        line += f",{tag_str}"
    line += f" {field_str}"
    if timestamp_ns:
        line += f" {timestamp_ns}"
    
    return line


def send_profile(profile_path: Path, test_run_id: str, influx_url: str, db_name: str, username: str = None, password: str = None) -> None:
    """Читает профиль и отправляет его в InfluxDB."""
    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)
    
    data_points: List[str] = []
    timestamp_ns = int(__import__("time").time() * 1_000_000_000)  # nanoseconds
    
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
            # Добавляем stage_idx в теги, чтобы каждая ступень была отдельной серией в InfluxDB
            fields = {
                "plateau_start_s": stage["plateau_start_s"],
                "plateau_end_s": stage["plateau_end_s"],
                "hold_s": stage["hold_s"],
                "threads": threads,
                "target_rps": target_rps,  # Теперь правильный target_rps для этой ступени
            }
            
            # Добавляем список транзакций в поля (только для первой ступени, чтобы не дублировать)
            if stage["stage_idx"] == 0 and transaction_names_str:
                fields["transaction_names"] = transaction_names_str
            
            line = format_influx_line(
                measurement="load_profile",
                tags={
                    "test_run": test_run_id,
                    "thread_group": tg_name,
                    "stage_idx": str(stage["stage_idx"]),  # Добавляем stage_idx в теги
                },
                fields=fields,
                timestamp_ns=timestamp_ns,
            )
            data_points.append(line)
    
    # Отправляем список samplers с бизнес-критериями (если нужно)
    for sampler in profile.get("samplers", []):
        # Поддерживаем бизнес-критерии: max_response_time_ms (максимальное время отклика в миллисекундах)
        # Критерий можно задать в profile.json: "max_response_time_ms": 10000
        max_response_time_ms = sampler.get("max_response_time_ms")
        
        fields = {
            "sampler_name": sampler.get("name", ""),
            "sampler_type": sampler.get("type", ""),
            "path_or_query": sampler.get("path_or_query", ""),
        }
        
        # Добавляем критерий, если он задан
        if max_response_time_ms is not None:
            fields["max_response_time_ms"] = float(max_response_time_ms)
        
        # ВАЖНО: Добавляем sampler_name в теги, чтобы каждый sampler был отдельной серией в InfluxDB
        # Иначе все samplers будут перезаписывать друг друга из-за одинакового timestamp
        line = format_influx_line(
            measurement="load_profile_samplers",
            tags={
                "test_run": test_run_id,
                "sampler_name": sampler.get("name", ""),  # Добавляем sampler_name в теги
            },
            fields=fields,
            timestamp_ns=timestamp_ns,
        )
        data_points.append(line)
    
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
