from __future__ import annotations

import json
from pathlib import Path

from .service import ProductionPlanningOptimizerService


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    sample_file = here / "mock_input.json"
    payload = json.loads(sample_file.read_text(encoding="utf-8"))

    svc = ProductionPlanningOptimizerService(top_problem_rows=6, top_options=3)
    result = svc.run(payload)

    print("=== FULL RESULT (solver status) ===")
    print(result.full_result["solver"]["status"])
    print("=== LLM PAYLOAD KEYS ===")
    print(list(result.llm_payload.keys()))
    print(json.dumps(result.llm_payload, ensure_ascii=False, indent=2))
