(() => {
  const grid = document.querySelector('#clansGrid'), notice = document.querySelector('#clansNotice');
  const detail = document.querySelector('#clanDialog'), application = document.querySelector('#applicationDialog');
  const csrf = () => decodeURIComponent(document.cookie.split('; ').find((part) => part.startsWith('wds_csrf=') || part.startsWith('__Host-wds_csrf='))?.split('=')[1] || '');
  const escape = (value) => String(value || '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const closeButtons = document.querySelectorAll('.dialog-close');
  closeButtons.forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
  [detail, application].forEach((dialog) => dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); }));
  const initials = (clan) => (clan.tag || clan.name || '').slice(0, 2);
  const clanTitle = (clan) => `${clan.tag ? `[${escape(clan.tag)}] ` : ''}${escape(clan.name)}`;
  const emblem = (clan, fallbackClass) => clan.emblem_url
    ? `<img src="${escape(encodeURI(clan.emblem_url))}" alt="" data-fallback="${escape(initials(clan))}" data-fallback-class="${fallbackClass}">`
    : `<b class="${fallbackClass}">${escape(initials(clan))}</b>`;
  // Ссылки на вложения Discord со временем истекают: тогда вместо логотипа показываем буквы.
  const watchLogos = (root) => root.querySelectorAll('img[data-fallback]').forEach((image) => image.addEventListener('error', () => {
    const letters = document.createElement('b');
    letters.className = image.dataset.fallbackClass;
    letters.textContent = image.dataset.fallback;
    image.replaceWith(letters);
  }));

  async function loadClans() {
    try {
      const response = await fetch('/api/public/clans'); const data = await response.json();
      const clans = data.clans || [];
      notice.hidden = !!clans.length;
      notice.textContent = clans.length ? '' : 'Кланов пока нет. Возможно, именно ваш станет первым.';
      grid.hidden = !clans.length;
      grid.innerHTML = clans.map((clan) => `<button class="public-clan-card" data-id="${escape(clan.id)}" style="--clan-color:${escape(clan.color)}"><span class="public-clan-cover" style="${clan.cover_url ? `background-image:url('${encodeURI(clan.cover_url)}')` : ''}"></span><span class="public-clan-card-content">${emblem(clan, 'public-clan-emblem')}<span><strong>${clanTitle(clan)}</strong><small>${escape(clan.description || 'Описание пока не добавлено')}</small></span><em>${clan.member_count} ${clan.member_count === 1 ? 'участник' : 'участников'}</em></span></button>`).join('');
      grid.querySelectorAll('[data-id]').forEach((card) => card.addEventListener('click', () => openClan(card.dataset.id)));
      watchLogos(grid);
    } catch { notice.textContent = 'Не удалось загрузить кланы. Повторите чуть позже.'; }
  }
  function botLinks(clan) {
    if (clan.source !== 'bot') return '';
    const links = [];
    if (clan.discord_url) links.push(`<a class="btn ghost small" href="${escape(clan.discord_url)}" target="_blank" rel="noopener noreferrer">Discord клана</a>`);
    if (clan.recruitment_open && clan.recruitment_url) links.push(`<a class="btn primary small" href="${escape(clan.recruitment_url)}" target="_blank" rel="noopener noreferrer">Вступить</a>`);
    return `<div class="public-clan-links"><span class="public-clan-recruitment">${clan.recruitment_open ? 'Набор открыт' : 'Набор закрыт'}</span>${links.join('')}</div>`;
  }
  async function openClan(id) {
    document.querySelector('#clanDialogContent').innerHTML = '<p class="muted">Загрузка карточки…</p>'; detail.showModal();
    try {
      const response = await fetch(`/api/public/clans/${encodeURIComponent(id)}`); const data = await response.json(); if (!response.ok) throw new Error(); const clan = data.clan;
      document.querySelector('#clanDialogContent').innerHTML = `<div class="public-clan-detail" style="--clan-color:${escape(clan.color)}"><div class="public-clan-detail-cover" style="${clan.cover_url ? `background-image:url('${encodeURI(clan.cover_url)}')` : ''}"></div><div class="public-clan-detail-title">${emblem(clan, '')}<div><div class="eyebrow">КЛАН</div><h2>${clanTitle(clan)}</h2><p>${escape(clan.description || 'Описание пока не добавлено')}</p></div></div><div class="public-clan-leader">Лидер <strong>${escape(clan.leader)}</strong></div>${clan.goals ? `<h3>Цели</h3><p class="public-clan-goals">${escape(clan.goals)}</p>` : ''}${botLinks(clan)}<h3>Состав · ${clan.member_count}</h3><div class="public-roster">${clan.members.map((member) => `<div><b>${escape(member.name)}</b><span>${escape(member.member_role)}</span></div>`).join('') || `<p class="muted">${clan.source === 'bot' ? 'Состав клана ведётся в Discord.' : 'Состав ещё не заполнен.'}</p>`}</div></div>`;
      watchLogos(document.querySelector('#clanDialogContent'));
    } catch { document.querySelector('#clanDialogContent').innerHTML = '<p class="muted">Карточка клана сейчас недоступна.</p>'; }
  }
  document.querySelector('#openClanApplication')?.addEventListener('click', async () => {
    const session = await fetch('/api/session').then((response) => response.json()).catch(() => null);
    if (!session?.authed) { location.href = '/api/auth/steam/start'; return; }
    application.showModal();
  });
  document.querySelector('#clanApplicationForm')?.addEventListener('submit', async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget), message = document.querySelector('#applicationMessage'), button = event.currentTarget.querySelector('button');
    message.textContent = ''; button.disabled = true;
    try {
      const response = await fetch('/api/public/clan-applications', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()}, body:JSON.stringify(Object.fromEntries(form))}); const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Не удалось отправить заявку');
      message.className = 'form-message ok'; message.textContent = data.discord_delivered ? 'Заявка отправлена в панель и Discord.' : 'Заявка сохранена в панели. Discord-канал будет подключён администратором.'; event.currentTarget.reset();
    } catch (error) { message.className = 'form-message error'; message.textContent = error.message; } finally { button.disabled = false; }
  });
  loadClans();
})();
