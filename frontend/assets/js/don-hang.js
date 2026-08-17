/* =========================================================
   don-hang.js
   - Tree: Loại đơn hàng -> Đơn hàng -> Sản phẩm
   - CRUD đơn hàng + quản lý chi tiết sản phẩm
   ========================================================= */

const API_BASE = "/api/v1/don-hang";

let TREE = [];
let PRODUCT_CACHE = [];

// UI expand/collapse
let expandedLoai = new Set();
let expandedDonHang = new Set();

// modal state
const modalOrderState = {
  mode: "create", // create | update
  editId: null,
  soChungTuLocked: null,
  pickTargetRowIndex: null
};

/* ---------------- Utils ---------------- */
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

function fmtDate(d) {
  if (!d) return "";
  // backend returns YYYY-MM-DD
  return d;
}

function normalize(str) {
  return (str || "").toString().toLowerCase();
}

/* ---------------- Tree load/render ---------------- */
async function loadTree() {
  TREE = await api(`${API_BASE}/tree`);
  // auto expand all loại lần đầu nếu chưa có snapshot
  if (expandedLoai.size === 0) {
    (TREE || []).forEach(l => expandedLoai.add(l.key));
  }
  render();
}

function filterTree(keyword) {
  const kw = normalize(keyword).trim();
  if (!kw) return TREE;

  // filter loosely: if loai label match, show all
  const out = [];
  for (const loai of (TREE || [])) {
    const loaiMatch = normalize(loai.label).includes(kw);

    const loaiClone = { ...loai, children: [] };
    for (const dh of (loai.children || [])) {
      const dhMatch = normalize(dh.so_chung_tu).includes(kw) || normalize(dh.khach_hang).includes(kw);
      const dhClone = { ...dh, children: [] };

      for (const sp of (dh.children || [])) {
        const spMatch = normalize(sp.ma_san_pham).includes(kw) || normalize(sp.ten_san_pham).includes(kw);
        if (loaiMatch || dhMatch || spMatch) dhClone.children.push(sp);
      }

      if (loaiMatch || dhMatch || dhClone.children.length > 0) loaiClone.children.push(dhClone);
    }

    if (loaiMatch || loaiClone.children.length > 0) out.push(loaiClone);
  }
  return out;
}

function render() {
  const tb = document.getElementById("tbBody");
  tb.innerHTML = "";

  const list = filterTree(document.getElementById("txtSearch").value);

  // Flatten: lấy tất cả đơn hàng từ mọi nhóm loại, hiển thị trực tiếp (không có dòng cha Loại)
  const donHangs = [];
  for (const loai of (list || [])) {
    for (const dh of (loai?.children || [])) {
      donHangs.push(dh);
    }
  }

  let stt = 1;
  for (const dh of donHangs) {
    tb.appendChild(buildDonHangRow(null, dh, stt));
    const dhExpanded = expandedDonHang.has(dh.key);

    if (dhExpanded) {
      for (const sp of (dh.children || [])) {
        tb.appendChild(buildSanPhamRow(null, dh, sp));
      }
    }

    stt++;
  }
}

function buildLoaiRow(loai) {
  const tr = document.createElement("tr");
  tr.className = "row-loai";

  const expanded = expandedLoai.has(loai.key);
  const chevron = expanded ? "bi-chevron-up" : "bi-chevron-down";

  // Cột: [toggle][STT][SoChungTu][KhachHang][Ngay][Ma][Ten][SL][ThaoTac]
  tr.innerHTML = `
    <td>
      <button class="tree-toggle" title="Mở/đóng" data-act="toggle-loai" data-key="${loai.key}">
        <i class="bi ${chevron}"></i>
      </button>
    </td>
    <td class="cell-muted"></td>
    <td colspan="6">${loai.label || "(Chưa phân loại)"}</td>
    <td></td>
  `;
  return tr;
}

function buildDonHangRow(loai, dh, stt) {
  const tr = document.createElement("tr");
  tr.className = "row-dh";

  const hasChildren = (dh.children || []).length > 0;
  const expanded = expandedDonHang.has(dh.key);
  const chevron = expanded ? "bi-chevron-up" : "bi-chevron-down";

  const toggleHtml = `
    <button class="tree-toggle" title="Mở/đóng" data-act="toggle-dh" data-key="${dh.key}" ${hasChildren ? "" : "disabled"}>
      <i class="bi ${chevron}"></i>
    </button>
  `;

  tr.innerHTML = `
    <td>${toggleHtml}</td>
    <td class="cell-muted">${stt}</td>
    <td class="nowrap">${dh.so_chung_tu ?? ""}</td>
    <td>${dh.khach_hang ?? ""}</td>
    <td class="nowrap">${fmtDate(dh.ngay_giao_hang)}</td>
    <td class="cell-muted"></td>
    <td class="cell-muted"></td>
    <td class="cell-muted"></td>
    <td class="nowrap">${dh.tinh_trang_don_hang ?? "Chưa lập kế hoạch"}</td>
    <td>
      <button class="icon-btn" title="Sửa" data-act="edit" data-id="${dh.don_hang_id}"><i class="bi bi-pencil-square"></i></button>
      <button class="icon-btn" title="Xóa" data-act="delete" data-id="${dh.don_hang_id}"><i class="bi bi-trash"></i></button>
    </td>
  `;
  return tr;
}

