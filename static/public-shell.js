(() => {
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
