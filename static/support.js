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
  const steamInput = document.querySelector('#orderSteam');

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


  const bindPlan = (button) => {
    button.onclick = () => {
      plan = { product: button.dataset.product || '', seats: Number(button.dataset.seats || 1) };
      title.textContent = button.dataset.plan;
      price.textContent =
        plan.seats > 1 ? `${button.dataset.price} · получателей: ${plan.seats}` : button.dataset.price;
      setMessage('', false);
      if (plan.seats > 1) {
        note.textContent = 'Клановый VIP пока оформляется через администрацию: напишите в поддержку.';
        submit.hidden = true;
        dialog.showModal();
        return;
      }
      note.textContent =
        'Введите SteamID64, на который оформляется VIP. Он должен быть привязан к вашему Discord кнопкой «Привязать Steam» в канале правил.';
      submit.hidden = false;
      steamInput.value = '';
      setMessage('', false);
      dialog.showModal();
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
        body: JSON.stringify({ product: plan.product, steam_id: steamInput.value.trim() }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        setMessage(
          data?.detail || data?.error?.message || 'Не удалось создать заказ, попробуйте позже.',
          false,
        );
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
