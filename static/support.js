(() => {
  const dialog = document.querySelector('#orderDialog');
  const form = document.querySelector('#orderForm');
  const note = document.querySelector('#orderNote');
  const message = document.querySelector('#orderCode');
  const submit = form.querySelector('button[type="submit"]');
  const title = document.querySelector('#orderTitle');
  const price = document.querySelector('#orderPrice');
  const invite = 'https://discord.gg/wdru-ru';
  let plan = null;
  let sessionPromise = null;

  const session = () =>
    (sessionPromise ||= fetch('/api/session', { credentials: 'same-origin' })
      .then((response) => (response.ok ? response.json() : null))
      .catch(() => null));

  const csrf = () =>
    decodeURIComponent(
      (
        (
          document.cookie
            .split('; ')
            .find((cookie) => cookie.startsWith('wds_csrf=') || cookie.startsWith('__Host-wds_csrf=')) || '='
        ).split('=')[1] || ''
      ),
    );

  function setMessage(text, ok) {
    message.className = ok ? 'form-message ok' : 'form-message';
    message.textContent = text;
  }

  async function checkAccess() {
    const data = await session();
    if (!data?.authed) {
      note.innerHTML = 'Для покупки нужен <a href="/api/auth/steam/start">вход через Steam</a>.';
      submit.hidden = true;
      return;
    }
    if (!data?.me?.discord?.bound) {
      note.innerHTML =
        'Привяжите <a href="/api/auth/discord/start">Discord-аккаунт</a> — оплаченные роли выдаются на него.';
      submit.hidden = true;
      return;
    }
    note.textContent =
      'Получатель — ваш Discord-аккаунт. После оплаты привяжите слот к Steam кнопкой «Привязать Steam» в канале правил Discord.';
    submit.hidden = false;
  }

  const bindPlan = (button) => {
    button.onclick = () => {
      plan = { product: button.dataset.product || '', seats: Number(button.dataset.seats || 1) };
      title.textContent = button.dataset.plan;
      price.textContent =
        plan.seats > 1 ? `${button.dataset.price} · получателей: ${plan.seats}` : button.dataset.price;
      setMessage('', false);
      if (plan.seats > 1) {
        note.innerHTML = `Клановый VIP оформляется в Discord: получателей выбирают командой в канале VIP. <a href="${invite}" target="_blank" rel="noopener noreferrer">Открыть Discord</a>`;
        submit.hidden = true;
        dialog.showModal();
        return;
      }
      note.textContent = 'Проверяем вход…';
      submit.hidden = true;
      dialog.showModal();
      checkAccess();
    };
  };

  const formatRub = (rub) => `${Number(rub).toLocaleString('ru-RU')} ₽`;

  function planCard(item, perks) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'support-plan';
    button.dataset.plan = item.name;
    button.dataset.price = formatRub(item.rub);
    button.dataset.seats = String(item.seats);
    // Клановый VIP оформляется в Discord, поэтому покупка с сайта для него не создаётся.
    if (item.kind === 'personal') button.dataset.product = item.id;
    const add = (tag, text) => {
      const node = document.createElement(tag);
      node.textContent = text;
      button.append(node);
    };
    add('small', item.kind === 'clan' ? 'КЛАНОВЫЙ VIP' : `VIP TIER ${item.tier}`);
    add('b', item.term);
    add('strong', formatRub(item.rub));
    add('span', perks[item.tier] || '');
    bindPlan(button);
    return button;
  }

  // Цены приходят с сервера. Старую цену не показываем: без ответа покупку не предлагаем.
  async function loadPlans() {
    const grid = document.querySelector('#supportPlans');
    try {
      const response = await fetch('/api/public/vip-catalog', { credentials: 'same-origin' });
      if (response.status === 503) {
        const closed = await response.json().catch(() => null);
        throw new Error(closed?.detail || 'Покупка временно недоступна.');
      }
      const data = response.ok ? await response.json() : null;
      if (!data?.ok) throw new Error('Не удалось загрузить тарифы. Обновите страницу чуть позже.');
      if (!data.items.length) throw new Error('Сейчас нет доступных тарифов.');
      grid.replaceChildren(...data.items.map((item) => planCard(item, data.perks || {})));
    } catch (error) {
      const text = document.createElement('p');
      text.className = 'muted';
      text.textContent = error.message || 'Не удалось загрузить тарифы.';
      grid.replaceChildren(text);
    }
  }

  loadPlans();

  dialog.querySelector('.dialog-close').onclick = () => dialog.close();

  form.onsubmit = async (event) => {
    event.preventDefault();
    if (!plan?.product || submit.hidden) return;
    submit.disabled = true;
    setMessage('Готовим заказ…', true);
    try {
      const response = await fetch('/api/donate/checkout', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
        body: JSON.stringify({ product: plan.product }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        setMessage(
          data?.detail || data?.error?.message || 'Не удалось создать заказ, попробуйте позже.',
          false,
        );
        if (response.status === 401) {
          window.location.assign('/api/auth/steam/start');
          return;
        }
        if (response.status === 409) {
          sessionPromise = null;
          await checkAccess();
        }
        submit.disabled = false;
        return;
      }
      window.location.assign(data.url);
    } catch (error) {
      setMessage('Сеть недоступна, попробуйте позже.', false);
      submit.disabled = false;
    }
  };
})();