function buildSanPhamRow(loai, dh, sp) {
  const tr = document.createElement("tr");
  tr.className = "row-sp";

  tr.innerHTML = `
    <td></td>
    <td class="cell-muted"></td>
    <td class="cell-muted"></td>
    <td class="cell-muted"></td>
    <td class="cell-muted"></td>
    <td class="nowrap">${sp.ma_san_pham ?? ""}</td>
    <td>${sp.ten_san_pham ?? ""}</td>
    <td class="left">${sp.so_luong_dat_hang ?? ""}</td>
    <td class="cell-muted"></td>
    <td></td>
  `;
  return tr;
}

/* ---------------- Modal Order ---------------- */
function resetOrderForm() {
  document.getElementById("fLoai").value = "";
  document.getElementById("fSoChungTu").value = "";
  document.getElementById("fKhachHang").value = "";
  document.getElementById("fNgayGiao").value = "";
  document.getElementById("tbDetails").innerHTML = "";
}

function openCreateOrder() {
  modalOrderState.mode = "create";
  modalOrderState.editId = null;
  modalOrderState.soChungTuLocked = null;
  document.getElementById("modalOrderTitle").textContent = "Thêm đơn hàng";

  resetOrderForm();
  document.getElementById("fSoChungTu").disabled = false;
  openModal("modalOrder");

  // add one empty row default
  addDetailRow();
}

function findDonHangInTree(donHangId) {
  // TREE vẫn là cấu trúc Loại -> Đơn hàng -> Sản phẩm; duyệt toàn bộ để tìm
  for (const loai of (TREE || [])) {
    for (const dh of (loai.children || [])) {
      if (Number(dh.don_hang_id) === Number(donHangId)) return dh;
    }
  }
  return null;
}

function openEditOrder(donHangId) {
  const dh = findDonHangInTree(donHangId);
  if (!dh) {
    toast("err", "Không tìm thấy", "Không tìm thấy đơn hàng trong cây. Hãy tải lại.");
    return;
  }

  modalOrderState.mode = "update";
  modalOrderState.editId = Number(dh.don_hang_id);
  modalOrderState.soChungTuLocked = dh.so_chung_tu;
  document.getElementById("modalOrderTitle").textContent = "Cập nhật đơn hàng";

  resetOrderForm();

  // Loại đơn hàng: chỉ cho phép 2 giá trị theo UI
  const loaiEl = document.getElementById("fLoai");
  const loai = (dh.loai_don_hang ?? "").toString();
  const allowed = ["Đơn hàng mới", "Đơn phát sinh"];
  loaiEl.value = allowed.includes(loai) ? loai : "";

  document.getElementById("fSoChungTu").value = dh.so_chung_tu ?? "";
  document.getElementById("fKhachHang").value = dh.khach_hang ?? "";
  document.getElementById("fNgayGiao").value = dh.ngay_giao_hang ?? "";

  // So chứng từ không đổi
  document.getElementById("fSoChungTu").disabled = true;

  // details
  const details = dh.children || [];
  if (details.length === 0) addDetailRow();
  else details.forEach((x) => addDetailRow({
    dinh_muc_id: x.dinh_muc_id,
    ma_san_pham: x.ma_san_pham,
    ten_san_pham: x.ten_san_pham,
    so_luong_dat_hang: x.so_luong_dat_hang,
    ngay_giao_hang: x.ngay_giao_hang
  }));

  openModal("modalOrder");
}

function renumberDetails() {
  const rows = Array.from(document.querySelectorAll("#tbDetails tr"));
  rows.forEach((tr, idx) => {
    const sttCell = tr.querySelector("td[data-col='stt']");
    if (sttCell) sttCell.textContent = String(idx + 1);
  });
}

