(() => {
  // На телефоне меню прокручивается вбок: показываем пункт текущей страницы.
  // Двигаем только само меню, scrollIntoView прокрутил бы ещё и страницу.
  const nav = document.querySelector(".public-nav");
  const activeLink = nav?.querySelector(".nav-link.active");
  if (activeLink && nav.scrollWidth > nav.clientWidth) {
    const navBox = nav.getBoundingClientRect();
    const linkBox = activeLink.getBoundingClientRect();
    nav.scrollLeft += linkBox.left - navBox.left - (nav.clientWidth - linkBox.width) / 2;
  }

  const actions = document.querySelector("[data-public-actions]");
  if (!actions) return;

  const csrf = () =>
    decodeURIComponent(
      (
        (
          document.cookie
            .split("; ")
            .find((c) => c.startsWith("wds_csrf=") || c.startsWith("__Host-wds_csrf=")) || "="
        ).split("=")[1] || ""
      ),
    );

  const escapeHtml = (value) =>
    String(value).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

  fetch("/api/session", { credentials: "same-origin" })
    .then((response) => (response.ok ? response.json() : null))
    .then((session) => {
      const me = session?.me;
      if (!session?.authed || !me) return;
      const name = escapeHtml(me.persona || me.steam_id || "Игрок");
      const hasPanel = Array.isArray(me.permissions) && me.permissions.length > 0;
      const chip = hasPanel
        ? `<a class="public-user-chip" title="Личный профиль" href="/#profile">${name}</a>`
        : `<span class="public-user-chip" title="Вход через Steam выполнен">${name}</span>`;
      actions.innerHTML = `${hasPanel ? '<a class="btn primary public-panel-link" href="/panel">Панель управления →</a>' : ''}${chip}<button class="public-logout" type="button" aria-label="Выйти из аккаунта">↗</button>`;
      actions.querySelector(".public-logout")?.addEventListener("click", async () => {
        try {
          await fetch("/api/auth/logout", {
            method: "POST",
            credentials: "same-origin",
            headers: { "X-CSRF-Token": csrf() },
          });
        } catch {}
        location.href = "/";
      });
    })
    .catch(() => {});
})();

// Баннер cookie: сайт использует только необходимые cookie, поэтому выбор информационный.
// Выбор хранится в localStorage (не cookie), его можно сменить по ссылке в подвале.
(() => {
  const KEY = "wds_cookie_choice";
  const read = () => {
    try { return localStorage.getItem(KEY); } catch { return null; }
  };
  const write = (value) => {
    try { localStorage.setItem(KEY, value); } catch { /* приватный режим: выбор не сохранится */ }
  };
  const showBanner = () => {
    if (document.querySelector(".cookie-banner")) return;
    const bar = document.createElement("div");
    bar.className = "cookie-banner";
    bar.setAttribute("role", "dialog");
    bar.setAttribute("aria-label", "Выбор cookie");
    bar.innerHTML =
      '<span class="cookie-banner-text">Мы используем только необходимые cookie: вход и защиту форм. Аналитики и рекламы нет. ' +
      '<a href="/cookies">Подробнее</a></span>' +
      '<span class="cookie-banner-actions">' +
      '<button type="button" class="btn ghost small" data-choice="declined">Отклонить</button>' +
      '<button type="button" class="btn primary small" data-choice="accepted">Принять</button>' +
      "</span>";
    bar.addEventListener("click", (event) => {
      const choice = event.target.closest("[data-choice]")?.dataset.choice;
      if (!choice) return;
      write(choice);
      bar.remove();
    });
    document.body.appendChild(bar);
  };
  if (!read()) showBanner();
  document.querySelectorAll("[data-cookie-settings]").forEach((button) => {
    button.addEventListener("click", () => {
      try { localStorage.removeItem(KEY); } catch { /* ignore */ }
      showBanner();
    });
  });
})();
