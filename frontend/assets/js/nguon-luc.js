// API endpoints
const API_BASE = '/api/v1/nguon-luc';

// Chuẩn hoá đọc lỗi backend
async function parseErrorDetail(res) {
  try {
    const j = await res.json();
    return j?.detail || j?.message || '';
  } catch {
    return '';
  }
}

async function assertOk(res) {
  if (res.ok) return;
  const detail = await parseErrorDetail(res);
  const err = new Error(detail || `HTTP_${res.status}`);
  err.status = res.status;
  throw err;
}

async function fetchTreeData(search = '') {
  const res = await fetch(`${API_BASE}/tree?search=${encodeURIComponent(search)}`);
  if (!res.ok) return [];
  return await res.json();
}

async function addCongDoanLon(data) {
  const res = await fetch(`${API_BASE}/congdoanlon`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  });
  await assertOk(res);
  return await res.json();
}

async function addCongDoan(data) {
  const res = await fetch(`${API_BASE}/congdoan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  });
  await assertOk(res);
  return await res.json();
}

// Attach/Detach Công đoạn - Bộ phận (Công đoạn lớn)
async function attachCongDoanToCongDoanLon(maCongDoan, maCongDoanLon) {
  const res = await fetch(`${API_BASE}/congdoan/attach`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ MaCongDoan: maCongDoan, MaCongDoanLon: maCongDoanLon })
  });
  await assertOk(res);
  return await res.json();
}

async function detachCongDoanFromCongDoanLon(maCongDoan) {
  const res = await fetch(`${API_BASE}/congdoan/detach/${encodeURIComponent(maCongDoan)}`, {
    method: 'DELETE'
  });
  await assertOk(res);
  return await res.json().catch(() => ({}));
}

async function addNguonLuc(data) {
  const res = await fetch(`${API_BASE}/nguonluc`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  });
  await assertOk(res);
  return await res.json();
}

// Tạo mapping Công đoạn - Nguồn lực (attach nguồn lực vào công đoạn)
async function addMappingCongDoanNguonLuc(maCongDoan, maNguonLuc) {
  const res = await fetch(`${API_BASE}/mapping`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ MaCongDoan: maCongDoan, MaNguonLuc: maNguonLuc })
  });

  await assertOk(res);
  return await res.json();
}

// Gỡ mapping Công đoạn - Nguồn lực (unlink)
async function deleteMappingCongDoanNguonLuc(maCongDoan, maNguonLuc) {
  const res = await fetch(`${API_BASE}/mapping/${encodeURIComponent(maCongDoan)}/${encodeURIComponent(maNguonLuc)}`, {
    method: 'DELETE'
  });

  await assertOk(res);
  return await res.json().catch(() => ({}));
}

function findParentCongDoanOfNguonLuc(maNguonLuc) {
  for (const bp of DATA) {
    for (const cd of (bp.children || [])) {
      for (const nl of (cd.children || [])) {
        if (nl.ma === maNguonLuc) return cd.ma;
      }
    }
  }
  return null;
}

function findCongDoanLonOfCongDoan(maCongDoan) {
  for (const bp of DATA) {
    for (const cd of (bp.children || [])) {
      if (cd.ma === maCongDoan) return bp.ma;
    }
  }
  return null;
}

async function editItem(loai, ma, data) {
  // Map logical type -> API path
  const path = (loai === 'bophan') ? 'congdoanlon'
    : (loai === 'congdoanlon') ? 'congdoanlon'
      : (loai === 'congdoan') ? 'congdoan'
        : (loai === 'nguonluc') ? 'nguonluc'
          : loai;

  const res = await fetch(`${API_BASE}/${path}/${encodeURIComponent(ma)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  });
  await assertOk(res);
  return await res.json();
}

async function deleteItem(loai, ma) {
  const path = (loai === 'bophan') ? 'congdoanlon'
    : (loai === 'congdoanlon') ? 'congdoanlon'
      : (loai === 'congdoan') ? 'congdoan'
        : (loai === 'nguonluc') ? 'nguonluc'
          : loai;

  const res = await fetch(`${API_BASE}/${path}/${encodeURIComponent(ma)}`, {
    method: 'DELETE'
  });
  await assertOk(res);
  return await res.json().catch(() => ({}));
}

// API helpers for get-one
async function getNguonLucByMa(ma) {
  const res = await fetch(`${API_BASE}/nguonluc/${encodeURIComponent(ma)}`);
  await assertOk(res);
  return await res.json();
}

// ===== Modal cập nhật (Edit) =====
let editing = { loai: null, ma: null, parentMa: null };

function openEditBoPhanModal(item) {
  editing = { loai: 'congdoanlon', ma: item.ma, parentMa: null };
  document.getElementById('eMaBoPhan').value = item.ma || '';
  document.getElementById('eTenBoPhan').value = item.ten || '';
  document.getElementById('eNhanSuBoPhan').value = item.nhan_su ?? '';
  openModal('modalEditBoPhan');
}

function openEditCongDoanModal(item) {
  // parentMa là mã bộ phận (công đoạn lớn)
  const bp = DATA.find(x => (x.children || []).some(cd => cd.ma === item.ma));
  editing = { loai: 'congdoan', ma: item.ma, parentMa: bp?.ma || null };
  document.getElementById('eMaCongDoan').value = item.ma || '';
  document.getElementById('eTenCongDoan').value = item.ten || '';
  document.getElementById('eNhanSuCongDoan').value = item.nhan_su ?? '';
  openModal('modalEditCongDoan');
}

function openEditNguonLucModal(item) {
  // parentMa là mã công đoạn
  const maCongDoan = findParentCongDoanOfNguonLuc(item.ma);
  editing = { loai: 'nguonluc', ma: item.ma, parentMa: maCongDoan || null };
  document.getElementById('eMaNguonLuc').value = item.ma || '';
  document.getElementById('eTenNguonLuc').value = item.ten || '';
  document.getElementById('eNhanSuPhuThuoc').value = item.nhan_su_phu_thuoc ?? '';
  document.getElementById('eThoiGianThietLap').value = item.thoi_gian_thiet_lap ?? '';
  openModal('modalEditNguonLuc');
}

// Save handlers
if (document.getElementById('btnSaveEditBoPhan')) {
  document.getElementById('btnSaveEditBoPhan').onclick = async () => {
    const ma = document.getElementById('eMaBoPhan').value.trim();
    const ten = document.getElementById('eTenBoPhan').value.trim();
    const soNhanSu = +document.getElementById('eNhanSuBoPhan').value;
    if (!ten) {
      showToast('error', 'Thiếu thông tin', 'Vui lòng nhập tên bộ phận!');
      return;
    }
    try {
      await editItem('congdoanlon', ma, { MaCongDoanLon: ma, TenCongDoanLon: ten, SoNhanSu: soNhanSu });
      closeModal('modalEditBoPhan');
      showToast('success', 'Thành công', 'Đã cập nhật bộ phận!');
      await render();
    } catch (e) {
      showToast('error', 'Lỗi', e?.message || 'Không thể cập nhật bộ phận!');
    }
  };
}

if (document.getElementById('btnSaveEditCongDoan')) {
  document.getElementById('btnSaveEditCongDoan').onclick = async () => {
    const ma = document.getElementById('eMaCongDoan').value.trim();
    const ten = document.getElementById('eTenCongDoan').value.trim();
    if (!ten) {
      showToast('error', 'Thiếu thông tin', 'Vui lòng nhập tên công đoạn!');
      return;
    }
    try {
      // Backend mới: CongDoanSchema: {MaCongDoan, TenCongDoan}
      await editItem('congdoan', ma, { MaCongDoan: ma, TenCongDoan: ten });
      closeModal('modalEditCongDoan');
      showToast('success', 'Thành công', 'Đã cập nhật công đoạn!');
      await render();
    } catch (e) {
      showToast('error', 'Lỗi', e?.message || 'Không thể cập nhật công đoạn!');
    }
  };
}

