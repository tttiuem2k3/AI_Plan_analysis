from __future__ import annotations

from datetime import date, datetime, timedelta
from math import ceil
from typing import Dict, List

from .enums import MoveType
from .models import Hotspots, ProposalMove, ProposalOption


def _parse_iso_dt(v) -> datetime | None:
    if v in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def _add_workdays_skip_sunday(d0: date, days: int) -> date:
    cur = d0
    remaining = max(0, int(days))
    while remaining > 0:
        cur = cur + timedelta(days=1)
        if cur.weekday() == 6:
            continue
        remaining -= 1
    return cur


def _compute_reasonable_due_date(problem_rows: List[Dict], kpi_snapshot: Dict) -> str | None:
    candidates: List[date] = []

    for row in problem_rows or []:
        if not isinstance(row, dict):
            continue
        due_dt = _parse_iso_dt(row.get("due_date"))
        end_dt = _parse_iso_dt(row.get("expected_end_date"))
        late_qty = int(row.get("late_qty") or 0)
        unplanned_qty = int(row.get("unplanned_qty") or 0)
        cap = int((row.get("capacity_per_day") or 0)) + int((row.get("overtime_capacity_per_day") or 0))

        base_dt = max([x for x in (due_dt, end_dt) if x is not None], default=None)
        if base_dt is None:
            continue

        shortage_qty = max(0, late_qty + unplanned_qty)
        if shortage_qty <= 0:
            candidates.append(base_dt.date())
            continue

        if cap <= 0:
            # If no executable capacity remains after measures 1-5, the earliest
            # honest DueDate is the latest known completion date rather than a
            # fabricated quantity-based extension.
            candidates.append(base_dt.date())
            continue

        extra_days = max(0, int(ceil(float(shortage_qty) / float(cap))) - 1)
        candidates.append(_add_workdays_skip_sunday(base_dt.date(), extra_days))

    if not candidates:
        if int(kpi_snapshot.get("total_tardiness_minutes") or 0) > 0:
            return (datetime.now() + timedelta(days=2)).date().isoformat()
        return None

    return max(candidates).isoformat()