function addDetailRow(detail = null) {
  const tb = document.getElementById("tbDetails");
  const tr = document.createElement("tr");

  const ma = detail?.ma_san_pham ?? "";
  const ten = detail?.ten_san_pham ?? "";
  const dinhMucId = detail?.dinh_muc_id ?? "";
  const sl = (detail?.so_luong_dat_hang ?? "") === null ? "" : (detail?.so_luong_dat_hang ?? "");

  // default theo ngày giao hàng của đơn nếu không có
  const orderNgay = document.getElementById("fNgayGiao")?.value || "";
  const ngayCt = detail?.ngay_giao_hang ?? orderNgay;

  tr.innerHTML = `
    <td data-col="stt" class="cell-muted"></td>
    <td>
      <div style="display:flex;gap:8px;align-items:center;">
        <input class="inp" data-f="ma" value="${ma}" placeholder="Chọn sản phẩm..." readonly style="width:100%;border:1px solid var(--border);border-radius:10px;padding:9px 10px;" />
        <button class="btn ghost" type="button" data-act="pick-product" style="padding:9px 10px;">Chọn</button>
      </div>
      <input type="hidden" data-f="dinh_muc_id" value="${dinhMucId}" />
    </td>
    <td>
      <input class="inp" data-f="ten" value="${ten}" readonly style="width:100%;border:1px solid var(--border);border-radius:10px;padding:9px 10px;" />
    </td>
    <td>
      <input class="inp" data-f="sl" type="number" min="0" step="1" value="${sl}" style="width:100%;border:1px solid var(--border);border-radius:10px;padding:9px 10px;" />
    </td>
    <td>
      <input class="inp" data-f="ngay" type="date" value="${ngayCt || ""}" style="width:100%;border:1px solid var(--border);border-radius:10px;padding:9px 10px;" />
    </td>
    <td>
      <button class="icon-btn" title="Xóa dòng" data-act="remove-row"><i class="bi bi-trash"></i></button>
    </td>
  `;

  tb.appendChild(tr);
  renumberDetails();
}

function collectOrderPayload() {
  const loai = document.getElementById("fLoai").value || null;
  const so = document.getElementById("fSoChungTu").value.trim();
  const kh = document.getElementById("fKhachHang").value.trim() || null;
  const ngay = document.getElementById("fNgayGiao").value || null;

  const rows = Array.from(document.querySelectorAll("#tbDetails tr"));
  const chi_tiets = [];

  for (const tr of rows) {
    const dinhMucId = tr.querySelector("input[data-f='dinh_muc_id']")?.value;
    const slRaw = tr.querySelector("input[data-f='sl']")?.value;
    const ngayCt = tr.querySelector("input[data-f='ngay']")?.value;

    if (!dinhMucId) continue;

    chi_tiets.push({
      dinh_muc_id: Number(dinhMucId),
      so_luong_dat_hang: slRaw === "" ? null : Number(slRaw),
      ngay_giao_hang: ngayCt ? ngayCt : null
    });
  }

  return {
    loai_don_hang: loai,
    so_chung_tu: so,
    khach_hang: kh,
    ngay_giao_hang: ngay,
    chi_tiets
  };
}

// Khi thay đổi ngày giao hàng của đơn: auto fill vào các dòng chi tiết đang trống
(function bindOrderNgayChangeOnce(){
  const el = document.getElementById("fNgayGiao");
  if (!el || el.dataset.boundNgayCt === "1") return;
  el.dataset.boundNgayCt = "1";
  el.addEventListener("change", () => {
    const orderNgay = el.value || "";
    if (!orderNgay) return;
    const rows = Array.from(document.querySelectorAll("#tbDetails tr"));
    for (const tr of rows) {
      const inp = tr.querySelector("input[data-f='ngay']");
      if (inp && !inp.value) inp.value = orderNgay;
    }
  });
})();

async function saveOrder() {
  const payload = collectOrderPayload();
  if (!payload.so_chung_tu) {
    toast("warn", "Thiếu dữ liệu", "Vui lòng nhập Số chứng từ");
    return;
  }
  if (payload.chi_tiets.length === 0) {
    toast("warn", "Thiếu dữ liệu", "Vui lòng chọn ít nhất 1 sản phẩm");
    return;
  }

  if (modalOrderState.mode === "create") {
    await api(`${API_BASE}/order`, { method: "POST", body: JSON.stringify(payload) });
    closeModal("modalOrder");
    await loadTree();
    toast("ok", "Thành công", "Đã thêm đơn hàng");
    return;
  }

  await api(`${API_BASE}/order/${modalOrderState.editId}`, { method: "PUT", body: JSON.stringify({
    loai_don_hang: payload.loai_don_hang,
    khach_hang: payload.khach_hang,
    ngay_giao_hang: payload.ngay_giao_hang,
    chi_tiets: payload.chi_tiets
  }) });

  closeModal("modalOrder");
  await loadTree();
  toast("ok", "Thành công", "Đã cập nhật đơn hàng");
}