if (document.getElementById('btnSaveEditNguonLuc')) {
  document.getElementById('btnSaveEditNguonLuc').onclick = async () => {
    const ma = document.getElementById('eMaNguonLuc').value.trim();
    const ten = document.getElementById('eTenNguonLuc').value.trim();
    const ns = Number(document.getElementById('eNhanSuPhuThuoc').value);
    const tg = Number(document.getElementById('eThoiGianThietLap').value);
    if (!ten) {
      showToast('error', 'Thiếu thông tin', 'Vui lòng nhập tên nguồn lực!');
      return;
    }
    try {
      // Backend schema NguonLucSchema: {MaNguonLuc, TenNguonLuc, NhanSuPhanBo, ThoiGianThietLap}
      await editItem('nguonluc', ma, {
        MaNguonLuc: ma,
        TenNguonLuc: ten,
        NhanSuPhanBo: Number.isFinite(ns) ? ns : null,
        ThoiGianThietLap: Number.isFinite(tg) ? tg : null
      });
      closeModal('modalEditNguonLuc');
      showToast('success', 'Thành công', 'Đã cập nhật nguồn lực!');
      await render();
    } catch (e) {
      showToast('error', 'Lỗi', e?.message || 'Không thể cập nhật nguồn lực!');
    }
  };
}

let DATA = [];
let expandedSet = new Set();
let firstLoad = true;

// Trạng thái hàng đang thêm mới (ma cha -> true)
let addingRow = { bophan: null, congdoan: null };

// Nếu nguồn lực được chọn từ thư viện thì khi lưu chỉ link mapping, không tạo mới DM_NguonLuc
// Lưu cả object để xác định chính xác mã được pick.
let addRowNguonLucPickedFromLib = null;
let addRowNguonLucPickedMa = '';

// Khi kế thừa công đoạn từ thư viện, cần biết công đoạn đó đã tồn tại để chỉ attach mapping
let addRowCongDoanPickedFromLib = null;
let addRowCongDoanPickedMa = '';

// Auto-code helpers for inline add-row
async function autoFillAddCodeFor(type) {
  if (type === 'congdoan') {
    const el = document.getElementById('addMaCongDoan');
    if (!el) return;
    el.value = await getNextCode('congdoan');
    el.disabled = true;
  }
  if (type === 'nguonluc') {
    const el = document.getElementById('addMaNguonLuc');
    if (!el) return;
    el.value = await getNextCode('nguonluc');
    el.disabled = true;
  }
}

async function render() {
  const tb = document.getElementById('tbBody');
  tb.innerHTML = '';
  let stt = 1;
  const search = document.getElementById('txtSearch').value || '';
  DATA = await fetchTreeData(search);

  // Khi re-render mà không còn add-row nguồn lực thì reset trạng thái picked
  if (!addingRow.congdoan) {
    addRowNguonLucPickedFromLib = null;
  }
  // Khi re-render mà không còn add-row công đoạn thì reset trạng thái picked
  if (!addingRow.bophan) {
    addRowCongDoanPickedFromLib = null;
    addRowCongDoanPickedMa = '';
  }

  if (firstLoad) {
    if (DATA && DATA.length > 0) {
      showToast('success', 'Sẵn sàng', 'Đã tải định mức nguồn lực');
    } else {
      showToast('error', 'Không có dữ liệu', 'Không tìm thấy nguồn lực nào!');
    }
    firstLoad = false;
  }
  DATA.forEach(bp => {
    tb.appendChild(buildRow(bp, 0, stt++));

    // Render Công đoạn trước
    if (expandedSet.has(bp.ma) && bp.children) {
      const cds = bp.children || [];
      const lastIndex = Math.max(0, cds.length - 1);

      cds.forEach((cd, idx) => {
        const isLastCongDoanInBoPhan = (idx === lastIndex);
        tb.appendChild(buildRow(cd, 1, undefined, { isLastCongDoanInBoPhan }));

        // Render Nguồn lực trước
        if (expandedSet.has(cd.ma) && cd.children) {
          const nls = cd.children || [];
          const lastNlIndex = Math.max(0, nls.length - 1);

          nls.forEach((nl, nlIdx) => {
            const isLastNguonLucInCongDoan = (nlIdx === lastNlIndex);
            tb.appendChild(buildRow(nl, 2, undefined, { isLastNguonLucInCongDoan }));
          });
        }

        // Sau cùng của nhóm Nguồn lực mới chèn add-row
        if (addingRow.congdoan === cd.ma) {
          tb.appendChild(buildAddRow('nguonluc', cd.ma));
        }
      });
    }

    // Sau cùng của nhóm Công đoạn mới chèn add-row
    if (addingRow.bophan === bp.ma) {
      tb.appendChild(buildAddRow('congdoan', bp.ma));
    }
  });
}

function buildRow(item, level, stt, opts = {}) {
  const isBophan = item.loai === 'Công đoạn lớn' || item.loai === 'Bộ phận';
  const isCongDoan = item.loai === 'Công đoạn';
  const isNguonLuc = item.loai === 'Nguồn lực';

  // Icon STT cho hàng Công đoạn (dựa theo vị trí cuối trong Bộ phận)
  // Ưu tiên dùng đúng path theo yêu cầu; fallback sang file đang tồn tại (dấu '-') để tránh 404.
  let sttCellHtml = `${stt || ''}`;
  if (isCongDoan) {
    const isLast = !!opts?.isLastCongDoanInBoPhan;

    const iconPreferred = isLast
      ? '../assets/icons/arrow-right_end_cd.png'
      : '../assets/icons/arrow-right_con_cd.png';

    sttCellHtml = `
      <span class="cd-stt" style="display:inline-flex;align-items:center;gap:6px;">
        <img src="${iconPreferred}" alt="" style="width:18px;height:18px;object-fit:contain;" />
        <span>${stt || ''}</span>
      </span>
    `;
  }

  // Icon STT cho hàng Nguồn lực (dựa theo vị trí cuối trong Công đoạn)
  if (isNguonLuc) {
    const isLast = !!opts?.isLastNguonLucInCongDoan;

    const iconPreferred = isLast
      ? '../assets/icons/arrow-right_end_nl.png'
      : '../assets/icons/arrow-right_con_nl.png';

    // (Icon NL hiện có đúng tên trong workspace, không cần fallback trừ khi user đổi tên sau này)
    sttCellHtml = `
      <span class="nl-stt" style="display:inline-flex;align-items:center;gap:6px;">
        <img src="${iconPreferred}" alt="" style="margin-left:6px;width:14px;height:14px;object-fit:contain;" />
        <span>${stt || ''}</span>
      </span>
    `;
  }

  let expander = '';
  const hasChildren = Array.isArray(item.children) && item.children.length > 0;
  if (isBophan && hasChildren) {
    expander = `<button class="expander" data-ma="${item.ma}" data-type="congdoanlon"><i class="bi ${expandedSet.has(item.ma) ? 'bi-chevron-up' : 'bi-chevron-down'}"></i></button>`;
  } else if (isCongDoan && hasChildren) {
    expander = `<button class="expander" data-ma="${item.ma}" data-type="congdoan"><i class="bi ${expandedSet.has(item.ma) ? 'bi-chevron-up' : 'bi-chevron-down'}"></i></button>`;
  }

  // Nút thao tác cho từng loại
  let actions = '';
  if (isBophan) {
    actions = `
      <button class="icon primary" data-action="add-congdoan" data-ma="${item.ma}"><i class="bi bi-plus-circle"></i></button>
      <button class="icon" data-action="edit" data-ma="${item.ma}"><i class="bi bi-pencil-square"></i></button>
      <button class="icon danger" data-action="delete" data-ma="${item.ma}"><i class="bi bi-trash3"></i></button>
    `;
  } else if (isCongDoan) {
    actions = `
      <button class="icon primary" data-action="add-nguonluc" data-ma="${item.ma}"><i class="bi bi-plus-circle"></i></button>
      <button class="icon" data-action="edit" data-ma="${item.ma}"><i class="bi bi-pencil-square"></i></button>
      <button class="icon danger" data-action="delete" data-ma="${item.ma}"><i class="bi bi-trash3"></i></button>
    `;
  } else if (isNguonLuc) {
    actions = `
      <button class="icon" data-action="unlink" data-ma="${item.ma}"><i class="bi bi-link-45deg"></i></button>
      <button class="icon" data-action="edit" data-ma="${item.ma}"><i class="bi bi-pencil-square"></i></button>
      <button class="icon danger" data-action="delete" data-ma="${item.ma}"><i class="bi bi-trash3"></i></button>
    `;
  }
  return htmlToElement(`
    <tr class="row-${isBophan ? 'bophan' : isCongDoan ? 'congdoan' : 'nguonluc'}">
      <td data-label="">${expander}</td>
      <td data-label="STT">${sttCellHtml}</td>
      <td data-label="Loại">${isBophan ? 'Bộ phận' : isCongDoan ? 'Công đoạn' : 'Nguồn lực'}</td>
      <td data-label="Mã">${item.ma}</td>
      <td data-label="Tên">${item.ten}</td>
      <td data-label="Nhân sự">${item.nhan_su ?? ''}</td>
      <td data-label="Nhân sự phụ thuộc">${item.nhan_su_phu_thuoc ?? ''}</td>
      <td data-label="Thời gian thiết lập">${item.thoi_gian_thiet_lap ?? ''}</td>
      <td data-label="Thao tác"><div class="actions">${actions}</div></td>
    </tr>
  `);
}

