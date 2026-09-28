(() => {
  const grid = document.querySelector('#clansGrid'), notice = document.querySelector('#clansNotice');
  const detail = document.querySelector('#clanDialog'), application = document.querySelector('#applicationDialog');
  const csrf = () => decodeURIComponent(document.cookie.split('; ').find((part) => part.startsWith('wds_csrf='))?.split('=')[1] || '');
  const escape = (value) => String(value || '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const closeButtons = document.querySelectorAll('.dialog-close');
  closeButtons.forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
  [detail, application].forEach((dialog) => dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); }));

  async function loadClans() {
    try {
      const response = await fetch('/api/public/clans'); const data = await response.json();
      const clans = data.clans || [];
      notice.hidden = !!clans.length;
      notice.textContent = clans.length ? '' : 'Кланов пока нет. Возможно, именно ваш станет первым.';
      grid.hidden = !clans.length;
      grid.innerHTML = clans.map((clan) => `<button class="public-clan-card" data-id="${clan.id}" style="--clan-color:${escape(clan.color)}"><span class="public-clan-cover" style="${clan.cover_url ? `background-image:url('${encodeURI(clan.cover_url)}')` : ''}"></span><span class="public-clan-card-content">${clan.emblem_url ? `<img src="${encodeURI(clan.emblem_url)}" alt="">` : `<b class="public-clan-emblem">${escape(clan.tag.slice(0,2))}</b>`}<span><strong>[${escape(clan.tag)}] ${escape(clan.name)}</strong><small>${escape(clan.description || 'Описание пока не добавлено')}</small></span><em>${clan.member_count} ${clan.member_count === 1 ? 'участник' : 'участников'}</em></span></button>`).join('');
      grid.querySelectorAll('[data-id]').forEach((card) => card.addEventListener('click', () => openClan(card.dataset.id)));
    } catch { notice.textContent = 'Не удалось загрузить кланы. Повторите чуть позже.'; }
  }
  async function openClan(id) {
    document.querySelector('#clanDialogContent').innerHTML = '<p class="muted">Загрузка карточки…</p>'; detail.showModal();
    try {
      const response = await fetch(`/api/public/clans/${encodeURIComponent(id)}`); const data = await response.json(); if (!response.ok) throw new Error(); const clan = data.clan;
      document.querySelector('#clanDialogContent').innerHTML = `<div class="public-clan-detail" style="--clan-color:${escape(clan.color)}"><div class="public-clan-detail-cover" style="${clan.cover_url ? `background-image:url('${encodeURI(clan.cover_url)}')` : ''}"></div><div class="public-clan-detail-title">${clan.emblem_url ? `<img src="${encodeURI(clan.emblem_url)}" alt="">` : `<b>${escape(clan.tag.slice(0,2))}</b>`}<div><div class="eyebrow">КЛАН</div><h2>[${escape(clan.tag)}] ${escape(clan.name)}</h2><p>${escape(clan.description || 'Описание пока не добавлено')}</p></div></div><div class="public-clan-leader">Лидер <strong>${escape(clan.leader)}</strong></div><h3>Состав · ${clan.member_count}</h3><div class="public-roster">${clan.members.map((member) => `<div><b>${escape(member.name)}</b><span>${escape(member.member_role)}</span></div>`).join('') || '<p class="muted">Состав ещё не заполнен.</p>'}</div></div>`;
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
