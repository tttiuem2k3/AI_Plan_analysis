/* =========================================================
   dinh-muc.js (v4 - FULL)
   - Tree TP/BTP: mặc định chỉ TP, xổ BTP theo TP
   - CRUD:
     + Thêm/Cập nhật/Xóa TP
     + Thêm BTP cấu thành cho TP (tạo mới hoặc chọn từ thư viện)
     + Cập nhật BTP (master)
     + Gỡ BTP khỏi TP
     + Thư viện BTP: tìm kiếm/chọn/cập nhật/xóa/thêm mới
   - Yêu cầu mới:
     (1) Khi bấm Thêm BTP hoặc Cập nhật BTP trong Thư viện => đóng Thư viện, mở modal Product ở mode BTP
     (2) Khi chọn BTP trong Thư viện => xổ đầy đủ dữ liệu vào dòng thêm BTP của TP
     (3) TP không chọn công đoạn, cong_doan luôn null
   ========================================================= */

const API_BASE = "/api/v1/dinh-muc";
const API_CONGDOAN = "/api/v1/cong-doan/list";

let TREE = [];
let CONG_DOAN = [];
let expandedSet = new Set();

// cache cho thư viện BTP
let BTP_LIB_CACHE = [];

// dòng đang thêm BTP cấu thành cho 1 TP
let currentAddRow = {
  tpId: null,
  rowEl: null,
  selectedBtp: null // object BTP đầy đủ khi chọn từ thư viện
};

/* ---------------- Utils ---------------- */
function fmt(val) {
  if (val === null || val === undefined || val === "") return "";
  const n = Number(val);
  if (Number.isNaN(n)) return val;
  return n.toLocaleString("en-US", { maximumFractionDigits: 5 });
}

function toast(type, title, msg) {
  const wrap = document.getElementById("toastWrap");
  const el = document.createElement("div");
  el.className = "toast";

  const icon = type === "ok"
    ? "bi-check-circle"
    : (type === "warn" ? "bi-exclamation-triangle" : "bi-x-circle");
  const color = type === "ok"
    ? "#16a34a"
    : (type === "warn" ? "#f59e0b" : "#dc2626");

  el.innerHTML = `
    <i class="bi ${icon}" style="color:${color};font-size:16px;margin-top:1px"></i>
    <div>
      <div class="t-title">${title}</div>
      <div class="t-msg">${msg}</div>
    </div>
  `;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

async function api(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || ("Lỗi API: " + res.status));
  return data;
}

/* ---------------- Modal helpers ---------------- */
function openModal(id) { document.getElementById(id).classList.remove("hidden"); }
function closeModal(id) { document.getElementById(id).classList.add("hidden"); }

document.querySelectorAll("[data-close]").forEach(btn => {
  btn.addEventListener("click", () => closeModal(btn.dataset.close));
});

function confirmDialog(title, msg) {
  return new Promise((resolve) => {
    document.getElementById("confirmTitle").textContent = title;
    document.getElementById("confirmMsg").textContent = msg;

    const ok = document.getElementById("confirmOk");
    const cancel = document.getElementById("confirmCancel");

    const cleanup = () => {
      ok.onclick = null;
      cancel.onclick = null;
      closeModal("modalConfirm");
    };

    ok.onclick = () => { cleanup(); resolve(true); };
    cancel.onclick = () => { cleanup(); resolve(false); };

    openModal("modalConfirm");
  });
}

/* ---------------- CongDoan master ---------------- */
async function loadCongDoan() {
  try {
    const rows = await api(API_CONGDOAN);
    // Backend hiện trả {MaCongDoan, TenCongDoan}
    CONG_DOAN = (rows || []).map(x => ({
      ma_cong_doan: x.ma_cong_doan ?? x.MaCongDoan,
      ten_cong_doan: x.ten_cong_doan ?? x.TenCongDoan
    })).filter(x => !!x.ma_cong_doan);
  } catch {
    CONG_DOAN = [];
  }

  const sel = document.getElementById("fCongDoan");
  sel.innerHTML = `<option value="">-- Chọn công đoạn --</option>`;
  CONG_DOAN.forEach(x => {
    const opt = document.createElement("option");
    opt.value = x.ma_cong_doan;
    // Hiển thị TÊN công đoạn, lưu MÃ công đoạn
    opt.textContent = x.ten_cong_doan;
    sel.appendChild(opt);
  });
}