// Chèn hàng nhập liệu inline cho thêm mới công đoạn/nguồn lực
function buildAddRow(type, parentMa) {
  if (type === 'congdoan') {
    // Xác định add-row công đoạn có phải là "cuối" trong bộ phận không
    const bp = (DATA || []).find(x => x.ma === parentMa);
    const cds = bp?.children || [];
    const isLastCongDoanInBoPhan = (cds.length === 0);

    const cdIcon = isLastCongDoanInBoPhan
      ? '../assets/icons/arrow-right_end_cd.png'
      : '../assets/icons/arrow-right_con_cd.png';

    return htmlToElement(`
      <tr class="row-congdoan add-row">
        <td data-label=""></td>
        <td data-label="STT">
          <span class="cd-stt" style="display:inline-flex;align-items:center;gap:6px;">
            <img src="${cdIcon}" alt="" style="width:18px;height:18px;object-fit:contain;" />
            <span></span>
          </span>
        </td>
        <td data-label="Loại">Công đoạn</td>
        <td data-label="Mã"><input id="addMaCongDoan" style="width:80px" disabled></td>
        <td data-label="Tên"><input id="addTenCongDoan" style="width:120px" placeholder="Tên công đoạn..."></td>
        <td data-label="Nhân sự"><input id="addNhanSuCongDoan" type="number" style="width:60px"></td>
        <td data-label="Nhân sự phụ thuộc"></td>
        <td data-label="Thời gian thiết lập"></td>
        <td data-label="Thao tác"><div class="actions">
            <button class="icon" data-action="inherit-congdoan" title="Kế thừa">
              <i class="bi bi-collection"></i>
            </button>
            <button class="icon primary" data-action="confirm-add-congdoan" title="Lưu">
              <i class="bi bi-check2"></i>
            </button>
            <button class="icon danger" data-action="cancel-add-congdoan" title="Hủy">
              <i class="bi bi-x-lg"></i>
            </button>
          </div>
        </td>
      </tr>
    `);
  } else if (type === 'nguonluc') {
    // Xác định add-row nguồn lực có phải là "cuối" trong công đoạn không
    const cd = (() => {
      for (const bp of (DATA || [])) {
        const found = (bp.children || []).find(x => x.ma === parentMa);
        if (found) return found;
      }
      return null;
    })();
    const nls = cd?.children || [];
    const isLastNguonLucInCongDoan = (nls.length === 0);

    const nlIcon = isLastNguonLucInCongDoan
      ? '../assets/icons/arrow-right_end_nl.png'
      : '../assets/icons/arrow-right_end_nl.png';

    return htmlToElement(`
      <tr class="row-nguonluc add-row">
        <td data-label=""></td>
        <td data-label="STT">
          <span class="nl-stt" style="display:inline-flex;align-items:center;gap:6px;">
            <img src="${nlIcon}" alt="" style="margin-left:6px;width:14px;height:14px;object-fit:contain;" />
            <span></span>
          </span>
        </td>
        <td data-label="Loại">Nguồn lực</td>
        <td data-label="Mã"><input id="addMaNguonLuc" style="width:80px" disabled></td>
        <td data-label="Tên"><input id="addTenNguonLuc" style="width:120px" placeholder="Tên nguồn lực..."></td>
        <td data-label="Nhân sự"></td>
        <td data-label="Nhân sự phụ thuộc"><input id="addNhanSuPhuThuoc" type="number" style="width:60px"></td>
        <td data-label="Thời gian thiết lập"><input id="addThoiGianThietLap" type="number" style="width:60px"></td>
        <td data-label="Thao tác">
          <div class="actions">
            <button class="icon" data-action="inherit-nguonluc" title="Kế thừa">
              <i class="bi bi-collection"></i>
            </button>
            <button class="icon primary" data-action="confirm-add-nguonluc" title="Lưu">
              <i class="bi bi-check2"></i>
            </button>
            <button class="icon danger" data-action="cancel-add-nguonluc" title="Hủy">
              <i class="bi bi-x-lg"></i>
            </button>
          </div>
        </td>
      </tr>
    `);
  }
}

function htmlToElement(html) {
  const template = document.createElement('template');
  template.innerHTML = html.trim();
  return template.content.firstChild;
}

// Toast helper
function showToast(type, title, msg) {
  const wrap = document.getElementById('toastWrap');
  const toast = document.createElement('div');
  toast.className = 'toast';
  let icon = '';
  if (type === 'success') {
    toast.style.borderColor = '#16A34A';
    icon = '<span style="color:#16A34A;font-size:15px;margin-top:4px;margin-right: 10px;vertical-align:middle"><i class="bi bi-check-circle"></i></span>';
  }
  if (type === 'error') {
    toast.style.borderColor = '#DC2626';
    icon = '<span style="color:#DC2626;font-size:15px;margin-top:4px;margin-right: 10px;vertical-align:middle"><i class="bi bi-x-circle"></i></span>';
  }
  toast.innerHTML = `<div style="display:flex;align-items:flex-start;font-size:13px;">${icon}<div><b class="t-title">${title}</b><div class="t-msg">${msg}</div></div></div>`;
  wrap.appendChild(toast);
  setTimeout(() => { toast.remove(); }, 3500);
}

