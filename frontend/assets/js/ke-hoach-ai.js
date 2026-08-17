// frontend/assets/js/ke-hoach-ai.js
(function () {
  const qs = new URLSearchParams(window.location.search);
  const planId = Number(qs.get("id"));

  // -------- DOM --------
  const aiSubtitle = document.getElementById("aiSubtitle");
  const errorHint = document.getElementById("errorHint");

  const btnReload = document.getElementById("btnReload");
  const btnClose = document.getElementById("btnClose");
  const btnConfirm = document.getElementById("btnConfirm");

  // Section 2 (UI mới)
  const aiAnalysisHost = document.getElementById("aiAnalysisHost");
  const aiSuggestionsHost = document.getElementById("aiSuggestionsHost");

  // Optional: nếu bạn có thêm badge/count/quality trên UI thì JS tự fill
  const aiSuggestCount = document.getElementById("aiSuggestCount");
  const aiOverviewQuality = document.getElementById("aiOverviewQuality");

  // Backward-compat (nếu trang còn bảng mô phỏng)
  const planBadge = document.getElementById("planBadge");
  const planBody = document.getElementById("planBody");
  const planTblSubtitle = document.getElementById("planTblSubtitle");
  const planCheckBadges = document.getElementById("planCheckBadges");
  const suggestList = document.getElementById("suggestList");

  // Loading overlay
  const aiLoadingOverlay = document.getElementById("aiLoadingOverlay");
  const aiLoadingSubtitle = document.getElementById("aiLoadingSubtitle");
  const btnCancelAi = document.getElementById("btnCancelAi");

  // -------- State --------
  let _charts = [];
  let _lastCalendar = null;
  let _chartHumanPerf = null;
  let _chartMachinePerf = null;
  let _chartAvailability = null;
  let _chartWarehouse = null;
  let _chartProductionQty = null;
  let _chartDefectQty = null;
  let _chartAxisLabels = [];

  let _aiAbort = null;
  let _aiInFlight = false;
  let _lastAiResp = null;

  // -------- Utils --------
  const FORECAST_BAND_PCT = 0.15;

  function getBandOptionsForLineLabel(lineLabel) {
    const lb = safeText(lineLabel).trim();
    if (lb === "Năng xuất hoạt động của máy móc" || lb === "Nguồn lực khả dụng") {
      return { pct: 0.2, minDelta: 4 };
    }
    return { pct: FORECAST_BAND_PCT, minDelta: 1 };
  }

  const safeText = (v) => (v === null || v === undefined ? "" : String(v));

  function escapeHtml(s) {
    const v = safeText(s);
    return v
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  const fmtDate = (d) => {
    if (!d) return "";
    if (typeof d === "string") return d.split("T")[0];
    try { return String(d).split("T")[0]; } catch { return ""; }
  };

  const fmtNum = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return "";
    return Math.round(n).toLocaleString("vi-VN");
  };

  const fmtPct2 = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return "--";
    return n.toFixed(2);
  };

  function buildLineTooltipLabel(chartId, value) {
    if (chartId === "chart2") return `Năng xuất hoạt động của nhân sự: ${fmtPct2(value)} %`;
    if (chartId === "chart3") return `Năng xuất hoạt động của máy móc: ${fmtPct2(value)} %`;
    if (chartId === "chart6") return `Nguồn lực khả dụng: ${fmtPct2(value)} %`;
    if (chartId === "chart4") return `Tổng số lượng phế phẩm: ${fmtNum(value)}`;
    if (chartId === "chart1") return `Tổng số lượng sản xuất: ${fmtNum(value)}`;
    return `${fmtNum(value)}`;
  }

  const parseMaybeJson = async (res) => {
    const ct = (res.headers.get("content-type") || "").toLowerCase();
    if (ct.includes("application/json")) {
      try { return await res.json(); } catch { /* fallthrough */ }
    }
    const txt = await res.text();
    try { return JSON.parse(txt); } catch { return { _rawText: txt }; }
  };

  function setOverlayVisible(visible, subtitle) {
    if (!aiLoadingOverlay) return;
    aiLoadingOverlay.style.display = visible ? "flex" : "none";
    aiLoadingOverlay.setAttribute("aria-hidden", visible ? "false" : "true");
    document.body.classList.toggle("ai-modal-open", !!visible);

    if (aiLoadingSubtitle && subtitle) aiLoadingSubtitle.textContent = subtitle;

    const disabled = !!visible;
    if (btnReload) btnReload.disabled = disabled;
    if (btnClose) btnClose.disabled = disabled;
    if (btnConfirm) btnConfirm.disabled = disabled;
  }

  function cancelAi(reason) {
    try { if (_aiAbort) _aiAbort.abort(reason || "cancel"); } catch { /* ignore */ }
  }

  // Debug hooks
  try {
    window.__KeHoachAi = window.__KeHoachAi || {};
    window.__KeHoachAi.setOverlayVisible = setOverlayVisible;
    window.__KeHoachAi.cancelAi = cancelAi;
  } catch { /* ignore */ }

  if (btnCancelAi) btnCancelAi.addEventListener("click", () => cancelAi("user_cancel"));

  // -------- Cache --------
  const AI_CACHE_KEY = () => `kehoach_ai_analysis:${planId}`;

  function saveAiCache(aiResp, forecastResp) {
    try {
      if (!planId) return;
      const cachePayload = {
        saved_at: new Date().toISOString(),
        planId,
        aiResp: aiResp || null,
        forecastResp: forecastResp || null,
      };

      // backward-compat: keep legacy field name for older restore paths
      cachePayload.resp = cachePayload.aiResp;

      localStorage.setItem(
        AI_CACHE_KEY(),
        JSON.stringify(cachePayload)
      );
    } catch { /* ignore */ }
  }

  function loadAiCache() {
    try {
      if (!planId) return null;
      const raw = localStorage.getItem(AI_CACHE_KEY());
      if (!raw) return null;
      const obj = JSON.parse(raw);
      if (!obj || typeof obj !== "object") return null;
      if (Number(obj.planId) !== Number(planId)) return null;
      return obj;
    } catch { return null; }
  }

  function clearAiCache() {
    try { localStorage.removeItem(AI_CACHE_KEY()); } catch { /* ignore */ }
  }

  // -------- Charts (giữ nguyên) --------
  function destroyCharts() {
    try { _charts.forEach((c) => c && c.destroy && c.destroy()); } catch { /* ignore */ }
    _charts = [];
    _chartHumanPerf = null;
    _chartMachinePerf = null;
    _chartAvailability = null;
    _chartWarehouse = null;
    _chartProductionQty = null;
    _chartDefectQty = null;
    _chartAxisLabels = [];
  }

  function hexToRgb(hex) {
    const h = String(hex || "").trim();
    if (!h.startsWith("#")) return null;
    const v = h.slice(1);
    const full = v.length === 3 ? v.split("").map((ch) => ch + ch).join("") : v;
    if (full.length !== 6) return null;
    const n = parseInt(full, 16);
    if (!Number.isFinite(n)) return null;
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }

  function withAlpha(color, alpha) {
    const rgb = hexToRgb(color);
    if (rgb) return `rgba(${rgb.r},${rgb.g},${rgb.b},${alpha})`;
    return `rgba(0,110,180,${alpha})`;
  }

  function buildForecastBand(mainData, yMin, yMax, pct = FORECAST_BAND_PCT, minDelta = 1) {
    const arr = Array.isArray(mainData) ? mainData : [];
    const nums = arr.map((v) => (Number.isFinite(Number(v)) ? Number(v) : 0));

    const finite = nums.filter((n) => Number.isFinite(n));
    const minV = finite.length ? Math.min(...finite) : 0;
    const maxV = finite.length ? Math.max(...finite) : 0;
    const amp = Math.max(1, maxV - minV);
    const delta = Math.max(Number(minDelta) || 1, amp * pct);

    const clampY = (v) => {
      let out = v;
      if (yMin !== null && Number.isFinite(Number(yMin))) out = Math.max(Number(yMin), out);
      if (yMax !== null && Number.isFinite(Number(yMax))) out = Math.min(Number(yMax), out);
      return out;
    };

    return {
      lower: nums.map((v) => clampY(v - delta)),
      upper: nums.map((v) => clampY(v + delta)),
    };
  }

  function addForecastBand(datasets, mainIndex = 0, yMin = null, yMax = null, bandOptions = null) {
    const base = datasets?.[mainIndex];
    if (!base || !Array.isArray(base.data)) return datasets;

    const hasFinitePoint = base.data.some((v) => Number.isFinite(Number(v)));
    if (!hasFinitePoint) return datasets;

    const color = base.borderColor || base.backgroundColor || "#006eb4";
    const pct = Number(bandOptions?.pct);
    const minDelta = Number(bandOptions?.minDelta);
    const { lower, upper } = buildForecastBand(
      base.data,
      yMin,
      yMax,
      Number.isFinite(pct) ? pct : FORECAST_BAND_PCT,
      Number.isFinite(minDelta) ? minDelta : 1,
    );

    const lowerDs = {
      label: "",
      data: lower,
      borderColor: "transparent",
      backgroundColor: "transparent",
      pointRadius: 0,
      pointHoverRadius: 0,
      borderWidth: 0,
      tension: base.tension ?? 0.25,
      fill: false,
      order: 1,
    };

    const upperDs = {
      label: "",
      data: upper,
      borderColor: "transparent",
      backgroundColor: withAlpha(color, 0.14),
      pointRadius: 0,
      pointHoverRadius: 0,
      borderWidth: 0,
      tension: base.tension ?? 0.25,
      fill: { target: 0 },
      order: 1,
    };

    const mainDs = { ...base, backgroundColor: base.backgroundColor || color, fill: false, order: 10 };

    return [lowerDs, upperDs, mainDs, ...datasets.filter((_, i) => i !== mainIndex)];
  }

  function createLineChart(elId, labels, datasets, yMin = null, yMax = null) {
    const el = document.getElementById(elId);
    if (!el) return null;

    const c = new Chart(el.getContext("2d"), {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            mode: "index",
            intersect: false,
            displayColors: true,
            boxPadding: 3,
            filter: (item) => {
              const dsLabel = safeText(item?.dataset?.label || "").trim();
              return !!dsLabel;
            },
            callbacks: {
              title: (items) => {
                if (!Array.isArray(items) || !items.length) return "";
                return safeText(items[0]?.label || "");
              },
              label: (ctx) => buildLineTooltipLabel(elId, ctx?.parsed?.y),
            },
          },
        },
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { ticks: { font: { size: 9 } } },
          y: {
            ticks: { font: { size: 9 } },
            beginAtZero: yMin !== null,
            min: yMin !== null ? yMin : undefined,
            max: yMax !== null ? yMax : undefined,
          },
        },
      },
    });

    _charts.push(c);
    return c;
  }

  function createBarChart(elId, labels, datasets, yMin = null, yMax = null) {
    const el = document.getElementById(elId);
    if (!el) return null;

    const c = new Chart(el.getContext("2d"), {
      type: "bar",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: true }, tooltip: { mode: "index", intersect: false } },
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { stacked: false, ticks: { font: { size: 9 } } },
          y: {
            stacked: false,
            ticks: { font: { size: 9 } },
            beginAtZero: yMin !== null,
            min: yMin !== null ? yMin : undefined,
            max: yMax !== null ? yMax : undefined,
          },
        },
      },
    });

    _charts.push(c);
    return c;
  }

  function buildCharts(calendarData) {
    destroyCharts();

    const days = Array.isArray(calendarData?.Days) ? calendarData.Days : [];
    const labels = days.map((d) => fmtDate(d));
    const axis = labels.length ? labels : ["T2", "T3", "T4", "T5", "T6", "T7", "CN"];
    _chartAxisLabels = axis.slice();
    const len = axis.length;

    const emptySeries = Array.from({ length: len }, () => null);

    const chartProductionQty = createLineChart("chart1", axis, addForecastBand([
      { label: "Tổng số lượng sản xuất", data: emptySeries.slice(), borderColor: "#006eb4", backgroundColor: "#006eb4", borderWidth: 2, tension: 0.25, pointRadius: 2 },
    ], 0, 0, null), 0, null);
    _chartProductionQty = chartProductionQty;

    const chartHuman = createLineChart("chart2", axis, addForecastBand([
      { label: "Năng xuất hoạt động của nhân sự", data: emptySeries.slice(), borderColor: "#f97316", backgroundColor: "#f97316", borderWidth: 3, tension: 0.25, pointRadius: 2 },
    ], 0, 0, 100, getBandOptionsForLineLabel("Năng xuất hoạt động của nhân sự")), 0, 100);
    _chartHumanPerf = chartHuman;

    const chartMachine = createLineChart("chart3", axis, addForecastBand([
      { label: "Năng xuất hoạt động của máy móc", data: emptySeries.slice(), borderColor: "#22c55e", backgroundColor: "#22c55e", borderWidth: 2, tension: 0.25, pointRadius: 2 },
    ], 0, 0, 100, getBandOptionsForLineLabel("Năng xuất hoạt động của máy móc")), 0, 100);
    _chartMachinePerf = chartMachine;

    const chartAvailability = createLineChart("chart6", axis, addForecastBand([
      { label: "Nguồn lực khả dụng", data: emptySeries.slice(), borderColor: "#a855f7", backgroundColor: "#a855f7", borderWidth: 2, tension: 0.25, pointRadius: 2 },
    ], 0, 0, 100, getBandOptionsForLineLabel("Nguồn lực khả dụng")), 0, 100);
    _chartAvailability = chartAvailability;

    const chartDefectQty = createLineChart("chart4", axis, addForecastBand([
      { label: "Số lượng phế phẩm", data: emptySeries.slice(), borderColor: "#ef4444", backgroundColor: "#ef4444", borderWidth: 2, tension: 0.25, pointRadius: 2 },
    ], 0, 0, null), 0, null);
    _chartDefectQty = chartDefectQty;

    const chartWarehouse = createBarChart("chart5", axis, [
      { label: "Số lượng nhập", data: emptySeries.slice(), backgroundColor: "#3b82f6", borderColor: "#3b82f6", borderWidth: 1 },
      { label: "Số lượng xuất", data: emptySeries.slice(), backgroundColor: "#ef4444", borderColor: "#ef4444", borderWidth: 1 },
    ], 0, null);
    _chartWarehouse = chartWarehouse;
  }

  async function hydrateHumanPerfChart(chart, axisLabels) {
    let overlayShown = false;
    try {
      if (!chart || !planId) return;
      // If axis isn't dates (fallback), we can't align -> keep empty.
      const looksLikeDates = Array.isArray(axisLabels) && axisLabels.length && /^\d{4}-\d{2}-\d{2}$/.test(String(axisLabels[0] || ""));
      if (!looksLikeDates) return;

      // HR forecast may take a few seconds on first load (model warm-up).
      setOverlayVisible(true, "Đang chạy mô hình AI dự báo nhân sự...");
      overlayShown = true;

      const res = await fetch(`/api/v1/kehoach/${planId}/hr-forecast`);
      const data = await parseMaybeJson(res);
      if (!res.ok) throw new Error(data?.detail || data?._rawText || "Không thể tải dự báo nhân sự");

      const days = Array.isArray(data?.days) ? data.days : [];
      const vals = Array.isArray(data?.avg_actual_staff_pct) ? data.avg_actual_staff_pct : [];
      const byDay = {};
      for (let i = 0; i < Math.min(days.length, vals.length); i++) {
        const k = String(days[i] || "").slice(0, 10);
        const v = vals[i];
        const num = (v === null || v === undefined || v === "") ? null : Number(v);
        byDay[k] = Number.isFinite(num) ? Math.max(0, Math.min(100, num)) : null;
      }

      const aligned = axisLabels.map((d) => {
        const k = String(d || "").slice(0, 10);
        return Object.prototype.hasOwnProperty.call(byDay, k) ? byDay[k] : null;
      });

      // If the backend returns no usable points, keep the demo series.
      const hasAnyPoint = aligned.some((x) => x !== null && x !== undefined && Number.isFinite(Number(x)));
      if (!hasAnyPoint) return;

      // `addForecastBand` returns [lowerBand, upperBand, mainLine, ...]
      // We want the orange line (mainLine) to follow real values.
      const dsets = Array.isArray(chart.data?.datasets) ? chart.data.datasets : [];

      let mainIdx = dsets.findIndex((ds) => (ds && typeof ds.label === "string" && ds.label === "Năng xuất hoạt động của nhân sự"));
      if (mainIdx < 0 && dsets.length >= 3 && dsets[2] && dsets[2].label === "Năng xuất hoạt động của nhân sự") mainIdx = 2;
      if (mainIdx < 0) mainIdx = Math.max(0, dsets.length - 1);

      dsets[mainIdx].data = aligned;

      // Keep forecast band consistent with the updated main line.
      // Default y range for this chart is 0..100.
      if (dsets.length >= 3 && dsets[0] && dsets[1] && dsets[0].label === "" && dsets[1].label === "") {
        const band = buildForecastBand(aligned, 0, 100);
        dsets[0].data = band.lower;
        dsets[1].data = band.upper;
      }

      chart.update();
    } catch (err) {
      // Keep demo series; surface error in the hint area.
      try {
        const hint = document.getElementById("errorHint");
        if (hint) {
          const msg = (err && err.message) ? err.message : String(err || "Lỗi dự báo nhân sự");
          hint.style.display = "block";
          hint.textContent = `Không thể tải dự báo nhân sự từ mô hình AI: ${msg}`;
        }
      } catch { /* ignore */ }
    } finally {
      if (overlayShown) setOverlayVisible(false);
    }
  }

  async function hydrateMachinePerfChart(chart, axisLabels) {
    let overlayShown = false;
    try {
      if (!chart || !planId) return;
      const looksLikeDates = Array.isArray(axisLabels) && axisLabels.length && /^\d{4}-\d{2}-\d{2}$/.test(String(axisLabels[0] || ""));
      if (!looksLikeDates) return;

      setOverlayVisible(true, "Đang chạy mô hình AI dự báo máy móc...");
      overlayShown = true;

      const res = await fetch(`/api/v1/kehoach/${planId}/machine-forecast`);
      const data = await parseMaybeJson(res);
      if (!res.ok) throw new Error(data?.detail || data?._rawText || "Không thể tải dự báo máy móc");

      const days = Array.isArray(data?.days) ? data.days : [];
      const vals = Array.isArray(data?.avg_machine_productivity_pct) ? data.avg_machine_productivity_pct : [];
      const byDay = {};
      for (let i = 0; i < Math.min(days.length, vals.length); i++) {
        const k = String(days[i] || "").slice(0, 10);
        const v = vals[i];
        const num = (v === null || v === undefined || v === "") ? null : Number(v);
        byDay[k] = Number.isFinite(num) ? Math.max(0, Math.min(100, num)) : null;
      }

      const aligned = axisLabels.map((d) => {
        const k = String(d || "").slice(0, 10);
        return Object.prototype.hasOwnProperty.call(byDay, k) ? byDay[k] : null;
      });

      const hasAnyPoint = aligned.some((x) => x !== null && x !== undefined && Number.isFinite(Number(x)));
      if (!hasAnyPoint) return;

      const dsets = Array.isArray(chart.data?.datasets) ? chart.data.datasets : [];
      let mainIdx = dsets.findIndex((ds) => (ds && typeof ds.label === "string" && ds.label === "Năng xuất hoạt động của máy móc"));
      if (mainIdx < 0 && dsets.length >= 3 && dsets[2] && dsets[2].label === "Năng xuất hoạt động của máy móc") mainIdx = 2;
      if (mainIdx < 0) mainIdx = Math.max(0, dsets.length - 1);

      dsets[mainIdx].data = aligned;

      if (dsets.length >= 3 && dsets[0] && dsets[1] && dsets[0].label === "" && dsets[1].label === "") {
        const band = buildForecastBand(aligned, 0, 100);
        dsets[0].data = band.lower;
        dsets[1].data = band.upper;
      }

      chart.update();
    } catch (err) {
      try {
        const hint = document.getElementById("errorHint");
        if (hint) {
          const msg = (err && err.message) ? err.message : String(err || "Lỗi dự báo máy móc");
          hint.style.display = "block";
          hint.textContent = `Không thể tải dự báo máy móc từ mô hình AI: ${msg}`;
        }
      } catch { /* ignore */ }
    } finally {
      if (overlayShown) setOverlayVisible(false);
    }
  }

  function alignSeriesByDay(days, values, axisLabels, minV, maxV) {
    const byDay = {};
    const dArr = Array.isArray(days) ? days : [];
    const vArr = Array.isArray(values) ? values : [];

    for (let i = 0; i < Math.min(dArr.length, vArr.length); i++) {
      const k = String(dArr[i] || "").slice(0, 10);
      const raw = vArr[i];
      const num = (raw === null || raw === undefined || raw === "") ? null : Number(raw);
      if (Number.isFinite(num)) {
        byDay[k] = Math.max(minV, Math.min(maxV, num));
      } else {
        byDay[k] = null;
      }
    }

    const axis = Array.isArray(axisLabels) ? axisLabels : [];
    return axis.map((d) => {
      const k = String(d || "").slice(0, 10);
      return Object.prototype.hasOwnProperty.call(byDay, k) ? byDay[k] : null;
    });
  }

  function applySeriesToForecastChart(chart, axisLabels, lineLabel, series, minV, maxV) {
    if (!chart || !Array.isArray(series)) return;

    const hasAnyPoint = series.some((x) => x !== null && x !== undefined && Number.isFinite(Number(x)));
    if (!hasAnyPoint) return;

    const dsets = Array.isArray(chart.data?.datasets) ? chart.data.datasets : [];
    let mainIdx = dsets.findIndex((ds) => (ds && typeof ds.label === "string" && ds.label === lineLabel));
    if (mainIdx < 0 && dsets.length >= 3 && dsets[2] && dsets[2].label === lineLabel) mainIdx = 2;
    if (mainIdx < 0) mainIdx = Math.max(0, dsets.length - 1);

    dsets[mainIdx].data = series;

    if (dsets.length >= 3 && dsets[0] && dsets[1] && dsets[0].label === "" && dsets[1].label === "") {
      const bandOpts = getBandOptionsForLineLabel(lineLabel);
      const band = buildForecastBand(series, minV, maxV, bandOpts.pct, bandOpts.minDelta);
      dsets[0].data = band.lower;
      dsets[1].data = band.upper;
    }

    try {
      if (chart.options && chart.options.scales && chart.options.scales.y) {
        chart.options.scales.y.beginAtZero = minV !== null;
        chart.options.scales.y.min = minV !== null ? minV : undefined;
        chart.options.scales.y.max = maxV !== null ? maxV : undefined;
      }
    } catch { /* ignore */ }

    chart.update();
  }

  function applySeriesToWarehouseBarChart(chart, axisLabels, importSeries, exportSeries) {
    if (!chart) return;
    const labels = Array.isArray(axisLabels) ? axisLabels : [];
    const imp = Array.isArray(importSeries) ? importSeries : [];
    const exp = Array.isArray(exportSeries) ? exportSeries : [];

    const hasAny = [...imp, ...exp].some((x) => x !== null && x !== undefined && Number.isFinite(Number(x)));
    if (!hasAny) return;

    chart.data.labels = labels;
    const dsets = Array.isArray(chart.data?.datasets) ? chart.data.datasets : [];

    let importIdx = dsets.findIndex((ds) => ds && ds.label === "Số lượng nhập");
    let exportIdx = dsets.findIndex((ds) => ds && ds.label === "Số lượng xuất");

    if (importIdx < 0) {
      dsets.push({ label: "Số lượng nhập", data: imp, backgroundColor: "#3b82f6", borderColor: "#3b82f6", borderWidth: 1 });
      importIdx = dsets.length - 1;
    }
    if (exportIdx < 0) {
      dsets.push({ label: "Số lượng xuất", data: exp, backgroundColor: "#ef4444", borderColor: "#ef4444", borderWidth: 1 });
      exportIdx = dsets.length - 1;
    }

    dsets[importIdx].data = imp;
    dsets[exportIdx].data = exp;
    chart.update();
  }

  function resolveSeriesDays(resp, sectionObj) {
    const topDays = Array.isArray(resp?.days) ? resp.days : [];
    if (topDays.length) return topDays;
    const secDays = Array.isArray(sectionObj?.days) ? sectionObj.days : [];
    return secDays;
  }

  function hydrateChartsFromAiResponse(aiResp) {
    const axis = Array.isArray(_chartAxisLabels) ? _chartAxisLabels : [];
    const looksLikeDates = axis.length && /^\d{4}-\d{2}-\d{2}$/.test(String(axis[0] || ""));
    if (!looksLikeDates) return;

    try {
      const m01 = aiResp?.m01_forecast || {};
      const m01Days = resolveSeriesDays(aiResp, m01);
      const actualQtySeries = alignSeriesByDay(m01Days, m01?.total_actual_qty, axis, 0, Number.MAX_SAFE_INTEGER);
      applySeriesToForecastChart(_chartProductionQty, axis, "Tổng số lượng sản xuất", actualQtySeries, 0, null);
    } catch { /* ignore */ }

    try {
      const m01 = aiResp?.m01_forecast || {};
      const m01Days = resolveSeriesDays(aiResp, m01);
      const defectQtySeries = alignSeriesByDay(m01Days, m01?.total_defect_qty, axis, 0, Number.MAX_SAFE_INTEGER);
      applySeriesToForecastChart(_chartDefectQty, axis, "Số lượng phế phẩm", defectQtySeries, 0, null);
    } catch { /* ignore */ }

    try {
      const hr = aiResp?.hr_forecast || {};
      const hrDays = resolveSeriesDays(aiResp, hr);
      const hrSeries = alignSeriesByDay(hrDays, hr?.avg_actual_staff_pct, axis, 0, 100);
      applySeriesToForecastChart(_chartHumanPerf, axis, "Năng xuất hoạt động của nhân sự", hrSeries, 0, 100);
    } catch { /* ignore */ }

    try {
      const mc = aiResp?.machine_forecast || {};
      const mcDays = resolveSeriesDays(aiResp, mc);
      const machineSeries = alignSeriesByDay(mcDays, mc?.avg_machine_productivity_pct, axis, 0, 100);
      applySeriesToForecastChart(_chartMachinePerf, axis, "Năng xuất hoạt động của máy móc", machineSeries, 0, 100);
    } catch { /* ignore */ }

    try {
      const hr = aiResp?.hr_forecast || {};
      const mc = aiResp?.machine_forecast || {};
      const ar = aiResp?.available_resources_forecast || {};
      const commonDays = resolveSeriesDays(aiResp, hr);

      const hrSeries = alignSeriesByDay(commonDays, hr?.avg_actual_staff_pct, axis, 0, 100);
      const machineSeries = alignSeriesByDay(commonDays, mc?.avg_machine_productivity_pct, axis, 0, 100);

      let availabilitySeries = alignSeriesByDay(resolveSeriesDays(aiResp, ar), ar?.available_resources_pct, axis, 0, 100);
      const hasBackendAvailability = availabilitySeries.some((x) => x !== null && x !== undefined && Number.isFinite(Number(x)));
      if (!hasBackendAvailability) {
        availabilitySeries = axis.map((_, i) => {
          const hrVal = hrSeries?.[i];
          const machineVal = machineSeries?.[i];
          const hrNum = Number(hrVal);
          const machineNum = Number(machineVal);
          if (!Number.isFinite(hrNum) || !Number.isFinite(machineNum)) return null;
          return Math.max(0, Math.min(100, (hrNum * (100 - machineNum)) / 100));
        });
      }

      applySeriesToForecastChart(_chartAvailability, axis, "Nguồn lực khả dụng", availabilitySeries, 0, 100);
    } catch { /* ignore */ }

    try {
      const wh = aiResp?.warehouse_forecast || {};
      const whDays = resolveSeriesDays(aiResp, wh);
      const importSeries = alignSeriesByDay(whDays, wh?.total_import_qty, axis, 0, Number.MAX_SAFE_INTEGER);
      const exportSeries = alignSeriesByDay(whDays, wh?.total_export_qty, axis, 0, Number.MAX_SAFE_INTEGER);
      applySeriesToWarehouseBarChart(_chartWarehouse, axis, importSeries, exportSeries);
    } catch { /* ignore */ }
  }

  async function hydrateChartsFromModelForecastApi() {
    try {
      if (!planId) return;
      const axis = Array.isArray(_chartAxisLabels) ? _chartAxisLabels : [];
      const looksLikeDates = axis.length && /^\d{4}-\d{2}-\d{2}$/.test(String(axis[0] || ""));
      if (!looksLikeDates) return;

      const res = await fetch(`/api/v1/kehoach/${planId}/model-forecast`);
      const data = await parseMaybeJson(res);
      if (!res.ok) throw new Error(data?.detail || data?._rawText || "Không thể tải dữ liệu model-forecast");

      const hr = data?.hr_forecast || {};
      const hrDays = resolveSeriesDays(data, hr);
      const hrSeries = alignSeriesByDay(hrDays, hr?.avg_actual_staff_pct, axis, 0, 100);
      applySeriesToForecastChart(_chartHumanPerf, axis, "Năng xuất hoạt động của nhân sự", hrSeries, 0, 100);

      const m01 = data?.m01_forecast || {};
      const m01Days = resolveSeriesDays(data, m01);
      const actualQtySeries = alignSeriesByDay(m01Days, m01?.total_actual_qty, axis, 0, Number.MAX_SAFE_INTEGER);
      applySeriesToForecastChart(_chartProductionQty, axis, "Tổng số lượng sản xuất", actualQtySeries, 0, null);

      const defectQtySeries = alignSeriesByDay(m01Days, m01?.total_defect_qty, axis, 0, Number.MAX_SAFE_INTEGER);
      applySeriesToForecastChart(_chartDefectQty, axis, "Số lượng phế phẩm", defectQtySeries, 0, null);

      const mc = data?.machine_forecast || {};
      const mcDays = resolveSeriesDays(data, mc);
      const machineSeries = alignSeriesByDay(mcDays, mc?.avg_machine_productivity_pct, axis, 0, 100);
      applySeriesToForecastChart(_chartMachinePerf, axis, "Năng xuất hoạt động của máy móc", machineSeries, 0, 100);

      const ar = data?.available_resources_forecast || {};
      let availabilitySeries = alignSeriesByDay(resolveSeriesDays(data, ar), ar?.available_resources_pct, axis, 0, 100);
      const hasBackendAvailability = availabilitySeries.some((x) => x !== null && x !== undefined && Number.isFinite(Number(x)));

      if (!hasBackendAvailability) {
        availabilitySeries = axis.map((_, i) => {
          const hrVal = hrSeries?.[i];
          const machineVal = machineSeries?.[i];
          const hrNum = Number(hrVal);
          const machineNum = Number(machineVal);
          if (!Number.isFinite(hrNum) || !Number.isFinite(machineNum)) return null;
          return Math.max(0, Math.min(100, (hrNum * (100 - machineNum)) / 100));
        });
      }
      applySeriesToForecastChart(_chartAvailability, axis, "Nguồn lực khả dụng", availabilitySeries, 0, 100);

      const wh = data?.warehouse_forecast || {};
      const whDays = resolveSeriesDays(data, wh);
      const importSeries = alignSeriesByDay(whDays, wh?.total_import_qty, axis, 0, Number.MAX_SAFE_INTEGER);
      const exportSeries = alignSeriesByDay(whDays, wh?.total_export_qty, axis, 0, Number.MAX_SAFE_INTEGER);
      applySeriesToWarehouseBarChart(_chartWarehouse, axis, importSeries, exportSeries);
    } catch {
      // Keep demo data when forecast suite is unavailable.
    }
  }

  // -------- Payload --------
  function buildAnalysisInput(calendarData) {
    const header = calendarData?.Header || {};
    const days = Array.isArray(calendarData?.Days) ? calendarData.Days : [];
    const rows = Array.isArray(calendarData?.Rows) ? calendarData.Rows : [];

    const slimRows = rows.map((r) => ({
      DonHangID: r.DonHangID,
      // Backward compat: backend prefers SoChungTu; older payloads used SoDonHang
      SoChungTu: r.SoChungTu || r.SoDonHang,
      SoDonHang: r.SoDonHang || r.SoChungTu,
      KhachHang: r.KhachHang,
      LineKey: r.LineKey,
      TP_DinhMucID: r.TP_DinhMucID,
      MaTP: r.MaTP,
      TenThanhPham: r.TenThanhPham,
      BTP_DinhMucID: r.BTP_DinhMucID,
      MaBTP: r.MaBTP,
      TenBanThanhPham: r.TenBanThanhPham,
      ThuTuSX: r.ThuTuSX,
      MaCongDoan: r.MaCongDoan,
      TenCongDoan: r.TenCongDoan,
      MaCongDoanLon: r.MaCongDoanLon,
      TenBoPhan: r.TenBoPhan,
      SoNhanSuBoPhan: r.SoNhanSuBoPhan,
      LoaiNguonLuc: r.LoaiNguonLuc,
      NguonLucText: r.NguonLucText,
      MaNguonLuc: r.MaNguonLuc,
      TenNguonLuc: r.TenNguonLuc,
      SoLuongNguonLuc: r.SoLuongNguonLuc,
      DinhMucThoiGian: r.DinhMucThoiGian,
      ThoiGianThietLapMay: r.ThoiGianThietLapMay,
      NangLucSanXuat: r.NangLucSanXuat,
      NangLucTangCa: r.NangLucTangCa,
      CapacityPerDay: r.CapacityPerDay,
      OvertimeCapacityPerDay: r.OvertimeCapacityPerDay,
      PlanWindowStart: r.PlanWindowStart,
      PlanWindowEnd: r.PlanWindowEnd,
      DueDT: r.DueDT,
      planned_qty: r.planned_qty || r.PlannedQty || r.plannedQty || r.PlannedTotal || r.TotalQty || r.SoLuongKeHoach,
      TotalQty: r.TotalQty,
      PlannedTotal: r.PlannedTotal,
      LateQty: r.LateQty,
      UnplannedQty: r.UnplannedQty,
      OvertimeQty: r.OvertimeQty,
      OverCapacityQty: r.OverCapacityQty,
      IsBlockedByPredecessor: r.IsBlockedByPredecessor,
      BlockedBy: r.BlockedBy,
      BlockedReason: r.BlockedReason,
      DailyQty: r.DailyQty,
      transaction_type: r.transaction_type || r.TransactionType || r.LoaiGiaoDich || r.LoaiNhapXuat,
      warehouse_id: r.warehouse_id || r.WarehouseID || r.MaKho || r.Kho || r.KhoID,
      item_id: r.item_id || r.ItemID || r.MaMatHang || r.MaHang || r.MaBTP || r.MaTP || r.BTP_DinhMucID || r.TP_DinhMucID,
      opening_stock: r.opening_stock || r.OpeningStock || r.TonDauKy,
    }));

    return { header, days, rows: slimRows };
  }

  // -------- API --------
  async function callAiAnalysis(payload, { signal } = {}) {
    const res = await fetch(`/api/v1/kehoach/ai-analysis-payload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    });

    const data = await parseMaybeJson(res);
    if (!res.ok) {
      const msg = safeText(data?.detail || data?.message || data?._rawText || `HTTP ${res.status}`);
      throw new Error(msg);
    }
    return data;
  }

  function buildForecastPayloadWithWarehouseSamples(payload) {
    if (!payload || typeof payload !== "object") return payload;

    const calendarKey = payload.calendar
      ? "calendar"
      : (payload.systemData ? "systemData" : (payload.system_data ? "system_data" : null));

    if (!calendarKey) return payload;

    const srcCalendar = payload[calendarKey];
    if (!srcCalendar || typeof srcCalendar !== "object") return payload;

    const hasUpperRows = Array.isArray(srcCalendar?.Rows);
    const hasLowerRows = Array.isArray(srcCalendar?.rows);
    const rowKey = hasUpperRows ? "Rows" : (hasLowerRows ? "rows" : null);
    if (!rowKey) return payload;

    const srcRows = srcCalendar[rowKey];

    let sampleWarehouseId = "WH_SAMPLE";

    for (const row of srcRows) {
      if (!row || typeof row !== "object") continue;

      const wh = row.warehouse_id || row.WarehouseID || row.MaKho || row.Kho || row.KhoID;
      if (!sampleWarehouseId || sampleWarehouseId === "WH_SAMPLE") {
        if (wh !== null && wh !== undefined && String(wh).trim() !== "") {
          sampleWarehouseId = String(wh).trim();
        }
      }

    }

    const missingTxIndexes = [];
    for (let i = 0; i < srcRows.length; i++) {
      const row = srcRows[i];
      if (!row || typeof row !== "object") continue;
      const tx = row.transaction_type || row.TransactionType || row.LoaiGiaoDich || row.LoaiNhapXuat;
      if (tx === null || tx === undefined || String(tx).trim() === "") {
        missingTxIndexes.push(i);
      }
    }

    const txSamplePool = [];
    const missCount = missingTxIndexes.length;
    const importCount = Math.ceil(missCount / 2);
    const exportCount = Math.floor(missCount / 2);
    for (let i = 0; i < importCount; i++) txSamplePool.push("IMPORT");
    for (let i = 0; i < exportCount; i++) txSamplePool.push("EXPORT");

    for (let i = txSamplePool.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      const t = txSamplePool[i];
      txSamplePool[i] = txSamplePool[j];
      txSamplePool[j] = t;
    }

    const missingTxMap = new Map();
    missingTxIndexes.forEach((rowIdx, i) => {
      missingTxMap.set(rowIdx, txSamplePool[i] || "IMPORT");
    });

    const patchedRows = srcRows.map((row, idx) => {
      if (!row || typeof row !== "object") return row;

      const tx = row.transaction_type || row.TransactionType || row.LoaiGiaoDich || row.LoaiNhapXuat;
      const wh = row.warehouse_id || row.WarehouseID || row.MaKho || row.Kho || row.KhoID;

      const process = row.process || row.Process || row.TenCongDoan || row.PhaseName || row.MaCongDoan || null;
      const majorProcess = row.major_process || row.TenBoPhan || row.TenCongDoanLon || row.PhaseGroupName || row.MaCongDoanLon || null;
      const resource = row.resource || row.TenNguonLuc || row.ResourceNames || row.NguonLucText || row.MaNguonLuc || row.ResourceIDs || null;
      const itemCode = row.item_code || row.ItemCode || row.item_id || row.ItemID || row.MaBTP || row.MaTP || row.BTP_DinhMucID || row.TP_DinhMucID || null;
      const itemName = row.item_name || row.ItemName || row.TenBanThanhPham || row.SemiFinishedProductName || row.TenThanhPham || row.FinishedProductName || null;

      const productionOrderId = row.production_order_id || row.DonHangID || row.LineKey || null;
      const poNumber = row.po_number || row.SoChungTu || row.SoDonHang || productionOrderId || null;
      const productionPriority = row.production_priority || row.ThuTuSX || null;
      const standardStaffQty = row.standard_staff_qty || row.SoNhanSuBoPhan || row.SoNhanSu || row.staff || null;
      const timeNorm = row.time_norm || row.DinhMucThoiGian || row.TimeNorm || row.TimeLimit || row.ThoiGianThietLapMay || null;

      return {
        ...row,
        transaction_type: (tx !== null && tx !== undefined && String(tx).trim() !== "") ? tx : (missingTxMap.get(idx) || "IMPORT"),
        warehouse_id: (wh !== null && wh !== undefined && String(wh).trim() !== "") ? wh : sampleWarehouseId,
        process,
        major_process: majorProcess,
        resource,
        item_code: itemCode,
        item_name: itemName,
        production_order_id: productionOrderId,
        po_number: poNumber,
        production_priority: productionPriority,
        standard_staff_qty: standardStaffQty,
        time_norm: timeNorm,
      };
    });

    return {
      ...payload,
      [calendarKey]: {
        ...(srcCalendar || {}),
        [rowKey]: patchedRows,
      },
    };
  }

  async function callModelForecastPayload(payload, { signal } = {}) {
    const payloadForForecast = buildForecastPayloadWithWarehouseSamples(payload);

    const res = await fetch(`/api/v1/kehoach/model-forecast-payload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payloadForForecast),
      signal,
    });

    const data = await parseMaybeJson(res);
    if (!res.ok) {
      const msg = safeText(data?.detail || data?.message || data?._rawText || `HTTP ${res.status}`);
      throw new Error(msg);
    }
    return data;
  }

  async function callAiApply(moves) {
    const res = await fetch(`/api/v1/kehoach/${planId}/ai-apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ moves: Array.isArray(moves) ? moves : [] }),
    });

    const data = await parseMaybeJson(res);
    if (!res.ok) {
      const msg = safeText(data?.detail?.message || data?.detail || data?.message || data?._rawText || `HTTP ${res.status}`);
      const err = new Error(msg);
      err._detail = data?.detail;
      throw err;
    }
    return data;
  }

  // =========================
  // UI v2 (tối giản + hiện đại)
  // =========================
  function setSuggestCount(n) {
    if (!aiSuggestCount) return;
    const num = Number(n || 0);
    aiSuggestCount.textContent = `${num} phương án`;
  }

  function setOverviewQuality(aiResp) {
    if (!aiOverviewQuality) return;

    const q = aiResp?.llm?.parsed?.quality ?? aiResp?.llm?.quality ?? aiResp?.quality ?? null;
    if (q === null || q === undefined || q === "") {
      aiOverviewQuality.textContent = "Độ tin cậy: --";
      return;
    }

    const qn = Number(q);
    if (!Number.isFinite(qn)) {
      aiOverviewQuality.textContent = `Độ tin cậy: ${safeText(q)}`;
      return;
    }

    const pct = qn <= 1 ? Math.round(qn * 100) : Math.round(qn);
    aiOverviewQuality.textContent = `Độ tin cậy: ${pct}%`;
  }

  function extractMachineCodes(text) {
    const t = safeText(text);
    const matches = t.match(/\bM\d{2,4}\b/gi) || [];
    const seen = new Set();
    const uniq = [];
    for (const m of matches) {
      const mm = m.toUpperCase();
      if (!seen.has(mm)) { seen.add(mm); uniq.push(mm); }
    }
    return uniq.slice(0, 10);
  }

  // Gom toàn bộ nội dung phân tích vào 1 ô text duy nhất
  function analysisToMinimalHtml(text) {
    const stripLeadingOrdinal = (s) => safeText(s || "").replace(/^\s*\d+\s*[\.)-]\s*/g, "").trim();
    const isStdHeading = (s) => /^nội\s+dung\s+phân\s+tích\s+kế\s+hoạch\s+sản\s+xuất\s*:?.*$/i.test(String(s || "").trim());

    const raw = safeText(text || "").trim();
    if (!raw) return "";

    const lines = raw
      .replace(/\r/g, "")
      .replace(/\s+•\s+/g, "\n• ")
      .replace(/\s+-\s+/g, "\n- ")
      .split(/\n+/)
      .map((ln) => stripLeadingOrdinal(ln).replace(/^[\-•]\s*/, "").trim())
      .filter((ln) => ln && !isStdHeading(ln));

    const merged = lines.length ? lines.join("\n") : stripLeadingOrdinal(raw);
    return `<div class="ai-analysis-textbox">${escapeHtml(merged)}</div>`;
  }

  function normalizePriorityText(s) {
    const t = safeText(s).toLowerCase();
    if (!t) return "Trung bình";
    if (t.includes("cao") || t.includes("high") || t.includes("p1")) return "Cao";
    if (t.includes("trung") || t.includes("medium") || t.includes("p2")) return "Trung bình";
    if (t.includes("thấp") || t.includes("low") || t.includes("p3")) return "Thấp";
    return safeText(s);
  }

  function priorityClassFromText(s) {
    const t = safeText(s).toLowerCase();
    if (t.includes("cao") || t.includes("high") || t.includes("p1")) return "ai-badge ai-badge--p1";
    if (t.includes("trung") || t.includes("medium") || t.includes("p2")) return "ai-badge ai-badge--p2";
    if (t.includes("thấp") || t.includes("low") || t.includes("p3")) return "ai-badge ai-badge--p3";
    return "ai-badge ai-badge--p2";
  }

  function buildMovesHtml(moves) {
    const arr = Array.isArray(moves) ? moves : [];
    if (!arr.length) return "";

    const items = arr.slice(0, 6).map((mv) => {
      // Hiển thị mô tả thân thiện, ưu tiên tên thay vì mã
      let desc = safeText(mv?.desc || mv?.description || mv?.note || mv?.reason || "").trim();
      // Nếu desc là mã máy, mã công đoạn, mã nguồn lực, mã bán thành phẩm/thành phẩm thì chuyển sang tên
      if (/^M\d{2,4}$/.test(desc) && mv.machine_name) desc = mv.machine_name;
      if (/^CD\d{2,4}$/.test(desc) && mv.step_name) desc = mv.step_name;
      if (/^NL\d{2,4}$/.test(desc) && mv.resource_name) desc = mv.resource_name;
      if (/^BTP\d{2,4}$/.test(desc) && mv.btp_name) desc = mv.btp_name;
      if (/^TP\d{2,4}$/.test(desc) && mv.tp_name) desc = mv.tp_name;
      // Hiển thị meta rõ ràng
      const meta = [
        mv.machine_name ? `Máy: ${escapeHtml(mv.machine_name)}` : "",
        mv.step_name ? `Công đoạn: ${escapeHtml(mv.step_name)}` : "",
        mv.resource_name ? `Nguồn lực: ${escapeHtml(mv.resource_name)}` : "",
        mv.btp_name ? `BTP: ${escapeHtml(mv.btp_name)}` : "",
        mv.tp_name ? `TP: ${escapeHtml(mv.tp_name)}` : ""
      ].filter(Boolean).join(" • ");
      return `<li><div>${escapeHtml(desc)}</div>${meta ? `<div class=\"ai-submeta\">${meta}</div>` : ""}</li>`;
    }).join("");

    return `
      <details class="ai-details">
        <summary>Điều chỉnh đề xuất</summary>
        <ul class="ai-list ai-list--compact">${items}</ul>
      </details>
    `;
  }

  function buildUpdatesHtml(updates) {
    const arr = Array.isArray(updates) ? updates : [];
    if (!arr.length) return "";

    const cards = arr.slice(0, 8).map((u, i) => {
      const orderNo = safeText(u?.OrderNo || "");
      const btp = safeText(u?.SemiFinishedProductCode || u?.SemiFinishedProductName || "");
      const tp = safeText(u?.FinishedProductCode || u?.FinishedProductName || "");
      const due = fmtDate(u?.DueDate);
      const resource = safeText(u?.ResourceIDs || "");
      const totalQty = fmtNum(u?.TotalQuantity);
      const dq = Array.isArray(u?.DailyQty) ? u.DailyQty : [];

      const dqHtml = dq.length
        ? `<ul class="ai-list ai-list--compact">${dq.slice(0, 10).map((d) => {
            const pdate = fmtDate(d?.ProductionDate);
            const qty = fmtNum(d?.Quantity);
            return `<li>${escapeHtml(pdate)}: ${escapeHtml(qty)}</li>`;
          }).join("")}</ul>`
        : "";

      return `
        <details class="ai-details" ${i === 0 ? "open" : ""}>
          <summary>Cập nhật ${i + 1}${orderNo ? ` • Đơn ${escapeHtml(orderNo)}` : ""}</summary>
          <div class="ai-p">BTP: <b>${escapeHtml(btp)}</b> • TP: <b>${escapeHtml(tp)}</b></div>
          <div class="ai-p">Tổng SL: <b>${escapeHtml(totalQty)}</b> • Hạn: <b>${escapeHtml(due)}</b></div>
          <div class="ai-p">Nguồn lực: <b>${escapeHtml(resource)}</b></div>
          ${dqHtml || `<div class="ai-muted">Chưa có DailyQty.</div>`}
        </details>
      `;
    }).join("");

    return `<div class="ai-updates-wrap">${cards}</div>`;
  }

  function updatesToPlanRows(updates) {
    const out = [];
    const arr = Array.isArray(updates) ? updates : [];
    arr.forEach((u) => {
      const product = safeText(u?.SemiFinishedProductName || u?.SemiFinishedProductCode || u?.FinishedProductName || u?.FinishedProductCode);
      const resource = safeText(u?.ResourceIDs || "");
      const dq = Array.isArray(u?.DailyQty) ? u.DailyQty : [];
      dq.forEach((d) => {
        out.push({
          day: fmtDate(d?.ProductionDate),
          product,
          step: "Cập nhật AI",
          qty: Number(d?.Quantity || 0),
          resource,
        });
      });
    });
    return out;
  }

  function openSuggestionDetailModal(sug, idx) {
    const old = document.getElementById("aiSuggestDetailModal");
    if (old && old.parentNode) old.parentNode.removeChild(old);

    const title = safeText(sug?.title || `Phương án ${idx + 1}`);
    const desc = safeText(sug?.description || "").trim();
    const contentLines = Array.isArray(sug?.content_lines)
      ? sug.content_lines
      : (Array.isArray(sug?.meta?.content_lines) ? sug.meta.content_lines : []);
    const updates = Array.isArray(sug?.updates) ? sug.updates : [];

    const linesHtml = contentLines.length
      ? `<ul style="margin:8px 0 0 18px; padding:0; line-height:1.55;">${contentLines
          .map((ln) => `<li style="margin:4px 0;">${escapeHtml(safeText(ln))}</li>`)
          .join("")}</ul>`
      : (desc ? `<div style="white-space:pre-wrap; line-height:1.55; margin-top:8px;">${escapeHtml(desc)}</div>` : "<div class=\"ai-muted\">Chưa có nội dung gợi ý điều chỉnh.</div>");

    const updatesHtml = updates.length
      ? `<div style="margin-top:12px;"><div style="font-weight:600; margin-bottom:6px;">Dữ liệu cập nhật đề xuất</div>
          ${updates.slice(0, 20).map((u, i) => {
            const orderNo = safeText(u?.OrderNo || "");
            const finishedName = safeText(u?.FinishedProductName || "").trim();
            const semiName = safeText(u?.SemiFinishedProductName || "").trim();

            const orderedFields = [
              "OrderNo",
              "FinishedProductCode",
              "SemiFinishedProductCode",
              "FinishedProductName",
              "SemiFinishedProductName",
              "TotalQuantity",
              "ResourceIDs",
              "ResourceWorker",
              "ResourceManchine",
              "StartDate",
              "EndDate",
              "DueDate",
              "DailyQty",
            ];

            const hiddenFields = new Set(["APK_MT2141", "APK_MT2142"]);

            const fieldLabels = {
              APK_MT2141: "Mã kế hoạch tổng",
              APK_MT2142: "Mã dòng kế hoạch",
              OrderNo: "Số đơn hàng",
              FinishedProductCode: "Mã thành phẩm",
              SemiFinishedProductCode: "Mã bán thành phẩm",
              FinishedProductName: "Tên thành phẩm",
              SemiFinishedProductName: "Tên bán thành phẩm",
              TotalQuantity: "Tổng số lượng",
              ResourceIDs: "Nguồn lực",
              ResourceWorker: "Nhân công",
              ResourceManchine: "Máy móc",
              StartDate: "Ngày bắt đầu",
              EndDate: "Ngày kết thúc",
              DueDate: "Hạn hoàn thành",
              DailyQty: "Ngày kế hoạch cần cập nhật",
            };

            const rendered = new Set();
            const rowHtml = [];
            const toFieldRow = (k, v) => {
              let valHtml = "--";
              if (Array.isArray(v)) {
                if (k === "DailyQty") {
                  valHtml = v.length
                    ? `<table style="border-collapse:collapse; width:max-content; max-width:100%;">
                        <thead>
                          <tr>
                            <th style="text-align:left; font-weight:600; padding:4px 8px; border-bottom:1px solid #e5e7eb;">Ngày</th>
                            <th style="text-align:right; font-weight:600; padding:4px 8px; border-bottom:1px solid #e5e7eb;">Số lượng</th>
                          </tr>
                        </thead>
                        <tbody>
                          ${v.map((d) => {
                            const p = fmtDate(d?.ProductionDate);
                            const q = fmtNum(d?.Quantity) || "0";
                            return `<tr>
                              <td style="padding:4px 8px; border-bottom:1px dashed #eef2f7; white-space:nowrap;">${escapeHtml(p || "--")}</td>
                              <td style="padding:4px 8px; border-bottom:1px dashed #eef2f7; text-align:right; white-space:nowrap;">${escapeHtml(q)}</td>
                            </tr>`;
                          }).join("")}
                        </tbody>
                      </table>`
                    : "--";
                } else {
                  valHtml = `<pre style="margin:0; white-space:pre-wrap;">${escapeHtml(JSON.stringify(v, null, 2))}</pre>`;
                }
              } else if (v && typeof v === "object") {
                valHtml = `<pre style="margin:0; white-space:pre-wrap;">${escapeHtml(JSON.stringify(v, null, 2))}</pre>`;
              } else {
                const lk = String(k || "").toLowerCase();
                const isDateField = lk.includes("date") || lk.includes("dt");
                const isNumericField = k === "TotalQuantity" || k === "ResourceWorker" || k === "ResourceManchine";

                let text = "";
                if (isDateField) {
                  text = fmtDate(v);
                } else if (isNumericField) {
                  text = fmtNum(v);
                } else {
                  text = safeText(v);
                }

                valHtml = escapeHtml(text || "--");
              }
              return `<div style="display:grid; grid-template-columns:180px 1fr; gap:8px; padding:6px 0; border-bottom:1px dashed #e5e7eb;">
                <div style="font-weight:600; color:#334155;">${escapeHtml(fieldLabels[k] || k)}</div>
                <div style="color:#0f172a;">${valHtml}</div>
              </div>`;
            };

            orderedFields.forEach((k) => {
              if (Object.prototype.hasOwnProperty.call(u || {}, k)) {
                rowHtml.push(toFieldRow(k, u[k]));
                rendered.add(k);
              }
            });

            Object.keys(u || {}).forEach((k) => {
              if (rendered.has(k) || hiddenFields.has(k)) return;
              rowHtml.push(toFieldRow(k, u[k]));
            });

            return `
              <details style="border:1px solid #e5e7eb; border-radius:8px; padding:8px 10px; margin-top:8px; background:#fff;">
                <summary style="cursor:pointer; font-weight:700; color:#0f2a54;">Cập nhật ${i + 1}${orderNo ? ` • ${escapeHtml(orderNo)}` : ""}${finishedName ? ` • ${escapeHtml(finishedName)}` : ""}${semiName ? ` • ${escapeHtml(semiName)}` : ""}</summary>
                <div style="margin-top:8px; font-size:13px;">${rowHtml.join("")}</div>
              </details>
            `;
          }).join("")}
        </div>`
      : "";

    const host = document.createElement("div");
    host.id = "aiSuggestDetailModal";
    host.style.cssText = "position:fixed; inset:0; background:rgba(15,23,42,.5); z-index:9999; display:flex; align-items:center; justify-content:center; padding:16px;";
    host.innerHTML = `
      <div style="width:min(760px,96vw); max-height:88vh; overflow:auto; background:#fff; border-radius:12px; box-shadow:0 20px 40px rgba(0,0,0,.2);">
        <div style="display:flex; align-items:center; justify-content:space-between; padding:14px 16px; border-bottom:1px solid #e5e7eb;">
          <div style="font-size:16px; font-weight:700; color:#0f172a;">Chi tiết gợi ý điều chỉnh</div>
          <button type="button" data-close="1" style="border:none; background:#eef2ff; color:#1e3a8a; border-radius:8px; padding:6px 10px; cursor:pointer;">Đóng</button>
        </div>
        <div style="padding:14px 16px;">
          <div style="font-size:15px; font-weight:600; color:#111827;">${escapeHtml(title)}</div>
          ${linesHtml}
          ${updatesHtml}
        </div>
      </div>
    `;

    host.addEventListener("click", (ev) => {
      const t = ev.target;
      if (!t) return;
      if (t === host || t.getAttribute?.("data-close") === "1") {
        if (host.parentNode) host.parentNode.removeChild(host);
      }
    });

    document.body.appendChild(host);
  }

  function renderAiAnalysis(aiResp) {
    if (!aiAnalysisHost) return;

    const analysis =
      aiResp?.overall_analysis?.analysis ||
      aiResp?.analysis ||
      aiResp?.Analysis ||
      aiResp?.llm?.parsed?.analysis ||
      aiResp?.llm?.analysis ||
      aiResp?.llm?.parsed?.Analysis ||
      aiResp?.llm?.Analysis ||
      "";

    setOverviewQuality(aiResp);

    const content = analysisToMinimalHtml(analysis);

    aiAnalysisHost.innerHTML = `
      <div class="ai-block">
        ${content || `<p class="ai-muted">Chưa có phân tích tổng thể từ AI.</p>`}
      </div>
    `;
  }

  function renderAiSuggestions(aiResp) {
    if (!aiSuggestionsHost) return;

    const list = Array.isArray(aiResp?.suggestions) ? aiResp.suggestions : [];
    setSuggestCount(list.length);

    if (!list.length) {
      aiSuggestionsHost.innerHTML = `<div class="ai-empty">Chưa có phương án tối ưu.</div>`;
      return;
    }

    aiSuggestionsHost.innerHTML = list.map((sug, idx) => {
      const title = safeText(sug?.title || `Phương án ${idx + 1}`).trim();
      const desc = safeText(sug?.description || "").trim();
      const meta = sug?.meta || {};

      const contentLines = Array.isArray(sug?.content_lines)
        ? sug.content_lines
        : (Array.isArray(meta?.content_lines) ? meta.content_lines : []);
      const contentLinesHtml = contentLines.length
        ? `<ul class="ai-list ai-list--compact">${contentLines
            .map((ln) => `<li>${escapeHtml(safeText(ln))}</li>`)
            .join("")}</ul>`
        : "";

      const movesArr = Array.isArray(sug?.moves)
        ? sug.moves
        : (Array.isArray(meta?.moves) ? meta.moves : []);
      const canApplyFlag = (sug?.can_apply !== undefined) ? sug.can_apply : meta?.can_apply;
      const canApply = (canApplyFlag === false) ? false : movesArr.length > 0;

      const pr = normalizePriorityText(
        sug?.priority || meta?.priority || meta?.muc_do_uu_tien || meta?.priority_level || ""
      );
      const prCls = priorityClassFromText(pr);

      const movesHtml = buildMovesHtml(meta?.moves);

      return `
        <article class="ai-card" data-index="${idx}">
          <header class="ai-card__head">
            <div class="ai-card__title">
              <div class="ai-kicker">Phương án ${idx + 1}</div>
              <div class="ai-h">${escapeHtml(title)}</div>
            </div>
            <span class="${prCls}">Ưu tiên: ${escapeHtml(pr)}</span>
          </header>

          ${contentLinesHtml || (desc ? `<div class="ai-p">${escapeHtml(desc)}</div>` : "")}

          ${movesHtml || ""}

          <footer class="ai-card__foot">
            <button class="btn-secondary" type="button" data-action="detail" data-idx="${idx}">Chi tiết</button>
            <button class="btn-primary" type="button" data-action="apply" data-idx="${idx}" ${canApply ? "" : "disabled"}>
              ${canApply ? "Áp dụng" : "Áp dụng phương án"}
            </button>
          </footer>
        </article>
      `;
    }).join("");

    // delegate events
    if (!aiSuggestionsHost.__aiHooked) {
      aiSuggestionsHost.__aiHooked = true;

      aiSuggestionsHost.addEventListener("click", (ev) => {
        const btn = ev.target?.closest?.("button");
        if (!btn) return;

        const action = btn.getAttribute("data-action");
        const idx = Number(btn.getAttribute("data-idx"));
        if (!Number.isFinite(idx)) return;

        if (action === "detail") {
          const sug = _lastAiResp?.suggestions?.[idx];
          if (sug) openSuggestionDetailModal(sug, idx);
          return;
        }

        if (action === "apply") {
          applySuggestionPersist(idx);
        }
      });
    }

    // auto select first
    applySuggestionToPlan(0, { silent: true });
  }

  function applySuggestionToPlan(idx, { silent } = {}) {
    if (!_lastAiResp?.suggestions?.length) return;
    const sug = _lastAiResp.suggestions[idx];
    if (!sug) return;

    const key = safeText(sug.key || `PA${idx + 1}`);
    const aiPlans = _lastAiResp?.ai_plans || {};
    const pa = aiPlans?.[key];
    const updatesRows = updatesToPlanRows(sug?.updates);

    // select card
    if (aiSuggestionsHost) {
      const cards = aiSuggestionsHost.querySelectorAll(".ai-card");
      cards.forEach((c) => c.classList.remove("is-selected"));
      const card = aiSuggestionsHost.querySelector(`.ai-card[data-index="${idx}"]`);
      if (card) card.classList.add("is-selected");
      if (!silent && card) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    // backward compat: update plan table if exists
    if (planBadge) planBadge.textContent = key;
    if (planTblSubtitle) planTblSubtitle.textContent = `Đang hiển thị mô phỏng của ${key}.`;
    if (pa?.rows) {
      renderAiPlanRows(pa.rows, pa.note);
    } else if (updatesRows.length) {
      renderAiPlanRows(updatesRows, "Dữ liệu cập nhật từ phương án AI.");
    } else {
      renderAiPlanRows([], "Chưa có dữ liệu cập nhật cho phương án này.");
    }
  }

  async function applySuggestionPersist(idx) {
    if (!_lastAiResp?.suggestions?.length) return;

    const sug = _lastAiResp.suggestions[idx];
    if (!sug) return;

    const key = safeText(sug.key || `PA${idx + 1}`);
    const moves = Array.isArray(sug?.moves) ? sug.moves : (Array.isArray(sug?.meta?.moves) ? sug.meta.moves : []);

    const canApplyFlag = (sug?.can_apply !== undefined) ? sug.can_apply : sug?.meta?.can_apply;
    if (canApplyFlag === false || !Array.isArray(moves) || moves.length === 0) {
      if (errorHint) {
        errorHint.textContent = `Phương án ${key} là gợi ý nghiệp vụ, chưa có thao tác (moves) để áp dụng tự động.`;
        errorHint.style.display = "block";
      }
      return;
    }

    // Keep UI selection behavior
    applySuggestionToPlan(idx, { silent: true });

    if (errorHint) errorHint.style.display = "none";
    setOverlayVisible(true, `Đang áp dụng ${key} vào kế hoạch sản xuất...`);

    try {
      const out = await callAiApply(moves);

      // Plan changed => cached AI analysis may be stale
      clearAiCache();

      // Refresh calendar/charts from response (fallback to re-fetch if needed)
      const cal = out?.calendar;
      if (cal && typeof cal === "object") {
        _lastCalendar = cal;
        buildCharts(cal);
      } else {
        const res = await fetch(`/api/v1/kehoach/${planId}/calendar`);
        const cal2 = await parseMaybeJson(res);
        if (res.ok) {
          _lastCalendar = cal2;
          buildCharts(cal2);
        }
      }

      if (planBadge) planBadge.textContent = key;
      if (planTblSubtitle) planTblSubtitle.textContent = `Đã áp dụng ${key} vào kế hoạch. (Bạn có thể “Phân tích lại” để lấy phương án mới.)`;
    } catch (e) {
      const detail = e?._detail;
      const msg = safeText(e?.message || e) || "Không thể áp dụng phương án";

      if (errorHint) {
        // If backend returns structured validation errors (409)
        const ve = Array.isArray(detail?.validation_errors) ? detail.validation_errors : null;
        if (ve && ve.length) {
          errorHint.textContent = `${msg} (Có ${ve.length} lỗi ràng buộc lịch).`;
        } else {
          errorHint.textContent = msg;
        }
        errorHint.style.display = "block";
      }
    } finally {
      setOverlayVisible(false);
    }
  }

  // -------- Table compat --------
  function renderAiPlanRows(rows, note) {
    if (!planBody) return;

    planBody.innerHTML = "";
    const arr = Array.isArray(rows) ? rows : [];

    if (!arr.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.style.color = "#64748b";
      td.style.fontSize = "12px";
      td.textContent = note || "Chưa có bảng kế hoạch AI cho phương án này.";
      tr.appendChild(td);
      planBody.appendChild(tr);
      return;
    }

    arr.forEach((r) => {
      const tr = document.createElement("tr");

      const c1 = document.createElement("td");
      c1.textContent = safeText(r.day || r.Ngay || "");

      const c2 = document.createElement("td");
      c2.textContent = safeText(r.product || r.SanPham || "");

      const c3 = document.createElement("td");
      c3.textContent = safeText(r.step || r.CongDoan || "");

      const c4 = document.createElement("td");
      c4.style.textAlign = "right";
      c4.textContent = Number(r.qty || r.SoLuong || 0) ? fmtNum(r.qty || r.SoLuong) : "";

      const c5 = document.createElement("td");
      c5.textContent = safeText(r.resource || r.NguonLuc || "");

      tr.appendChild(c1);
      tr.appendChild(c2);
      tr.appendChild(c3);
      tr.appendChild(c4);
      tr.appendChild(c5);

      planBody.appendChild(tr);
    });
  }

  // -------- Render wrapper --------
  function renderAiResult(aiResp) {
    _lastAiResp = aiResp;

    // LLM error (if any)
    const llmErr = safeText(aiResp?.llm_error || "");
    if (llmErr && errorHint) {
      errorHint.textContent = "LLM lỗi: " + llmErr;
      errorHint.style.display = "block";
    }

    renderAiAnalysis(aiResp);
    renderAiSuggestions(aiResp);

    // optional compat click for suggestList cũ
    if (suggestList && !suggestList.__aiHooked) {
      suggestList.__aiHooked = true;
      suggestList.addEventListener("click", (ev) => {
        const item = ev.target?.closest?.(".suggest-item");
        if (!item) return;
        const key = item.getAttribute("data-key");
        if (!key) return;

        const aiPlans = aiResp?.ai_plans || {};
        const pa = aiPlans?.[key];
        if (planBadge) planBadge.textContent = key;
        renderAiPlanRows(pa?.rows, pa?.note);
        if (planTblSubtitle) planTblSubtitle.textContent = "Đang hiển thị mô phỏng của " + key + ".";
      });
    }
  }

  // -------- Main load --------
  async function load() {
    if (errorHint) errorHint.style.display = "none";

    if (!planId) {
      if (errorHint) {
        errorHint.textContent = "Thiếu tham số id kế hoạch.";
        errorHint.style.display = "block";
      }
      return;
    }

    if (_aiInFlight) cancelAi("restart");

    try {
      // 1) load calendar
      const res = await fetch(`/api/v1/kehoach/${planId}/calendar`);
      const cal = await parseMaybeJson(res);
      if (!res.ok) throw new Error(cal?.detail || cal?._rawText || "Không thể tải dữ liệu kế hoạch");
      _lastCalendar = cal;

      const header = cal?.Header || {};
      const ma = header?.MaKeHoach || header?.KeHoachID || planId;

      if (aiSubtitle) {
        aiSubtitle.innerHTML =
          `Kế hoạch: <b>${escapeHtml(ma)}</b> • Khoảng: <b>${escapeHtml(fmtDate(header?.TuNgay))} → ${escapeHtml(fmtDate(header?.DenNgay))}</b> • Trạng thái: <b>${escapeHtml(header?.TrangThai)}</b>`;
      }

      buildCharts(cal);
      // Keep a single forecast source in temporary mode to avoid chart overwrite.
      // Forecast charts will be updated by `model-forecast-payload` call below.

      // 2) restore from cache (no rerun APIs)
      const cached = loadAiCache();
      if (cached && typeof cached === "object") {
        const cachedForecast = cached?.forecastResp;
        const cachedAiResp = cached?.aiResp || cached?.resp;

        if (cachedForecast && typeof cachedForecast === "object") {
          hydrateChartsFromAiResponse(cachedForecast);
        }
        if (cachedAiResp && typeof cachedAiResp === "object") {
          renderAiResult(cachedAiResp);
          if (planTblSubtitle) planTblSubtitle.textContent = "Đang hiển thị kết quả AI từ cache. Bấm “Phân tích lại” để chạy mới.";
          return;
        }
      }

      // 3) skeleton minimal
      if (aiAnalysisHost) aiAnalysisHost.innerHTML = `<div class="ai-skeleton">AI đang phân tích tổng thể...</div>`;
      if (aiSuggestionsHost) aiSuggestionsHost.innerHTML = `<div class="ai-skeleton">AI đang tạo phương án tối ưu...</div>`;
      setSuggestCount(0);
      if (aiOverviewQuality) aiOverviewQuality.textContent = "Độ tin cậy: --";

      renderAiPlanRows([], "AI đang phân tích, vui lòng chờ kết quả...");
      if (planTblSubtitle) planTblSubtitle.textContent = "Đang phân tích, sẽ hiển thị khi có kết quả.";
      if (planCheckBadges) planCheckBadges.textContent = "";
      if (planBadge) planBadge.textContent = "...";

      // 4) Run forecast + AI analysis from payload
      const forecastPayload = {
        planId,
        systemData: buildAnalysisInput(cal),
      };

      _aiAbort = new AbortController();
      _aiInFlight = true;

      const timeoutMs = 500000;
      const timeoutId = setTimeout(() => {
        try { _aiAbort.abort("timeout"); } catch { /* ignore */ }
      }, timeoutMs);

      setOverlayVisible(true, "Đang chạy AI phân tích và dự báo...");

      try {
        const forecastResp = await callModelForecastPayload(forecastPayload, { signal: _aiAbort.signal });

        // Update charts from payload-based forecast response.
        hydrateChartsFromAiResponse(forecastResp);

        // Re-enable AI plan analysis flow.
        setOverlayVisible(true, "Đang chạy AI phân tích phương án tối ưu...");
        const aiResp = await callAiAnalysis(forecastPayload, { signal: _aiAbort.signal });
        renderAiResult(aiResp);
        saveAiCache(aiResp, forecastResp);
      } catch (e) {
        const aborted = e?.name === "AbortError" || String(e?.message || "").toLowerCase().includes("abort");
        const msg = safeText(e?.message || e) || "Unknown error";

        if (errorHint) {
          errorHint.textContent = aborted
            ? "Đã hủy/timeout khi chờ AI phân tích. Bạn có thể bấm “Phân tích lại”."
            : "Không gọi được luồng AI phân tích/dự báo: " + msg;
          errorHint.style.display = "block";
        }

        // fallback UI
        if (aiAnalysisHost) aiAnalysisHost.innerHTML = `<p class="ai-muted">${escapeHtml(aborted ? "Đã hủy/timeout khi chạy AI phân tích." : "Không gọi được luồng AI phân tích/dự báo: " + msg)}</p>`;
        if (aiSuggestionsHost) aiSuggestionsHost.innerHTML = `<div class="ai-empty">Chưa lấy được phương án AI.</div>`;

        renderAiPlanRows([], aborted ? "Đã hủy/timeout. Hãy thử lại." : "Không gọi được luồng AI phân tích/dự báo: " + msg);
        if (planTblSubtitle) planTblSubtitle.textContent = "Chưa lấy được dữ liệu AI phân tích/dự báo.";
        if (planBadge) planBadge.textContent = "...";
      } finally {
        clearTimeout(timeoutId);
        setOverlayVisible(false);
        _aiInFlight = false;
        _aiAbort = null;
      }
    } catch (e) {
      destroyCharts();
      if (aiSubtitle) aiSubtitle.textContent = "Không thể tải dữ liệu.";
      if (errorHint) {
        errorHint.textContent = e?.message || "Không thể tải dữ liệu";
        errorHint.style.display = "block";
      }
      if (aiAnalysisHost) aiAnalysisHost.innerHTML = `<p class="ai-muted">Không thể tải dữ liệu kế hoạch.</p>`;
      if (aiSuggestionsHost) aiSuggestionsHost.innerHTML = `<div class="ai-empty">Chưa có phương án tối ưu.</div>`;
    }
  }

  // -------- Navigation --------
  function goBack() {
    window.location.href = "./ke-hoach.html";
  }

  if (btnClose) btnClose.addEventListener("click", goBack);
  if (btnConfirm) btnConfirm.addEventListener("click", goBack);
  if (btnReload) btnReload.addEventListener("click", () => { clearAiCache(); load(); });

  window.addEventListener("beforeunload", () => {
    cancelAi("unload");
    destroyCharts();
  });

  load();
})();