function congDoanName(ma) {
  if (!ma) return "";
  const x = CONG_DOAN.find(c => c.ma_cong_doan === ma);
  return x ? x.ten_cong_doan : ma;
}

function buildCongDoanSelect(value = "") {
  const opts = [`<option value="">--</option>`]
    .concat(CONG_DOAN.map(c =>
      // Hiển thị TÊN công đoạn, lưu MÃ công đoạn
      `<option value="${c.ma_cong_doan}" ${c.ma_cong_doan === value ? 'selected' : ''}>${c.ten_cong_doan}</option>`
    ))
    .join("");

  const html = `<select class="select" data-f="congdoan">${opts}</select>`;
  return html;
}

/* =========================================================
   MODAL PRODUCT (Add/Edit TP/BTP) - Rules: TP no congdoan
   ========================================================= */
const modalProduct = {
  mode: "create", // create | update
  editId: null,
  editMaLocked: null
};

function applyLoaiRules() {
  const loai = document.getElementById("fLoai").value;
  const wrap = document.getElementById("wrapCongDoan");
  const sel = document.getElementById("fCongDoan");

  if (loai === "TP") {
    sel.value = "";
    sel.disabled = true;
    wrap.classList.add("is-disabled");
  } else {
    sel.disabled = false;
    wrap.classList.remove("is-disabled");
  }
}

// đổi loại khi tạo mới
document.getElementById("fLoai").addEventListener("change", applyLoaiRules);

function openCreateProductModal(defaultLoai = "TP") {
  modalProduct.mode = "create";
  modalProduct.editId = null;
  modalProduct.editMaLocked = null;

  document.getElementById("modalProductTitle").textContent =
    defaultLoai === "BTP" ? "Thêm bán thành phẩm" : "Thêm sản phẩm";

  document.getElementById("fLoai").value = defaultLoai;
  document.getElementById("fMa").value = "";
  document.getElementById("fTen").value = "";
  document.getElementById("fDL").value = "";
  document.getElementById("fTG").value = "";
  document.getElementById("fCongDoan").value = "";

  document.getElementById("fLoai").disabled = false;
  document.getElementById("fMa").disabled = false;

  applyLoaiRules();
  openModal("modalProduct");
}

function openEditProductModal(product) {
  modalProduct.mode = "update";
  modalProduct.editId = product.dinh_muc_id;
  modalProduct.editMaLocked = product.ma_san_pham || "";

  document.getElementById("modalProductTitle").textContent =
    (product.loai_san_pham === "TP" ? "Cập nhật thành phẩm" : "Cập nhật bán thành phẩm");

  document.getElementById("fLoai").value = product.loai_san_pham;
  document.getElementById("fMa").value = product.ma_san_pham || "";
  document.getElementById("fTen").value = product.ten_san_pham || "";
  document.getElementById("fDL").value = (product.dinh_luong ?? "") === null ? "" : (product.dinh_luong ?? "");
  document.getElementById("fTG").value = (product.dinh_muc_thoi_gian ?? "") === null ? "" : (product.dinh_muc_thoi_gian ?? "");
  document.getElementById("fCongDoan").value = product.cong_doan || "";

  // edit thì không cho đổi loại
  document.getElementById("fLoai").disabled = true;
  document.getElementById("fMa").disabled = true;

  applyLoaiRules();

  if (product.loai_san_pham === "BTP") {
    toast("warn", "Lưu ý", "Sửa BTP có thể ảnh hưởng các TP khác nếu BTP đang dùng chung.");
  }

  openModal("modalProduct");
}

document.getElementById("btnAddProduct").addEventListener("click", () => {
  openCreateProductModal("TP");
});