// Modal logic (simple prompt for demo, replace with real modal)
async function handleAdd(loai, parentMa) {
  let data = {};
  if (loai === 'congdoanlon') {
    data.MaCongDoanLon = prompt('Mã công đoạn lớn:');
    data.TenCongDoanLon = prompt('Tên công đoạn lớn:');
    data.SoNhanSu = +prompt('Số nhân sự:');
    if (!data.MaCongDoanLon || !data.TenCongDoanLon) return;
    await addCongDoanLon(data);
  } else if (loai === 'congdoan') {
    data.MaCongDoan = prompt('Mã công đoạn:');
    data.TenCongDoan = prompt('Tên công đoạn:');
    data.MaCongDoanLon = parentMa;
    if (!data.MaCongDoan || !data.TenCongDoan) return;
    await addCongDoan(data);
  } else if (loai === 'nguonluc') {
    data.MaNguonLuc = prompt('Mã nguồn lực:');
    data.TenNguonLuc = prompt('Tên nguồn lực:');
    data.NhanSuPhanBo = +prompt('Nhân sự phân bổ:');
    data.ThoiGianThietLap = +prompt('Thời gian thiết lập:');
    if (!data.MaNguonLuc || !data.TenNguonLuc) return;
    await addNguonLuc(data);
  }
  await render();
}

async function handleEdit(loai, ma) {
  const item = findItem(loai, ma);
  if (!item) return;
  let data = { ...item };
  data.ten = prompt('Tên mới:', item.ten);
  if (data.ten === null) return;
  if (loai === 'nguonluc') {
    data.nhan_su = +prompt('Nhân sự:', item.nhan_su);
    data.nhan_su_phu_thuoc = +prompt('Nhân sự phụ thuộc:', item.nhan_su_phu_thuoc);
    data.thoi_gian_thiet_lap = +prompt('Thời gian thiết lập:', item.thoi_gian_thiet_lap);
  } else {
    data.nhan_su = +prompt('Nhân sự:', item.nhan_su);
  }
  await editItem(loai, ma, data);
  await render();
}

async function handleDelete(loai, ma) {
  // Nếu xóa công đoạn: kiểm tra nguồn lực phụ thuộc trước
  if (loai === 'congdoan') {
    const cd = findItem('congdoan', ma);
    const children = cd?.children || [];
    if (children.length > 0) {
      expandedSet.add(ma);
      await render();

      await confirmDialog(
        'Không thể xóa công đoạn',
        `Công đoạn đang có ${children.length} nguồn lực phụ thuộc.\nHãy gỡ liên kết các nguồn lực trước!`
      );
      return;
    }
  }

  // Nếu xóa nguồn lực: chỉ xóa vĩnh viễn khi KHÔNG còn được dùng ở công đoạn khác.
  // Yêu cầu: nếu còn dùng ở công đoạn khác => không cho xóa và vẫn giữ nguồn lực ở công đoạn hiện tại.
  if (loai === 'nguonluc') {
    const ok2 = await confirmDialog(
      'Xác nhận',
      'Xóa vĩnh viễn nguồn lực này? (Chỉ xóa được khi nguồn lực không còn được dùng ở công đoạn khác)'
    );
    if (!ok2) return;

    const maNguonLuc = ma;
    const maCongDoan = findParentCongDoanOfNguonLuc(maNguonLuc);
    if (!maCongDoan) {
      showToast('error', 'Lỗi', 'Không xác định được công đoạn của nguồn lực để xóa.');
      return;
    }

    try {
      // 1) Thử unlink khỏi công đoạn hiện tại
      await deleteMappingCongDoanNguonLuc(maCongDoan, maNguonLuc);

      // 2) Thử xóa vĩnh viễn. Nếu vẫn còn mapping ở công đoạn khác => backend sẽ trả 409
      await deleteItem('nguonluc', maNguonLuc);

      showToast('success', 'Đã xóa', 'Đã xóa vĩnh viễn nguồn lực.');
      await render();
    } catch (e) {
      // Nếu không xóa được vĩnh viễn (còn được dùng ở công đoạn khác):
      // rollback việc unlink để nguồn lực vẫn hiển thị ở công đoạn hiện tại theo yêu cầu.
      if (e?.status === 409 || String(e?.message || '').includes('FK') || String(e?.message || '').includes('REFERENCE')) {
        try {
          await addMappingCongDoanNguonLuc(maCongDoan, maNguonLuc);
        } catch {
          // ignore
        }
        showToast('error', 'Không thể xóa', e?.message || 'Nguồn lực còn đang được sử dụng trong công đoạn khác.');
      } else {
        // lỗi khác: cố gắng rollback mapping để tránh mất dữ liệu trên UI
        try {
          await addMappingCongDoanNguonLuc(maCongDoan, maNguonLuc);
        } catch {
          // ignore
        }
        showToast('error', 'Lỗi', e?.message || 'Không thể xóa nguồn lực!');
      }
      await render();
    }
    return;
  }

  const ok = await confirmDialog('Xác nhận', 'Bạn có chắc chắn muốn xóa?');
  if (!ok) return;
  try {
    // Công đoạn: cần gỡ mapping khỏi bộ phận trước để tránh lỗi FK
    if (loai === 'congdoan') {
      const bpMa = findCongDoanLonOfCongDoan(ma);
      if (bpMa) {
        await detachCongDoanFromCongDoanLon(ma);
      }
    }

    await deleteItem(loai, ma);
    showToast('success', 'Đã xóa', 'Xóa thành công!');
  } catch (e) {
    if (loai === 'nguonluc' && (e?.status === 409 || String(e?.message || '').includes('FK') || String(e?.message || '').includes('REFERENCE'))) {
      showToast('error', 'Không thể xóa', 'Nguồn lực đang được dùng trong công đoạn. Hãy gỡ liên kết trước.');
    } else if (loai === 'congdoan' && (e?.status === 409 || String(e?.message || '').includes('FK') || String(e?.message || '').includes('REFERENCE'))) {
      showToast('error', 'Không thể xóa công đoạn', e?.message || 'Công đoạn đang có dữ liệu liên quan. Hãy gỡ nguồn lực phụ thuộc trước.');
    } else {
      showToast('error', 'Lỗi', e?.message || 'Không thể xóa!');
    }
  }
  await render();
}

function findItem(loai, ma) {
  for (const bp of DATA) {
    if (loai === 'bophan' && bp.ma === ma) return bp;
    for (const cd of bp.children || []) {
      if (loai === 'congdoan' && cd.ma === ma) return cd;
      for (const nl of cd.children || []) {
        if (loai === 'nguonluc' && nl.ma === ma) return nl;
      }
    }
  }
  return null;
}

