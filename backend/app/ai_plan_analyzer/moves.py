from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Tuple

from .dtos import Move, Operation, Plan


def _move_to_dict(mv: Any) -> Dict[str, Any]:
    """Convert Move-like objects to dict safely (supports dataclass slots)."""

    if mv is None:
        return {}
    try:
        if is_dataclass(mv):
            return asdict(mv)
    except Exception:
        pass
    if hasattr(mv, "model_dump"):
        try:
            return mv.model_dump()  # type: ignore[no-any-return]
        except Exception:
            pass
    # Last resort: best-effort getattr on known fields
    out: Dict[str, Any] = {}
    for k in (
        "type",
        "reason",
        "expected_impact",
        "machine",
        "op_ids_before",
        "op_ids_after",
        "op_id",
        "from_machine",
        "to_machine",
        "from_day",
        "to_day",
    ):
        try:
            if hasattr(mv, k):
                out[k] = getattr(mv, k)
        except Exception:
            continue
    return out


def apply_moves(plan: Plan, moves: List[Move]) -> tuple[Plan, Dict[str, Any]]:
    """Apply a list of moves to a plan.

    This patcher is intentionally minimal and safe. It only mutates:
    - operation machine assignment for MOVE_TO_ALTERNATE_MACHINE
    - machine sequence metadata for SWAP_ORDER_ON_MACHINE (stored in plan.constraints)

    For day-bucket shifts, SHIFT_TO_FILL_IDLE_WINDOW currently stores intent in op.meta.

    Returns:
      patched_plan, affected_scope
    """

    patched = copy.deepcopy(plan)
    affected_machines: set[str] = set()
    affected_ops: set[str] = set()

    # Sequence intent: machine -> list[op_id]
    seq_intent: Dict[str, List[str]] = patched.constraints.setdefault("machine_sequences", {})  # type: ignore[assignment]

    for mv in moves:
        if mv.type == "MOVE_TO_ALTERNATE_MACHINE":
            if not mv.op_id or not mv.to_machine:
                continue
            op = _find_op(patched, mv.op_id)
            if not op:
                continue
            affected_ops.add(op.op_id)
            if op.machine:
                affected_machines.add(op.machine)
            op.machine = mv.to_machine
            affected_machines.add(mv.to_machine)

        elif mv.type == "SWAP_ORDER_ON_MACHINE":
            if not mv.machine or not mv.op_ids_after:
                continue
            seq_intent[mv.machine] = list(mv.op_ids_after)
            affected_machines.add(mv.machine)
            affected_ops.update(mv.op_ids_after)

        elif mv.type in ("BATCH_SAME_SETUP_FAMILY", "PULL_FORWARD_CRITICAL_OP", "SHIFT_TO_FILL_IDLE_WINDOW"):
            # Store intent only; rescheduler will interpret.
            if mv.op_id:
                op = _find_op(patched, mv.op_id)
                if op:
                    op.meta.setdefault("intents", []).append({"type": mv.type, "move": _move_to_dict(mv)})
                    affected_ops.add(op.op_id)
            if mv.machine:
                affected_machines.add(mv.machine)

    affected_scope = {
        "machines": sorted(affected_machines),
        "ops": sorted(affected_ops),
    }
    return patched, affected_scope


def _find_op(plan: Plan, op_id: str) -> Operation | None:
    for op in plan.operations:
        if op.op_id == op_id:
            return op
    return None