document.getElementById("btnSaveProduct").addEventListener("click", async () => {
  try {
    const loai = document.getElementById("fLoai").value;

    const maFromForm = document.getElementById("fMa").value.trim();
    const maLocked = modalProduct.mode === "update" ? (modalProduct.editMaLocked || maFromForm) : maFromForm;

    const payload = {
      loai_san_pham: loai,
      ma_san_pham: maLocked,
      ten_san_pham: document.getElementById("fTen").value.trim(),
      dinh_luong: document.getElementById("fDL").value === "" ? null : Number(document.getElementById("fDL").value),
      dinh_muc_thoi_gian: document.getElementById("fTG").value === "" ? null : Number(document.getElementById("fTG").value),

      // master: TP không có công đoạn
      thu_tu_sx: null,
      cong_doan: (loai === "TP") ? null : (document.getElementById("fCongDoan").value || null)
    };

    if (!payload.ma_san_pham || !payload.ten_san_pham) {
      toast("warn", "Thiếu dữ liệu", "Vui lòng nhập Mã và Tên sản phẩm");
      return;
    }

    if (modalProduct.mode === "create") {
      await api(`${API_BASE}/product`, { method: "POST", body: JSON.stringify(payload) });
      closeModal("modalProduct");
      await loadTree();
      toast("ok", "Thành công", "Đã thêm sản phẩm");
      return;
    }

    await api(`${API_BASE}/product/${modalProduct.editId}`, { method: "PUT", body: JSON.stringify(payload) });
    closeModal("modalProduct");

    await loadTree();

    // nếu modal thư viện đang mở -> reload
    if (!document.getElementById("modalBtpLibrary").classList.contains("hidden")) {
      await loadBtpLib();
    }

    toast("ok", "Thành công", "Đã cập nhật sản phẩm");
  } catch (e) {
    toast("err", "Không thể lưu", e.message);
  }
});

/* ---------------- Tree load/render ---------------- */
async function loadTree() {
  TREE = await api(`${API_BASE}/tree`);
  render();
}

function filterTP(keyword) {
  const kw = (keyword || "").trim().toLowerCase();
  if (!kw) return TREE;

  return TREE.filter(tp =>
    (tp.ma_san_pham || "").toLowerCase().includes(kw) ||
    (tp.ten_san_pham || "").toLowerCase().includes(kw)
  );
}

function render() {
  const tb = document.getElementById("tbBody");
  tb.innerHTML = "";

  const list = filterTP(document.getElementById("txtSearchTP").value);

  let stt = 1;
  for (let i = 0; i < list.length; i++) {
    const tp = list[i];
    tb.appendChild(buildTPRow(tp, stt));
    const isExpanded = expandedSet.has(tp.dinh_muc_id);

    // Render BTP và xác định BTP cuối trong TP
    if (tp.children && tp.children.length) {
      const btps = tp.children;
      const lastIndex = Math.max(0, btps.length - 1);
      btps.forEach((btp, idx) => {
        const isLastBtpInTp = (idx === lastIndex);
        tb.appendChild(buildBTPRow(tp.dinh_muc_id, btp, !isExpanded, { isLastBtpInTp }));
      });
    }
    stt++;
  }
}

function findProductInTree(productId) {
  for (const tp of TREE) {
    if (tp.dinh_muc_id === productId) return tp;
    for (const btp of (tp.children || [])) {
      if (btp.dinh_muc_id === productId) return btp;
    }
  }
  return null;
}

function findProductInLib(productId) {
  return BTP_LIB_CACHE.find(x => x.dinh_muc_id === productId) || null;
}

/* ---------------- Rows ---------------- */
function buildTPRow(tp, stt) {
  const tr = document.createElement("tr");
  tr.className = "row-tp";
  tr.dataset.tpId = tp.dinh_muc_id;

  const expanded = expandedSet.has(tp.dinh_muc_id);
  const chevron = expanded ? "bi-chevron-up" : "bi-chevron-down";

  // Chỉ hiển thị STT cho TP, BTP thì để trống
  const sttDisplay = (typeof stt === 'number' && stt > 0) ? stt : '';

  // Chỉ hiển thị nút xổ khi TP có ít nhất 1 BTP
  const hasChildren = Array.isArray(tp.children) && tp.children.length > 0;
  const expanderHtml = hasChildren ? `
      <button class="expander" data-action="toggle" title="Xổ BTP">
        <i class="bi ${chevron}"></i>
      </button>
    ` : '';

  tr.innerHTML = `
    <td>${expanderHtml}</td>
    <td class="cell-stt">${sttDisplay}</td>
    <td>Thành phẩm</td>
    <td class="cell-ma">${tp.ma_san_pham || ""}</td>
    <td class="cell-ten">${tp.ten_san_pham || ""}</td>
    <td class="cell-dl">${fmt(tp.dinh_luong)}</td>
    <td class="cell-tg">${fmt(tp.dinh_muc_thoi_gian)}</td>
    <td class="cell-tt"></td>
    <td class="cell-cd">${congDoanName(tp.cong_doan)}</td>
    <td>
      <div class="actions">
        <button class="icon primary" data-action="add-btp" title="Thêm BTP cấu thành">
          <i class="bi bi-plus-circle"></i>
        </button>
        <button class="icon" data-action="edit-tp" title="Cập nhật thành phẩm">
          <i class="bi bi-pencil-square"></i>
        </button>
        <button class="icon danger" data-action="delete-tp" title="Xóa thành phẩm">
          <i class="bi bi-trash3"></i>
        </button>
      </div>
    </td>
  `;
  return tr;
}