document.getElementById('tbBody').onclick = async function(e) {
  const btn = e.target.closest('button');
  if (!btn) return;

  const ma = btn.dataset.ma;
  const action = btn.dataset.action;

  // Kế thừa công đoạn
  if (action === 'inherit-congdoan') {
    openModal('modalCongDoanLib');
    await loadCongDoanLib();
    return;
  }

  // Kế thừa nguồn lực
  if (action === 'inherit-nguonluc') {
    openModal('modalNguonLucLib');
    await loadNguonLucLib();
    return;
  }

  // Thêm công đoạn cho bộ phận
  if (action === 'add-congdoan') {
    // 1) sổ bộ phận để hiển thị các công đoạn hiện có
    if (ma && !expandedSet.has(ma)) {
      expandedSet.add(ma);
      // render 1 lần để user thấy các dòng con đã có
      await render();
    }

    // 2) sau đó mới hiển thị hàng thêm mới
    addingRow.bophan = ma;
    addingRow.congdoan = null;
    addRowCongDoanPickedFromLib = null;
    addRowCongDoanPickedMa = '';
    await render();

    // 3) tự sinh mã công đoạn và khóa input mã
    await autoFillAddCodeFor('congdoan');
    return;
  }

  // Thêm nguồn lực cho công đoạn
  if (action === 'add-nguonluc') {
    // 1) sổ công đoạn để hiển thị các nguồn lực hiện có
    if (ma && !expandedSet.has(ma)) {
      expandedSet.add(ma);
      await render();
    }

    // 2) sau đó mới hiển thị hàng thêm mới
    addingRow.congdoan = ma;
    addingRow.bophan = null;
    addRowNguonLucPickedFromLib = null;
    await render();

    // 3) tự sinh mã nguồn lực và khóa input mã
    await autoFillAddCodeFor('nguonluc');
    return;
  }

  // Cancel thêm công đoạn
  if (action === 'cancel-add-congdoan') {
    addingRow.bophan = null;
    addRowCongDoanPickedFromLib = null;
    addRowCongDoanPickedMa = '';
    await render();
    return;
  }
  // Cancel thêm nguồn lực
  if (action === 'cancel-add-nguonluc') {
    addingRow.congdoan = null;
    addRowNguonLucPickedFromLib = null;
    addRowNguonLucPickedMa = '';
    await render();
    return;
  }
  // Xác nhận thêm công đoạn
  if (action === 'confirm-add-congdoan') {
    const maCd = document.getElementById('addMaCongDoan').value.trim();
    const tenCd = document.getElementById('addTenCongDoan').value.trim();
    if (!maCd || !tenCd) {
      showToast('error', 'Thiếu thông tin', 'Vui lòng nhập mã và tên công đoạn!');
      return;
    }

    const maCongDoanLon = addingRow.bophan;
    if (!maCongDoanLon) {
      showToast('error', 'Thiếu thông tin', 'Chưa xác định bộ phận để gán công đoạn.');
      return;
    }

    // Nếu chọn từ thư viện => chỉ attach mapping, KHÔNG tạo mới DM_CongDoan
    const pickedFromLib = !!addRowCongDoanPickedMa && addRowCongDoanPickedMa === maCd;

    try {
      if (!pickedFromLib) {
        // 1) Tạo công đoạn trong DM_CongDoan
        await addCongDoan({ MaCongDoan: maCd, TenCongDoan: tenCd });
      }

      // 2) Attach công đoạn vào bộ phận theo mapping mới (DM_CongDoan_CongDoanLon)
      await attachCongDoanToCongDoanLon(maCd, maCongDoanLon);

      addingRow.bophan = null;
      addRowCongDoanPickedFromLib = null;
      addRowCongDoanPickedMa = '';
      showToast('success', 'Thành công', pickedFromLib ? 'Đã gán công đoạn vào bộ phận!' : 'Đã thêm công đoạn và gán vào bộ phận!');
      await render();
    } catch (err) {
      if (err?.status === 409) {
        showToast('error', 'Xung đột dữ liệu', err?.message || 'Công đoạn đã tồn tại hoặc đã được gán vào bộ phận khác.');
      } else {
        showToast('error', 'Lỗi', err?.message || 'Không thể thêm/gán công đoạn!');
      }
    }
    return;
  }
  // Xác nhận thêm nguồn lực (nguồn lực nằm trong công đoạn)
  if (action === 'confirm-add-nguonluc') {
    const maNl = document.getElementById('addMaNguonLuc').value.trim();
    const tenNl = document.getElementById('addTenNguonLuc').value.trim();
    const nspt = +document.getElementById('addNhanSuPhuThuoc').value;
    const tg = +document.getElementById('addThoiGianThietLap').value;

    if (!maNl) {
      showToast('error', 'Thiếu thông tin', 'Vui lòng nhập/chọn mã nguồn lực!');
      return;
    }

    const maCongDoan = addingRow.congdoan;
    if (!maCongDoan) {
      showToast('error', 'Thiếu thông tin', 'Chưa xác định công đoạn để thêm nguồn lực.');
      return;
    }

    // Nếu chọn từ thư viện => chỉ link mapping, KHÔNG tạo mới nguồn lực
    const pickedFromLib = !!addRowNguonLucPickedMa && addRowNguonLucPickedMa === maNl;

    try {
      if (pickedFromLib) {
        await addMappingCongDoanNguonLuc(maCongDoan, maNl);
        addingRow.congdoan = null;
        addRowNguonLucPickedFromLib = null;
        addRowNguonLucPickedMa = '';
        showToast('success', 'Thành công', 'Đã gắn nguồn lực vào công đoạn!');
        await render();
        return;
      }

      // Nhập tay => tạo mới DM_NguonLuc rồi gắn mapping
      if (!tenNl) {
        showToast('error', 'Thiếu thông tin', 'Vui lòng nhập tên nguồn lực!');
        return;
      }

      // 1) Tạo DM_NguonLuc
      await addNguonLuc({
        MaNguonLuc: maNl,
        TenNguonLuc: tenNl,
        NhanSuPhanBo: nspt,
        ThoiGianThietLap: tg
      });

      // 2) Gắn nguồn lực vào công đoạn qua mapping
      await addMappingCongDoanNguonLuc(maCongDoan, maNl);

      addingRow.congdoan = null;
      addRowNguonLucPickedFromLib = null;
      addRowNguonLucPickedMa = '';
      showToast('success', 'Thành công', 'Đã thêm nguồn lực vào công đoạn!');
      await render();
    } catch (err) {
      if (err?.status === 409) {
        showToast('error', 'Xung đột dữ liệu', err?.message || 'Dữ liệu đã tồn tại (trùng mã hoặc đã liên kết).');
      } else {
        showToast('error', 'Lỗi', err?.message || 'Không thể thêm nguồn lực!');
      }
    }
    return;
  }
  // Gỡ nguồn lực khỏi công đoạn (chỉ unlink mapping)
  if (action === 'unlink') {
    const maNguonLuc = ma;
    const maCongDoan = findParentCongDoanOfNguonLuc(maNguonLuc);
    if (!maCongDoan) {
      showToast('error', 'Lỗi', 'Không xác định được công đoạn của nguồn lực để gỡ.');
      return;
    }

    const ok = await confirmDialog('Xác nhận', 'Gỡ nguồn lực khỏi công đoạn này?');
    if (!ok) return;

    try {
      await deleteMappingCongDoanNguonLuc(maCongDoan, maNguonLuc);
      showToast('success', 'Thành công', 'Đã gỡ nguồn lực khỏi công đoạn.');
      await render();
    } catch (err) {
      showToast('error', 'Lỗi', err?.message || 'Không thể gỡ nguồn lực!');
    }
    return;
  }
  if (btn.classList.contains('expander')) {
    if (expandedSet.has(ma)) expandedSet.delete(ma); else expandedSet.add(ma);
    await render();
    return;
  }
  if (btn.dataset.action === 'edit') {
    const rowType = btn.closest('tr').className.replace('row-', '');
    const item = findItem(rowType, ma);
    if (!item) return;

    // Mở modal edit tương ứng (khóa Loại + Mã)
    if (rowType === 'bophan') {
      openEditBoPhanModal(item);
    } else if (rowType === 'congdoan') {
      openEditCongDoanModal(item);
    } else if (rowType === 'nguonluc') {
      openEditNguonLucModal(item);
    }
    return;
  }
  if (btn.dataset.action === 'delete') {
    const loai = btn.closest('tr').className.replace('row-', '');
    await handleDelete(loai, ma);
    return;
  }
};

