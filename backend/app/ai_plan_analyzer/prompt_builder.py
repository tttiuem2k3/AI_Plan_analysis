from __future__ import annotations

import json
from typing import Any, Dict, Optional


ALLOWED_MOVE_TYPES = [
    # Core moves that backend can actually apply/interpret
    "SWAP_ORDER_ON_MACHINE",
    "MOVE_TO_ALTERNATE_MACHINE",
    "SHIFT_TO_FILL_IDLE_WINDOW",
    "PULL_FORWARD_CRITICAL_OP",
    "BATCH_SAME_SETUP_FAMILY",
    # Business/capacity intents (may be interpreted downstream)
    "INCREASE_OT_OR_ADD_SHIFT",
    "HIRE_OUTSOURCE_LABOR",
    "REQUEST_DUE_DATE_EXTENSION",
]


# Template priority required by business
TEMPLATE_PROPOSAL_TYPES = [
    {
        "no": 1,
        "name": "Tăng ca để kịp hạn giao",
        "intent": "INCREASE_OT_OR_ADD_SHIFT",
    },
    {
        "no": 2,
        "name": "Bổ sung/đổi máy để kịp tiến độ",
        "intent": "MOVE_TO_ALTERNATE_MACHINE",
    },
    {
        "no": 3,
        "name": "Điều chuyển nhân sự giữa công đoạn",
        "intent": "HIRE_OUTSOURCE_LABOR",
    },
    {
        "no": 4,
        "name": "Thuê nhân sự thời vụ",
        "intent": "HIRE_OUTSOURCE_LABOR",
    },
    {
        "no": 5,
        "name": "Làm vào ngày nghỉ (Chủ nhật)",
        "intent": "INCREASE_OT_OR_ADD_SHIFT",
    },
    {
        "no": 6,
        "name": "Điều chỉnh lại lịch giao hàng",
        "intent": "REQUEST_DUE_DATE_EXTENSION",
    },
]


def build_system_prompt() -> str:
    # Strongly instruct JSON-only and schema compliance.
    return (
        "Bạn là trợ lý phân tích và tối ưu kế hoạch sản xuất.\n"
        "Chỉ được trả về JSON thuần (không markdown/không giải thích ngoài JSON).\n"
        "Không bịa dữ liệu; chỉ dùng thông tin trong INPUT_JSON.\n"
        "KHÔNG được bịa mã máy/nguồn lực/công đoạn/kế hoạch. Nếu thiếu dữ liệu tên thì ghi 'không đủ dữ liệu'.\n"
        "Mục tiêu: phân tích rõ vấn đề và đưa ra phương án gợi ý theo đúng khuôn mẫu nghiệp vụ.\n"
        "Ngôn ngữ hiển thị phải thân thiện với người dùng phổ thông, ưu tiên câu ngắn rõ nghĩa, tránh thuật ngữ kỹ thuật nội bộ.\n"
        "Quy ước hiển thị mã-tên: luôn ưu tiên dạng [Mã]-[Tên] nếu có đủ dữ liệu (TP/BTP/công đoạn/bộ phận/nguồn lực/máy/kế hoạch).\n"
        "Danh sách move types hợp lệ: "
        + ",".join(ALLOWED_MOVE_TYPES)
        + "."
    )


