#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Init InfluxDB 1.x: create database, user, grant, default retention policy,
and warm-up required measurements by writing and deleting a dummy point.

Usage:
  python init_influxdb.py [influx_config.json]
"""

import json
import sys
from urllib.request import urlopen, Request
from urllib.parse import urlencode


def load_config(path: str):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return {
        "url": cfg.get("influx_url", "http://localhost:8086"),
        "db": cfg.get("influx_db", "jmeter"),
        "user": cfg.get("influx_user", "jmeter_user"),
        "pass": cfg.get("influx_pass", "changeme"),
    }


def query(url: str, q: str, user: str = None, password: str = None):
    data = urlencode({"q": q}).encode("utf-8")
    req = Request(f"{url}/query", data=data, method="POST")
    if user and password:
        # Basic auth header if needed (Influx 1.x allows both with/without auth)
        import base64
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urlopen(req) as resp:
        return resp.read()


def write_line(url: str, db: str, line: str, user: str = None, password: str = None):
    params = f"db={db}"
    req = Request(f"{url}/write?{params}", data=line.encode("utf-8"), method="POST")
    if user and password:
        import base64
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urlopen(req) as resp:
        return resp.read()


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "influx_config_localhost.json"
    cfg = load_config(cfg_path)
    url, db, user, password = cfg["url"], cfg["db"], cfg["user"], cfg["pass"]

    print(f"[INFO] InfluxDB: {url}, db={db}, user={user}")

    # Create database (idempotent)
    try:
        query(url, f'CREATE DATABASE "{db}"', user, password)
        print("[OK] Database ensured")
    except Exception as e:
        print(f"[WARN] CREATE DATABASE: {e}")

    # Create user and grant (best-effort, may fail if auth disabled or user exists)
    try:
        query(url, f"CREATE USER {user} WITH PASSWORD '{password}'", user, password)
        print("[OK] User ensured")
    except Exception as e:
        print(f"[WARN] CREATE USER: {e}")
    try:
        query(url, f"GRANT ALL ON {db} TO {user}", user, password)
        print("[OK] Grants ensured")
    except Exception as e:
        print(f"[WARN] GRANT: {e}")

    # Default retention policy
    try:
        query(url, f'CREATE RETENTION POLICY "autogen" ON "{db}" DURATION 0 REPLICATION 1 DEFAULT', user, password)
        print("[OK] Retention policy ensured")
    except Exception as e:
        print(f"[WARN] RETENTION POLICY: {e}")

    # Warm-up measurements: write and delete a dummy point to verify access
    measurements = [
        "load_profile",
        "load_profile_thread_group_info",
        "load_stage_change",
        "jmeter",
    ]
    for m in measurements:
        try:
            # tags must be valid for line protocol
            line = f'{m},test_run=init_check,thread_group=init field=1i'
            write_line(url, db, line, user, password)
            # delete the dummy series
            query(url, f"DELETE FROM {m} WHERE test_run='init_check'", user, password)
            print(f"[OK] Measurement '{m}' warm-up done")
        except Exception as e:
            print(f"[WARN] Warm-up '{m}': {e}")

    print("[DONE] InfluxDB initialization completed")


if __name__ == "__main__":
    main()