document.getElementById('btnAddDropdown').onclick = function() {
  // Khi bấm Thêm mới, mở modal thêm nguồn lực, reset các trường
  showAddNguonLucModal();
};

// ===== Helpers: cập nhật UI của modal thêm mới theo loại =====
function getAddModalTitleByLoai(loai) {
  if (loai === 'congdoanlon') return 'Thêm mới bộ phận';
  if (loai === 'congdoan') return 'Thêm mới công đoạn';
  if (loai === 'nguonluc') return 'Thêm mới nguồn lực';
  return 'Thêm mới';
}

function applyAddModalUiByLoai(loai) {
  // Title
  const titleEl = document.getElementById('modalNguonLucTitle');
  if (titleEl) titleEl.innerText = getAddModalTitleByLoai(loai);

  // Field switch
  if (loai === 'congdoanlon' || loai === 'congdoan') {
    document.getElementById('lblNhanSuNguonLuc')?.classList.remove('hidden');
    document.getElementById('lblNhanSuPhuThuocNguonLuc')?.classList.add('hidden');
    document.getElementById('lblThoiGianThietLapNguonLuc')?.classList.add('hidden');
  } else if (loai === 'nguonluc') {
    document.getElementById('lblNhanSuNguonLuc')?.classList.add('hidden');
    document.getElementById('lblNhanSuPhuThuocNguonLuc')?.classList.remove('hidden');
    document.getElementById('lblThoiGianThietLapNguonLuc')?.classList.remove('hidden');
  }
}

// Hiển thị modal thêm mới nguồn lực
async function showAddNguonLucModal() {
  document.getElementById('modalNguonLuc').classList.remove('hidden');
  // Mặc định chọn loại là Công đoạn lớn
  document.getElementById('fLoaiNguonLuc').value = 'congdoanlon';
  // Tự động sinh mã cho Công đoạn lớn
  const maEl = document.getElementById('fMaNguonLuc');
  maEl.value = await getNextCode('congdoanlon');
  maEl.disabled = true;

  document.getElementById('fTenNguonLuc').value = '';
  document.getElementById('fNhanSuNguonLuc').value = '';
  document.getElementById('fNhanSuPhuThuocNguonLuc').value = '';
  document.getElementById('fThoiGianThietLapNguonLuc').value = '';

  // Label/field + title theo loại
  applyAddModalUiByLoai('congdoanlon');

  // Đổi tên hiển thị loại trong select
  const selectLoai = document.getElementById('fLoaiNguonLuc');
  if (selectLoai) {
    selectLoai.options[0].text = 'Bộ phận'; // Đổi từ Công đoạn lớn thành Bộ phận
  }
  document.getElementById('modalNguonLuc').dataset.parentMa = '';
}

// Đóng modal
function closeNguonLucModal() {
  document.getElementById('modalNguonLuc').classList.add('hidden');
}

document.querySelectorAll('[data-close="modalNguonLuc"]').forEach(btn => {
  btn.onclick = closeNguonLucModal;
});

document.getElementById('btnSaveNguonLuc').onclick = async function() {
  // Khi lưu từ modal thêm mới (Bộ phận/Công đoạn/Nguồn lực)
  try {
    const loai = (document.getElementById('fLoaiNguonLuc')?.value || '').trim();

    const ma = document.getElementById('fMaNguonLuc')?.value?.trim() || '';
    const ten = document.getElementById('fTenNguonLuc')?.value?.trim() || '';

    // Field trên modal đang dùng cho:
    // - congdoanlon/congdoan: số nhân sự
    // - nguonluc: nhân sự phụ thuộc + thời gian thiết lập
    const soNhanSu = Number(document.getElementById('fNhanSuNguonLuc')?.value);
    const nhanSuPhuThuoc = Number(document.getElementById('fNhanSuPhuThuocNguonLuc')?.value);
    const thoiGian = Number(document.getElementById('fThoiGianThietLapNguonLuc')?.value);

    if (!ma) {
      showToast('error', 'Thiếu thông tin', 'Không xác định được mã cần tạo.');
      return;
    }

    if (!ten) {
      showToast('error', 'Thiếu thông tin', 'Vui lòng nhập tên!');
      return;
    }

    if (loai === 'congdoanlon') {
      // Tạo Bộ phận (DM_CongDoanLon)
      await addCongDoanLon({
        MaCongDoanLon: ma,
        TenCongDoanLon: ten,
        SoNhanSu: Number.isFinite(soNhanSu) ? soNhanSu : null
      });

      closeNguonLucModal();
      showToast('success', 'Thành công', 'Đã thêm mới bộ phận!');
      await render();
      return;
    }

    if (loai === 'congdoan') {
      // Tạo Công đoạn (DM_CongDoan) - KHÔNG tạo vào DM_NguonLuc
      // Backend mới: CongDoanSchema chỉ còn {MaCongDoan, TenCongDoan}
      await addCongDoan({
        MaCongDoan: ma,
        TenCongDoan: ten
      });

      closeNguonLucModal();
      showToast('success', 'Thành công', 'Đã thêm mới công đoạn!');
      await render();
      return;
    }

    if (loai === 'nguonluc') {
      // Tạo Nguồn lực (DM_NguonLuc)
      await addNguonLuc({
        MaNguonLuc: ma,
        TenNguonLuc: ten,
        NhanSuPhanBo: Number.isFinite(nhanSuPhuThuoc) ? nhanSuPhuThuoc : null,
        ThoiGianThietLap: Number.isFinite(thoiGian) ? thoiGian : null
      });

      closeNguonLucModal();
      showToast('success', 'Thành công', 'Đã thêm mới nguồn lực!');
      await render();
      return;
    }

    showToast('error', 'Lỗi', 'Loại thêm mới không hợp lệ.');
  } catch (e) {
    showToast('error', 'Lỗi', e?.message || 'Không thể thêm mới!');
  }
};

// Sửa lại sự kiện dropdown để chỉ mở modal thêm mới nguồn lực
const dropdownItems = document.querySelectorAll('#addDropdownMenu .dropdown-item');
dropdownItems.forEach(item => {
  item.onclick = function() {
    document.getElementById('addDropdownMenu').classList.add('hidden');
    showAddNguonLucModal(); // Không truyền loại, sẽ chọn trong modal
  };
});

// Sự kiện thay đổi loại trong modal
if (document.getElementById('fLoaiNguonLuc')) {
  document.getElementById('fLoaiNguonLuc').onchange = async function() {
    const loai = this.value;
    // Tự động sinh mã theo loại
    const maEl = document.getElementById('fMaNguonLuc');
    maEl.value = await getNextCode(loai);
    maEl.disabled = true;

    // Cập nhật field + title theo loại
    applyAddModalUiByLoai(loai);
  };
}

// Nút Thêm mới trong thư viện Công đoạn: đóng thư viện, mở modal thêm Công đoạn
if (document.getElementById('btnAddCongDoanInLib')) {
  document.getElementById('btnAddCongDoanInLib').addEventListener('click', async () => {
    closeModal('modalCongDoanLib');

    // mở modal thêm mới và set loại = công đoạn
    document.getElementById('modalNguonLuc').classList.remove('hidden');
    document.getElementById('fLoaiNguonLuc').value = 'congdoan';
    const maEl = document.getElementById('fMaNguonLuc');
    maEl.value = await getNextCode('congdoan');
    maEl.disabled = true;

    document.getElementById('fTenNguonLuc').value = '';
    document.getElementById('fNhanSuNguonLuc').value = '';
    document.getElementById('fNhanSuPhuThuocNguonLuc').value = '';
    document.getElementById('fThoiGianThietLapNguonLuc').value = '';

    // hiển thị field theo loại (công đoạn)
    document.getElementById('lblNhanSuNguonLuc').classList.remove('hidden');
    document.getElementById('lblNhanSuPhuThuocNguonLuc').classList.add('hidden');
    document.getElementById('lblThoiGianThietLapNguonLuc').classList.add('hidden');

    document.getElementById('modalNguonLucTitle').innerText = 'Thêm mới công đoạn';
    document.getElementById('modalNguonLuc').dataset.parentMa = '';
  });
}