def build_payload(
    *,
    errors: Optional[Dict[str, Any]] = None,
    ortools_options: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build INPUT_JSON for the LLM.

    Design goal:
    - Keep payload small to reduce tokens/latency.
    - Prefer passing only business-relevant signals (errors + OR-Tools suggestions + KPI/hotspots).
    """

    out: Dict[str, Any] = {
        "schema_version": "v2",
        "errors": errors or {},
        "ortools_options": ortools_options or {},
        "kpi_snapshot": {},
        "hotspots": {},
        "business": {},
    }

    ctx = context if isinstance(context, dict) else {}

    # Canonical top-level fields used by prompt rules.
    kpi_snapshot = ctx.get("kpi_snapshot") if isinstance(ctx.get("kpi_snapshot"), dict) else {}
    hotspots = ctx.get("hotspots") if isinstance(ctx.get("hotspots"), dict) else {}
    business = ctx.get("business") if isinstance(ctx.get("business"), dict) else {}

    # Backward compatibility: lift from ortools_options.context when top-level ctx is missing.
    ort_ctx: Dict[str, Any] = {}
    if isinstance(out.get("ortools_options"), dict):
        raw_ort_ctx = out["ortools_options"].get("context")
        if isinstance(raw_ort_ctx, dict):
            ort_ctx = raw_ort_ctx

    if not kpi_snapshot and isinstance(ort_ctx.get("kpi_snapshot"), dict):
        kpi_snapshot = ort_ctx.get("kpi_snapshot") or {}
    if not hotspots and isinstance(ort_ctx.get("hotspots"), dict):
        hotspots = ort_ctx.get("hotspots") or {}
    if not business and isinstance(ort_ctx.get("business"), dict):
        business = ort_ctx.get("business") or {}

    out["kpi_snapshot"] = kpi_snapshot
    out["hotspots"] = hotspots
    out["business"] = business

    # Keep compatibility path for existing debugging and older prompt consumers.
    if isinstance(out.get("ortools_options"), dict):
        out["ortools_options"].setdefault(
            "context",
            {
                "kpi_snapshot": kpi_snapshot,
                "hotspots": hotspots,
                "business": business,
            },
        )

    return out


def build_user_prompt(payload: Dict[str, Any], top_n: int = 2) -> str:
    """Build a compact prompt for proposal generation.

    Notes:
    - Keep output small: 1-2 proposals, each 1-3 moves.
    - Ask model to focus on errors + OR-Tools suggestions (and compact context if present).
    """

    # Clamp to keep response small & fast.
    try:
        top_n = int(top_n)
    except Exception:
        top_n = 2
    top_n = max(1, min(20, top_n))

    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    schema_hint = (
        "{"
        '"analysis":{"items":["<item1>","<item2>","<item3>","<item4>"]},'
        '"proposals":[{'
        '"id":"PA1",'
        '"template_no":1,'
        '"title":"...",'
        '"content_lines":["...","..."],'
        '"priority":"P1",'
        '"can_apply":false,'
        '"moves":[{'
        '"type":"...",'
        '"reason":"...",'
        '"op_id":"(nếu tác động 1 công đoạn)",'
        '"machine":"(nếu liên quan máy)",'
        '"from_machine":"(nếu chuyển máy)",'
        '"to_machine":"(nếu chuyển máy)",'
        '"op_ids_after":["..."],'
        '"expected_impact":{"late_ops_delta":0,"tardiness_minutes_delta":0,"setup_minutes_delta":0}'
        "}]"
        "}]"
        "}"
    )

    template_hint_lines = []
    try:
        for t in TEMPLATE_PROPOSAL_TYPES:
            no = int(t.get("no") or 0)
            name = str(t.get("name") or "")
            if 1 <= no <= 6 and name:
                template_hint_lines.append(f"  - {no}) {name}")
    except Exception:
        template_hint_lines = []

    template_hints = "\n".join(template_hint_lines) if template_hint_lines else "  - 1) ...\n  - 2) ...\n  - 3) ...\n  - 4) ...\n  - 5) ...\n  - 6) ..."

    return (
        "INPUT_JSON="
        + compact
        + "\n\n"
        "YÊU CẦU BẮT BUỘC:\n"
        f"- Trả về đúng JSON theo schema: {schema_hint}.\n"
        "- Chỉ output JSON thuần, không markdown, không văn bản ngoài JSON.\n"
        "- PHẢI tuân thủ khuôn mẫu dưới đây.\n"
        "- TUYỆT ĐỐI KHÔNG bịa mã máy/nguồn lực/công đoạn/kế hoạch.\n"
        "  Chỉ được dùng mã xuất hiện trong một trong các nguồn: INPUT_JSON.business.problem_rows[].resource.code, INPUT_JSON.hotspots.bottleneck_machines, INPUT_JSON.hotspots.bottleneck_ops[].machine, INPUT_JSON.errors.baseline_validation_errors_sample[].context.(prev|cur).machine.\n"
        "  Nếu không đủ dữ liệu để nêu mã-tên thì phải ghi 'không đủ dữ liệu' thay vì tự tạo mã mới.\n"
        "- analysis.items PHẢI có đúng 4 phần tử theo đúng ý nghĩa sau:\n"
        "  1) Bán thành phẩm [Mã BTP]-[Tên BTP] của thành phẩm [Mã TP]-[Tên TP] đang bị sản xuất trễ so với thời hạn giao hàng ...\n"
        "  2) Mô tả tình trạng ràng buộc nguồn lực/công đoạn.\n"
        "     - NẾU INPUT_JSON.errors.baseline_validation_errors_count = 0 (hoặc baseline_validation_summary rỗng) thì KHÔNG được khẳng định có 'xung đột'; phải ghi rõ 'không ghi nhận xung đột nguồn lực theo kiểm tra tự động' và nêu nguyên nhân chính khác (ví dụ: bị chặn theo chuỗi công đoạn, thiếu khung thời gian, quá tải năng lực).\n"
        "     - NẾU có lỗi xung đột trong INPUT_JSON.errors.baseline_validation_errors_sample thì mới được mô tả 'xung đột' và chỉ được nêu nguồn lực/máy/kế hoạch có trong INPUT_JSON.\n"
        "  3) Kế hoạch sản xuất chưa tối ưu ở chỗ ... và cần có các điểm cải thiện ...\n"
        "  4) Các nội dung phân tích tổng thể kế hoạch sản xuất khác ...\n"
        "- Nếu kế hoạch có nhiều bán thành phẩm lỗi, analysis được phép gom theo thành phẩm chính bị ảnh hưởng; nhưng proposal phải đầy đủ và chính xác theo từng tình huống.\n"
        "- Vì trong một kế hoạch có thể có NHIỀU đơn hàng (Số chứng từ khác nhau):\n"
        "  - MỖI khi nhắc tới một BTP/TP cụ thể trong analysis/proposals, bắt buộc ghi rõ thuộc đơn hàng nào theo mẫu: 'của đơn hàng [Số chứng từ]-[Khách hàng]' (nếu có dữ liệu).\n"
        "  - Dữ liệu đơn hàng lấy từ INPUT_JSON.business.problem_rows[].so_don_hang và INPUT_JSON.business.problem_rows[].khach_hang (fallback: INPUT_JSON.ortools_options.context.business.problem_rows).\n"
        "- Hạn chế thuật ngữ tiếng Anh trong nội dung hiển thị (VD: unplanned/planned/OT/overtime).\n"
        "  - Dùng tiếng Việt tương đương: 'chưa lên kế hoạch', 'đã lên kế hoạch', 'tăng ca'.\n"
        "- Tuyệt đối KHÔNG dùng các key kỹ thuật trong nội dung hiển thị cho người dùng: 'LateQty', 'UnplannedQty', 'late_ops_delta', 'tardiness_minutes_delta', 'setup_minutes_delta'.\n"
        "  - Nếu cần diễn tả tác động, phải viết bằng tiếng Việt tự nhiên, ví dụ: 'Tác động dự kiến: giảm 149 công đoạn trễ, giảm 187763 phút chậm tiến độ.'\n"
        "- Tuyệt đối KHÔNG đưa định danh kỹ thuật vào nội dung hiển thị: op_id, op_ids_after, chuỗi dạng 'KHSX/...::...'.\n"
        "  - Khi cần mô tả hành động, chỉ viết theo ngữ nghĩa nghiệp vụ: 'chuyển công đoạn ... sang máy ...', 'điều phối lại thứ tự sản xuất ...'.\n"
        "- Quy tắc nguồn lực bắt buộc khi diễn giải:\n"
        "  - Chỉ mã nguồn lực KHÁC M000 mới được gọi là 'máy'.\n"
        "  - M000/LABOR phải gọi là 'nguồn lực nhân công', không gọi là máy.\n"
        "- Quy tắc bắt buộc theo phụ thuộc công đoạn (dependency):\n"
        "  - Nếu INPUT_JSON.business.problem_rows[].dependency.is_blocked_by_predecessor = true thì nguyên nhân gốc là dependency.blocked_by.\n"
        "  - TUYỆT ĐỐI KHÔNG đề xuất tăng ca/đổi máy/điều nhân sự cho bước đang bị chặn.\n"
        "  - Proposal phải ưu tiên giải quyết bước gây chặn (blocked_by) trước (ví dụ tăng ca/bổ sung nguồn lực/đổi máy ở công đoạn blocked_by) rồi mới nói tới bước sau.\n"
        "- KHUÔN MẪU template_no 1 -> 6 (đúng ý nghĩa template):\n"
        + template_hints
        + "\n"
        "- proposals phải tuân theo thứ tự ưu tiên template_no 1 -> 6. Chỉ chọn tối đa"
        + str(top_n)
        + " phương án phù hợp nhất.\n"
        "- QUY TẮC ƯU TIÊN TUẦN TỰ BẮT BUỘC: phải xét theo thứ tự 1 -> 2 -> 3 -> 4 -> 5 -> 6.\n"
        "  - Chỉ chuyển sang nhóm tiếp theo khi nhóm hiện tại không đủ giúp kịp hạn giao.\n"
        "  - Nghĩa là: ưu tiên tăng ca (1), rồi bổ sung/đổi máy (2), rồi điều chuyển nhân sự (3), rồi thuê thời vụ (4), rồi làm Chủ nhật (5), cuối cùng mới đề xuất dời lịch giao (6).\n"
        "  - Với nhân sự thuê ngoài (template 4): giờ hành chính tối đa 15 người/ngày, giờ tăng ca tối đa 15 người/ngày.\n"
        "  - Chỉ dùng template 6 khi đã đi hết khả năng của 1->5 mà vẫn không đáp ứng hạn giao; khi đó phải nêu ngày giao mới sớm nhất có thể theo dữ liệu.\n"
        "- Số lượng proposals được phép linh hoạt từ 1 đến tối đa top_n theo mức độ vấn đề của dữ liệu; không bắt buộc phải đủ N phương án.\n"
        "- Khi số phương án ít hơn top_n, chỉ trả các template thực sự phù hợp; không tạo phương án cho đủ số lượng.\n"
        "- Khi cần nhiều hơn 6 phương án, được phép lặp template_no phù hợp (ví dụ nhiều phương án tăng ca/đổi máy khác nhau).\n"
        "- Mỗi proposal.content_lines phải là MỘT DANH SÁCH các dòng; MỖI DÒNG là 1 ý và phải bắt đầu bằng '+ '.\n"
        "  Không gộp nhiều ý vào cùng một string (không viết 1 dòng chứa nhiều dấu '+').\n"
        "- Trong proposals bắt buộc hiển thị BTP/TP/nguồn lực theo mẫu [Mã]-[Tên] nếu có dữ liệu.\n"
        "- Dữ liệu OR-Tools phải đọc từ INPUT_JSON.ortools_options + INPUT_JSON.kpi_snapshot + INPUT_JSON.hotspots + INPUT_JSON.business.\n"
        "- Mã BTP và mã TP phải dùng đúng trường mã sản phẩm trong INPUT_JSON.business.problem_rows (hoặc INPUT_JSON.ortools_options.context.business.problem_rows nếu dữ liệu cũ).\n"
        "- Dữ liệu nhân sự phải đọc từ INPUT_JSON.business.problem_rows[].workforce và INPUT_JSON.business.problem_rows[].dept (fallback: INPUT_JSON.ortools_options.context.business.problem_rows).\n"
        "- Nhân sự giờ hành chính phải bằng workforce.regular_staff_required.\n"
        "- Nhân sự tăng ca phải dùng workforce.ot_staff_recommended và không vượt workforce.ot_staff_max.\n"
        "- Nhân sự thuê ngoài giờ hành chính phải dùng workforce.outsource_staff_recommended; nhân sự thuê ngoài giờ tăng ca phải dùng workforce.outsource_ot_staff_recommended; mỗi loại không vượt 15 người/ngày.\n"
        "- Ràng buộc cứng nhân sự: số lượng nhân sự tăng ca tối đa = số lượng nhân sự công đoạn lớn; số lượng nhân sự thuê ngoài tối đa = 15 người/ngày.\n"
        "- Template 1 (Làm thêm giờ tăng ca):\n"
        "  + Bán thành phẩm [Mã BTP]-[Tên BTP] của thành phẩm [Mã TP]-[Tên TP], cần sản xuất tăng ca để đáp ứng thời gian giao hàng, với thông tin cụ thể sau:\n"
        "  + Số lượng cần sản xuất tăng ca: ...\n"
        "  + Tổng số giờ cần tăng ca: ...\n"
        "  + Số lượng nhân sự [Tên công đoạn lớn] cần tăng ca: ...\n"
        "  + Số lượng máy cần cho tăng ca: Máy [Mã máy]-[Tên máy], Máy [Mã máy]-[Tên máy],... (NẾU nguồn lực là nhân công/M000 thì ghi 'không áp dụng (nguồn lực nhân công)' hoặc 'không đủ dữ liệu' - KHÔNG được ghi 'Nhân công' ở dòng máy)\n"
        "  + Bắt đầu tăng ca: từ ngày ... đến ngày ...\n"
        "  + (Có thể lặp lại block trên cho BTP khác)\n"
        "- Template 2 (Bổ sung máy sản xuất):\n"
        "  + Bán thành phẩm [Mã BTP]-[Tên BTP] của thành phẩm [Mã TP]-[Tên TP], cần bổ sung thêm máy để sản xuất kịp tiến độ giao hàng với thông tin máy cụ thể như sau: Máy [Mã máy]-[Tên máy], ...\n"
        "  + Việc điều chỉnh này sẽ ảnh hưởng (NẾU CÓ) đến các kế hoạch: [Mã kế hoạch],... dùng cùng nguồn lực trên, nên sẽ điều chỉnh các kế hoạch bị ảnh hưởng này như sau: ...\n"
        "  + (Có thể lặp lại block trên cho BTP khác)\n"
        "- Template 3 (Điều nhân sự từ công đoạn khác):\n"
        "  + Bán thành phẩm [Mã BTP]-[Tên BTP] của thành phẩm [Mã TP]-[Tên TP], cần chuyển [Số lượng nhân sự] nhân sự thuộc [Tên công đoạn lớn] từ kế hoạch [Mã kế hoạch] để sản xuất kịp tiến độ giao hàng.\n"
        "  + Việc điều chỉnh này sẽ ảnh hưởng đến các kế hoạch [Mã kế hoạch],... nên cần điều chỉnh như sau ...\n"
        "- Template 4 (Thuê thêm nhân sự thời vụ):\n"
        "  + Bán thành phẩm [Mã BTP]-[Tên BTP] của thành phẩm [Mã TP]-[Tên TP] cần thuê thêm nhân sự thời vụ làm việc giờ hành chính để đáp ứng thời gian giao hàng, với thông tin cụ thể sau:\n"
        "  + Số lượng cần sản xuất: ...\n"
        "  + Số lượng nhân sự [Tên công đoạn lớn] cần thuê: ...\n"
        "  + Bắt đầu làm việc: từ ngày ... đến ngày ...\n"
        "  + Số lượng máy cần: Máy [Mã máy]-[Tên máy], ...\n"
        "  + Và tăng ca (nếu có):\n"
        "  + Số lượng cần sản xuất tăng ca: ...\n"
        "  + Tổng số giờ cần tăng ca: ...\n"
        "  + Số lượng nhân sự [Tên công đoạn lớn] cần tăng ca: ...\n"
        "  + Số lượng máy cần cho tăng ca: Máy [Mã máy]-[Tên máy], ...\n"
        "  + Bắt đầu tăng ca: từ ngày ... đến ngày ...\n"
        "- Template 5 (Làm vào ngày nghỉ hàng tuần):\n"
        "  + Bán thành phẩm [Mã BTP]-[Tên BTP] của thành phẩm [Mã TP]-[Tên TP], cần làm việc vào ngày nghỉ hàng tuần để sản xuất kịp tiến độ giao hàng, với thông tin cụ thể sau:\n"
        "  + Số lượng nhân sự [Tên công đoạn lớn]: ...\n"
        "  + Công đoạn: ...\n"
        "  + Ngày cần làm việc (Chủ nhật): ...\n"
        "  + Số lượng cần sản xuất vào ngày nghỉ: ...\n"
        "- Template 6 (Điều chỉnh lại ngày giao hàng):\n"
        "  + Thành phẩm [Mã TP]-[Tên TP], từ kế hoạch hiện tại đã áp dụng các biện pháp nhưng vẫn không đáp ứng được lịch giao [Ngày giao hàng], sẽ điều chỉnh lại lịch giao hàng sang [Ngày giao hàng mới].\n"
        "- Quy tắc bắt buộc theo nguồn lực: BTP dùng M000 tăng sản lượng bằng tăng nhân sự; BTP dùng máy móc tăng sản lượng bằng tăng máy sản xuất.\n"
        "- Mỗi proposal có thể chứa nhiều bán thành phẩm/thành phẩm cần điều chỉnh, nhưng phải cụ thể và chính xác.\n"
        "- can_apply trong output chỉ là gợi ý của LLM; backend sẽ validate lại StartDate/EndDate/DailyQty/resource/capacity/DueDate/conflict và quyết định can_apply cuối cùng.\n"
        "- Không đặt can_apply=true nếu proposal không có moves cụ thể, không có thay đổi dữ liệu thực thi, hoặc không chắc chắn qua được validate.\n"
        "- Mỗi proposal chỉ 1-4 moves; ưu tiên ít thay đổi nhưng hiệu quả.\n"
        "- Moves phải đủ trường để hệ thống thực thi/hiển thị: MOVE_TO_ALTERNATE_MACHINE cần op_id+to_machine; SWAP_ORDER_ON_MACHINE cần machine+op_ids_after.\n"
        "- Các trường kỹ thuật (op_id/op_ids_after/expected_impact) chỉ để hệ thống xử lý; tuyệt đối không lặp lại các khóa này bằng nguyên văn trong title/description/content_lines.\n"
        "- Không lặp lại INPUT_JSON trong output.\n"
        "- Không thêm bất kỳ text nào ngoài JSON.\n"
    )