function buildBTPRow(tpId, btp, hidden, opts = {}) {
  const tr = document.createElement("tr");
  tr.className = "row-btp" + (hidden ? " is-hidden" : "");
  tr.dataset.parentTpId = tpId;
  tr.dataset.btpId = btp.dinh_muc_id;

  const congdoanDisplay = congDoanName(btp.cong_doan) || btp.cong_doan_ten || "";

  const isLast = !!opts?.isLastBtpInTp;
  const iconBtp = isLast
    ? '../assets/icons/arrow-right_end_btp.png'
    : '../assets/icons/arrow-right_con_btp.png';

  tr.innerHTML = `
    <td></td>
    <td class="cell-stt">
      <span class="btp-stt" style="display:inline-flex;align-items:center;gap:6px;">
        <img src="${iconBtp}" alt="" style="width:18px;height:18px;object-fit:contain;" />
        <span></span>
      </span>
    </td>
    <td>Bán thành phẩm</td>
    <td class="cell-ma">${btp.ma_san_pham || ""}</td>
    <td class="cell-ten">${btp.ten_san_pham || ""}</td>
    <td class="cell-dl">${fmt(btp.dinh_luong)}</td>
    <td class="cell-tg">${fmt(btp.dinh_muc_thoi_gian)}</td>
    <td class="cell-tt">${btp.thu_tu_sx_tp ?? ""}</td>
    <td class="cell-cd">${congdoanDisplay}</td>
    <td>
      <div class="actions">
        <button class="icon primary" data-action="unlink-btp" title="Gỡ bán thành phẩm khỏi thành phẩm">
          <i class="bi bi-link-45deg"></i>
        </button>
        <button class="icon" data-action="edit-btp" title="Cập nhật bán thành phẩm">
          <i class="bi bi-pencil-square"></i>
        </button>
        <button class="icon danger" data-action="delete-btp" title="Xóa bán thành phẩm">
          <i class="bi bi-trash3"></i>
        </button>
      </div>
    </td>
  `;
  return tr;
}

/* ---------------- Add BTP inline row ---------------- */
function addInlineBTPRow(tpId) {
  toggleChildren(tpId, true);

  const tb = document.getElementById("tbBody");
  const existed = tb.querySelector(`tr[data-new-btp-of='${tpId}']`);
  if (existed) {
    existed.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }

  const tpRow = tb.querySelector(`tr.row-tp[data-tp-id='${tpId}']`);
  let insertAfter = tpRow;
  const childs = Array.from(tb.querySelectorAll(`tr[data-parent-tp-id='${tpId}']`));
  if (childs.length) insertAfter = childs[childs.length - 1];

  const nextNo = (TREE.find(x => x.dinh_muc_id === tpId)?.children?.length || 0) + 1;

  const tr = document.createElement("tr");
  tr.className = "row-btp";
  tr.dataset.newBtpOf = tpId;

  tr.innerHTML = `
    <td></td>
    <td class="cell-stt">
      <span class="btp-stt" style="display:inline-flex;align-items:center;gap:6px;">
        <img src="../assets/icons/arrow-right_end_btp.png" alt="" style="width:14px;height:14px;object-fit:contain;" />
        <span></span>
      </span>
    </td>
    <td>Bán thành phẩm</td>
    <td class="cell-ma"><input class="input" data-f="ma" placeholder="Mã bán thành phẩm..."></td>
    <td class="cell-ten"><input class="input" data-f="ten" placeholder="Tên bán thành phẩm..."></td>
    <td class="cell-dl"><input class="input" data-f="dl" type="number" step="0.00001"></td>
    <td class="cell-tg"><input class="input" data-f="tg" type="number" step="0.01"></td>

    <td class="cell-tt"><input class="input" data-f="tt" type="number" value="${nextNo}" disabled></td>
    <td class="cell-cd">${buildCongDoanSelect("")}</td>

    <td>
      <div class="actions">
        <button class="icon" data-action="open-btp-lib" title="Chọn bán thành phẩm">
          <i class="bi bi-collection"></i>
        </button>
        <button class="icon primary" data-action="save-new-btp" title="Lưu">
          <i class="bi bi-check2"></i>
        </button>
        <button class="icon danger" data-action="cancel-new-btp" title="Hủy">
          <i class="bi bi-x-lg"></i>
        </button>
      </div>
    </td>
  `;

  insertAfter.insertAdjacentElement("afterend", tr);
  tr.scrollIntoView({ behavior: "smooth", block: "center" });

  // reset dòng hiện tại
  currentAddRow = { tpId, rowEl: tr, selectedBtp: null };
}