// Nút Thêm mới trong thư viện Nguồn lực: đóng thư viện, mở modal thêm Nguồn lực
if (document.getElementById('btnAddNguonLucInLib')) {
  document.getElementById('btnAddNguonLucInLib').addEventListener('click', async () => {
    closeModal('modalNguonLucLib');

    document.getElementById('modalNguonLuc').classList.remove('hidden');
    document.getElementById('fLoaiNguonLuc').value = 'nguonluc';
    const maEl = document.getElementById('fMaNguonLuc');
    maEl.value = await getNextCode('nguonluc');
    maEl.disabled = true;

    document.getElementById('fTenNguonLuc').value = '';
    document.getElementById('fNhanSuNguonLuc').value = '';
    document.getElementById('fNhanSuPhuThuocNguonLuc').value = '';
    document.getElementById('fThoiGianThietLapNguonLuc').value = '';

    // hiển thị field theo loại (nguồn lực)
    document.getElementById('lblNhanSuNguonLuc').classList.add('hidden');
    document.getElementById('lblNhanSuPhuThuocNguonLuc').classList.remove('hidden');
    document.getElementById('lblThoiGianThietLapNguonLuc').classList.remove('hidden');

    document.getElementById('modalNguonLucTitle').innerText = 'Thêm mới nguồn lực';
    document.getElementById('modalNguonLuc').dataset.parentMa = '';
  });
}

document.getElementById('btnReload').onclick = render;
document.getElementById('txtSearch').oninput = render;
render();

// Lấy số thứ tự tiếp theo cho mã tự động
async function getNextCode(loai) {
  let list = [];
  if (loai === 'congdoanlon') {
    list = await fetch(`${API_BASE}/congdoanlon`).then(r => r.json());
    const maxNum = Math.max(0, ...list.map(x => parseInt((x.MaCongDoanLon||'').replace(/BP0*/,''))||0));
    return 'BP' + String(maxNum+1).padStart(3,'0');
  } else if (loai === 'congdoan') {
    list = await fetch(`${API_BASE}/congdoan`).then(r => r.json());
    const maxNum = Math.max(0, ...list.map(x => parseInt((x.MaCongDoan||'').replace(/CD0*/,''))||0));
    return 'CD' + String(maxNum+1).padStart(3,'0');
  } else if (loai === 'nguonluc') {
    list = await fetch(`${API_BASE}/nguonluc`).then(r => r.json());
    const maxNum = Math.max(0, ...list.map(x => parseInt((x.MaNguonLuc||'').replace(/M0*/,''))||0));
    return 'M' + String(maxNum+1).padStart(3,'0');
  }
  return '';
}

// Modal helpers
function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

// Wire close buttons (nguon-luc.html có data-close)
document.querySelectorAll('[data-close]').forEach(btn => {
  btn.addEventListener('click', () => closeModal(btn.dataset.close));
});

async function fetchCongDoanList(q = '') {
  const res = await fetch(`${API_BASE}/congdoan${q ? `?search=${encodeURIComponent(q)}` : ''}`);
  if (!res.ok) return [];
  return await res.json();
}

async function fetchNguonLucList(q = '') {
  const res = await fetch(`${API_BASE}/nguonluc${q ? `?search=${encodeURIComponent(q)}` : ''}`);
  if (!res.ok) return [];
  return await res.json();
}

let LIB_PICKING = null; // 'congdoan' | 'nguonluc'
let CONGDOAN_LIB_CACHE = [];
let NGUONLUC_LIB_CACHE = [];

async function loadCongDoanLib() {
  const q = (document.getElementById('txtSearchCongDoanLib')?.value || '').trim();
  CONGDOAN_LIB_CACHE = await fetchCongDoanList(q);
  const tb = document.getElementById('tbCongDoanLib');
  if (!tb) return;
  tb.innerHTML = '';
  CONGDOAN_LIB_CACHE.forEach(cd => {
    const tr = document.createElement('tr');
    tr.dataset.ma = cd.MaCongDoan;
    tr.innerHTML = `
      <td>${cd.MaCongDoan}</td>
      <td>${cd.TenCongDoan}</td>
      <td>${cd.SoNhanSu ?? ''}</td>
      <td>
        <div class="actions">
          <button class="icon primary" data-action="pick-congdoan-lib" data-ma="${cd.MaCongDoan}" title="Chọn">
            <i class="bi bi-check2-circle"></i>
          </button>
          <button class="icon" data-action="edit-congdoan-lib" data-ma="${cd.MaCongDoan}" title="Cập nhật">
            <i class="bi bi-pencil-square"></i>
          </button>
          <button class="icon danger" data-action="delete-congdoan-lib" data-ma="${cd.MaCongDoan}" title="Xóa">
            <i class="bi bi-trash3"></i>
          </button>
        </div>
      </td>
    `;
    tb.appendChild(tr);
  });
}

async function loadNguonLucLib() {
  const q = (document.getElementById('txtSearchNguonLucLib')?.value || '').trim();
  NGUONLUC_LIB_CACHE = await fetchNguonLucList(q);
  const tb = document.getElementById('tbNguonLucLib');
  if (!tb) return;
  tb.innerHTML = '';
  NGUONLUC_LIB_CACHE.forEach(nl => {
    const tr = document.createElement('tr');
    tr.dataset.ma = nl.MaNguonLuc;
    tr.innerHTML = `
      <td>${nl.MaNguonLuc}</td>
      <td>${nl.TenNguonLuc}</td>
      <td>${nl.NhanSuPhuThuoc ?? ''}</td>
      <td>${nl.ThoiGianThietLap ?? ''}</td>
      <td>
        <div class="actions">
          <button class="icon primary" data-action="pick-nguonluc-lib" data-ma="${nl.MaNguonLuc}" title="Chọn">
            <i class="bi bi-check2-circle"></i>
          </button>
          <button class="icon" data-action="edit-nguonluc-lib" data-ma="${nl.MaNguonLuc}" title="Cập nhật">
            <i class="bi bi-pencil-square"></i>
          </button>
          <button class="icon danger" data-action="delete-nguonluc-lib" data-ma="${nl.MaNguonLuc}" title="Xóa">
            <i class="bi bi-trash3"></i>
          </button>
        </div>
      </td>
    `;
    tb.appendChild(tr);
  });
}

// Events search/reload library
if (document.getElementById('txtSearchCongDoanLib')) {
  document.getElementById('txtSearchCongDoanLib').addEventListener('input', () => loadCongDoanLib().catch(() => {}));
}
if (document.getElementById('btnReloadCongDoanLib')) {
  document.getElementById('btnReloadCongDoanLib').addEventListener('click', () => loadCongDoanLib().catch(() => {}));
}
if (document.getElementById('txtSearchNguonLucLib')) {
  document.getElementById('txtSearchNguonLucLib').addEventListener('input', () => loadNguonLucLib().catch(() => {}));
}
if (document.getElementById('btnReloadNguonLucLib')) {
  document.getElementById('btnReloadNguonLucLib').addEventListener('click', () => loadNguonLucLib().catch(() => {}));
}

