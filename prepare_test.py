#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Подготовка к новому тесту: парсинг JMX и отправка профиля в InfluxDB"""

import sys
import subprocess
from pathlib import Path
from datetime import datetime

def main():
    if len(sys.argv) < 2:
        jmx_file = "SimpleLoadTest.jmx"
    else:
        jmx_file = sys.argv[1]
    
    if len(sys.argv) < 3:
        config_file = "influx_config_localhost.json"
    else:
        config_file = sys.argv[2]
    
    print("=" * 60)
    print("ПОДГОТОВКА К НОВОМУ ТЕСТУ")
    print("=" * 60)
    print()
    
    # Шаг 1: Парсинг JMX
    print("[1/3] Парсинг JMX файла:", jmx_file)
    result = subprocess.run(
        [sys.executable, "parse_jmx_profile.py", jmx_file],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace'
    )
    
    if result.returncode != 0:
        print("[ERROR] Ошибка при парсинге JMX:")
        print(result.stderr)
        sys.exit(1)
    
    print("[OK] Профиль создан:", jmx_file.replace('.jmx', '.profile.json'))
    print()
    
    # Шаг 2: Генерация test_run_id
    test_run_id = f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print("[2/3] Генерация test_run_id")
    print(f"Test Run ID: {test_run_id}")
    
    # Сохраняем test_run_id в файл
    with open("test_run_id.txt", "w", encoding="utf-8") as f:
        f.write(test_run_id)
    print("[OK] test_run_id сохранен в test_run_id.txt")
    print()
    
    # Шаг 3: Отправка профиля в InfluxDB
    profile_file = jmx_file.replace('.jmx', '.profile.json')
    print("[3/3] Отправка профиля в InfluxDB...")
    result = subprocess.run(
        [sys.executable, "send_profile_to_influx.py", profile_file, test_run_id, config_file],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace'
    )
    
    if result.returncode != 0:
        print("[ERROR] Ошибка при отправке профиля:")
        print(result.stderr.encode('utf-8', errors='replace').decode('cp1251', errors='replace'))
        sys.exit(1)
    
    # Выводим stdout с обработкой кодировки
    output = result.stdout.encode('utf-8', errors='replace').decode('cp1251', errors='replace')
    if output.strip():
        print(output)
    print()
    print("=" * 60)
    print("ГОТОВО!")
    print("=" * 60)
    print()
    print(f"Test Run ID: {test_run_id}")
    print()
    print("Следующие шаги:")
    print(f"1. Откройте {jmx_file} в JMeter")
    print("2. В User Defined Variables найдите test_run")
    print(f"3. Замените значение на: {test_run_id}")
    print("4. Запустите тест")
    print()
    print("После завершения теста запустите:")
    print(f"  python check_load_profile.py {test_run_id} {config_file}")

if __name__ == "__main__":
    main()
