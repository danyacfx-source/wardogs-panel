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
  // Блок «VIP клана» виден только лидеру и заместителю (сервер отдаёт 403 остальным).
  async function renderClanVip(clanId, host) {
    const response = await fetch(`/api/clans/${encodeURIComponent(clanId)}/vip`, { credentials: 'same-origin' }).catch(() => null);
    if (!response || !response.ok) return;
    const data = await response.json();
    const pool = data.pool;
    const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString('ru-RU') : '—');
    const granted = new Set(data.grants.map((g) => g.steam_id));
    const grantItems = data.grants.length
      ? data.grants.map((g) => `<li>${escape(g.name)} <small>${escape(g.steam_id)}</small> <button type="button" class="mini-btn danger" data-revoke="${escape(g.steam_id)}">Снять</button></li>`).join('')
      : '<li class="muted">Мест пока не выдано</li>';
    const options = data.members
      .filter((m) => !granted.has(m.steam_id))
      .map((m) => `<option value="${escape(m.steam_id)}">${escape(m.name)} (${escape(m.role || '')})</option>`)
      .join('');
    const packs = data.packs
      .filter((k) => k.enabled)
      .map((k) => `<button type="button" class="btn ghost small" data-pack="${k.pack}">${k.pack} мест · ${k.rub} ₽</button>`)
      .join(' ');
    const box = document.createElement('section');
    box.className = 'clan-vip-box';
    box.innerHTML =
      `<h3>VIP клана</h3>` +
      `<p>Мест: <b>${pool.used}</b> из <b>${pool.seats}</b>, свободно <b>${pool.free}</b>. ` +
      (pool.active ? `Срок до <b>${fmtDate(pool.expires_utc)}</b>.` : 'Пакет не куплен или срок истёк.') +
      `</p>` +
      `<p class="clan-vip-packs">${packs || '<span class="muted">Пакеты сейчас недоступны</span>'}</p>` +
      `<ul class="clan-vip-grants">${grantItems}</ul>` +
      (pool.free > 0 && options
        ? `<form class="clan-vip-grant"><select name="steam_id">${options}</select> <button class="btn primary small" type="submit">Выдать место</button></form>`
        : '') +
      `<p class="form-message" data-vip-msg></p>`;
    host.appendChild(box);
    const say = (text, ok) => {
      const msg = box.querySelector('[data-vip-msg]');
      msg.className = ok ? 'form-message ok' : 'form-message error';
      msg.textContent = text;
    };
    const refresh = () => { box.remove(); renderClanVip(clanId, host); };
    box.querySelectorAll('[data-pack]').forEach((button) => {
      button.onclick = async () => {
        button.disabled = true;
        const result = await fetch(`/api/clans/${encodeURIComponent(clanId)}/vip/order`, {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
          body: JSON.stringify({ pack: Number(button.dataset.pack) }),
        });
        const payload = await result.json().catch(() => ({}));
        if (!result.ok) { button.disabled = false; say(payload.detail || 'Не удалось создать заявку', false); return; }
        location.assign(payload.url);
      };
    });
    const grantForm = box.querySelector('.clan-vip-grant');
    if (grantForm) {
      grantForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const steamId = new FormData(grantForm).get('steam_id');
        const result = await fetch(`/api/clans/${encodeURIComponent(clanId)}/vip/grants`, {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
          body: JSON.stringify({ steam_id: steamId }),
        });
        const payload = await result.json().catch(() => ({}));
        if (!result.ok) { say(payload.detail || 'Не удалось выдать место', false); return; }
        refresh();
      });
    }
    box.querySelectorAll('[data-revoke]').forEach((button) => {
      button.onclick = async () => {
        if (!window.confirm('Снять место у этого игрока? Слот пропадёт на всех серверах.')) return;
        const result = await fetch(`/api/clans/${encodeURIComponent(clanId)}/vip/grants/${encodeURIComponent(button.dataset.revoke)}`, {
          method: 'DELETE', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrf() },
        });
        const payload = await result.json().catch(() => ({}));
        if (!result.ok) { say(payload.detail || 'Не удалось снять место', false); return; }
        refresh();
      };
    });
  }

  async function openClan(id) {
    document.querySelector('#clanDialogContent').innerHTML = '<p class="muted">Загрузка карточки…</p>'; detail.showModal();
    try {
      const response = await fetch(`/api/public/clans/${encodeURIComponent(id)}`); const data = await response.json(); if (!response.ok) throw new Error(); const clan = data.clan;
      document.querySelector('#clanDialogContent').innerHTML = `<div class="public-clan-detail" style="--clan-color:${escape(clan.color)}"><div class="public-clan-detail-cover" style="${clan.cover_url ? `background-image:url('${encodeURI(clan.cover_url)}')` : ''}"></div><div class="public-clan-detail-title">${emblem(clan, '')}<div><div class="eyebrow">КЛАН</div><h2>${clanTitle(clan)}</h2><p>${escape(clan.description || 'Описание пока не добавлено')}</p></div></div><div class="public-clan-leader">Лидер <strong>${escape(clan.leader)}</strong></div>${clan.goals ? `<h3>Цели</h3><p class="public-clan-goals">${escape(clan.goals)}</p>` : ''}${botLinks(clan)}<h3>Состав · ${clan.member_count}</h3><div class="public-roster">${clan.members.map((member) => `<div><b>${escape(member.name)}</b><span>${escape(member.member_role)}</span></div>`).join('') || `<p class="muted">${clan.source === 'bot' ? 'Состав клана ведётся в Discord.' : 'Состав ещё не заполнен.'}</p>`}</div></div>`;
      watchLogos(document.querySelector('#clanDialogContent'));
      renderClanVip(id, document.querySelector('#clanDialogContent'));
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
