(() => {
  const actions = document.querySelector("[data-public-actions]");
  if (!actions) return;

  fetch("/api/session", { credentials: "same-origin" })
    .then((response) => response.ok ? response.json() : null)
    .then((session) => {
      const me = session?.me;
      if (!session?.authed || !me) return;
      const name = String(me.persona || me.steam_id || "Игрок");
      const hasPanel = Array.isArray(me.permissions) && me.permissions.length > 0;
      actions.innerHTML = `${hasPanel ? '<a class="btn primary public-panel-link" href="/panel">Панель управления →</a>' : ''}<span class="public-user-chip" title="Вход через Steam выполнен">${name}</span><form action="/api/auth/logout" method="post"><button class="public-logout" type="submit" aria-label="Выйти из аккаунта">↗</button></form>`;
    })
    .catch(() => {});
})();