def build_proposals(top_n: int, hotspots: Hotspots, problem_rows: List[Dict], kpi_snapshot: Dict) -> List[ProposalOption]:
    options: List[ProposalOption] = []
    has_lateness = (
        int(kpi_snapshot.get("late_ops_count", 0) or 0) > 0
        or int(kpi_snapshot.get("total_late_qty", 0) or 0) > 0
        or int(kpi_snapshot.get("total_unplanned_qty", 0) or 0) > 0
        or int(kpi_snapshot.get("total_tardiness_minutes", 0) or 0) > 0
    )

    if hotspots.bottleneck_machines:
        m = hotspots.bottleneck_machines[0]
        options.append(
            ProposalOption(
                id="OPT1",
                rank=1,
                score=0.95,
                title="Điều phối lại thứ tự trên máy nghẽn chính",
                reason_summary=f"Máy {m} là bottleneck chính, cần swap/move để giảm trễ.",
                moves=[
                    ProposalMove(
                        type=MoveType.SWAP_ORDER_ON_MACHINE,
                        reason="Giảm waiting giữa các công đoạn liên tiếp.",
                        operation_id=(problem_rows[0]["operation_id"] if problem_rows else ""),
                        machine=m,
                        op_ids_after=[x.get("operation_id") for x in problem_rows[:3]],
                        expected_impact={
                            "late_ops_delta": -max(1, int(kpi_snapshot.get("late_ops_count", 0) * 0.2)),
                            "tardiness_minutes_delta": -max(30, int(kpi_snapshot.get("total_tardiness_minutes", 0) * 0.15)),
                            "setup_minutes_delta": -20,
                        },
                    )
                ],
                expected_impact={
                    "late_ops_delta": -max(1, int(kpi_snapshot.get("late_ops_count", 0) * 0.2)),
                    "tardiness_minutes_delta": -max(30, int(kpi_snapshot.get("total_tardiness_minutes", 0) * 0.15)),
                    "setup_minutes_delta": -20,
                },
            )
        )

    if problem_rows:
        options.append(
            ProposalOption(
                id="OPT2",
                rank=2,
                score=0.86,
                title="Tăng ca có kiểm soát cho công đoạn trễ nhất",
                reason_summary="Bổ sung OT cho công đoạn có LateQty cao nhất để kéo về trước hạn.",
                moves=[
                    ProposalMove(
                        type=MoveType.INCREASE_OT_OR_ADD_SHIFT,
                        reason="Tăng cửa sổ xử lý trong ngày cho công đoạn đang trễ.",
                        operation_id=problem_rows[0].get("operation_id", ""),
                        machine=problem_rows[0].get("resource", {}).get("code"),
                        expected_impact={
                            "late_ops_delta": -1,
                            "tardiness_minutes_delta": -120,
                            "setup_minutes_delta": 0,
                        },
                    )
                ],
                expected_impact={"late_ops_delta": -1, "tardiness_minutes_delta": -120, "setup_minutes_delta": 0},
            )
        )

    if len(problem_rows) >= 2:
        options.append(
            ProposalOption(
                id="OPT3",
                rank=3,
                score=0.8,
                title="Điều phối nguồn lực thay thế",
                reason_summary="Chuyển operation sang máy thay thế để cắt hàng đợi ở bottleneck.",
                moves=[
                    ProposalMove(
                        type=MoveType.MOVE_TO_ALTERNATE_MACHINE,
                        reason="Giảm tải máy nghẽn bằng máy cùng nhóm.",
                        operation_id=problem_rows[1].get("operation_id", ""),
                        from_machine=problem_rows[1].get("resource", {}).get("code"),
                        to_machine=(hotspots.bottleneck_machines[1] if len(hotspots.bottleneck_machines) > 1 else hotspots.bottleneck_machines[0] if hotspots.bottleneck_machines else None),
                        expected_impact={
                            "late_ops_delta": -1,
                            "tardiness_minutes_delta": -60,
                            "setup_minutes_delta": 10,
                        },
                    )
                ],
                expected_impact={"late_ops_delta": -1, "tardiness_minutes_delta": -60, "setup_minutes_delta": 10},
            )
        )

    if has_lateness:
        proposed_due_date = _compute_reasonable_due_date(problem_rows, kpi_snapshot)
        reason = (
            f"Kế hoạch hiện tại chưa khả thi để hoàn tất đủ sản lượng trước hạn; đề xuất dời ngày giao sang {proposed_due_date}."
            if proposed_due_date
            else "Kế hoạch hiện tại chưa khả thi để hoàn tất đủ sản lượng trước hạn; cần điều chỉnh ngày giao hàng."
        )
        due_option = ProposalOption(
            id="OPT_DUE",
            rank=999,
            score=0.79,
            title="Điều chỉnh lại ngày giao hàng",
            reason_summary=reason,
            moves=[
                ProposalMove(
                    type=MoveType.REQUEST_DUE_DATE_EXTENSION,
                    reason=reason,
                    operation_id=(problem_rows[0].get("operation_id") if problem_rows else ""),
                    expected_impact={
                        "late_ops_delta": -max(1, int(kpi_snapshot.get("late_ops_count", 0) or 1)),
                        "tardiness_minutes_delta": -max(60, int(kpi_snapshot.get("total_tardiness_minutes", 0) or 60)),
                        "setup_minutes_delta": 0,
                    },
                )
            ],
            expected_impact={
                "late_ops_delta": -max(1, int(kpi_snapshot.get("late_ops_count", 0) or 1)),
                "tardiness_minutes_delta": -max(60, int(kpi_snapshot.get("total_tardiness_minutes", 0) or 60)),
                "setup_minutes_delta": 0,
                "proposed_due_date": proposed_due_date,
            },
        )
        options.append(due_option)

    options_sorted = sorted(options, key=lambda x: (x.rank, -x.score))
    top_n = max(1, top_n)
    picked = options_sorted[:top_n]

    # Guarantee due-date extension option appears when plan is late/infeasible.
    if has_lateness:
        due_opt = next((o for o in options_sorted if o.id == "OPT_DUE"), None)
        if due_opt and all(o.id != "OPT_DUE" for o in picked):
            if len(picked) >= top_n:
                picked[-1] = due_opt
            else:
                picked.append(due_opt)

    # Re-rank output order to be stable and contiguous.
    picked = sorted(picked, key=lambda x: -x.score)
    for i, opt in enumerate(picked, start=1):
        opt.rank = i
    return picked