/* =========================================================
   BTP LIBRARY overlay
   ========================================================= */
async function loadBtpLib() {
  const q = document.getElementById("txtSearchBTP").value.trim();
  const list = await api(`${API_BASE}/btp/list${q ? `?q=${encodeURIComponent(q)}` : ""}`);

  BTP_LIB_CACHE = list;

  const tb = document.getElementById("tbBtpLib");
  tb.innerHTML = "";

  list.forEach(btp => {
    const tr = document.createElement("tr");
    tr.dataset.btpId = btp.dinh_muc_id;
    tr.innerHTML = `
      <td>${btp.ma_san_pham}</td>
      <td>${btp.ten_san_pham}</td>
      <td>${fmt(btp.dinh_luong)}</td>
      <td>${fmt(btp.dinh_muc_thoi_gian)}</td>
      <td>${congDoanName(btp.cong_doan)}</td>
      <td>
        <div class="actions">
          <button class="icon primary" data-action="pick-btp" title="Chọn BTP">
            <i class="bi bi-check2-circle"></i>
          </button>
          <button class="icon" data-action="edit-btp-lib" title="Cập nhật BTP">
            <i class="bi bi-pencil-square"></i>
          </button>
          <button class="icon danger" data-action="delete-btp-lib" title="Xóa BTP">
            <i class="bi bi-trash3"></i>
          </button>
        </div>
      </td>
    `;
    tb.appendChild(tr);
  });
}

document.getElementById("txtSearchBTP").addEventListener("input", () => {
  loadBtpLib().catch(() => {});
});

document.getElementById("btnReloadBTP").addEventListener("click", () => {
  loadBtpLib().catch(() => {});
});

/* =========================================================
   MAIN EVENTS
   ========================================================= */
document.getElementById("txtSearchTP").addEventListener("input", render);

document.getElementById("btnReload").addEventListener("click", async () => {
  try {
    await loadTree();
    toast("ok", "Tải lại", "Đã tải lại dữ liệu");
  } catch (e) {
    toast("err", "Lỗi", e.message);
  }
});

