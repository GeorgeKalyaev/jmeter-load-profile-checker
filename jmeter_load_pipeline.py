#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Один вход для подготовки прогона и для отчета после теста (без Docker).

  1) Подготовка (JMX рядом со скриптом или путь явно):
       python jmeter_load_pipeline.py prepare [plan.jmx] [--config influx_config.json]

     По шагам: парсинг JMX -> JSON профиля -> test_run -> отправка в Influx -> запись test_run в JMX.

  2) Запустите нагрузочный тест в JMeter вручную.

  3) Отчет HTML/JSON:
       python jmeter_load_pipeline.py report [--config influx_config.json]

     Берет test_run из test_run_id.txt (создан на шаге prepare).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from prepare_test import patch_test_run_in_jmx


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _resolve_config(script_dir: Path, config_name: str) -> Path:
    p = script_dir / config_name
    if p.is_file():
        return p
    alt = script_dir / "influx_config.json"
    if alt.is_file():
        return alt
    return p


def _resolve_jmx(script_dir: Path, jmx_arg: str | None) -> Path:
    if jmx_arg:
        p = Path(jmx_arg)
        if not p.is_file():
            p = script_dir / jmx_arg
        if not p.is_file():
            sys.exit(f"[ERROR] JMX не найден: {jmx_arg}")
        return p.resolve()
    default = script_dir / "SimpleLoadTest.jmx"
    if default.is_file():
        return default
    jmx_list = sorted(script_dir.glob("*.jmx"))
    if len(jmx_list) == 1:
        return jmx_list[0]
    if not jmx_list:
        sys.exit("[ERROR] Нет .jmx в папке со скриптом. Укажите файл: prepare path/to/plan.jmx")
    sys.exit(
        "[ERROR] Несколько .jmx в папке. Укажите явно: prepare <файл.jmx>\n"
        f"  Найдено: {', '.join(x.name for x in jmx_list)}"
    )


def _run_step(title: str, cmd: list[str], cwd: Path) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)
    sys.stdout.flush()
    r = subprocess.run(cmd, cwd=str(cwd))
    if r.returncode != 0:
        sys.exit(r.returncode)


def cmd_prepare(jmx: Path, config: Path, script_dir: Path) -> None:
    profile_path = jmx.with_suffix(".profile.json")
    py = sys.executable

    print()
    print("*" * 60)
    print("  JMETER LOAD PIPELINE — подготовка к прогону")
    print("*" * 60)
    sys.stdout.flush()

    _run_step(
        f"[1/4] Parse JMX -> JSON profile\n      JMX: {jmx.name}\n      Out: {profile_path.name}",
        [py, str(script_dir / "parse_jmx_profile.py"), str(jmx), str(profile_path)],
        script_dir,
    )
    if not profile_path.is_file():
        sys.exit(f"[ERROR] Не создан файл профиля: {profile_path}")

    test_run_id = f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    tid_path = script_dir / "test_run_id.txt"
    tid_path.write_text(test_run_id, encoding="utf-8")

    print()
    print("=" * 60)
    print(f"[2/4] Test run ID\n      {test_run_id}\n      Saved: {tid_path.name}")
    print("=" * 60)
    sys.stdout.flush()

    _run_step(
        "[3/4] Send profile to InfluxDB (load_profile, load_profile_samplers, ...)",
        [
            py,
            str(script_dir / "send_profile_to_influx.py"),
            str(profile_path),
            test_run_id,
            str(config),
        ],
        script_dir,
    )

    print()
    print("=" * 60)
    print(f"[4/4] Write test_run into JMX User Defined Variables\n      {jmx.name}")
    print("=" * 60)
    sys.stdout.flush()
    patch_test_run_in_jmx(jmx, test_run_id)
    print(f"[OK] Variable test_run = {test_run_id}")

    try:
        cfg = json.loads(config.read_text(encoding="utf-8"))
        influx_url = cfg.get("influx_url", "?")
        influx_db = cfg.get("influx_db", "?")
    except Exception:
        influx_url = influx_db = "?"

    print()
    print("*" * 60)
    print("  DONE — дальше только JMeter")
    print("*" * 60)
    print(f"  Influx (from config): {influx_url}  db={influx_db}")
    print("  1) Open this JMX in JMeter and run the load test.")
    print("  2) After the test finishes, build the report:")
    print(f"       python jmeter_load_pipeline.py report --config {config.name}")
    print("*" * 60)


def cmd_report(config: Path, script_dir: Path) -> None:
    tid_path = script_dir / "test_run_id.txt"
    if not tid_path.is_file():
        sys.exit(f"[ERROR] Нет {tid_path.name}. Сначала выполните: python jmeter_load_pipeline.py prepare")
    test_run_id = tid_path.read_text(encoding="utf-8").strip()
    py = sys.executable

    print()
    print("*" * 60)
    print("  JMETER LOAD PIPELINE — отчет после теста")
    print("*" * 60)
    print(f"  test_run: {test_run_id}")
    print("*" * 60)
    sys.stdout.flush()

    _run_step(
        "[1/1] check_load_profile.py -> HTML + JSON",
        [py, str(script_dir / "check_load_profile.py"), test_run_id, str(config)],
        script_dir,
    )

    print()
    print("*" * 60)
    print("  DONE")
    print(f"  HTML: load_profile_check_{test_run_id}.html")
    print(f"  JSON: load_profile_check_{test_run_id}.json")
    print("*" * 60)


def main() -> None:
    script_dir = _script_dir()
    parser = argparse.ArgumentParser(description="JMeter load profile pipeline (prepare / report)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_prep = sub.add_parser("prepare", help="JMX -> profile JSON -> Influx -> patch test_run in JMX")
    p_prep.add_argument(
        "jmx",
        nargs="?",
        default=None,
        help="JMX file (default: SimpleLoadTest.jmx or single .jmx in script folder)",
    )
    p_prep.add_argument(
        "--config",
        default="influx_config_localhost.json",
        help="Influx JSON config (default: influx_config_localhost.json)",
    )

    p_rep = sub.add_parser("report", help="Build HTML/JSON from Influx for last test_run_id.txt")
    p_rep.add_argument(
        "--config",
        default="influx_config_localhost.json",
        help="Influx JSON config (default: influx_config_localhost.json)",
    )

    args = parser.parse_args()
    config_path = _resolve_config(script_dir, args.config)
    if not config_path.is_file():
        sys.exit(f"[ERROR] Конфиг не найден: {config_path}")

    if args.command == "prepare":
        jmx_path = _resolve_jmx(script_dir, args.jmx)
        cmd_prepare(jmx_path, config_path, script_dir)
    else:
        cmd_report(config_path, script_dir)


if __name__ == "__main__":
    main()
