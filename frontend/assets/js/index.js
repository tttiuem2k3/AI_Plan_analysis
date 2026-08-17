/** frontend/assets/js/index.js */
const AUTO_OPEN_DELAY_MS = 100;

const sidebar = document.getElementById("sidebar");
const menuList = document.getElementById("menuList");
const contentFrame = document.getElementById("contentFrame");
const leftHoverZone = document.getElementById("leftHoverZone");
const collapseBtn = document.getElementById("menuCollapseBtn");

const CLS_COLLAPSED = "sidebar-collapsed";
const CLS_PEEK = "sidebar-peek";

let hoverTimer = null;

// tạo "nửa nút" mở rộng (handle) bằng JS để không phải sửa nhiều HTML
// const expandHandle = document.createElement("button");
// expandHandle.className = "menu-expand-handle";
// expandHandle.type = "button";
// expandHandle.setAttribute("aria-label", "Mở rộng menu");
// expandHandle.innerHTML = `<i class="bi bi-chevron-right"></i>`;
// document.body.appendChild(expandHandle);

function isCollapsed() {
  return document.body.classList.contains(CLS_COLLAPSED);
}

function isPeeking() {
  return document.body.classList.contains(CLS_PEEK);
}

/** Thu gọn: luôn về trạng thái collapsed, xóa peek */
function closeSidebar() {
  document.body.classList.add(CLS_COLLAPSED);
  document.body.classList.remove(CLS_PEEK);
}

/** Mở ghim (PIN): menu giữ + content chừa chỗ */
function openSidebarPinned() {
  document.body.classList.remove(CLS_COLLAPSED);
  document.body.classList.remove(CLS_PEEK);
}

/** Mở tạm thời (PEEK): menu overlay, KHÔNG đẩy content */
function openSidebarPeek() {
  // chỉ peek khi đang collapsed
  if (!isCollapsed()) return;
  document.body.classList.remove(CLS_COLLAPSED);
  document.body.classList.add(CLS_PEEK);
}

/** Nếu đang peek mà click trong sidebar => PIN */
function pinIfPeeking() {
  if (isPeeking()) openSidebarPinned();
}

/** Nếu đang peek mà click ra ngoài / rời sidebar => đóng lại */
function closeIfPeeking() {
  if (isPeeking()) closeSidebar();
}

function clearHoverTimer() {
  if (hoverTimer) {
    clearTimeout(hoverTimer);
    hoverTimer = null;
  }
}

/** Load page vào iframe + active menu */
function loadPage(url, clickedLi) {
  if (!url) return;
  contentFrame.src = url;

  menuList.querySelectorAll("li").forEach(li => li.classList.remove("active"));
  if (clickedLi) clickedLi.classList.add("active");
}

/** Click menu */
menuList.addEventListener("click", (e) => {
  const li = e.target.closest("li");
  if (!li) return;

  // nếu menu đang peek mà click vào menu item => pin luôn
  pinIfPeeking();

  const url = li.getAttribute("data-url");
  loadPage(url, li);
});

/** Thu gọn menu (nút trên sidebar) */
collapseBtn.addEventListener("click", () => {
  closeSidebar();
});

// Bỏ sự kiện mở rộng menu bằng "nửa nút"
// expandHandle.addEventListener("click", () => {
//   openSidebar();
// });

/** SMART OPEN: hover mép trái đủ 3s thì peek */
leftHoverZone.addEventListener("mouseenter", () => {
  // chỉ áp dụng khi đang collapsed
  if (!isCollapsed()) return;

  clearHoverTimer();
  hoverTimer = setTimeout(() => {
    openSidebarPeek();
  }, AUTO_OPEN_DELAY_MS);
});

leftHoverZone.addEventListener("mouseleave", () => {
  clearHoverTimer();
});

/** Nếu rê vào sidebar thì hủy timer (tránh mở sai) */
sidebar.addEventListener("mouseenter", () => {
  clearHoverTimer();
});

/** Click bất kỳ trong sidebar => PIN (giữ menu) */
sidebar.addEventListener("pointerdown", () => {
  pinIfPeeking();
});

/** Nếu đang peek mà rời sidebar => đóng lại */
sidebar.addEventListener("mouseleave", () => {
  // đóng hơi trễ 1 chút để tránh giật khi di chuột nhanh
  setTimeout(() => {
    // vẫn còn đang peek thì đóng
    closeIfPeeking();
  }, 120);
});

/** Nếu đang peek mà click ra ngoài sidebar => đóng */
document.addEventListener("pointerdown", (e) => {
  if (!isPeeking()) return;
  if (sidebar.contains(e.target)) return; // click trong sidebar thì đã pin
  closeSidebar();
});

/** ESC: nếu đang peek thì đóng */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (isPeeking()) closeSidebar();
  }
});

/** Mặc định menu mở ghim (bạn có thể đổi thành closeSidebar() nếu muốn mặc định thu gọn) */
openSidebarPinned();

/** Mặc định mở trang Kế hoạch sản xuất và active đúng menu D */
const defaultLi = menuList.querySelector('li[data-url="pages/ke-hoach.html"]');
if (defaultLi) {
  loadPage("pages/ke-hoach.html", defaultLi);
}
