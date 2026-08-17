// frontend/assets/js/ke-hoach.js
document.addEventListener('DOMContentLoaded', function() {
  loadDepartments();
  loadMachines();

  // load danh sách kế hoạch ngay khi vào trang
  loadPlanList();

  // Nếu có lỗi JS ở các hàm khác, sự kiện này sẽ không chạy. Đảm bảo không có lỗi ở các hàm loadDepartments, loadMachines, loadPlanList, loadBTPTable, loadSummary.
  // Nếu không dùng các hàm này, hãy comment chúng lại để tránh lỗi.

  // Nếu không có search-btn, hãy comment đoạn này lại:
  // document.getElementById('search-btn').addEventListener('click', function() {
  //   loadPlanList();
  //   loadBTPTable();
  //   loadSummary();
  // });

  // Đảm bảo DOM đã sẵn sàng và không có lỗi JS trước khi đến đoạn này:
  document.getElementById('btnCreatePlan').addEventListener('click', function() {
    document.getElementById('modalOrderSelect').classList.remove('hidden');
    loadOrderList();
  });

  // Đóng modal khi nhấn nút đóng
  document.querySelectorAll('[data-close="modalOrderSelect"]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      document.getElementById('modalOrderSelect').classList.add('hidden');
    });
  });

  // Close modals
  document.querySelectorAll('[data-close="modalPlanDetail"]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      document.getElementById('modalPlanDetail').classList.add('hidden');
    });
  });

  // Tiny modal notify helpers
  function showToastModal(title, message) {
    const modal = document.getElementById('modalToast');
    const t = document.getElementById('toastTitle');
    const m = document.getElementById('toastMessage');
    if (t) t.textContent = title || 'Thông báo';
    if (m) m.textContent = message || '';
    if (modal) modal.classList.remove('hidden');
  }

  document.querySelectorAll('[data-close="modalToast"]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      document.getElementById('modalToast').classList.add('hidden');
    });
  });

  // Confirm modal helpers
  let _confirmResolve = null;
  function showConfirmModal(title, message) {
    const modal = document.getElementById('modalConfirm');
    const t = document.getElementById('confirmTitle');
    const m = document.getElementById('confirmMessage');
    if (t) t.textContent = title || 'Xác nhận';
    if (m) m.textContent = message || '';
    if (modal) modal.classList.remove('hidden');

    return new Promise((resolve) => {
      _confirmResolve = resolve;
    });
  }

  function closeConfirmModal(result) {
    const modal = document.getElementById('modalConfirm');
    if (modal) modal.classList.add('hidden');
    if (typeof _confirmResolve === 'function') {
      const r = _confirmResolve;
      _confirmResolve = null;
      r(Boolean(result));
    }
  }

  document.querySelectorAll('[data-close="modalConfirm"]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      closeConfirmModal(false);
    });
  });

  const btnConfirmOk = document.getElementById('btnConfirmOk');
  if (btnConfirmOk) {
    btnConfirmOk.addEventListener('click', function() {
      closeConfirmModal(true);
    });
  }

  // Multi-select: tạo kế hoạch từ nhiều đơn hàng
  const btnCreatePlanFromOrders = document.getElementById('btnCreatePlanFromOrders');
  if (btnCreatePlanFromOrders) {
    btnCreatePlanFromOrders.addEventListener('click', function() {
      const checked = Array.from(document.querySelectorAll('#tbOrderList input.order-select:checked'));
      const orderIds = checked
        .map(el => Number(el.dataset.orderId))
        .filter(x => Number.isFinite(x) && x > 0);

      if (!orderIds.length) {
        showToastModal('Thông báo', 'Vui lòng chọn ít nhất 1 đơn hàng');
        return;
      }

      btnCreatePlanFromOrders.disabled = true;

      fetch('/api/v1/kehoach/plan-from-orders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ don_hang_ids: orderIds })
      })
        .then(res => res.json().then(j => ({ ok: res.ok, data: j })))
        .then(({ ok, data }) => {
          if (!ok) throw new Error(data?.detail || 'Không thể lập kế hoạch');

          // đóng modal chọn đơn hàng
          document.getElementById('modalOrderSelect').classList.add('hidden');

          // refresh danh sách kế hoạch để thấy kế hoạch mới
          loadPlanList();

          showToastModal('Tạo kế hoạch', 'Đã tạo kế hoạch thành công');

          const newId = Number(data?.ke_hoach_id || data?.KeHoachID || 0);
          if (newId) {
            window.location.href = `./ke-hoach-detail.html?id=${encodeURIComponent(newId)}`;
          }
        })
        .catch((e) => {
          showToastModal('Lỗi', e.message || 'Không thể lập kế hoạch');
        })
        .finally(() => {
          btnCreatePlanFromOrders.disabled = false;
        });
    });
  }

  // Select all checkbox
  const chkOrderAll = document.getElementById('chkOrderAll');
  if (chkOrderAll) {
    chkOrderAll.addEventListener('change', function() {
      const want = Boolean(chkOrderAll.checked);
      document.querySelectorAll('#tbOrderList input.order-select').forEach(el => { el.checked = want; });
    });
  }

  // reload
  const btnReload = document.getElementById('btnReload');
  if (btnReload) {
    btnReload.addEventListener('click', function() {
      loadPlanList();
    });
  }

  // search (client-side filter over the loaded list)
  const txtSearch = document.getElementById('txtSearch');
  if (txtSearch) {
    txtSearch.addEventListener('input', function() {
      const kw = (txtSearch.value || '').trim().toLowerCase();
      document.querySelectorAll('#tbPlanList tr').forEach(tr => {
        const text = (tr.innerText || '').toLowerCase();
        tr.style.display = (!kw || text.includes(kw)) ? '' : 'none';
      });
    });
  }

  function fmtDateTime(d) {
    if (!d) return '';
    if (typeof d === 'string') return d.replace('T', ' ').slice(0, 16);
    try { return String(d).replace('T', ' ').slice(0, 16); } catch { return ''; }
  }

  // actions in plan list: view / delete
  const tbPlanList = document.getElementById('tbPlanList');
  if (tbPlanList) {
    tbPlanList.addEventListener('click', async function(ev) {
      const btn = ev.target.closest('button[data-act][data-id]');
      if (!btn) return;

      const act = btn.dataset.act;
      const id = Number(btn.dataset.id);
      if (!id) return;

      if (act === 'delete-plan') {
        const tr = btn.closest('tr');
        const tds = tr ? tr.querySelectorAll('td') : null;
        // Column 2 in the table is MaKeHoach (index 1). Column 1 is the row index.
        const maKeHoach = (tds && tds.length >= 2 ? (tds[1].textContent || '').trim() : '') || String(id);

        const ok = await showConfirmModal(
          `Xóa kế hoạch`,
          `Xác nhận xóa kế hoạch ${maKeHoach}`
        );
        if (!ok) return;

        btn.disabled = true;
        fetch(`/api/v1/kehoach/${id}`, { method: 'DELETE' })
          .then(res => res.json().then(j => ({ ok: res.ok, data: j })))
          .then(({ ok, data }) => {
            if (!ok) throw new Error(data?.detail || 'Không thể xóa kế hoạch');
            loadPlanList();
            // No success notification required.
          })
          .catch(e => showToastModal('Lỗi', e.message || 'Không thể xóa kế hoạch'))
          .finally(() => { btn.disabled = false; });
        return;
      }

      if (act === 'ai-analyze') {
        window.location.href = `./ke-hoach-ai.html?id=${encodeURIComponent(id)}`;
        return;
      }

      if (act === 'view-plan') {
        // Mở màn hình mới xem chi tiết kế hoạch
        window.location.href = `./ke-hoach-detail.html?id=${encodeURIComponent(id)}`;
      }
    });
  }

  function openPlanDetailModal(planId) {
    const modal = document.getElementById('modalPlanDetail');
    const titleEl = document.getElementById('planDetailTitle');
    const tbDetail = document.getElementById('tbPlanDetail');
    const tbBtp = document.getElementById('tbPlanBTP');

    if (titleEl) titleEl.textContent = 'Chi tiết kế hoạch';
    if (tbDetail) tbDetail.innerHTML = '<tr><td colspan="12" style="text-align:center;color:#888;">Đang tải...</td></tr>';
    if (tbBtp) tbBtp.innerHTML = '<tr><td colspan="15" style="text-align:center;color:#888;">Đang tải...</td></tr>';

    if (modal) modal.classList.remove('hidden');

    const parseMaybeJson = async (res) => {
      const ct = (res.headers.get('content-type') || '').toLowerCase();
      // Success responses should be JSON; error responses may be plain text
      if (ct.includes('application/json')) {
        try {
          return await res.json();
        } catch {
          // fallthrough
        }
      }
      const txt = await res.text();
      // try to parse JSON even if header is wrong
      try {
        return JSON.parse(txt);
      } catch {
        return { _rawText: txt };
      }
    };

    Promise.all([
      fetch(`/api/v1/kehoach/${planId}`),
      fetch(`/api/v1/kehoach/${planId}/chitiet`),
      fetch(`/api/v1/kehoach/${planId}/btp`),
    ])
      .then(async ([r1, r2, r3]) => {
        const j1 = await parseMaybeJson(r1);
        const j2 = await parseMaybeJson(r2);
        const j3 = await parseMaybeJson(r3);

        if (!r1.ok) throw new Error(j1?.detail || j1?._rawText || 'Không thể tải kế hoạch');
        if (!r2.ok) throw new Error(j2?.detail || j2?._rawText || 'Không thể tải chi tiết');
        if (!r3.ok) throw new Error(j3?.detail || j3?._rawText || 'Không thể tải BTP');
        return { header: j1, chitiet: j2, btp: j3 };
      })
      .then(({ header, chitiet, btp }) => {
        const ma = header?.MaKeHoach || header?.KeHoachID || planId;
        if (titleEl) titleEl.textContent = `Chi tiết kế hoạch ${ma}`;

        // Render segments
        if (tbDetail) {
          tbDetail.innerHTML = '';
          const arr = Array.isArray(chitiet) ? chitiet : [];
          if (arr.length === 0) {
            tbDetail.innerHTML = '<tr><td colspan="12" style="text-align:center;color:#888;">Chưa có chi tiết</td></tr>';
          } else {
            arr.forEach((s, idx) => {
              const tr = document.createElement('tr');
              tr.innerHTML = `
                <td>${idx + 1}</td>
                <td>${s.SegmentType ?? ''}</td>
                <td>${s.DonHangID ?? ''}</td>
                <td>${s.TP_DinhMucID ?? ''}</td>
                <td>${s.BTP_DinhMucID ?? ''}</td>
                <td>${s.ThuTuSX ?? ''}</td>
                <td>${s.MaCongDoan ?? ''}</td>
                <td>${s.MaCongDoanLon ?? ''}</td>
                <td>${s.MaNguonLuc ?? ''}</td>
                <td>${s.SoLuongSX ?? ''}</td>
                <td>${fmtDateTime(s.StartDT)}</td>
                <td>${fmtDateTime(s.EndDT)}</td>
              `;
              tbDetail.appendChild(tr);
            });
          }
        }

        // Render grouped BTP
        if (tbBtp) {
          tbBtp.innerHTML = '';
          const rows = Array.isArray(btp) ? btp : [];
          if (rows.length === 0) {
            tbBtp.innerHTML = '<tr><td colspan="15" style="text-align:center;color:#888;">Chưa có dữ liệu</td></tr>';
          } else {
            rows.forEach((r, idx) => {
              // Row có thể là tuple/list hoặc object tùy driver
              const get = (obj, key, i) => {
                if (!obj) return '';
                if (Array.isArray(obj)) return obj[i] ?? '';
                return obj[key] ?? '';
              };
              const tr = document.createElement('tr');
              tr.innerHTML = `
                <td>${idx + 1}</td>
                <td>${get(r, 'DonHangID', 0)}</td>
                <td>${get(r, 'LineKey', 1)}</td>
                <td>${get(r, 'TP_DinhMucID', 2)}</td>
                <td>${get(r, 'BTP_DinhMucID', 3)}</td>
                <td>${get(r, 'ThuTuSX', 4)}</td>
                <td>${get(r, 'MaCongDoan', 5)}</td>
                <td>${get(r, 'MaCongDoanLon', 6)}</td>
                <td>${get(r, 'MaNguonLuc', 7)}</td>
                <td>${get(r, 'TotalSetup', 8)}</td>
                <td>${get(r, 'TotalRun', 9)}</td>
                <td>${get(r, 'TotalQty', 10)}</td>
                <td>${fmtDateTime(get(r, 'Start', 11))}</td>
                <td>${fmtDateTime(get(r, 'End', 12))}</td>
                <td>${fmtDateTime(get(r, 'DueDT', 13))}</td>
              `;
              tbBtp.appendChild(tr);
            });
          }
        }
      })
      .catch((e) => {
        if (titleEl) titleEl.textContent = `Chi tiết kế hoạch ${planId}`;
        const msg = (e && e.message) ? e.message : 'Không thể tải dữ liệu';
        if (tbDetail) tbDetail.innerHTML = `<tr><td colspan="12" style="text-align:center;color:#b42318;">${msg}</td></tr>`;
        if (tbBtp) tbBtp.innerHTML = `<tr><td colspan="15" style="text-align:center;color:#b42318;">${msg}</td></tr>`;
      });
  }

  function loadPlanList() {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 12000);

    fetch('/api/v1/kehoach/', { signal: controller.signal })
      .then(res => res.json())
      .then(data => {
        clearTimeout(timeoutId);
        const tbody = document.getElementById('tbPlanList');
        if (!tbody) return;
        tbody.innerHTML = '';

        if (!Array.isArray(data) || data.length === 0) {
          tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#888;">Chưa có kế hoạch</td></tr>';
          return;
        }

        const parseMaKeHoach = (ma) => {
          const s = String(ma || '').trim();
          const m = s.match(/^KHSX\/(\d{2})\/(\d{4})\/(\d{3})$/i);
          if (!m) return null;
          return {
            month: Number(m[1]),
            year: Number(m[2]),
            seq: Number(m[3]),
          };
        };

        const list = [...data].sort((a, b) => {
          const pa = parseMaKeHoach(a?.MaKeHoach);
          const pb = parseMaKeHoach(b?.MaKeHoach);

          if (pa && pb) {
            if (pa.year !== pb.year) return pa.year - pb.year;
            if (pa.month !== pb.month) return pa.month - pb.month;
            if (pa.seq !== pb.seq) return pa.seq - pb.seq;
          }

          const ad = String(a?.NgayLap || '');
          const bd = String(b?.NgayLap || '');
          if (ad !== bd) return ad.localeCompare(bd);
          return Number(a?.KeHoachID || 0) - Number(b?.KeHoachID || 0);
        });

        const fmtDate = (d) => {
          if (!d) return '';
          if (typeof d === 'string') return d.split('T')[0];
          try { return String(d).split('T')[0]; } catch { return ''; }
        };

        list.forEach((p, idx) => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>${idx + 1}</td>
            <td>${p.MaKeHoach ?? p.KeHoachID ?? ''}</td>
            <td>${p.SoChungTu ?? p.so_chung_tu ?? ''}</td>
            <td>${fmtDate(p.NgayLap)}</td>
            <td>${fmtDate(p.TuNgay)}</td>
            <td>${fmtDate(p.DenNgay)}</td>
            <td>${p.TrangThai ?? ''}</td>
            <td>
              <button class="icon-btn" title="Xem" data-act="view-plan" data-id="${p.KeHoachID}"><i class="bi bi-eye"></i></button>
              <button class="icon-btn" title="Xóa" data-act="delete-plan" data-id="${p.KeHoachID}"><i class="bi bi-trash"></i></button>
              <button class="icon-btn" title="AI phân tích" data-act="ai-analyze" data-id="${p.KeHoachID}"><i class="bi bi-cpu"></i></button>
            </td>
          `;
          tbody.appendChild(tr);
        });

        // re-apply current keyword filter (if any)
        if (txtSearch) {
          const kw = (txtSearch.value || '').trim().toLowerCase();
          document.querySelectorAll('#tbPlanList tr').forEach(tr => {
            const text = (tr.innerText || '').toLowerCase();
            tr.style.display = (!kw || text.includes(kw)) ? '' : 'none';
          });
        }
      })
      .catch((err) => {
        clearTimeout(timeoutId);
        const tbody = document.getElementById('tbPlanList');
        if (!tbody) return;
        if (err && err.name === 'AbortError') {
          tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#b42318;">API tải danh sách kế hoạch quá lâu (timeout 12s)</td></tr>';
          return;
        }
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#888;">Không thể tải danh sách kế hoạch</td></tr>';
      });
  }

  function loadBTPTable() {
    // TODO: Gọi API /api/v1/kehoachsx/btp?filter... và render bảng BTP
  }

  function loadSummary() {
    // TODO: Gọi API /api/v1/kehoachsx/summary?filter... và render tổng hợp
  }

  function loadDepartments() {
    // TODO: Gọi API /api/v1/department và render vào #department-filter
  }

  function loadMachines() {
    // TODO: Gọi API /api/v1/machine và render vào #machine-filter
  }

  function loadOrderList() {
    // Gọi API lấy danh sách đơn hàng chưa lập kế hoạch và render vào #tbOrderList
    fetch('/api/v1/don-hang/chua-lap-ke-hoach')
      .then(res => res.json())
      .then(data => {
        const tbody = document.getElementById('tbOrderList');
        tbody.innerHTML = '';
        const chkAll = document.getElementById('chkOrderAll');
        if (chkAll) chkAll.checked = false;
        if (!Array.isArray(data) || data.length === 0) {
          tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#888;">Không có đơn hàng</td></tr>';
          return;
        }

        const fmtDate = (d) => {
          if (!d) return '';
          // backend trả YYYY-MM-DD
          if (typeof d === 'string') return d.split('T')[0];
          try { return String(d).split('T')[0]; } catch { return ''; }
        };

        data.forEach((order, idx) => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>${idx + 1}</td>
            <td>${order.so_chung_tu ?? ''}</td>
            <td>${order.khach_hang ?? ''}</td>
            <td>${fmtDate(order.ngay_giao_hang)}</td>
            <td>${order.tinh_trang_don_hang ?? ''}</td>
            <td style="text-align:center;">
              <input type="checkbox" class="order-select" data-order-id="${order.don_hang_id}" />
            </td>
          `;
          tbody.appendChild(tr);
        });
      })
      .catch(() => {
        const tbody = document.getElementById('tbOrderList');
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#888;">Không thể tải danh sách đơn hàng</td></tr>';
      });
  }
});