/* ---------------- Main table actions ---------------- */
document.getElementById("tbBody").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;

  const action = btn.dataset.action;
  const tpRow = e.target.closest("tr.row-tp");
  const btpRow = e.target.closest("tr.row-btp");

  try {
    if (action === "toggle" && tpRow) {
      toggleChildren(Number(tpRow.dataset.tpId));
      return;
    }

    if (action === "add-btp" && tpRow) {
      addInlineBTPRow(Number(tpRow.dataset.tpId));
      return;
    }

    if (action === "edit-tp" && tpRow) {
      const tpId = Number(tpRow.dataset.tpId);
      const tp = findProductInTree(tpId);
      if (!tp) { toast("err", "Lỗi", "Không tìm thấy TP"); return; }
      openEditProductModal(tp);
      return;
    }

    if (action === "edit-btp" && btpRow) {
      const btpId = Number(btpRow.dataset.btpId);
      const btp = findProductInTree(btpId);
      if (!btp) { toast("err", "Lỗi", "Không tìm thấy BTP"); return; }
      openEditProductModal(btp);
      return;
    }

    if (action === "cancel-new-btp" && btpRow && btpRow.dataset.newBtpOf) {
      btpRow.remove();
      currentAddRow = { tpId: null, rowEl: null, selectedBtp: null };
      return;
    }

    if (action === "open-btp-lib" && btpRow && btpRow.dataset.newBtpOf) {
      openModal("modalBtpLibrary");
      await loadBtpLib();
      return;
    }

    if (action === "save-new-btp" && btpRow && btpRow.dataset.newBtpOf) {
      const tpId = Number(btpRow.dataset.newBtpOf);
      const congdoan = btpRow.querySelector("select[data-f='congdoan']")?.value || null;

      // 1) nếu đã chọn từ thư viện => link
      if (currentAddRow.selectedBtp) {
        await api(`${API_BASE}/tp/${tpId}/btp/link`, {
          method: "POST",
          body: JSON.stringify({
            btp_id: currentAddRow.selectedBtp.dinh_muc_id,
            cong_doan_tp: null
          })
        });

        btpRow.remove();
        currentAddRow = { tpId: null, rowEl: null, selectedBtp: null };

        await loadTree();
        expandedSet.add(tpId);
        render();
        toggleChildren(tpId, true);

        toast("ok", "Thành công", "Đã map BTP vào TP");
        return;
      }

      // 2) tạo mới BTP + link
      const payload = {
        ma_san_pham: btpRow.querySelector("input[data-f='ma']").value.trim(),
        ten_san_pham: btpRow.querySelector("input[data-f='ten']").value.trim(),
        dinh_luong: btpRow.querySelector("input[data-f='dl']").value === "" ? null : Number(btpRow.querySelector("input[data-f='dl']").value),
        dinh_muc_thoi_gian: btpRow.querySelector("input[data-f='tg']").value === "" ? null : Number(btpRow.querySelector("input[data-f='tg']").value),
        cong_doan: congdoan
      };

      if (!payload.ma_san_pham || !payload.ten_san_pham) {
        toast("warn", "Thiếu dữ liệu", "Vui lòng nhập Mã và Tên BTP");
        return;
      }

      await api(`${API_BASE}/tp/${tpId}/btp/create`, {
        method: "POST",
        body: JSON.stringify(payload)
      });

      btpRow.remove();
      currentAddRow = { tpId: null, rowEl: null, selectedBtp: null };

      await loadTree();
      expandedSet.add(tpId);
      render();
      toggleChildren(tpId, true);

      toast("ok", "Thành công", "Đã thêm BTP cấu thành");
      return;
    }

    if (action === "delete-tp" && tpRow) {
      const tpId = Number(tpRow.dataset.tpId);
      const ok = await confirmDialog("Xóa thành phẩm", "Bạn có chắc muốn xóa thành phẩm này?");
      if (!ok) return;

      await api(`${API_BASE}/product/${tpId}`, { method: "DELETE" });
      await loadTree();
      toast("ok", "Đã xóa", "Đã xóa thành phẩm");
      return;
    }

    if (action === "unlink-btp" && btpRow && btpRow.dataset.parentTpId) {
      const tpId = Number(btpRow.dataset.parentTpId);
      const btpId = Number(btpRow.dataset.btpId);

      const ok = await confirmDialog("Gỡ bán thành phẩm", "Bạn có chắc muốn gỡ bán thành phẩm khỏi thành phẩm này?");
      if (!ok) return;

      await api(`${API_BASE}/tp/${tpId}/btp/${btpId}`, { method: "DELETE" });

      await loadTree();
      expandedSet.add(tpId);
      render();
      toggleChildren(tpId, true);

      toast("ok", "Thành công", "Đã gỡ bán thành phẩm");
      return;
    }

    if (action === "delete-btp" && btpRow) {
      const btpId = Number(btpRow.dataset.btpId);

      const ok = await confirmDialog("Xóa bán thành phẩm", "Bạn có chắc muốn xóa bán thành phẩm? (nếu đang dùng ở thành phẩm khác hệ thống sẽ chặn)");
      if (!ok) return;

      await api(`${API_BASE}/product/${btpId}`, { method: "DELETE" });
      await loadTree();
      toast("ok", "Đã xóa", "Đã xóa bán thành phẩm");
      return;
    }

  } catch (err) {
    toast("err", "Lỗi", err.message);
  }
});

