"""
Симуляция расписания kg.apc Ultimate Thread Group и выделение «бизнес-ступеней» —
отрезков времени, когда суммарное число активных потоков (по всем строкам) постоянно.

Модель одной строки UTG (колонки: Start threads, Initial delay, Startup time, Hold, Shutdown):
- потоки строки считаются ДОБАВЛЯЕМЫМИ к общей нагрузке (типичная «лесенка»);
- ramp-up: линейный рост 0 → T за [delay, delay+ramp_up);
- плато: T на [delay+ramp_up, delay+ramp_up+hold);
- ramp-down: линейное снижение T → 0 за [..., +ramp_down); при ramp_down=0 — мгновенный обрыв после hold.

Итоговые ступени профиля: смежные секунды [s, s+1), где round(сумма вкладов) одинакова и > 0.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def _row_contribution(
    t: float,
    thread_add: int,
    delay: int,
    ramp_up: int,
    hold: int,
    ramp_down: int,
) -> float:
    """Вклад одной строки UTG в активные потоки в момент времени t (секунды от старта теста)."""
    if t < delay:
        return 0.0

    if ramp_up > 0:
        ramp_end = delay + ramp_up
        if t < ramp_end:
            return float(thread_add) * (t - delay) / float(ramp_up)
    else:
        ramp_end = delay

    hold_end = ramp_end + hold
    if t < hold_end:
        return float(thread_add)

    all_end = hold_end + ramp_down
    if ramp_down > 0:
        if t < all_end:
            return float(thread_add) * (1.0 - (t - hold_end) / float(ramp_down))
        return 0.0

    return 0.0


def _total_threads_at(t: float, rows: List[Dict[str, Any]]) -> float:
    s = 0.0
    for r in rows:
        s += _row_contribution(
            t,
            int(r["threads"]),
            int(r["init_delay_s"]),
            int(r["ramp_up_s"]),
            int(r["hold_s"]),
            int(r["ramp_down_s"]),
        )
    return s


def _horizon_seconds(rows: List[Dict[str, Any]]) -> int:
    h = 0
    for r in rows:
        d = int(r["init_delay_s"])
        ru = int(r["ramp_up_s"])
        hld = int(r["hold_s"])
        rd = int(r["ramp_down_s"])
        end_t = d + ru + hld + rd
        if end_t > h:
            h = end_t
    return max(h, 1)


def _is_second_stable(s: int, rows: List[Dict[str, Any]], eps: float = 0.05) -> bool:
    """True, если в течение секунды s суммарные потоки почти не меняются (не ramp)."""
    t0 = float(s) + 0.1
    t1 = float(s) + 0.9
    return abs(_total_threads_at(t0, rows) - _total_threads_at(t1, rows)) < eps


def business_stages_from_utg_rows(
    raw_rows: List[Dict[str, Any]],
    *,
    min_plateau_s: int = 1,
    stability_eps: float = 0.05,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Строит список ступеней для профиля (plateau_start/end, суммарные threads, hold_s).

    Args:
        raw_rows: список dict с ключами threads, init_delay_s, ramp_up_s, hold_s, ramp_down_s
        min_plateau_s: отбросить плато короче (сек)
        stability_eps: порог «плато»: за секунду сумма вкладов меньше eps

    Returns:
        (stages, raw_rows) — stages для JSON-профиля; raw_rows echo для отладки
    """
    if not raw_rows:
        return [], []

    horizon = _horizon_seconds(raw_rows)
    # Только секунды без наклона (все строки в hold или ноль); игнорируем ramp, чтобы не дробить на 1–2 с
    totals: List[Optional[int]] = []
    for s in range(horizon):
        if not _is_second_stable(s, raw_rows, eps=stability_eps):
            totals.append(None)
            continue
        tm = s + 0.5
        v = int(round(_total_threads_at(tm, raw_rows)))
        totals.append(v if v > 0 else None)

    stages: List[Dict[str, Any]] = []
    run_start: Optional[int] = None
    run_val: Optional[int] = None

    def flush_run(start: int, end_exclusive: int, val: int) -> None:
        if val <= 0:
            return
        dur = end_exclusive - start
        if dur < min_plateau_s:
            return
        stages.append(
            {
                "stage_idx": len(stages) + 1,
                "threads": val,
                "init_delay_s": start,
                "ramp_up_s": 0,
                "hold_s": dur,
                "ramp_down_s": 0,
                "plateau_start_s": start,
                "plateau_end_s": end_exclusive,
            }
        )

    for s in range(horizon):
        tv = totals[s]
        if tv is None:
            if run_start is not None:
                flush_run(run_start, s, run_val or 0)
                run_start = None
                run_val = None
            continue
        if run_start is None:
            run_start = s
            run_val = tv
            continue
        if tv == run_val:
            continue
        flush_run(run_start, s, run_val or 0)
        run_start = s
        run_val = tv

    if run_start is not None and run_val is not None:
        flush_run(run_start, horizon, run_val)

    return stages, raw_rows


def legacy_naive_stages_from_rows(raw_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Старое правило: одна строка UTG = одна ступень (без учёта наложения строк)."""
    stages: List[Dict[str, Any]] = []
    for idx, r in enumerate(raw_rows):
        ps = int(r["init_delay_s"]) + int(r["ramp_up_s"])
        pe = ps + int(r["hold_s"])
        stages.append(
            {
                "stage_idx": idx + 1,
                "threads": int(r["threads"]),
                "init_delay_s": int(r["init_delay_s"]),
                "ramp_up_s": int(r["ramp_up_s"]),
                "hold_s": int(r["hold_s"]),
                "ramp_down_s": int(r["ramp_down_s"]),
                "plateau_start_s": ps,
                "plateau_end_s": pe,
            }
        )
    return stages


def parse_utg_rows_from_element(utg_elem: Any) -> List[Dict[str, Any]]:
    """Сырые строки UTG из XML (тот же порядок полей, что в parse_jmx_profile)."""
    rows: List[Dict[str, Any]] = []
    data_root = utg_elem.find(".//collectionProp[@name='ultimatethreadgroupdata']")
    if data_root is None:
        return rows
    for row in data_root.findall("collectionProp"):
        values = [sp.text or "" for sp in row.findall("stringProp")]
        if len(values) < 5:
            continue
        try:
            threads = int(values[0])
            init_delay = int(values[1])
            ramp_up = int(values[2])
            hold = int(values[3])
            ramp_down = int(values[4])
        except ValueError:
            continue
        rows.append(
            {
                "threads": threads,
                "init_delay_s": init_delay,
                "ramp_up_s": ramp_up,
                "hold_s": hold,
                "ramp_down_s": ramp_down,
            }
        )
    return rows


if __name__ == "__main__":
    # Пример: три строки как в обсуждении (лесенка)
    demo = [
        {"threads": 10, "init_delay_s": 0, "ramp_up_s": 20, "hold_s": 1020, "ramp_down_s": 20},
        {"threads": 20, "init_delay_s": 340, "ramp_up_s": 20, "hold_s": 680, "ramp_down_s": 20},
        {"threads": 30, "init_delay_s": 680, "ramp_up_s": 20, "hold_s": 340, "ramp_down_s": 20},
    ]
    st, _ = business_stages_from_utg_rows(demo)
    if not st:
        st = legacy_naive_stages_from_rows(demo)
    print("Business stages (demo):")
    for stg in st:
        print(
            f"  #{stg['stage_idx']}: t=[{stg['plateau_start_s']}, {stg['plateau_end_s']})s "
            f"threads={stg['threads']} duration={stg['hold_s']}s"
        )