// LIB tables click (pick)
if (document.getElementById('tbCongDoanLib')) {
  document.getElementById('tbCongDoanLib').addEventListener('click', async (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;

    const action = btn.dataset.action;
    const ma = btn.dataset.ma;

    // Chọn để kế thừa công đoạn
    if (action === 'pick-congdoan-lib') {
      const cd = CONGDOAN_LIB_CACHE.find(x => x.MaCongDoan === ma);
      if (!cd) return;

      // Rule: không cho kế thừa nếu công đoạn đã nằm trong bộ phận khác
      const inBp = findCongDoanLonOfCongDoan(cd.MaCongDoan);
      if (inBp && inBp !== addingRow.bophan) {
        showToast('error', 'Không thể kế thừa', 'Công đoạn này đã tồn tại trong bộ phận khác');
        return;
      }

      // Đổ dữ liệu vào hàng add công đoạn hiện tại
      const maEl = document.getElementById('addMaCongDoan');
      const tenEl = document.getElementById('addTenCongDoan');
      const nsEl = document.getElementById('addNhanSuCongDoan');
      if (maEl) maEl.value = cd.MaCongDoan;
      if (tenEl) tenEl.value = cd.TenCongDoan;
      if (nsEl) nsEl.value = cd.SoNhanSu ?? '';

      // Đánh dấu picked-from-lib để khi lưu chỉ attach mapping
      addRowCongDoanPickedFromLib = cd;
      addRowCongDoanPickedMa = cd.MaCongDoan;

      // Nếu user sửa lại mã khác với mã đã pick => coi như tạo mới
      if (maEl) {
        maEl.oninput = () => {
          const v = maEl.value.trim();
          if (!v || v !== addRowCongDoanPickedMa) {
            addRowCongDoanPickedFromLib = null;
            addRowCongDoanPickedMa = '';
          }
        };
      }

      closeModal('modalCongDoanLib');
      showToast('success', 'Đã chọn', 'Đã kế thừa công đoạn từ thư viện');
      return;
    }

    // Cập nhật công đoạn ngay trong thư viện
    if (action === 'edit-congdoan-lib') {
      const item = CONGDOAN_LIB_CACHE.find(x => x.MaCongDoan === ma);
      if (!item) return;

      closeModal('modalCongDoanLib');
      // Mở modal cập nhật công đoạn (mã/loại đã khóa)
      openEditCongDoanModal({
        loai: 'Công đoạn',
        ma: item.MaCongDoan,
        ten: item.TenCongDoan,
        nhan_su: item.SoNhanSu
      });
      return;
    }

    // Xóa công đoạn ngay trong thư viện (xóa hẳn khỏi DB)
    if (action === 'delete-congdoan-lib') {
      const ok = await confirmDialog('Xác nhận', 'Xóa công đoạn này khỏi hệ thống?');
      if (!ok) return;
      try {
        await deleteItem('congdoan', ma);
        showToast('success', 'Đã xóa', 'Đã xóa công đoạn.');
        await loadCongDoanLib();
        await render();
      } catch (err) {
        showToast('error', 'Lỗi', err?.message || 'Không thể xóa công đoạn!');
      }
      return;
    }
  });
}

if (document.getElementById('tbNguonLucLib')) {
  document.getElementById('tbNguonLucLib').addEventListener('click', async (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;

    const action = btn.dataset.action;
    const ma = btn.dataset.ma;

    if (action === 'pick-nguonluc-lib') {
      const nl = NGUONLUC_LIB_CACHE.find(x => x.MaNguonLuc === ma);
      if (!nl) return;

      const maEl = document.getElementById('addMaNguonLuc');
      const tenEl = document.getElementById('addTenNguonLuc');
      const nsptEl = document.getElementById('addNhanSuPhuThuoc');
      const tgEl = document.getElementById('addThoiGianThietLap');

      if (maEl) maEl.value = nl.MaNguonLuc;
      if (tenEl) tenEl.value = nl.TenNguonLuc;
      if (nsptEl) nsptEl.value = nl.NhanSuPhanBo ?? nl.NhanSuPhuThuoc ?? '';
      if (tgEl) tgEl.value = nl.ThoiGianThietLap ?? '';

      addRowNguonLucPickedFromLib = nl;
      addRowNguonLucPickedMa = nl.MaNguonLuc;

      if (maEl) {
        maEl.oninput = () => {
          const v = maEl.value.trim();
          if (!v || v !== addRowNguonLucPickedMa) {
            addRowNguonLucPickedFromLib = null;
            addRowNguonLucPickedMa = '';
          }
        };
      }

      closeModal('modalNguonLucLib');
      showToast('success', 'Đã chọn', 'Đã kế thừa nguồn lực từ thư viện');
      return;
    }

    // Cập nhật nguồn lực ngay trong thư viện
    if (action === 'edit-nguonluc-lib') {
      const item = NGUONLUC_LIB_CACHE.find(x => x.MaNguonLuc === ma);
      if (!item) return;

      closeModal('modalNguonLucLib');
      openEditNguonLucModal({
        loai: 'Nguồn lực',
        ma: item.MaNguonLuc,
        ten: item.TenNguonLuc,
        nhan_su_phu_thuoc: item.NhanSuPhanBo ?? item.NhanSuPhuThuoc,
        thoi_gian_thiet_lap: item.ThoiGianThietLap
      });
      return;
    }

    // Xóa nguồn lực ngay trong thư viện (xóa hẳn khỏi DB)
    if (action === 'delete-nguonluc-lib') {
      const ok = await confirmDialog('Xác nhận', 'Xóa nguồn lực này khỏi hệ thống?');
      if (!ok) return;
      try {
        await deleteItem('nguonluc', ma);
        showToast('success', 'Đã xóa', 'Đã xóa nguồn lực.');
        await loadNguonLucLib();
        await render();
      } catch (err) {
        // Nếu đang được dùng trong công đoạn => backend trả 409
        if (err?.status === 409) {
          showToast('error', 'Không thể xóa', err?.message || 'Nguồn lực đang được dùng trong công đoạn. Hãy gỡ liên kết trước.');
        } else {
          showToast('error', 'Lỗi', err?.message || 'Không thể xóa nguồn lực!');
        }
      }
      return;
    }
  });
}

// Nếu user sửa tay mã nguồn lực sau khi pick từ thư viện => coi như tạo mới, reset trạng thái.
// Dùng capture để bắt cả trường hợp input nằm trong DOM được render lại.
if (document.getElementById('tbBody')) {
  document.getElementById('tbBody').addEventListener('input', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLInputElement)) return;
    if (t.id !== 'addMaNguonLuc') return;
    if (!addRowNguonLucPickedMa) return;

    const cur = t.value.trim();
    if (cur !== addRowNguonLucPickedMa) {
      addRowNguonLucPickedFromLib = null;
      addRowNguonLucPickedMa = '';
    }
  }, true);
}

function confirmDialog(title, msg) {
  return new Promise((resolve) => {
    const titleEl = document.getElementById('confirmTitle');
    const msgEl = document.getElementById('confirmMsg');
    const ok = document.getElementById('confirmOk');
    const cancel = document.getElementById('confirmCancel');

    if (titleEl) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = msg;

    const cleanup = () => {
      if (ok) ok.onclick = null;
      if (cancel) cancel.onclick = null;
      closeModal('modalConfirm');
    };

    if (ok) ok.onclick = () => { cleanup(); resolve(true); };
    if (cancel) cancel.onclick = () => { cleanup(); resolve(false); };

    openModal('modalConfirm');
  });
}