/* =========================================================
   LIBRARY EVENTS (NEW RULES)
   ========================================================= */
document.getElementById("tbBtpLib").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;

  const tr = e.target.closest("tr");
  const btpId = Number(tr.dataset.btpId);

  try {
    // CHỌN BTP: đổ FULL dữ liệu vào dòng add BTP của TP
    if (btn.dataset.action === "pick-btp") {
      if (!currentAddRow.rowEl) {
        toast("warn", "Chưa có dòng thêm", "Hãy bấm “Thêm BTP” ở TP trước");
        return;
      }

      const btp = findProductInLib(btpId);
      if (!btp) { toast("err", "Lỗi", "Không tìm thấy BTP"); return; }

      currentAddRow.selectedBtp = btp;

      const row = currentAddRow.rowEl;

      // đổ FULL dữ liệu
      row.querySelector("input[data-f='ma']").value = btp.ma_san_pham || "";
      row.querySelector("input[data-f='ten']").value = btp.ten_san_pham || "";
      row.querySelector("input[data-f='dl']").value = (btp.dinh_luong ?? "") === null ? "" : (btp.dinh_luong ?? "");
      row.querySelector("input[data-f='tg']").value = (btp.dinh_muc_thoi_gian ?? "") === null ? "" : (btp.dinh_muc_thoi_gian ?? "");

      // set công đoạn
      const sel = row.querySelector("select[data-f='congdoan']");
      if (sel) sel.value = btp.cong_doan || "";

      // disable mã/tên để tránh nhầm "tạo mới"
      row.querySelector("input[data-f='ma']").disabled = true;
      row.querySelector("input[data-f='ten']").disabled = true;

      closeModal("modalBtpLibrary");
      toast("ok", "Đã chọn BTP", "Bấm nút Lưu để map BTP vào TP");
      return;
    }

    // CẬP NHẬT BTP trong thư viện:
    // => đóng thư viện, mở modal cập nhật BTP
    if (btn.dataset.action === "edit-btp-lib") {
      const btp = findProductInLib(btpId);
      if (!btp) { toast("err", "Lỗi", "Không tìm thấy BTP"); return; }

      closeModal("modalBtpLibrary");      // ✅ yêu cầu mới
      openEditProductModal(btp);          // ✅ mở modal sản phẩm (BTP)
      return;
    }

    // XÓA BTP trong thư viện
    if (btn.dataset.action === "delete-btp-lib") {
      const ok = await confirmDialog("Xóa BTP", "Xóa bán thành phẩm khỏi danh mục? (nếu đang dùng ở thành phẩm khác sẽ bị chặn)");
      if (!ok) return;

      await api(`${API_BASE}/product/${btpId}`, { method: "DELETE" });
      await loadBtpLib();
      await loadTree();
      toast("ok", "Đã xóa", "Đã xóa bán thành phẩm");
      return;
    }

  } catch (err) {
    toast("err", "Lỗi", err.message);
  }
});

/* Nút Thêm BTP trong thư viện:
   => đóng thư viện, mở modal tạo mới BTP */
document.getElementById("btnAddBTPInLib").addEventListener("click", () => {
  closeModal("modalBtpLibrary");     // ✅ yêu cầu mới
  openCreateProductModal("BTP");     // ✅ mở modal BTP
});

// Toggle hiển thị/gấp BTP cho TP
function toggleChildren(tpId, expand = null) {
  if (expand === true) {
    expandedSet.add(tpId);
  } else if (expand === false) {
    expandedSet.delete(tpId);
  } else {
    if (expandedSet.has(tpId)) expandedSet.delete(tpId);
    else expandedSet.add(tpId);
  }
  render();
}

/* =========================================================
   INIT
   ========================================================= */
(async function init() {
  try {
    await loadCongDoan();
    await loadTree();
    toast("ok", "Sẵn sàng", "Đã tải danh mục định mức");
  } catch (e) {
    toast("err", "Không tải được dữ liệu", e.message);
  }
})();
