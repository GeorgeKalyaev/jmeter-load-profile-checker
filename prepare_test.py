#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Подготовка к новому тесту: парсинг JMX и отправка профиля в InfluxDB"""

import sys
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime


def patch_test_run_in_jmx(jmx_path: Path, test_run_id: str) -> None:
    tree = ET.parse(jmx_path)
    root = tree.getroot()
    for elem in root.iter("elementProp"):
        if elem.get("name") == "test_run":
            for sp in elem.findall("stringProp"):
                if sp.get("name") == "Argument.value":
                    sp.text = test_run_id
                    break
    tree.write(jmx_path, encoding="UTF-8", xml_declaration=True)


def main():
    argv = [a for a in sys.argv[1:] if a != "--patch-jmx"]
    patch_jmx = "--patch-jmx" in sys.argv

    if len(argv) < 1:
        jmx_file = "SimpleLoadTest.jmx"
    else:
        jmx_file = argv[0]

    if len(argv) < 2:
        config_file = "influx_config_localhost.json"
    else:
        config_file = argv[1]
    
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
    if patch_jmx:
        jp = Path(jmx_file)
        patch_test_run_in_jmx(jp, test_run_id)
        print(f"[OK] test_run записан в JMX: {jp.name}")
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
    if not patch_jmx:
        print(f"1. Откройте {jmx_file} в JMeter")
        print("2. В User Defined Variables найдите test_run")
        print(f"3. Замените значение на: {test_run_id}")
        print("4. Запустите тест")
    else:
        print(f"1. Откройте {jmx_file} в JMeter (test_run уже подставлен)")
        print("2. Запустите тест")
    print()
    print("После завершения теста запустите:")
    print(f"  python check_load_profile.py {test_run_id} {config_file}")

if __name__ == "__main__":
    main()
