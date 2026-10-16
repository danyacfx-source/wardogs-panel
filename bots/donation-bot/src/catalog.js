'use strict';

const TIERS = [
  { name: 'Друг стаи', roleId: '1553090039615852675', min: 50000, icon: '🐾' },
  { name: 'Снабженец', roleId: '1553092734791983174', min: 250000, icon: '📦' },
  { name: 'Опора стаи', roleId: '1553093158441590975', min: 500000, icon: '🛡️' },
  { name: 'Хранитель рубежа', roleId: '1553093418031386714', min: 1000000, icon: '⚔️' },
  { name: 'Легенда WARDOGS|RU', roleId: '1553093599862988910', min: 2000000, icon: '🏅' },
];

const PRODUCTS = {
  personal_1: { name: 'Личный VIP · 1 месяц', kind: 'personal', months: 1, seats: 1, amount: 50000 },
  personal_3: { name: 'Личный VIP · 3 месяца', kind: 'personal', months: 3, seats: 1, amount: 120000 },
  personal_12: { name: 'Личный VIP · 12 месяцев', kind: 'personal', months: 12, seats: 1, amount: 480000 },
  clan_10: { name: 'VIP для клана · 10 мест · 1 месяц', kind: 'clan', months: 1, seats: 10, amount: 450000 },
  clan_20: { name: 'VIP для клана · 20 мест · 1 месяц', kind: 'clan', months: 1, seats: 20, amount: 850000 },
  clan_30: { name: 'VIP для клана · 30 мест · 1 месяц', kind: 'clan', months: 1, seats: 30, amount: 1200000 },
};

const CHANNELS = {
  support: '1552893505620418640', vip: '1552895310945189938',
  thanks: '1552893588780621904', top: '1552893678035673219',
};
const VIP_ROLE_ID = '1552725871830503484';

module.exports = { TIERS, PRODUCTS, CHANNELS, VIP_ROLE_ID };