/* ---------------- Product library ---------------- */
async function loadProductLib(keyword = null) {
  const q = keyword ? `?q=${encodeURIComponent(keyword)}` : "";
  PRODUCT_CACHE = await api(`${API_BASE}/product/list${q}`);
  renderProductLib();
}

function renderProductLib() {
  const tb = document.getElementById("tbProductLib");
  tb.innerHTML = "";

  for (const p of (PRODUCT_CACHE || [])) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="nowrap">${p.ma_san_pham ?? ""}</td>
      <td>${p.ten_san_pham ?? ""}</td>
      <td>
        <button class="btn primary" type="button" data-act="choose-product" data-id="${p.dinh_muc_id}">Chọn</button>
      </td>
    `;
    tb.appendChild(tr);
  }
}

function openProductLib(targetRowIndex) {
  modalOrderState.pickTargetRowIndex = targetRowIndex;
  document.getElementById("txtSearchProduct").value = "";
  openModal("modalProductLib");
  loadProductLib().catch(e => toast("err", "Không thể tải", e.message));
}

function chooseProduct(dinhMucId) {
  const p = (PRODUCT_CACHE || []).find(x => Number(x.dinh_muc_id) === Number(dinhMucId));
  if (!p) return;

  const rows = Array.from(document.querySelectorAll("#tbDetails tr"));
  const idx = modalOrderState.pickTargetRowIndex ?? 0;
  const tr = rows[idx];
  if (!tr) return;

  tr.querySelector("input[data-f='dinh_muc_id']").value = String(p.dinh_muc_id);
  tr.querySelector("input[data-f='ma']").value = p.ma_san_pham ?? "";
  tr.querySelector("input[data-f='ten']").value = p.ten_san_pham ?? "";

  closeModal("modalProductLib");
}

/* ---------------- Events ---------------- */
document.getElementById("btnReload").addEventListener("click", () => loadTree().catch(e => toast("err", "Lỗi", e.message)));
document.getElementById("btnAdd").addEventListener("click", openCreateOrder);
document.getElementById("btnSaveOrder").addEventListener("click", () => saveOrder().catch(e => toast("err", "Không thể lưu", e.message)));
document.getElementById("btnAddProductRow").addEventListener("click", () => addDetailRow());

document.getElementById("txtSearch").addEventListener("input", () => render());

// product lib search/reload
const productSearch = document.getElementById("txtSearchProduct");
let productSearchTimer = null;
productSearch.addEventListener("input", () => {
  clearTimeout(productSearchTimer);
  productSearchTimer = setTimeout(() => {
    loadProductLib(productSearch.value.trim() || null)
      .catch(e => toast("err", "Không thể tải", e.message));
  }, 250);
});

document.getElementById("btnReloadProduct").addEventListener("click", () => {
  loadProductLib(productSearch.value.trim() || null)
    .catch(e => toast("err", "Không thể tải", e.message));
});

// Delegate actions on main table (expand/edit/delete)
document.getElementById("tbBody").addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;
  const act = el.dataset.act;

  if (act === "toggle-loai") {
    const key = el.dataset.key;
    if (expandedLoai.has(key)) expandedLoai.delete(key);
    else expandedLoai.add(key);
    render();
    return;
  }

  if (act === "toggle-dh") {
    const key = el.dataset.key;
    if (expandedDonHang.has(key)) expandedDonHang.delete(key);
    else expandedDonHang.add(key);
    render();
    return;
  }

  if (act === "edit") {
    openEditOrder(Number(el.dataset.id));
    return;
  }

  if (act === "delete") {
    const id = Number(el.dataset.id);
    const ok = await confirmDialog("Xóa đơn hàng", "Bạn có chắc chắn muốn xóa đơn hàng này?");
    if (!ok) return;
    try {
      await api(`${API_BASE}/order/${id}`, { method: "DELETE" });
      await loadTree();
      toast("ok", "Thành công", "Đã xóa đơn hàng");
    } catch (e) {
      toast("err", "Không thể xóa", e.message);
    }
  }
});

// details row actions

document.getElementById("tbDetails").addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;

  const tr = el.closest("tr");
  const rows = Array.from(document.querySelectorAll("#tbDetails tr"));
  const idx = rows.indexOf(tr);

  if (el.dataset.act === "remove-row") {
    tr.remove();
    renumberDetails();
    return;
  }

  if (el.dataset.act === "pick-product") {
    openProductLib(idx);
    return;
  }
});

// choose product

document.getElementById("tbProductLib").addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-act='choose-product']");
  if (!el) return;
  chooseProduct(Number(el.dataset.id));
});

/* ---------------- Boot ---------------- */
loadTree().catch(e => toast("err", "Lỗi", e.message));
