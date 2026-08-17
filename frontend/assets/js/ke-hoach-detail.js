// frontend/assets/js/ke-hoach-detail.js

(function () {
  const qs = new URLSearchParams(window.location.search);
  const planId = Number(qs.get('id'));

  const pageTitle = document.getElementById('pageTitle');
  const planMeta = document.getElementById('planMeta');
  const thead = document.getElementById('theadPlanCalendar');
  const tbody = document.getElementById('tbodyPlanCalendar');
  const emptyHint = document.getElementById('emptyHint');
  const errorHint = document.getElementById('errorHint');
  const btnReload = document.getElementById('btnReload');
  const btnExportExcel = document.getElementById('btnExportExcel');

  // Keep latest loaded calendar data for export
  let lastHeader = null;
  let lastDays = [];
  let lastRows = [];
  let lastPlanCode = null;

  const fmtDate = (d) => {
    if (!d) return '';
    if (typeof d === 'string') return d.split('T')[0];
    try { return String(d).split('T')[0]; } catch { return ''; }
  };

  const fmtNum = (n) => {
    const v = Number(n);
    if (!Number.isFinite(v)) return '';
    return v.toLocaleString('vi-VN');
  };

  const parseMaybeJson = async (res) => {
    const ct = (res.headers.get('content-type') || '').toLowerCase();
    if (ct.includes('application/json')) {
      try { return await res.json(); } catch { /* fallthrough */ }
    }
    const txt = await res.text();
    try { return JSON.parse(txt); } catch { return { _rawText: txt }; }
  };

  const safeText = (v) => (v === null || v === undefined) ? '' : String(v);

  const sanitizeFilenamePart = (s) => {
    const raw = safeText(s).trim() || 'ke-hoach';
    // Windows-forbidden filename chars: \ / : * ? " < > |
    return raw.replace(/[\\/:*?"<>|]+/g, '_');
  };

  const buildDailyQtyDisplay = (totalQty, capacityPerDay, overtimeCapacityPerDay) => {
    const v = Number(totalQty || 0);
    const cap = Number(capacityPerDay || 0);
    const otCap = Number(overtimeCapacityPerDay || 0);
    if (!v) return '';
    // Export as displayed in UI: formatted text (not numeric)
    if (!cap || v <= cap || !otCap) return fmtNum(v);
    const overtime = Math.max(0, v - cap);
    return `${fmtNum(cap)}\n+${fmtNum(overtime)} TC`;
  };

  const exportCalendarToExcel = () => {
    if (!btnExportExcel) return;
    if (!window.XLSX) {
      alert('Thiếu thư viện xuất Excel (XLSX). Vui lòng kiểm tra kết nối internet để tải thư viện.');
      return;
    }

    const ds = Array.isArray(lastDays) ? lastDays : [];
    const rows = Array.isArray(lastRows) ? lastRows : [];
    if (!rows.length || !ds.length) {
      alert('Chưa có dữ liệu kế hoạch để xuất Excel.');
      return;
    }

    const fixedHeaders = [
      'Số chứng từ',
      'Thành phẩm',
      'Bán thành phẩm',
      'Bộ phận',
      'Công đoạn',
      'Nguồn lực',
      'Số lượng nguồn lực',
      'Thời gian định mức',
      'Thời gian thiết lập',
      'Năng lực sản xuất',
      'Số lượng sản xuất',
    ];
    const dayHeaders = ds.map(d => fmtDate(d));
    const aoa = [fixedHeaders.concat(dayHeaders)];

    rows.forEach((r) => {
      const soCt = safeText(r.SoChungTu) || safeText(r.SoDonHang);
      const tp = safeText(r.TenThanhPham) || `TP ${safeText(r.TP_DinhMucID)}`;
      const btp = safeText(r.TenBanThanhPham) || `BTP ${safeText(r.BTP_DinhMucID)}`;
      const bp = safeText(r.TenBoPhan);
      const cd = safeText(r.TenCongDoan) || safeText(r.MaCongDoan);
      const nl = safeText(r.NguonLucText) || safeText(r.TenNguonLuc) || safeText(r.MaNguonLuc);
      const slnl = Number(r.SoLuongNguonLuc);
      const dm = r.DinhMucThoiGian;
      const tglm = r.ThoiGianThietLapMay;
      const nlsx = Math.round(Number(r.NangLucSanXuat) || 0);
      const sl = Number(r.TotalQty);

      const base = [
        soCt,
        tp,
        btp,
        bp,
        cd,
        nl,
        Number.isFinite(slnl) ? fmtNum(slnl) : safeText(r.SoLuongNguonLuc),
        (dm === null || dm === undefined) ? '' : safeText(dm),
        (tglm === null || tglm === undefined) ? '' : safeText(tglm),
        Number.isFinite(nlsx) ? fmtNum(nlsx) : safeText(r.NangLucSanXuat),
        Number.isFinite(sl) ? fmtNum(sl) : safeText(r.TotalQty),
      ];

      const daily = Array.isArray(r.DailyQty) ? r.DailyQty : [];
      const capPerDay = Number(r.CapacityPerDay || r.NangLucSanXuat || 0);
      const otCapPerDay = Number(r.OvertimeCapacityPerDay || r.NangLucTangCa || 0);

      const perDay = [];
      for (let i = 0; i < ds.length; i++) {
        perDay.push(buildDailyQtyDisplay(daily[i], capPerDay, otCapPerDay));
      }

      aoa.push(base.concat(perDay));
    });

    const ws = window.XLSX.utils.aoa_to_sheet(aoa);

    // Auto-fit column widths based on displayed text (headers + values)
    try {
      const colCount = aoa[0]?.length || 0;
      const widths = new Array(colCount).fill(8);

      for (let r = 0; r < aoa.length; r++) {
        const row = aoa[r] || [];
        for (let c = 0; c < colCount; c++) {
          const cell = row[c];
          const s = safeText(cell);
          if (!s) continue;
          // Multi-line cells: take longest line
          const maxLineLen = s.split('\n').reduce((m, line) => Math.max(m, (line || '').length), 0);
          // Add a bit of padding
          const wch = Math.min(60, Math.max(8, maxLineLen + 2));
          if (wch > widths[c]) widths[c] = wch;
        }
      }

      ws['!cols'] = widths.map((wch) => ({ wch }));
    } catch (e) { /* ignore */ }

    const wb = window.XLSX.utils.book_new();
    window.XLSX.utils.book_append_sheet(wb, ws, 'KeHoach');

    // Improve readability for multi-line daily qty cells (e.g. "77\n+35 TC")
    try {
      ws['!rows'] = aoa.map((_, idx) => (idx === 0 ? { hpt: 18 } : { hpt: 30 }));
    } catch (e) { /* ignore */ }

    const code = lastPlanCode || (lastHeader && (lastHeader.MaKeHoach || lastHeader.KeHoachID)) || planId;
    const filename = `${sanitizeFilenamePart(code)}.xlsx`;
    window.XLSX.writeFile(wb, filename);
  };

  const parseResList = (v) => {
    const s = safeText(v).trim();
    if (!s) return [];
    // Backend currently returns comma-separated machine names like "CNC 1, CNC 2" or "Nhân công"
    return s.split(',').map(x => x.trim()).filter(Boolean);
  };

  const renderNguonLucCell = (nguonLucText) => {
    const items = parseResList(nguonLucText);
    const main = items[0] || '';

    if (!main) return document.createTextNode('');
    if (items.length <= 1) return document.createTextNode(main);

    const wrap = document.createElement('span');
    wrap.className = 'res-list';
    wrap.tabIndex = 0;

    const mainEl = document.createElement('span');
    mainEl.className = 'res-main';
    mainEl.textContent = main;

    const chev = document.createElement('span');
    chev.className = 'res-chevron';
    chev.title = 'Chọn nguồn lực';
    chev.innerHTML = '<i class="bi bi-chevron-down"></i>';

    const pop = document.createElement('span');
    pop.className = 'res-pop';
    pop.setAttribute('role', 'list');

    // close helper
    const close = () => {
      wrap.classList.remove('open');
    };

    // open toggle only when clicking the chevron (as requested UX)
    chev.addEventListener('click', (e) => {
      e.stopPropagation();
      wrap.classList.toggle('open');
    });

    // clicking outside closes
    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target)) close();
    });

    // escape closes
    wrap.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') close();
    });

    items.forEach((it) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'res-item';
      row.innerHTML = `<span class="res-dot"></span><span>${it}</span>`;
      row.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        mainEl.textContent = it;
        close();
      });
      pop.appendChild(row);
    });

    wrap.appendChild(mainEl);
    wrap.appendChild(chev);
    wrap.appendChild(pop);
    return wrap;
  };

  const buildCalendarTable = ({ header, days, rows }) => {
    const ds = Array.isArray(days) ? days : [];
    const baseRowsRaw = Array.isArray(rows) ? rows : [];

    const renderDailyQtyCell = (td, totalQty, capacityPerDay, overtimeCapacityPerDay) => {
      const v = Number(totalQty || 0);
      const cap = Number(capacityPerDay || 0);
      const otCap = Number(overtimeCapacityPerDay || 0);

      if (!v) {
        td.textContent = '';
        return;
      }

      // Default: show total
      if (!cap || v <= cap || !otCap) {
        td.textContent = fmtNum(v);
        return;
      }

      const overtime = Math.max(0, v - cap);
      td.innerHTML = `<div>${fmtNum(cap)}</div><small>+${fmtNum(overtime)} TC</small>`;
    };

    // Sort for display:
    // 1) Thành phẩm (TP_DinhMucID) then 2) hạn giao (nếu có) then 3) ThuTuSX then 4) Công đoạn
    const baseRows = baseRowsRaw.slice().sort((a, b) => {
      const tpA = Number(a?.TP_DinhMucID || 0);
      const tpB = Number(b?.TP_DinhMucID || 0);
      if (tpA !== tpB) return tpA - tpB;

      const dueA = safeText(a?.DueDT || a?.DueDt || '');
      const dueB = safeText(b?.DueDT || b?.DueDt || '');
      if (dueA !== dueB) return dueA.localeCompare(dueB);

      const ttA = Number(a?.ThuTuSX || 0);
      const ttB = Number(b?.ThuTuSX || 0);
      if (ttA !== ttB) return ttA - ttB;

      const cdA = safeText(a?.MaCongDoan || a?.TenCongDoan || '');
      const cdB = safeText(b?.MaCongDoan || b?.TenCongDoan || '');
      return cdA.localeCompare(cdB);
    });

    // Header
    const trh = document.createElement('tr');
    const fixed = [
      { label: 'Số chứng từ', sticky: true },
      { label: 'Thành phẩm', sticky: true },
      { label: 'Bán thành phẩm', sticky: false },
      { label: 'Bộ phận', sticky: false },
      { label: 'Công đoạn', sticky: false },
      { label: 'Nguồn lực', sticky: false },
      { label: 'Số lượng nguồn lực', sticky: false },
      { label: 'Thời gian định mức', sticky: false },
      { label: 'Thời gian thiết lập', sticky: false },
      { label: 'Năng lực sản xuất', sticky: false },
      { label: 'Số lượng sản xuất', sticky: false },
    ];

    fixed.forEach((c, idx) => {
      const th = document.createElement('th');
      th.className = 'th-blue' + (idx === 0 ? ' sticky-col' : (idx === 1 ? ' sticky-col-2' : ''));
      th.textContent = c.label;
      trh.appendChild(th);
    });

    ds.forEach((d, i) => {
      const th = document.createElement('th');
      const iso = fmtDate(d);
      const dt = iso ? new Date(`${iso}T00:00:00`) : null;
      const day = dt ? dt.getDay() : -1; // 0=CN, 6=T7
      const isSunday = day === 0;

      th.className = 'th-blue th-date' + (i === 0 ? ' first-date' : '') + (isSunday ? ' is-sunday' : '');
      th.textContent = iso;
      th.title = isSunday ? 'Chủ nhật' : '';
      trh.appendChild(th);
    });

    thead.innerHTML = '';
    thead.appendChild(trh);

    // Body
    tbody.innerHTML = '';
    if (baseRows.length === 0) {
      emptyHint.style.display = 'block';
      return;
    }
    emptyHint.style.display = 'none';

    baseRows.forEach((r) => {
      const tr = document.createElement('tr');

      const soCt = safeText(r.SoChungTu) || safeText(r.SoDonHang);
      const tp = safeText(r.TenThanhPham) || `TP ${safeText(r.TP_DinhMucID)}`;
      const btp = safeText(r.TenBanThanhPham) || `BTP ${safeText(r.BTP_DinhMucID)}`;
      const bp = safeText(r.TenBoPhan);
      const cd = safeText(r.TenCongDoan) || safeText(r.MaCongDoan);
      const nl = safeText(r.NguonLucText) || safeText(r.TenNguonLuc) || safeText(r.MaNguonLuc);
      const slnl = r.SoLuongNguonLuc;
      const dm = r.DinhMucThoiGian;
      const tglm = r.ThoiGianThietLapMay;
      const nlsx = Math.round(Number(r.NangLucSanXuat) || 0);
      const sl = r.TotalQty;

      const fixedVals = [soCt, tp, btp, bp, cd, nl, slnl, dm, tglm, nlsx, sl];
      fixedVals.forEach((val, idx) => {
        const td = document.createElement('td');
        if (idx === 0) td.className = 'sticky-col';
        if (idx === 1) td.classList.add('sticky-col-2');
        if (idx >= 6) td.classList.add('cell-num');

        // Nguồn lực column (index 5): render as hover-expand list
        if (idx === 5) {
          td.innerHTML = '';
          td.appendChild(renderNguonLucCell(val));
        } else {
          td.textContent = (idx >= 6) ? fmtNum(val) : safeText(val);
        }

        tr.appendChild(td);
      });

      const daily = Array.isArray(r.DailyQty) ? r.DailyQty : [];
      const capPerDay = Number(r.CapacityPerDay || r.NangLucSanXuat || 0);
      const otCapPerDay = Number(r.OvertimeCapacityPerDay || r.NangLucTangCa || 0);
      for (let i = 0; i < ds.length; i++) {
        const td = document.createElement('td');
        const iso = fmtDate(ds[i]);
        const dt = iso ? new Date(`${iso}T00:00:00`) : null;
        const day = dt ? dt.getDay() : -1;
        const isSunday = day === 0;

        td.className = 'cell-num td-date' + (i === 0 ? ' first-date' : '') + (isSunday ? ' is-sunday' : '');
        renderDailyQtyCell(td, daily[i], capPerDay, otCapPerDay);
        tr.appendChild(td);
      }

      tbody.appendChild(tr);
    });
  };

  const load = async () => {
    emptyHint.style.display = 'none';
    errorHint.style.display = 'none';

    if (!planId) {
      errorHint.textContent = 'Thiếu tham số id kế hoạch.';
      errorHint.style.display = 'block';
      return;
    }

    try {
      const res = await fetch(`/api/v1/kehoach/${planId}/calendar`);
      const data = await parseMaybeJson(res);
      if (!res.ok) throw new Error(data?.detail || data?._rawText || 'Không thể tải dữ liệu');

      const header = data?.Header || {};
      const days = data?.Days || [];
      const rows = data?.Rows || [];

      lastHeader = header;
      lastDays = Array.isArray(days) ? days : [];
      lastRows = Array.isArray(rows) ? rows : [];

      const ma = header?.MaKeHoach || header?.KeHoachID || planId;
      lastPlanCode = ma;
      if (pageTitle) pageTitle.textContent = `Chi tiết kế hoạch ${ma}`;
      if (planMeta) planMeta.textContent = `Từ ${fmtDate(header?.TuNgay)} đến ${fmtDate(header?.DenNgay)} | Trạng thái: ${safeText(header?.TrangThai)}`;

      buildCalendarTable({ header, days, rows });
    } catch (e) {
      errorHint.textContent = (e && e.message) ? e.message : 'Không thể tải dữ liệu';
      errorHint.style.display = 'block';
      thead.innerHTML = '';
      tbody.innerHTML = '';
    }
  };

  // --- Bổ sung: Render AI proposals & analysis ---
  const aiBlock = document.getElementById('aiBlock');
  const aiAnalysisBlock = document.getElementById('aiAnalysisBlock');

  async function loadAiAnalysisAndProposals(planId) {
    if (!planId) return;
    try {
      const res = await fetch(`/api/v1/kehoach/${planId}/ai`);
      const data = await parseMaybeJson(res);
      if (!res.ok) throw new Error(data?.detail || data?._rawText || 'Không thể tải dữ liệu AI');
      renderAiAnalysisBlock(data?.analysis);
      renderAiProposalsBlock(data?.suggestions);
    } catch (e) {
      if (aiAnalysisBlock) aiAnalysisBlock.innerHTML = `<div class="ai-empty">Không thể tải phân tích AI.</div>`;
      if (aiBlock) aiBlock.innerHTML = `<div class="ai-empty">Không thể tải phương án AI.</div>`;
    }
  }

  function renderAiAnalysisBlock(analysis) {
    if (!aiAnalysisBlock) return;
    if (!analysis) {
      aiAnalysisBlock.innerHTML = '<div class="ai-empty">Chưa có phân tích tổng thể từ AI.</div>';
      return;
    }
    aiAnalysisBlock.innerHTML = `<div class="ai-block"><h3>Phân tích tổng thể từ AI</h3><div>${escapeHtml(analysis)}</div></div>`;
  }

  function buildMovesHtml(moves) {
    const arr = Array.isArray(moves) ? moves : [];
    if (!arr.length) return '';
    const items = arr.slice(0, 6).map((mv) => {
      let desc = safeText(mv?.desc || mv?.description || mv?.note || mv?.reason || '').trim();
      if (/^M\d{2,4}$/.test(desc) && mv.machine_name) desc = mv.machine_name;
      if (/^CD\d{2,4}$/.test(desc) && mv.step_name) desc = mv.step_name;
      if (/^NL\d{2,4}$/.test(desc) && mv.resource_name) desc = mv.resource_name;
      if (/^BTP\d{2,4}$/.test(desc) && mv.btp_name) desc = mv.btp_name;
      if (/^TP\d{2,4}$/.test(desc) && mv.tp_name) desc = mv.tp_name;
      const meta = [
        mv.machine_name ? `Máy: ${escapeHtml(mv.machine_name)}` : '',
        mv.step_name ? `Công đoạn: ${escapeHtml(mv.step_name)}` : '',
        mv.resource_name ? `Nguồn lực: ${escapeHtml(mv.resource_name)}` : '',
        mv.btp_name ? `BTP: ${escapeHtml(mv.btp_name)}` : '',
        mv.tp_name ? `TP: ${escapeHtml(mv.tp_name)}` : ''
      ].filter(Boolean).join(' • ');
      return `<li><div>${escapeHtml(desc)}</div>${meta ? `<div class=\"ai-submeta\">${meta}</div>` : ''}</li>`;
    }).join('');
    return `
      <details class="ai-details">
        <summary>Điều chỉnh đề xuất</summary>
        <ul class="ai-list ai-list--compact">${items}</ul>
      </details>
    `;
  }

  function renderAiProposalsBlock(suggestions) {
    if (!aiBlock) return;
    const list = Array.isArray(suggestions) ? suggestions : [];
    if (!list.length) {
      aiBlock.innerHTML = '<div class="ai-empty">Chưa có phương án tối ưu từ AI.</div>';
      return;
    }
    aiBlock.innerHTML = list.map((sug, idx) => {
      const title = safeText(sug?.title || `Phương án ${idx + 1}`).trim();
      const desc = safeText(sug?.description || '').trim();
      const meta = sug?.meta || {};
      const movesHtml = buildMovesHtml(meta?.moves);
      return `
        <article class="ai-card" data-index="${idx}">
          <header class="ai-card__head">
            <div class="ai-card__title">
              <div class="ai-kicker">Phương án ${idx + 1}</div>
              <div class="ai-h">${escapeHtml(title)}</div>
            </div>
          </header>
          ${desc ? `<div class="ai-p">${escapeHtml(desc)}</div>` : ''}
          ${movesHtml || ''}
        </article>
      `;
    }).join('');
  }

  if (btnReload) btnReload.addEventListener('click', load);
  if (btnExportExcel) btnExportExcel.addEventListener('click', exportCalendarToExcel);
  load();
  loadAiAnalysisAndProposals(planId);
})();
