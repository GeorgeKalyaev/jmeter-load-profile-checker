#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка, что профиль попал в InfluxDB: несколько SELECT по measurement load_profile.

Usage:
  python verify_profile_in_influx.py <test_run_id> [influx_config.json]

Пример (после send_profile_to_influx):
  python verify_profile_in_influx.py test_pifagor_20260101_120000 influx_config_local_noauth.json
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


def load_cfg(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def influx_query(
    q: str,
    influx_url: str,
    db_name: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, Any]:
    url = f"{influx_url}/query?db={urllib.parse.quote(db_name)}&q={urllib.parse.quote(q)}"
    req = urllib.request.Request(url)
    if username and password:
        import base64
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def print_series(data: Dict[str, Any]) -> None:
    results = data.get("results") or []
    if not results:
        print("(пустой ответ)")
        return
    series_list = results[0].get("series") or []
    if not series_list:
        print("(нет series — запрос не вернул строк)")
        return
    for s in series_list:
        name = s.get("name", "")
        tags = s.get("tags") or {}
        cols = s.get("columns") or []
        vals = s.get("values") or []
        print(f"\n--- measurement={name!r} tags={tags} rows={len(vals)} ---")
        print("columns:", cols)
        for row in vals[:25]:
            print(" ", dict(zip(cols, row)))
        if len(vals) > 25:
            print(f"  ... ещё {len(vals) - 25} строк(и)")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    test_run = sys.argv[1]
    cfg_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "influx_config_local_noauth.json"
    cfg = load_cfg(cfg_path)
    base = cfg["influx_url"].rstrip("/")
    db = cfg.get("influx_db", "jmeter")
    user = (cfg.get("influx_user") or "").strip() or None
    pwd = (cfg.get("influx_pass") or "").strip() or None

    print(f"Influx: {base} db={db} test_run={test_run!r}\n")

    # Примечание: stage_idx — тег (строка). Нумерация с 1 (первая ступень — '1'); старые прогоны могли писать '0'.
    queries: List[str] = [
        f'''SELECT COUNT(*) FROM "load_profile" WHERE "test_run" = '{test_run}' ''',
        f'''SELECT * FROM "load_profile" WHERE "test_run" = '{test_run}' AND "stage_idx" = '1' LIMIT 3''',
        f'''SELECT * FROM "load_profile" WHERE "test_run" = '{test_run}' AND "stage_idx" = '2' LIMIT 3''',
        f'''SELECT * FROM "load_profile" WHERE "test_run" = '{test_run}' AND "stage_idx" != '1' LIMIT 5''',
        f'''SHOW TAG VALUES FROM "load_profile" WITH KEY = "stage_idx" WHERE "test_run" = '{test_run}' ''',
    ]

    for q in queries:
        print("=" * 72)
        print("QUERY:", q.strip())
        try:
            data = influx_query(q, base, db, user, pwd)
            if data.get("results") and data["results"][0].get("error"):
                print("ERROR:", data["results"][0]["error"])
            else:
                print_series(data)
        except Exception as e:
            print(f"Ошибка запроса: {e}")

    print("\n" + "=" * 72)
    print("Готово. Если строк нет — проверьте test_run и что send_profile_to_influx отработал без ошибок.")


if __name__ == "__main__":
    main()
