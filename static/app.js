/* WARDOGS сайт: мониторинг (/) и панель управления (/panel). Vue 3. */
const { createApp } = Vue;
if (typeof window.WARDOGS_RENDER !== "function") {
  throw new Error("Precompiled Vue render is missing");
}

const MODE = document.body.dataset.panelMode || "monitor";

const MAP_RU = { Kavkazi: "Bakurani", Europe: "Ozeti", NorthAmerica: "Zestafona" };
const LIGHT_RU = {
  DayStartClear: "Рассвет", DayEarlyClear: "Раннее утро", DayEarlyFog: "Раннее утро, туман",
  DayClear: "День", DayLateClear: "Поздний день", DayLateGray: "Серый день",
  DayLateGrayFog: "Серый день, туман", DayEndClear: "Закат",
};
const PERM_LABELS = {
  players_view: "Просмотр игроков",
  player_notes: "Заметки игроков",
  player_tags: "Метки игроков",
  clans: "Управление кланами",
  reserved_view: "Резерв: просмотр", reserved_edit: "Резерв: изменение",
  vip_view: "VIP: просмотр", vip_edit: "VIP: изменение",
  tickets_view: "Тикеты: просмотр", tickets_edit: "Тикеты: изменение",
  broadcast: "Объявление", kick: "Кик", kill: "Убить", message: "ЛС",
  move_faction: "Перевод в команду", ban_view: "Просмотр банов", ban_add: "Добавление бана", ban_remove: "Снятие бана",
  change_map: "Смена карты", change_lighting: "Смена света",
  match_end: "Итог матча", match_restart: "Рестарт матча",
  audit: "Аудит", config_view: "Конфиг: чтение", config_edit: "Конфиг: запись", roles: "Права ролей",
};
const PERM_HINTS = {
  players_view: "Видеть игроков и открывать карточки",
  player_notes: "Создавать и удалять внутренние заметки в карточках игроков",
  player_tags: "Добавлять и удалять административные метки игроков",
  clans: "Создавать кланы и управлять составом",
  reserved_view: "Просматривать резервные слоты",
  reserved_edit: "Добавлять и снимать резервные слоты",
  vip_view: "Просматривать выданные VIP-слоты и сроки",
  vip_edit: "Выдавать, продлевать, синхронизировать и отзывать VIP",
  tickets_view: "Просматривать внутренние обращения и обсуждения",
  tickets_edit: "Создавать, изменять, комментировать и закрывать тикеты",
  broadcast: "Отправлять объявление всем игрокам",
  kick: "Кикать игроков",
  kill: "Использовать slay",
  message: "Отправлять личные сообщения",
  move_faction: "Переводить игроков между фракциями",
  ban_view: "Просматривать бан-лист",
  ban_add: "Выдавать баны",
  ban_remove: "Снимать баны",
  change_map: "Менять карту и редактировать ротацию",
  change_lighting: "Менять освещение",
  match_end: "Завершать матч",
  match_restart: "Перезапускать матч",
  audit: "Просматривать аудит",
  config_view: "Читать конфигурацию",
  config_edit: "Изменять конфигурацию",
  roles: "Управлять ролями и администрацией",
};
const PERM_GROUPS = [
  { id: "players", label: "Игроки", perms: ["players_view", "player_notes", "player_tags", "message", "broadcast", "kick", "kill", "move_faction"] },
  { id: "community", label: "Сообщество", perms: ["clans"] },
  { id: "tickets", label: "Тикеты", perms: ["tickets_view", "tickets_edit"] },
  { id: "moderation", label: "Модерация", perms: ["ban_view", "ban_add", "ban_remove"] },
  { id: "server", label: "Сервер и матч", perms: ["change_map", "change_lighting", "match_end", "match_restart"] },
  { id: "reserved", label: "Резерв и VIP", perms: ["reserved_view", "reserved_edit", "vip_view", "vip_edit"] },
  { id: "security", label: "Аудит и настройки", perms: ["audit", "config_view", "config_edit", "roles"] },
];
const ROLE_PRESETS = {
  observer: {
    label: "Наблюдатель",
    perms: ["players_view", "reserved_view"],
  },
  moderator: {
    label: "Модератор",
    perms: ["players_view", "player_notes", "player_tags", "clans", "tickets_view", "tickets_edit", "message", "broadcast", "kick", "kill", "move_faction", "ban_view", "ban_add", "ban_remove", "reserved_view", "vip_view"],
  },
  administrator: {
    label: "Администратор",
    perms: ["players_view", "player_notes", "player_tags", "clans", "tickets_view", "tickets_edit", "message", "broadcast", "kick", "kill", "move_faction", "ban_view", "ban_add", "ban_remove", "change_map", "change_lighting", "match_end", "match_restart", "reserved_view", "reserved_edit", "vip_view", "vip_edit", "audit", "config_view"],
  },
};
function pad(n) { return String(n).padStart(2, "0"); }
const PLAYER_LIST_KEEP_MS = 60000;
// Цвета сторон для матчей: событие match_ended цвет не передаёт, а сервер может быть уже другим.
const KNOWN_FACTION_COLORS = { valkyra: "#ff5a5f", lonestar: "#5aa9ff", manticore: "#4cd787" };
const PLAYER_GONE_KEEP_MS = 20000;

const app = createApp({
  render: window.WARDOGS_RENDER,
  data() {
    return {
      state: {
        authed: false,
        servers: [],
        sid: "",
        tab: "status",
        refresh: 5,
        refreshing: false,
        ovById: {},
        me: null,
        roleDrafts: {},
      },
      booted: false,
      clockNow: Date.now(),
      panelReachable: null,
      connections: {},
      operations: [],
      showOperations: false,
      overviewAttention: { tickets: null, vips: [], error: '' },
      payments: { plans: [], items: [], loading: false, error: '', provider_connected: false },
      profile: { loading: false, error: "", data: null, open: false },
      seedShop: { loading: false, error: "", data: null, buying: false, buyResult: "" },
      botWizard: { open: false, step: 1, id: '', busy: false },
      botScopeChoices: [{id:'heartbeat',label:'Состояние подключения'}, {id:'events.write',label:'Отправка событий'}],
      botEvents: [],
      botEventsName: '',
      noAccess: false,
      navOpen: false,
      playersView: "live",
      playersLayout: "board",
      boardQuery: "",
      boardOffline: [],
      boardSearchBusy: false,
      boardSort: "name",
      playerMeta: {},
      playerGrace: {},
      vipView: "table",
      auditJson: null,
      statsPeriods: [{ id: "week", label: "Неделя" }, { id: "last", label: "Прошлая" }, { id: "all", label: "Всё" }, { id: "custom", label: "Период" }],
      auditSections: [{ id: "", label: "Все разделы" }, { id: "bans", label: "Баны" }, { id: "players", label: "Игроки" }, { id: "vip", label: "VIP и резерв" }, { id: "match", label: "Карта и матч" }, { id: "clans", label: "Кланы" }, { id: "tickets", label: "Тикеты" }, { id: "settings", label: "Настройки" }],
      reports: {
        playerStats: { items: [], total: 0, page: 1, perPage: 50, loading: false, error: "", q: "", server: "", scope: "all", sort: "online_seconds", direction: "desc", period: "week", firstDay: "", lastDay: "" },
        playerNames: { items: [], total: 0, page: 1, perPage: 50, loading: false, error: "", q: "" },
        matches: { items: [], live: [], total: 0, page: 1, perPage: 50, loading: false, error: "", server: "", player: "" },
        auditLog: { items: [], total: 0, page: 1, perPage: 50, loading: false, error: "", q: "", section: "", server: "" },
      },
      maintenance: { closed: {} },
      vipPrices: { items: [], saving: false, error: "", maxRub: 1000000 },
      devSections: { loading: false, error: "", items: [], defaultMessage: "", saving: "" },
      authConfigured: false,
      devAuthEnabled: false,
      busy: false,
      lastUpdate: "",
      toasts: [],
      catalog: { maps: {}, lightings: [] },
      form: { map: "", experiences: [], lighting: "", zoneAlternator: "" },
      audit: [],
      siteAudit: [],
      auditLimit: 50,
      killfeedRows: [],
      killfeedStats: {},
      killfeedStatus: {},
      killfeedKind: "all",
      killfeedSetup: null,
      killfeedLoading: false,
      killfeedTimer: null,
      cfg: { text: "", revision: "", writable: true, warnings: [], loaded: false },
      cfgResult: null,
      cfgResultStatus: "",
      rolesList: [],
      allPerms: [],
      staff: { items: [], count: 0, loading: false, error: "" },
      discordAdmin: { bindings: [], diagnostics: [], voice_channels: [], actions: [], bots: [], summary: {}, loading: false, error: "" },
      botDraft: { name: "", bot_id: "", guild_id: "", scopes: ["heartbeat", "events.write"] },
      botIssuedToken: "",
      systemStatus: { services: [], history: [], loading: false, error: "" },
      serverView: "overview",
      serverApiRoutes: ["GET /v1/status", "GET /v1/players", "GET /v1/rotation", "GET /v1/health", "GET /v1/bans", "GET /v1/reserved-slots", "POST /v1/broadcast", "POST /v1/match/map", "POST /v1/match/end", "POST /v1/match/restart", "PUT /v1/world/lighting", "POST /v1/players/{id}/kick", "POST /v1/players/{id}/kill", "PATCH /v1/players/{id}", "GET /v1/config", "PUT /v1/config"],
      serverBuild: "",
      staffQuery: "",
      clans: { items: [], loading: false, error: "" },
      selectedClan: null,
      clanQuery: "",
      tickets: { items: [], count: 0, loading: false, error: "", selected: null, detailLoading: false },
      ticketQuery: "",
      ticketStatusFilter: "active",
      ticketPriorityFilter: "all",
      ticketCommentDraft: "",
      ticketTypes: { items: [], loading: false, error: "", busy: false },
      ticketTypeDraft: { open: false, isNew: true, type_id: "", label: "", description: "", emoji: "", color: "#5865F2", position: 0, enabled: true },
      roleSearch: "",
      rolePermissionFilter: "all",
      rotationEdit: false,
      rotationSaving: false,
      rotationDraft: [],
      reserved: { slots: [], count: 0, max_reserved_slots: null, max_players: null, limit_supported: false, add_supported: false, remove_supported: false, loaded: false },
      reservedLimit: 0,
      reservedSteamId: "",
      vip: { items: [], count: 0, history: [], loading: false, error: "" },
      vipQuery: "",
      vipFilter: "active",
      vipTierFilter: "",
      playerSearch: "",
      playerSearchResults: [],
      playerSearchBusy: false,
      playersTableQuery: "",
      playersTableSort: "kills",
      playersTablePage: 1,
      playersTablePageSize: 50,
      bansQuery: "",
      bansSort: "newest",
      bansPage: 1,
      bansPageSize: 50,
      banSelected: [],
      roleCopySources: {},
      roleSelected: [],
      playerCard: { open: false, loading: false, profile: null, live: null, error: "" },
      selectedPunishmentIds: [],
      playerCardFaction: "",
      playerCardClanId: "",
      playerCardTab: "punishments",
      playerNotes: { items: [], loading: false, error: "", canEdit: false, draft: "" },
      actionDialog: {
        open: false,
        title: "",
        message: "",
        confirmText: "Подтвердить",
        danger: false,
        fields: [],
        error: "",
      },
      _timer: null,
      _dialogResolve: null,
    };
  },

  computed: {
    currentConnection() {
      const c = this.connections[this.state.sid];
      if (!c) return { state: 'pending', label: 'ПОДКЛЮЧЕНИЕ', age: null };
      const age = c.success ? Math.max(0, Math.floor((this.clockNow-c.success)/1000)) : null;
      const state = !c.ok ? 'offline' : age > Math.max(20,this.state.refresh*3) ? 'pending' : 'online';
      return { ...c, age, state, label: state === 'online' ? 'LIVE' : state === 'offline' ? 'НЕТ СВЯЗИ' : 'ДАННЫЕ УСТАРЕЛИ' };
    },
    expiringVips() { return this.overviewAttention.vips.filter(v=>v.expires_utc && v.expires_utc*1000>this.clockNow && v.expires_utc*1000-this.clockNow<7*86400000); },
    overview() { return this.state.ovById[this.state.sid] || null; },
    ov() { return this.overview; },
    me() { return this.state.me; },
    perms() { return (this.me && this.me.permissions) || []; },
    hasPerms() { return this.perms.length > 0; },
    // Закрытый раздел текущей вкладки. Владелец работает в нём как обычно.
    currentMaintenance() { return this.maintenance.closed[this.state.tab] || null; },
    sectionClosed() { return this.has("owner") ? null : this.currentMaintenance; },
    boardSortLabel() { return { name: "по имени", kills: "по убийствам", hours: "по часам" }[this.boardSort]; },
    playerCardSteamId() { return this.playerCard.profile?.steam_id || this.playerCard.live?.steamId || ""; },
    boardMatches() {
      const query = this.boardQuery.trim().toLowerCase();
      if (!query) return [];
      const matches = [];
      for (const server of this.state.servers) {
        for (const player of this.state.ovById[server.id]?.players?.players || []) {
          if (String(player.name || "").toLowerCase().includes(query) || String(player.steamId || "").includes(query)) {
            matches.push({ server, player });
          }
        }
      }
      return matches.slice(0, 30);
    },
    factionColumns() {
      const sid = this.state.sid;
      const meta = this.playerMeta[sid] || {};
      const grace = this.playerGrace[sid] || {};
      const scores = this.ov?.status?.factionScores || [];
      const columns = new Map(scores.map((item) => [item.name, { name: item.name, color: item.colorHex, score: item.score, players: [] }]));
      const live = this.ov?.players?.players || [];
      const seen = new Set(live.map((player) => player.steamId));
      const add = (player, gone) => {
        const name = player.faction || "Без стороны";
        if (!columns.has(name)) columns.set(name, { name, color: "#a7a2a0", score: null, players: [] });
        columns.get(name).players.push({ ...player, gone, meta: meta[player.steamId] || {} });
      };
      live.forEach((player) => add(player, false));
      Object.values(grace).forEach((player) => { if (!seen.has(player.steamId)) add(player, true); });
      // По умолчанию порядок по имени: строки не прыгают при каждом опросе, пока ищешь игрока глазами.
      const sorters = {
        name: (a, b) => String(a.name || "").localeCompare(String(b.name || ""), "ru"),
        kills: (a, b) => (b.kills || 0) - (a.kills || 0) || String(a.steamId).localeCompare(String(b.steamId)),
        hours: (a, b) => (b.meta.play_seconds || 0) - (a.meta.play_seconds || 0) || String(a.steamId).localeCompare(String(b.steamId)),
      };
      return [...columns.values()].filter((column) => column.players.length || column.score != null).map((column) => {
        column.players.sort(sorters[this.boardSort]);
        column.admins = column.players.filter((player) => player.meta.admin).length;
        column.steamHours = column.players.reduce((sum, player) => sum + (player.meta.game_hours || 0), 0);
        return column;
      });
    },
    matchRows() { return [...this.reports.matches.live, ...this.reports.matches.items]; },
    // VIP зеркалится на все серверы: одинаковые слоты показываем одним значком.
    cardVipChips() {
      const groups = new Map();
      for (const slot of (this.playerCard.profile?.vip_slots || []).filter((item) => item.active)) {
        const key = `${slot.tier || 1}:${slot.expires_utc || ""}`;
        if (!groups.has(key)) groups.set(key, { key, tier: slot.tier || 1, expires: slot.expires_utc, servers: [] });
        groups.get(key).servers.push(slot.server_id);
      }
      return [...groups.values()].map((group) => ({
        ...group,
        server: group.servers.length < this.state.servers.length ? group.servers.join(", ") : "",
      }));
    },
    cardKills() {
      const stats = this.playerCard.activity?.stats;
      return stats && (stats.kills || stats.deaths) ? stats.kills : (this.playerCard.profile?.kills || 0);
    },
    cardDeaths() {
      const stats = this.playerCard.activity?.stats;
      return stats && (stats.kills || stats.deaths) ? stats.deaths : (this.playerCard.profile?.deaths || 0);
    },
    cardKd() {
      const ratio = this.cardKills / Math.max(1, this.cardDeaths);
      return ratio.toFixed(2).replace(".", ",");
    },
    cardKillRows() {
      const activity = this.playerCard.activity;
      if (!activity) return [];
      return this.playerCardTab === "teamkills" ? activity.teamkills : activity.kills;
    },
    cheaterTone() {
      const data = this.playerCard.cheater?.data;
      if (!data || data.score == null) return "";
      return data.vac_bans > 0 || data.game_bans > 0 ? "bad" : "good";
    },
    playerChart() {
      const byDay = new Map((this.playerCard.activity?.daily || []).map((item) => [item.day, item]));
      const days = 35;
      const today = new Date();
      const bars = [];
      let maxHours = 0;
      for (let offset = days - 1; offset >= 0; offset -= 1) {
        const date = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() - offset));
        const day = date.toISOString().slice(0, 10);
        const entry = byDay.get(day);
        const hours = (entry?.play_seconds || 0) / 3600;
        maxHours = Math.max(maxHours, hours);
        bars.push({ day, hours, seed: (entry?.seed_seconds || 0) / 3600, label: `${day.slice(8)}.${day.slice(5, 7)}`, offset });
      }
      const top = Math.max(2, Math.ceil(maxHours / 2) * 2);
      const ticks = [];
      for (let tick = top; tick >= 0; tick -= top > 8 ? Math.ceil(top / 4) : 2) ticks.push(tick);
      if (ticks[ticks.length - 1] !== 0) ticks.push(0);
      return {
        hasData: maxHours > 0,
        ticks,
        bars: bars.map((bar) => ({
          ...bar,
          height: Math.min(100, (bar.hours / top) * 100),
          seedShare: bar.hours ? Math.min(100, (bar.seed / bar.hours) * 100) : 0,
          showLabel: bar.offset % 2 === 0,
          title: `${bar.label}: ${bar.hours.toFixed(1)} ч, сид ${bar.seed.toFixed(1)} ч`,
        })),
      };
    },
    vipPriceGroups() {
      const items = this.vipPrices.items;
      const groups = [1, 2, 3].map((tier) => ({
        title: `VIP Tier ${tier}`,
        hint: { 1: "VIP и резервный слот", 2: "VIP и личный тикет", 3: "VIP, тикет и объявление о входе" }[tier],
        items: items.filter((item) => item.kind === "personal" && item.tier === tier),
      }));
      groups.push({ title: "VIP для клана", hint: "Оформляется в Discord, уровень Tier 1", items: items.filter((item) => item.kind === "clan") });
      return groups;
    },
    devPanelSections() { return this.devSections.items.filter((item) => item.group === "panel"); },
    devSiteSections() { return this.devSections.items.filter((item) => item.group === "site"); },
    tabs() {
      if (MODE === "monitor") {
        const t = [{ id: "status", label: "Мониторинг" }];
        if (this.has("players_view") || ["kick", "kill", "message", "move_faction"].some((p) => this.has(p))) {
          t.push({ id: "players", label: "Игроки" });
        }
        return t;
      }
      const t = [{ id: "status", label: "Мониторинг" }];
      const any = (...ps) => ps.some((p) => this.perms.includes(p));
      if (any("players_view", "broadcast", "kick", "kill", "message", "move_faction")) t.push({ id: "players", label: "Игроки" });
      if (this.perms.includes("players_view")) t.push({ id: "matches", label: "Матчи" });
      if (this.perms.includes("players_view")) t.push({ id: "killfeed", label: "Killfeed" });
      if (any("change_map", "change_lighting", "match_end", "match_restart")) t.push({ id: "controls", label: "Карта / Матч" });
      if (this.perms.length) t.push({ id: "rotation", label: "Ротация" });
      if (any("ban_view", "ban_add", "ban_remove")) t.push({ id: "bans", label: "Баны" });
      if (any("reserved_view", "reserved_edit")) t.push({ id: "reserved", label: "Резервные слоты" });
      if (any("vip_view", "vip_edit")) t.push({ id: "vip", label: "VIP-слоты" });
      if (this.perms.includes("clans")) t.push({ id: "clans", label: "Кланы" });
      if (any("tickets_view", "tickets_edit")) t.push({ id: "tickets", label: "Тикеты" });
      if (this.perms.includes("audit")) t.push({ id: "audit", label: "Аудит" });
      if (this.perms.includes("audit")) t.push({ id: "journal", label: "Журнал" });
      if (any("config_view", "config_edit")) t.push({ id: "config", label: "Конфиг" });
      if (this.perms.includes("roles")) t.push({ id: "staff", label: "Администрация" });
      if (this.perms.includes("owner")) t.push({ id: "discord", label: "Боты и Discord" });
      if (this.perms.includes("owner")) t.push({ id: "system", label: "Состояние" });
      if (this.perms.includes("owner")) t.push({ id: "dev", label: "Dev" });
      if (this.perms.includes("roles")) t.push({ id: "roles", label: "Права ролей" });
      return t;
    },
    sortedFactions() {
      const list = (this.ov && this.ov.status && this.ov.status.factionScores) || [];
      return [...list].sort((a, b) => (b.score || 0) - (a.score || 0));
    },
    sortedPlayers() {
      const list = (this.ov && this.ov.players && this.ov.players.players) || [];
      const query = this.playersTableQuery.trim().toLowerCase();
      const filtered = query
        ? list.filter((player) => `${player.name || ""} ${player.steamId || ""} ${player.faction || ""}`.toLowerCase().includes(query))
        : list;
      const sorters = {
        kills: (a, b) => (b.kills || 0) - (a.kills || 0),
        deaths: (a, b) => (b.deaths || 0) - (a.deaths || 0),
        kd: (a, b) => {
          const ratio = (player) => {
            const deaths = Number(player.deaths || 0);
            return deaths > 0 ? Number(player.kills || 0) / deaths : null;
          };
          const aRatio = ratio(a), bRatio = ratio(b);
          if (aRatio === null || bRatio === null) {
            if (aRatio !== bRatio) return aRatio === null ? 1 : -1;
            return Number(b.kills || 0) - Number(a.kills || 0);
          }
          return bRatio - aRatio || Number(b.kills || 0) - Number(a.kills || 0);
        },
        name: (a, b) => String(a.name || "").localeCompare(String(b.name || ""), "ru"),
        ping: (a, b) => (a.pingMs || 0) - (b.pingMs || 0),
      };
      return [...filtered].sort(sorters[this.playersTableSort] || sorters.kills);
    },
    pagedPlayers() {
      const start = (this.playersTablePage - 1) * this.playersTablePageSize;
      return this.sortedPlayers.slice(start, start + this.playersTablePageSize);
    },
    playersTablePages() {
      return Math.max(1, Math.ceil(this.sortedPlayers.length / this.playersTablePageSize));
    },
    playerAnalytics() {
      const players = this.ov?.players?.players || [];
      const sum = (key) => players.reduce((total, player) => total + Number(player[key] || 0), 0);
      const pings = players.map((player) => Number(player.pingMs || 0)).filter((value) => value >= 0);
      return { kills: sum("kills"), deaths: sum("deaths"), cash: sum("cash"), avgPing: pings.length ? Math.round(pings.reduce((a,b)=>a+b,0)/pings.length) : 0, maxPing: pings.length ? Math.max(...pings) : 0 };
    },
    topPlayers() {
      return [...(this.ov?.players?.players || [])].sort((a,b)=>(b.kills||0)-(a.kills||0) || (b.cash||0)-(a.cash||0));
    },
    filteredBans() {
      const list = (this.ov && this.ov.bans && this.ov.bans.bans) || [];
      const query = this.bansQuery.trim().toLowerCase();
      const filtered = query
        ? list.filter((ban) => `${ban.steamId || ""} ${ban.displayName || ""} ${ban.reason || ""} ${ban.bannedBy || ""}`.toLowerCase().includes(query))
        : list;
      const time = (value) => {
        const parsed = Date.parse(value || "");
        return Number.isFinite(parsed) ? parsed : 0;
      };
      const sorters = {
        newest: (a, b) => time(b.bannedAtUtc) - time(a.bannedAtUtc),
        oldest: (a, b) => time(a.bannedAtUtc) - time(b.bannedAtUtc),
        steam: (a, b) => String(a.steamId || "").localeCompare(String(b.steamId || "")),
        reason: (a, b) => String(a.reason || "").localeCompare(String(b.reason || ""), "ru"),
        expiry: (a, b) => (a.expiresUtc || Number.MAX_SAFE_INTEGER) - (b.expiresUtc || Number.MAX_SAFE_INTEGER),
      };
      return [...filtered].sort(sorters[this.bansSort] || sorters.newest);
    },
    pagedBans() {
      const start = (this.bansPage - 1) * this.bansPageSize;
      return this.filteredBans.slice(start, start + this.bansPageSize);
    },
    bansPages() {
      return Math.max(1, Math.ceil(this.filteredBans.length / this.bansPageSize));
    },
    selectedBansCount() { return this.banSelected.length; },
    dirtyRolesCount() {
      return this.rolesList.filter((role) => this.roleDirty(role)).length;
    },
    selectedRolesCount() { return this.roleSelected.length; },
    filteredClans() {
      const query = this.clanQuery.trim().toLowerCase();
      if (!query) return this.clans.items;
      return this.clans.items.filter((clan) =>
        `${clan.name || ""} ${clan.tag || ""} ${clan.description || ""}`.toLowerCase().includes(query)
      );
    },
    filteredVips() {
      const query = this.vipQuery.trim().toLowerCase();
      return this.vip.items.filter((item) => {
        if (this.vipTierFilter && String(item.tier || 1) !== this.vipTierFilter) return false;
        if (this.vipFilter === "active" && !item.active) return false;
        if (this.vipFilter === "expired" && item.active) return false;
        if (this.vipFilter === "errors" && item.sync_state !== "error") return false;
        if (!query) return true;
        return `${item.display_name || ""} ${item.steam_id || ""} ${item.note || ""}`.toLowerCase().includes(query);
      });
    },
    filteredTickets() {
      const query = this.ticketQuery.trim().toLowerCase();
      return this.tickets.items.filter((ticket) => {
        if (this.ticketStatusFilter === "active" && ["resolved", "closed"].includes(ticket.status)) return false;
        if (!["all", "active"].includes(this.ticketStatusFilter) && ticket.status !== this.ticketStatusFilter) return false;
        if (this.ticketPriorityFilter !== "all" && ticket.priority !== this.ticketPriorityFilter) return false;
        if (!query) return true;
        return `${ticket.title || ""} ${ticket.description || ""} ${ticket.player_steam_id || ""} ${ticket.created_by_name || ""}`.toLowerCase().includes(query);
      });
    },
    nextEntry() {
      if (!this.ov || !this.ov.rotation) return null;
      return (this.ov.rotation.entries || []).find((e) => e.status === "next") || null;
    },
  },

  watch: {
    // Права могли забрать, пока панель открыта: не оставляем пользователя на чужой вкладке.
    perms() {
      if (MODE === "panel" && this.state.tab !== "server" && !this.tabs.some((t) => t.id === this.state.tab)) {
        this.state.tab = "status";
      }
    },
    playersView(view) {
      if (view === "stats") this.loadReport("playerStats");
      if (view === "names") this.loadReport("playerNames");
    },
    playersTableQuery() { this.playersTablePage = 1; },
    playersTableSort() { this.playersTablePage = 1; },
    bansQuery() { this.bansPage = 1; },
    bansSort() { this.bansPage = 1; },
    playerCardTab(tab) {
      if (tab === "notes" && this.playerCard.open) this.loadPlayerNotes();
      if (tab === "clan" && this.playerCard.open && this.has("clans")) this.loadClans();
    },
    sectionClosed(current, previous) {
      if (previous && !current) this.loadTabData(this.state.tab);
      else if (current) this.stopKillfeedPolling();
    },
    "state.tab"(t) {
      this.navOpen = false;
      this.loadTabData(t);
    },
    serverView(v) {
      if (v === "server") this.loadRoutes();
    },
    "state.sid"() {
      this.ensureCatalog();
      this.serverApiRoutes = [];
      this.serverBuild = "";
      if (!this.state.ovById[this.state.sid]) this.loadOverview(this.state.sid, true);
      if (this.sectionClosed) return;
      if (this.state.tab === "players") this.loadPlayerMeta();
      if (this.state.tab === "audit") { this.loadAudit(); this.loadSiteAudit(); }
      if (this.state.tab === "killfeed") { this.killfeedSetup = null; this.loadKillfeed(); }
      if (this.state.tab === "config") this.loadConfig();
      if (this.state.tab === "rotation") this.ensureCatalog();
      if (this.state.tab === "staff") this.loadStaff();
      if (this.state.tab === "discord") this.loadDiscordAdmin();
      if (this.state.tab === "system") this.loadSystemStatus();
      if (this.state.tab === "roles") this.loadRoles();
      if (this.state.tab === "reserved") this.loadReserved();
      if (this.state.tab === "vip") this.loadVipSlots();
    },
  },

  mounted() {
    const vm = this;
    this.api = {
      async request(method, url, body) {
        const mutation = ['POST','PUT','PATCH','DELETE'].includes(method) && !url.includes('/auth/');
        const operation = mutation ? vm.beginOperation(url, method) : null;
        const controller = new AbortController();
        const timeout = setTimeout(()=>controller.abort(), 25000);
        const opts = { method, credentials: "same-origin", signal: controller.signal };
        const csrf = document.cookie.split("; ").find((c) => c.startsWith("wds_csrf=") || c.startsWith("__Host-wds_csrf="))?.split("=")[1];
        if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && csrf) opts.headers = { "X-CSRF-Token": decodeURIComponent(csrf) };
        if (["POST", "PUT", "PATCH", "DELETE"].includes(method) &&
            (url.includes("/api/server/") || url.includes("/api/players/") ||
             url.startsWith("/api/clans") || url.startsWith("/api/tickets") ||
             url.startsWith("/api/discord/") || url === "/api/roles")) {
          const idempotencyKey = globalThis.crypto?.randomUUID?.() ||
            `${Date.now()}-${Math.random().toString(36).slice(2)}`;
          opts.headers = { ...(opts.headers || {}), "Idempotency-Key": idempotencyKey };
        }
        if (body !== undefined) {
          opts.headers = { ...(opts.headers || {}), "Content-Type": "application/json" };
          opts.body = JSON.stringify(body);
        }
        let r;
        try {
          r = await fetch(url, opts);
        } catch (error) {
          clearTimeout(timeout);
          vm.panelReachable = false;
          if (operation) Object.assign(operation,{state:'unknown',detail:'Ответ не получен. Проверьте результат перед повтором.'});
          return {
            status: 0,
            data: {
              ok: false,
              detail: "Сеть недоступна",
              error: { code: "network", message: String(error?.message || error) },
            },
          };
        }
        let data = null;
        try { data = await r.json(); } catch (e) { /* не JSON */ }
        clearTimeout(timeout);
        vm.panelReachable = true;
        if (r.status === 401 && url !== "/api/auth/logout") vm.handleUnauthorized();
        if (r.status === 503 && data?.error?.code === "maintenance") vm.loadMaintenance();
        if (operation) {
          const ok = r.ok && data && data.ok !== false && !data.error;
          Object.assign(operation,{state:ok ? (data.synced === false ? 'pending' : 'confirmed') : 'error', detail: ok ? (data.synced === false ? 'Сохранено, сервер ещё не подтвердил синхронизацию' : 'Запрос подтверждён API') : String(data?.detail || data?.error?.message || `Ошибка ${r.status}`)});
        }
        return { status: r.status, data };
      },
    };
    document.addEventListener("keydown", this.handleGlobalKeydown);
    document.addEventListener("visibilitychange", this.handleVisibilityChange);
    this._clock = setInterval(()=>{this.clockNow=Date.now();},1000);
      this._attentionTimer=setInterval(()=>{if(this.booted && !this.noAccess && !document.hidden && MODE==='panel') {this.loadMaintenance();if(this.state.tab==='status') this.loadAttention();if(this.state.tab==='players' && this.playersView==='live') this.loadPlayerMeta();if(this.state.tab==='discord' && !this.discordAdmin.loading) this.loadDiscordAdmin(true);}},30000);
    this.init();
  },
  beforeUnmount() {
    document.removeEventListener("keydown", this.handleGlobalKeydown);
    document.removeEventListener("visibilitychange", this.handleVisibilityChange);
    this.stopTimer();
    clearInterval(this._clock);
    clearInterval(this._attentionTimer);
  },

  methods: {
    beginOperation(url, method) {
      const names = {kick:'Кик игрока',kill:'Убийство игрока',message:'Сообщение игроку',broadcast:'Объявление',end:'Завершение матча',restart:'Перезапуск матча',map:'Смена карты',lighting:'Освещение',bans:'Баны','vip-slots':'VIP',bots:'Подключение бота',orders:'Заявка VIP'};
      const parts=url.split('/');
      const label=[...parts].reverse().map(p=>names[p]).find(Boolean) || 'Изменение данных';
      const sid=parts[2]==='server' ? parts[3] : '';
      this.operations.unshift({id:crypto.randomUUID(),label,server:this.state.servers.find(s=>s.id===sid)?.name || '',state:'sending',detail:'Ожидаем ответ',time:Date.now()});
      this.operations.splice(50);
      return this.operations[0];
    },
    operationLabel(state) { return {sending:'Отправлено',confirmed:'Подтверждено',pending:'Ожидает синхронизации',unknown:'Результат неизвестен',error:'Ошибка'}[state] || state; },
    async loadAttention() {
      const tasks=[];
      if((this.has('tickets_view') || this.has('tickets_edit')) && (this.has('owner') || !this.maintenance.closed.tickets)) tasks.push(this.api.request('GET','/api/tickets').then(r=>{this.overviewAttention.tickets=r.status===200 ? (r.data?.items || []).filter(t=>!['closed','resolved'].includes(t.status)).length : null;}));
      if((this.has('vip_view') || this.has('vip_edit')) && (this.has('owner') || !this.maintenance.closed.vip)) tasks.push(Promise.all(this.state.servers.map(async s=>{const r=await this.api.request('GET',`/api/server/${s.id}/vip-slots`); return r.status===200 ? (r.data.items || []).map(v=>({...v,server_name:s.name,server_id:s.id})) : null;})).then(rows=>{this.overviewAttention.vips=rows.flatMap(r=>r || []);this.overviewAttention.error=rows.some(r=>r===null) ? 'Не все серверы передали сведения VIP' : '';}));
      await Promise.allSettled(tasks);
    },
    saveViewPreferences() {
      if(!this.booted || MODE!=='panel') return;
      try { sessionStorage.setItem('wardogs-view',JSON.stringify(Object.fromEntries(['playersTableQuery','playersTableSort','bansQuery','bansSort','vipQuery','vipFilter','ticketQuery','ticketStatusFilter','ticketPriorityFilter'].map(k=>[k,this[k]])))); } catch {}
    },
    toast(text, kind = "") {
      this.toasts.push({ text, kind });
      setTimeout(() => this.toasts.shift(), 3500);
    },
    has(p) { return this.perms.includes(p); },
    permLabel(p) { return p === "owner" ? "Владелец панели" : (PERM_LABELS[p] || p); },
    _loadTabSections(t) {
      if (t === "controls") this.ensureCatalog();
      if (t === "rotation") this.ensureCatalog();
      if (t === "audit") { this.loadAudit(); this.loadSiteAudit(); }
      if (t === "journal") this.loadReport("auditLog");
      if (t === "matches") this.loadReport("matches");
      if (t === "players") this.loadPlayerMeta();
      if (t === "players" && this.playersView !== "live") this.loadReport(this.playersView === "stats" ? "playerStats" : "playerNames");
      if (t === "killfeed") this.startKillfeedPolling();
      else this.stopKillfeedPolling();
      if (t === "config") this.ensureConfig();
      if (t === "staff") this.loadStaff();
      if (t === "discord") this.loadDiscordAdmin();
      if (t === "system") this.loadSystemStatus();
      if (t === "clans") this.loadClans();
      if (t === "roles") this.loadRoles();
      if (t === "reserved") this.loadReserved();
      if (t === "vip") { this.loadVipSlots(); this.loadPayments(); }
      if (t === "tickets") { this.loadTickets(); this.loadTicketTypes(); }
      if (t === "server") this.loadRoutes();
      if (t === "dev") { this.loadDevSections(); this.loadVipPrices(); }
    },
    async loadTabData(t) {
      if (this.sectionClosed) { this.stopKillfeedPolling(); return; }
      this._loadTabSections(t);
    },
    // Один неудачный опрос списка игроков не должен очищать экран: показываем прошлый список,
    // а если сервер пропустил отдельного игрока, держим его строку ещё немного.
    keepPlayerList(id, cached, next) {
      const now = Date.now();
      const expected = Number(next.status?.players?.current || 0);
      const list = next.players?.players;
      const unusable = next.players_visible !== false && next.status && (!list || (!list.length && expected > 0));
      if (unusable && cached?.players && now - (cached._playersTs || 0) < PLAYER_LIST_KEEP_MS) {
        next.players = cached.players;
        next._playersTs = cached._playersTs;
        return next;
      }
      next._playersTs = now;
      const grace = { ...(this.playerGrace[id] || {}) };
      const current = new Set((list || []).map((player) => player.steamId));
      for (const player of cached?.players?.players || []) {
        if (!current.has(player.steamId) && !grace[player.steamId]) grace[player.steamId] = { ...player, _goneAt: now };
      }
      for (const steamId of Object.keys(grace)) {
        if (current.has(steamId) || now - grace[steamId]._goneAt > PLAYER_GONE_KEEP_MS) delete grace[steamId];
      }
      this.playerGrace[id] = next.players_visible === false ? {} : grace;
      return next;
    },
    async loadPlayerMeta() {
      const sid = this.state.sid;
      if (!sid || !this.ov?.players_visible) return;
      const { status, data } = await this.api.request("GET", `/api/server/${encodeURIComponent(sid)}/players/meta`);
      if (status === 200 && data?.ok) this.playerMeta[sid] = data.players || {};
    },
    cycleBoardSort() {
      const order = ["name", "kills", "hours"];
      this.boardSort = order[(order.indexOf(this.boardSort) + 1) % order.length];
    },
    queueBoardSearch() {
      clearTimeout(this._boardTimer);
      this.boardOffline = [];
      const query = this.boardQuery.trim();
      if (!query || !this.has("players_view")) return;
      this.boardSearchBusy = true;
      this._boardTimer = setTimeout(async () => {
        const { status, data } = await this.api.request("GET", `/api/players?q=${encodeURIComponent(query)}&limit=20`);
        if (query !== this.boardQuery.trim()) return;
        this.boardSearchBusy = false;
        const online = new Set(this.boardMatches.map((match) => match.player.steamId));
        this.boardOffline = status === 200 && data?.ok ? (data.items || []).filter((item) => !online.has(item.steam_id)) : [];
      }, 300);
    },
    openBoardMatch(match) {
      this.state.sid = match.server.id;
      this.openPlayerCard(match.player, match.player);
    },
    reportPages(report) { return Math.max(1, Math.ceil(report.total / report.perPage)); },
    // Поиск ждёт паузу в наборе, чтобы не отправлять запрос на каждую букву.
    queueReport(name) {
      clearTimeout(this._reportTimer);
      this._reportTimer = setTimeout(() => this.loadReport(name, 1), 300);
    },
    reportRequest(name, report) {
      const params = new URLSearchParams({ page: report.page, per_page: report.perPage });
      const add = (key, value) => { if (value) params.set(key, value); };
      if (name === "playerStats") {
        const [first, last] = this.statsRange(report);
        add("first_day", first); add("last_day", last); add("q", report.q); add("server", report.server);
        add("scope", report.scope); add("sort", report.sort); add("direction", report.direction);
        return `/api/players/stats?${params}`;
      }
      if (name === "playerNames") { add("q", report.q); return `/api/players/names?${params}`; }
      if (name === "matches") { add("server", report.server); add("player", report.player); return `/api/matches?${params}`; }
      add("q", report.q); add("section", report.section); add("server", report.server);
      return `/api/audit/search?${params}`;
    },
    statsRange(report) {
      if (report.period === "all") return ["", ""];
      if (report.period === "custom") return [report.firstDay, report.lastDay];
      const today = new Date();
      const iso = (date) => date.toISOString().slice(0, 10);
      const monday = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() - ((today.getUTCDay() + 6) % 7)));
      if (report.period === "week") return [iso(monday), iso(today)];
      return [iso(new Date(monday.getTime() - 7 * 86400000)), iso(new Date(monday.getTime() - 86400000))];
    },
    async loadReport(name, page) {
      const report = this.reports[name];
      if (page) report.page = page;
      const requestId = (report.requestId || 0) + 1;
      report.requestId = requestId;
      report.loading = true;
      report.error = "";
      const { status, data } = await this.api.request("GET", this.reportRequest(name, report));
      // Ответ на устаревший запрос не должен затереть результат более нового поиска.
      if (requestId !== report.requestId) return;
      report.loading = false;
      if (status === 200 && data?.ok) {
        report.items = data.items || [];
        report.total = data.total || 0;
        if (name === "matches") report.live = data.live || [];
      } else if (status !== 503) {
        report.error = data?.detail || data?.error?.message || "Не удалось загрузить данные";
      }
    },
    sortPlayerStats(key) {
      const report = this.reports.playerStats;
      if (report.sort === key) report.direction = report.direction === "asc" ? "desc" : "asc";
      else { report.sort = key; report.direction = key === "name" ? "asc" : "desc"; }
      this.loadReport("playerStats", 1);
    },
    setStatsPeriod(period) {
      const report = this.reports.playerStats;
      report.period = period;
      if (period !== "custom" || (report.firstDay && report.lastDay)) this.loadReport("playerStats", 1);
    },
    matchDuration(match) {
      const end = match.ended_utc || this.clockNow / 1000;
      return match.started_utc ? this.fmtPlaytime(Math.max(0, end - match.started_utc)) : "—";
    },
    auditActionLabel(event) {
      const labels = {
        "ban.add": "Бан", "ban.remove": "Разбан", "ban.update": "Изменение бана", "ban.expire": "Бан истёк",
        "rcon.kick": "Кик", "rcon.kill": "Slay", "rcon.message": "Варн", "rcon.move_faction": "Смена стороны",
        "rcon.change_map": "Смена карты", "rcon.change_lighting": "Смена освещения", "rcon.match_end": "Завершение матча",
        "rcon.match_restart": "Перезапуск матча", "broadcast": "Объявление", "rotation.update": "Изменение ротации",
        "dev.vip_prices": "Цены VIP", "vip.ticket": "Личный тикет VIP", "vip.ticket.close": "Личный тикет закрыт автоматически", "vip.save": "Выдача VIP", "vip.delete": "Отзыв VIP", "vip.expire": "VIP истёк", "reserved.add": "Резерв выдан",
        "reserved.remove": "Резерв снят", "reserved.limit": "Лимит резерва", "roles.update": "Права роли",
        "config.edit": "Настройки", "config.put": "Конфиг сервера", "dev.maintenance": "Техработы",
        "player.punishment.hide": "Скрыта запись наказания", "player.punishment.clear": "Очищена история наказаний",
      };
      return labels[event] || event;
    },
    auditPlayerId(entry) { return /^\d{17}$/.test(entry.detail || ""); },
    showAuditJson(entry) {
      let detail = entry.detail;
      try { detail = JSON.parse(entry.detail); } catch { /* обычный текст */ }
      this.auditJson = JSON.stringify({ ...entry, detail }, null, 2);
    },
    async loadMaintenance() {
      const { status, data } = await this.api.request("GET", "/api/maintenance");
      if (status === 200 && data?.ok) this.maintenance.closed = data.closed || {};
    },
    async loadVipPrices() {
      const { status, data } = await this.api.request("GET", "/api/dev/vip-prices");
      if (status === 200 && data?.ok) {
        this.vipPrices.items = data.items || [];
        this.vipPrices.maxRub = data.max_rub || this.vipPrices.maxRub;
      } else {
        this.vipPrices.error = data?.detail || "Не удалось загрузить цены";
      }
    },
    async saveVipPrices() {
      if (this.vipPrices.saving) return;
      this.vipPrices.saving = true;
      this.vipPrices.error = "";
      const items = this.vipPrices.items.map((item) => ({
        id: item.id, enabled: !!item.enabled, rub: item.rub === "" || item.rub == null ? null : item.rub,
      }));
      const { status, data } = await this.api.request("PUT", "/api/dev/vip-prices", { items });
      this.vipPrices.saving = false;
      if (status !== 200 || !data?.ok) {
        this.vipPrices.error = data?.detail || "Не удалось сохранить цены";
        return;
      }
      this.vipPrices.items = data.items || [];
      if (data.discord?.ok) this.toast("Цены сохранены, сообщение в Discord обновлено", "ok");
      else this.toast(`Цены сохранены. Discord не обновлён: ${data.discord?.error || "бот недоступен"}`, "warn");
    },
    async loadDevSections() {
      this.devSections.loading = true;
      this.devSections.error = "";
      const { status, data } = await this.api.request("GET", "/api/dev/sections");
      this.devSections.loading = false;
      if (status !== 200 || !data?.ok) {
        this.devSections.error = data?.detail || "Не удалось загрузить разделы";
        return;
      }
      this.devSections.defaultMessage = data.default_message || "";
      this.devSections.items = (data.sections || []).map((item) => ({
        ...item,
        draftMessage: item.message || "",
        draftUntil: item.until_utc ? this.toLocalInput(item.until_utc) : "",
      }));
    },
    toLocalInput(seconds) {
      const date = new Date(seconds * 1000);
      return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
    },
    async saveDevSection(item, closed) {
      if (this.devSections.saving) return;
      let untilUtc = null;
      if (closed && item.draftUntil) {
        const parsed = new Date(item.draftUntil).getTime();
        if (!Number.isFinite(parsed)) { this.toast("Некорректное время окончания", "err"); return; }
        untilUtc = parsed / 1000;
      }
      this.devSections.saving = item.id;
      const { status, data } = await this.api.request("PUT", `/api/dev/sections/${encodeURIComponent(item.id)}`, {
        closed, message: item.draftMessage, until_utc: untilUtc,
      });
      this.devSections.saving = "";
      if (status !== 200 || !data?.ok) {
        this.toast(data?.detail || "Не удалось сохранить", "err");
        return;
      }
      this.toast(closed ? `«${item.label}»: закрыт на техработы` : `«${item.label}»: снова открыт`, closed ? "warn" : "ok");
      this.maintenance.closed = data.closed || {};
      await this.loadDevSections();
    },
    // Скрытая вкладка не опрашивает сервер; при возврате данные обновляются сразу.
    handleVisibilityChange() {
      if (!document.hidden && this.booted && !this.noAccess && this._timer) this.refreshAll(true);
    },
    async handleUnauthorized() {
      if (MODE !== "panel" || this.noAccess || this._authCheck) return;
      this._authCheck = true;
      try {
        // Обновление ролей меняет сессию: запрос со старой cookie мог получить 401, хотя вход в порядке.
        const response = await fetch("/api/session", { credentials: "same-origin" });
        const data = response.ok ? await response.json() : null;
        if (data?.ok && !data.authed) {
          this.state.authed = false;
          this.state.me = null;
          this.noAccess = true;
          this.toast("Сессия завершена, войдите снова", "warn");
        }
      } catch {
        // Сеть недоступна: об этом уже сообщает индикатор связи.
      } finally {
        this._authCheck = false;
      }
    },
    permHint(p) { return PERM_HINTS[p] || p; },
    handleGlobalKeydown(event) {
      if (event.key === "/" && this.state.tab === "players" && !["INPUT", "TEXTAREA", "SELECT"].includes(event.target?.tagName)) {
        const input = document.getElementById("faction-search-input");
        if (input) { event.preventDefault(); input.focus(); return; }
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        document.querySelector(".admin-search input")?.focus();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "r") {
        event.preventDefault();
        this.refreshAll(true);
        return;
      }
      if (event.key === "Tab" && (this.actionDialog.open || this.playerCard.open)) {
        const root = document.querySelector(this.actionDialog.open ? ".action-dialog" : ".player-card");
        const focusable = [...(root?.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])') || [])];
        if (!focusable.length) return;
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        return;
      }
      if (event.key === "Escape") {
        if (this.actionDialog.open) this.cancelActionDialog();
        else if (this.playerCard.open) this.closePlayerCard();
        else if (this.profile.open) this.closeProfileCard();
      }
    },
    openActionDialog({ title, message = "", confirmText = "Подтвердить", danger = false, fields = [] }) {
      if (this._dialogResolve) this._dialogResolve(null);
      this._stopPersonaWatch();
      this.actionDialog = {
        open: true,
        title,
        message: danger ? `${message}\nВыбранный сервер: ${this.state.servers.find(s=>s.id===this.state.sid)?.name || this.state.sid}` : message,
        confirmText,
        danger,
        fields: fields.map((field) => ({ ...field, value: field.value ?? "" })),
        error: "",
      };
      this._setupPersonaAutofill();
      return new Promise((resolve) => {
        this._dialogResolve = resolve;
        this.$nextTick(() => document.querySelector(".action-dialog input, .action-dialog textarea")?.focus());
      });
    },
    _stopPersonaWatch() {
      if (this._personaWatchStop) {
        this._personaWatchStop();
        this._personaWatchStop = null;
      }
    },
    _setupPersonaAutofill() {
      const dialog = this.actionDialog;
      const steamField = dialog?.fields?.find((f) => f.validator === "steam64");
      const nameField = dialog?.fields?.find((f) => f.key === "display_name");
      if (!steamField || !nameField) return;
      let timer = null;
      let seq = 0;
      const stop = this.$watch(() => String(steamField.value || "").trim(), (value) => {
        if (!/^\d{17}$/.test(value)) return;
        clearTimeout(timer);
        const mySeq = ++seq;
        timer = setTimeout(async () => {
          if (String(nameField.value || "").trim()) return;
          try {
            const { status, data } = await this.api.request("GET", `/api/steam/persona?steam_id=${value}`);
            if (mySeq !== seq) return;
            if (status === 200 && data?.ok && data.name && !String(nameField.value || "").trim()) {
              nameField.value = data.name;
            }
          } catch { /* silent */ }
        }, 450);
      });
      this._personaWatchStop = () => {
        clearTimeout(timer);
        stop();
      };
    },
    confirmAction(title, message, confirmText = "Подтвердить", danger = true) {
      return this.openActionDialog({ title, message, confirmText, danger });
    },
    submitActionDialog() {
      const values = {};
      for (const field of this.actionDialog.fields) {
        const value = String(field.value ?? "").trim();
        if (field.required && !value) {
          this.actionDialog.error = `Заполните поле «${field.label}»`;
          return;
        }
        if (field.validator === "steam64" && value && !/^\d{17}$/.test(value)) {
          this.actionDialog.error = "SteamID64 должен состоять из 17 цифр";
          return;
        }
        if (field.validator === "discordid" && value && !/^\d{1,20}$/.test(value)) {
          this.actionDialog.error = "Discord ID — от 1 до 20 цифр";
          return;
        }
        if (field.maxLength && value.length > field.maxLength) {
          this.actionDialog.error = `Поле «${field.label}» длиннее ${field.maxLength} символов`;
          return;
        }
        values[field.key] = value;
      }
      const resolve = this._dialogResolve;
      this._dialogResolve = null;
      this._stopPersonaWatch();
      this.actionDialog.open = false;
      if (resolve) resolve(values);
    },
    cancelActionDialog() {
      const resolve = this._dialogResolve;
      this._dialogResolve = null;
      this._stopPersonaWatch();
      this.actionDialog.open = false;
      if (resolve) resolve(null);
    },

    async init() {
      if (MODE === "monitor" && ["#community", "#support"].includes(location.hash)) {
        location.replace("/community");
        return;
      }
      const { data } = await this.api.request("GET", "/api/session");
      if (!data || !data.ok) { this.booted = true; return; }
      this.state.authed = !!data.authed;
      this.state.me = data.me || null;
      this.authConfigured = !!data.auth_configured;
      this.devAuthEnabled = !!data.dev_auth_enabled;
      this.state.servers = data.servers || [];
      this.state.refresh = data.refresh || 5;
      if (this.state.servers.length) this.state.sid = this.state.sid || this.state.servers[0].id;

      if (MODE === "panel" && (!this.state.authed || !this.hasPerms)) {
        this.noAccess = true;
        this.booted = true;
        // Если права выдадут или Discord снова ответит, панель откроется без перезагрузки вручную.
        if (this.state.authed) this.startTimer();
        return;
      }

      const q = new URLSearchParams(location.search);
      if (q.get("bind") === "discord" && q.get("ok")) this.toast("Discord привязан", "ok");
      else if (q.get("bind") === "discord") this.toast(`Привязка Discord не удалась (${q.get("fail") || "?"})`, "err");
      else if (q.get("auth") === "steam" && q.get("fail")) this.toast("Вход через Steam не удался", "err");
      if (MODE === "panel" && this.has("roles")) this.loadRoles();
      if (MODE === "panel") await this.loadMaintenance();
      if (location.hash === "#profile" && this.state.authed) this.openProfileCard();
      if (MODE === "panel") {
        try { const saved=JSON.parse(sessionStorage.getItem('wardogs-view') || '{}'); for(const k of ['playersTableQuery','playersTableSort','bansQuery','bansSort','vipQuery','vipFilter','ticketQuery','ticketStatusFilter','ticketPriorityFilter']) if(typeof saved[k]==='string') this[k]=saved[k]; } catch {}
        this.$watch(()=>[this.playersTableQuery,this.playersTableSort,this.bansQuery,this.bansSort,this.vipQuery,this.vipFilter,this.ticketQuery,this.ticketStatusFilter,this.ticketPriorityFilter],()=>this.saveViewPreferences());
        this.loadAttention();
      }
      this.booted = true;
      this.startTimer();
      this.refreshAll(false);
    },

    loginSteam() { location.href = "/api/auth/steam/start"; },
    async loginDev() {
      const { status, data } = await this.api.request("POST", "/api/auth/dev");
      if (status === 200 && data && data.ok) location.href = data.redirect || "/panel";
      else this.toast("Локальный вход недоступен", "err");
    },
    bindDiscord() { location.href = "/api/auth/discord/start"; },
    goPanel() { location.href = "/panel"; },
    goProfile() { this.openProfileCard(); },
    async refreshRoles(silent = false) {
      // Фоновое обновление не должно блокировать действия пользователя через busy.
      if (!silent) this.busy = true;
      const { data } = await this.api.request("POST", "/api/auth/refresh");
      if (!silent) this.busy = false;
      if (data && data.authed) {
        const wasLocked = this.noAccess;
        this.state.me = data;
        if (!silent) this.toast(this.perms.length ? "Права обновлены" : "Роли обновлены, права не выданы", this.perms.length ? "ok" : "warn");
        if (MODE === "panel" && !this.hasPerms) this.noAccess = true;
        else if (MODE === "panel" && wasLocked) location.reload();
      } else {
        if (!silent) this.toast("Не удалось обновить роли", "err");
      }
    },
    async logout() {
      await this.api.request("POST", "/api/auth/logout");
      this.stopTimer();
      location.href = "/";
    },

    selectServer(id) {
      this.state.sid = id;
      this.navOpen = false;
      if (MODE === "panel") { this.serverView = "overview"; this.state.tab = "server"; }
    },

    openPlayerCommand(label) {
      this.state.tab = "players";
      this.toast(`${label}: выберите игрока в списке`, "ok");
    },

    factionPlayerCount(name) {
      const players = this.ov?.players?.players || [];
      return players.filter((player) => String(player.faction || "").toLowerCase() === String(name || "").toLowerCase()).length;
    },

    factionAnalytics(name) {
      const players = (this.ov?.players?.players || []).filter((player) => String(player.faction || "").toLowerCase() === String(name || "").toLowerCase());
      const sum = (key) => players.reduce((total, player) => total + Number(player[key] || 0), 0);
      return { players: players.length, kills: sum("kills"), deaths: sum("deaths"), cash: sum("cash"), ping: players.length ? Math.round(sum("pingMs") / players.length) : 0 };
    },

    factionColor(name) {
      return (this.ov?.status?.factionScores || []).find((faction) => faction.name === name)?.colorHex || KNOWN_FACTION_COLORS[String(name || "").toLowerCase()] || "#777";
    },

    nickClass(steamId) {
      const meta = this.playerMeta[this.state.sid]?.[steamId];
      if (!meta) return "";
      if (meta.rank) return `rank-${meta.rank}`;
      return meta.vip ? "nick-vip" : "";
    },

    chooseFaction(player, faction, event) {
      event?.currentTarget?.closest("details")?.removeAttribute("open");
      this.moveFaction(player, faction);
    },

    isOffline(sid) {
      const o = this.state.ovById[sid];
      const c=this.connections[sid];
      return !o || !!o.error || (c && (!c.ok || this.clockNow-c.success > Math.max(20000,this.state.refresh*3000)));
    },
    plCount(sid) {
      const o = this.state.ovById[sid];
      if (!o || o.error || !o.status) return null;
      const p = o.status.players || {};
      return `${p.current}/${p.max}`;
    },
    livePhase(sid) {
      const o = this.state.ovById[sid];
      const current = Number(o?.status?.players?.current || 0);
      return current >= 20 ? "Идёт матч" : "Сид";
    },
    totalPlayers() {
      return Object.values(this.state.ovById).reduce((sum, o) => {
        if (!o || o.error || !o.status) return sum;
        return sum + Number((o.status.players || {}).current || 0);
      }, 0);
    },

    startTimer() {
      this.stopTimer();
      this._timer = setInterval(() => { if (!this.noAccess && !document.hidden) this.refreshAll(false); }, this.state.refresh * 1000);
      if (this.state.authed && this.authConfigured) {
        this._roleTimer = setInterval(() => { if (!document.hidden) this.refreshRoles(true); }, 60 * 1000);
      }
    },
    stopTimer() {
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
      if (this._roleTimer) { clearInterval(this._roleTimer); this._roleTimer = null; }
    },
    setPlayersTablePage(page) {
      this.playersTablePage = Math.max(1, Math.min(Number(page) || 1, this.playersTablePages));
    },
    setBansPage(page) {
      this.bansPage = Math.max(1, Math.min(Number(page) || 1, this.bansPages));
    },
    async refreshBans() {
      await this.loadOverview(this.state.sid, true);
      this.bansPage = Math.min(this.bansPage, this.bansPages);
      this.toast("Бан-лист обновлён", "ok");
    },
    async copyText(value, label = "Значение") {
      try {
        await navigator.clipboard.writeText(String(value || ""));
        this.toast(`${label} скопирован`, "ok");
      } catch {
        this.toast("Не удалось скопировать", "err");
      }
    },

    async refreshAll(force) {
      // Опрос не ждёт все серверы разом: зависший сервер не должен задерживать остальные.
      if (!force) {
        for (const server of this.state.servers) {
          this.loadOverview(server.id, false).then(() => { this.lastUpdate = new Date().toLocaleTimeString("ru-RU"); });
        }
        return;
      }
      if (this.state.refreshing) return;
      this.state.refreshing = true;
      try {
        await Promise.allSettled(this.state.servers.map((s) => this.loadOverview(s.id, true)));
        this.lastUpdate = new Date().toLocaleTimeString("ru-RU");
      } finally {
        this.state.refreshing = false;
      }
    },

    async loadOverview(id, force) {
      const cached = this.state.ovById[id];
      if (cached && !force && Date.now() - (cached._ts || 0) < 1500) return;
      this._overviewInFlight = this._overviewInFlight || {};
      if (this._overviewInFlight[id] && !force) return;
      this._overviewInFlight[id] = true;
      try {
        const { status, data } = await this.api.request("GET", `/api/server/${id}/overview${force ? "?fresh=1" : ""}`);
        const ok=status===200 && data && data.ok!==false && !data.error;
        this.connections[id]={ok:!!ok,success:ok ? Date.now() : this.connections[id]?.success || null,detail:ok ? '' : data?.error?.message || data?.detail || 'Не удалось получить состояние сервера'};
        if(ok || !cached?.status) {
          const next=data?.status || data?.error ? {...data,_ts:Date.now()} : {error:{message:'Сервер недоступен'},_ts:Date.now()};
          this.state.ovById[id]=this.keepPlayerList(id,cached,next);
        }
      } catch {
        this.connections[id]={ok:false,success:this.connections[id]?.success || null,detail:'Нет связи с панелью'};
      } finally {
        this._overviewInFlight[id] = false;
      }
    },

    async loadReserved() {
      if (!this.state.sid) return;
      const { status, data } = await this.api.request("GET", `/api/server/${this.state.sid}/reserved-slots`);
      if (status === 200 && data) {
        this.reserved = { ...data, slots: data.reserved_slots || [], loaded: true };
        this.reservedLimit = Number.isInteger(data.max_reserved_slots) ? data.max_reserved_slots : 0;
      }
      else this.toast((data && (data.detail || data.error?.message)) || "Резерв недоступен", "err");
    },
    async searchPlayers() {
      const query = this.playerSearch.trim();
      if (!query || !this.has("players_view")) {
        this.playerSearchResults = [];
        return;
      }
      this.playerSearchBusy = true;
      try {
        const { status, data } = await this.api.request("GET", `/api/players?q=${encodeURIComponent(query)}&limit=20`);
        this.playerSearchResults = status === 200 && data?.ok ? (data.items || []) : [];
        if (status >= 400) this.toast(data?.detail || "Поиск игроков недоступен", "err");
      } finally {
        this.playerSearchBusy = false;
      }
    },
    async loadPlayerActivity(steamId, requestId) {
      const { status, data } = await this.api.request("GET", `/api/players/${encodeURIComponent(steamId)}/activity`);
      if (requestId !== this._playerRequestId || !this.playerCard.profile) return;
      if (status === 200 && data?.ok) this.playerCard.activity = data;
      else this.playerCard.activity = { daily: [], sessions: [], matches: [], kills: [], teamkills: [], seed_seconds: 0, seed_balance: null, clan_vip_until: null, wins: 0, losses: 0, win_rate: null, rating: null, paid_tier: 0, paid_until: null, stats: { kills: 0, deaths: 0, teamkills: 0, headshots: 0 } };
    },
    fmtHours(seconds) { return `${(Number(seconds || 0) / 3600).toFixed(1).replace(".", ",")} ч`; },
    fmtThousands(value) { return Number(value || 0).toLocaleString("ru-RU"); },
    fmtDay(seconds) { return new Date(seconds * 1000).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }); },
    cardServerName(id) { return this.state.servers.find((server) => server.id === id)?.name || id || "панель"; },
    punishmentShort(event) { return { "ban.add": "BAN", "ban.remove": "UNBAN", "rcon.kick": "KICK", "rcon.kill": "SLAY", "rcon.message": "WARN" }[event] || event; },
    punishmentStatus(status) { return { active: "Действует", expired: "Истёк", removed: "Разбанен", replaced: "Заменён" }[status] || ""; },
    punishmentTerm(item) {
      if (!item.expires_utc) return item.reason ? "навсегда" : "срок не указан";
      const days = Math.max(0, (item.expires_utc - item.timestamp_utc) / 86400);
      return days >= 1 ? `${Math.round(days)} д` : `${Math.max(1, Math.round(days * 24))} ч`;
    },
    killKind(kill) {
      if (kill.suicide) return "СУИЦИД";
      if (kill.teamkill) return "ТК";
      return kill.victim_id === this.playerCard.profile?.steam_id ? "СМЕРТЬ" : "УБИЙСТВО";
    },
    killTitle(kill) {
      if (kill.suicide) return kill.victim_name || "Игрок";
      if (kill.victim_id === this.playerCard.profile?.steam_id) return kill.killer_name || "Окружение";
      return kill.victim_name || "Игрок";
    },
    async loadPlayerSteam(steamId, requestId) {
      const { status, data } = await this.api.request("GET", `/api/players/${encodeURIComponent(steamId)}/steam`);
      if (requestId !== this._playerRequestId || !this.playerCard.profile) return;
      if (status === 200 && data?.ok) {
        this.playerCard.profile.avatar_url = data.avatar_url || "";
        this.playerCard.profile.steam_profile_url = data.steam_profile_url;
        this.playerCard.profile.steam = data.steam;
        this.playerCard.profile.game_hours = data.game_hours;
      } else {
        this.playerCard.profile.steam = { available: false };
      }
    },
    async openPlayerCard(playerOrSteamId, live = null) {
      const steamId = typeof playerOrSteamId === "string" ? playerOrSteamId : playerOrSteamId?.steamId;
      if (!steamId) return;
      this._playerReturnFocus=document.activeElement;
      const requestId=(this._playerRequestId || 0)+1; this._playerRequestId=requestId;
      live=live || (this.ov?.players?.players || []).find(p=>p.steamId===steamId) || null;
      this.playerSearchResults = [];
      this.playerCardTab = "punishments";
      this.playerCardClanId = "";
      this.playerNotes = { items: [], loading: false, error: "", canEdit: false, draft: "" };
      this.selectedPunishmentIds = [];
      this.playerCard = { open: true, loading: true, profile: null, live: live || (typeof playerOrSteamId === "object" ? playerOrSteamId : null), error: "" };
      this.playerCardFaction = this.playerCard.live?.faction || "";
      const { status, data } = await this.api.request("GET", `/api/players/${encodeURIComponent(steamId)}`);
      if(requestId!==this._playerRequestId || !this.playerCard.open) return;
      if (status === 200 && data?.profile) {
        this.playerCard.profile = data.profile;
        this.playerCardClanId = data.profile.clan?.id || "";
      } else if (this.playerCard.live) {
        const livePlayer = this.playerCard.live;
        this.playerCard.profile = {
          steam_id: livePlayer.steamId,
          name: livePlayer.name || "Игрок",
          kills: Number(livePlayer.kills || 0),
          deaths: Number(livePlayer.deaths || 0),
          kd: Number(livePlayer.kills || 0) / Math.max(1, Number(livePlayer.deaths || 0)),
          server_count: 1,
          total_seconds: 0,
          session_count: 1,
          current_session_seconds: 0,
          first_seen_utc: null,
          last_seen_utc: null,
          punishments: [],
          can_view_punishments: ["audit", "ban_view", "ban_add", "ban_remove"].some((permission) => this.has(permission)),
          can_manage_punishments: this.has("audit"),
          tags: [],
          can_edit_tags: this.has("player_tags"),
          clan: null,
          can_edit_clan: this.has("clans"),
          vip_slots: [],
          can_view_vip: this.has("vip_view") || this.has("vip_edit"),
          servers: [{
            server_id: this.state.sid,
            kills: Number(livePlayer.kills || 0),
            deaths: Number(livePlayer.deaths || 0),
            cash: Number(livePlayer.cash || 0),
            ping_ms: Number(livePlayer.pingMs || 0),
            faction: livePlayer.faction || "",
            seen_utc: null,
            total_seconds: 0,
            session_count: 1,
            current_session_seconds: 0,
            online: true,
          }],
        };
        this.playerCard.error = "";
      } else {
        this.playerCard.error = data?.detail || "Карточка игрока пока пуста";
      }
      this.playerCard.loading = false;
      if (this.playerCard.profile) {
        this.loadPlayerCheaterCheck(steamId, requestId);
        this.loadPlayerSteam(steamId, requestId);
        this.loadPlayerActivity(steamId, requestId);
        if (this.has("player_notes")) this.loadPlayerNotes();
      }
    },
    async loadPlayerCheaterCheck(steamId, requestId) {
      this.playerCard.cheater = { loading: true, configured: true, reason: "", data: null };
      const { status, data } = await this.api.request("GET", `/api/players/${encodeURIComponent(steamId)}/cheatercheck`);
      if (requestId !== this._playerRequestId || !this.playerCard.open) return;
      if (status === 200 && data?.ok) {
        this.playerCard.cheater = { loading: false, configured: data.configured !== false, reason: data.reason || "", data: data.player || null };
      } else {
        this.playerCard.cheater = { loading: false, configured: true, reason: "unavailable", data: null };
      }
    },
    closePlayerCard() {
      this._playerRequestId=(this._playerRequestId || 0)+1;
      this._playerReturnFocus?.focus?.({preventScroll:true});
      this.playerCardTab = "punishments";
      this.playerCardFaction = "";
      this.playerCardClanId = "";
      this.playerNotes = { items: [], loading: false, error: "", canEdit: false, draft: "" };
      this.playerCard = { open: false, loading: false, profile: null, live: null, error: "" };
    },
    playerCardSteamId() {
      return this.playerCard.profile?.steam_id || this.playerCard.live?.steamId || "";
    },
    async loadPlayerNotes() {
      const steamId = this.playerCardSteamId();
      if (!steamId || this.playerNotes.loading) return;
      this.playerNotes.loading = true;
      this.playerNotes.error = "";
      try {
        const { status, data } = await this.api.request("GET", `/api/players/${encodeURIComponent(steamId)}/notes`);
        if (status === 200 && data?.ok) {
          this.playerNotes.items = data.notes || [];
          this.playerNotes.canEdit = !!data.can_edit;
        } else {
          this.playerNotes.error = data?.detail || data?.error?.message || "Не удалось загрузить заметки";
        }
      } finally {
        this.playerNotes.loading = false;
      }
    },
    async addPlayerNote() {
      const steamId = this.playerCardSteamId();
      const note = this.playerNotes.draft.trim();
      if (!steamId || !note || !this.playerNotes.canEdit || this.busy) return;
      if (note.length > 2000) return this.toast("Заметка длиннее 2000 символов", "warn");
      this.busy = true;
      try {
        const { status, data } = await this.api.request(
          "POST",
          `/api/players/${encodeURIComponent(steamId)}/notes`,
          { note },
        );
        if (status === 200 && data?.ok) {
          this.playerNotes.items.unshift(data.note);
          this.playerNotes.draft = "";
          this.toast("Заметка добавлена", "ok");
        } else {
          this.toast(data?.detail || data?.error?.message || "Не удалось добавить заметку", "err");
        }
      } finally {
        this.busy = false;
      }
    },
    async deletePlayerNote(item) {
      const steamId = this.playerCardSteamId();
      if (!steamId || !item?.id || !this.playerNotes.canEdit) return;
      const confirmed = await this.confirmAction(
        "Удалить заметку",
        "Заметка будет удалена без возможности восстановления. Действие попадёт в аудит.",
        "Удалить",
      );
      if (!confirmed) return;
      this.busy = true;
      try {
        const { status, data } = await this.api.request(
          "DELETE",
          `/api/players/${encodeURIComponent(steamId)}/notes/${encodeURIComponent(item.id)}`,
          {},
        );
        if (status === 200 && data?.ok) {
          this.playerNotes.items = this.playerNotes.items.filter((note) => note.id !== item.id);
          this.toast("Заметка удалена", "ok");
        } else {
          this.toast(data?.detail || data?.error?.message || "Не удалось удалить заметку", "err");
        }
      } finally {
        this.busy = false;
      }
    },
    async addPlayerTag() {
      const steamId = this.playerCardSteamId();
      if (!steamId || !this.playerCard.profile?.can_edit_tags) return;
      const values = await this.openActionDialog({
        title: "Добавить метку игроку",
        message: "Метка хранится в панели и видна сотрудникам в карточке игрока.",
        confirmText: "Добавить",
        fields: [
          { key: "label", label: "Название", required: true, maxLength: 32, placeholder: "Например: требует наблюдения" },
          {
            key: "color", label: "Цвет", type: "select", value: "orange",
            options: [
              { value: "gray", label: "Серый" }, { value: "orange", label: "Оранжевый" },
              { value: "red", label: "Красный" }, { value: "green", label: "Зелёный" },
              { value: "blue", label: "Синий" }, { value: "purple", label: "Фиолетовый" },
              { value: "pink", label: "Розовый" },
            ],
          },
        ],
      });
      if (!values) return;
      const { status, data } = await this.api.request(
        "POST",
        `/api/players/${encodeURIComponent(steamId)}/tags`,
        values,
      );
      if (status === 200 && data?.ok) {
        const exists = (this.playerCard.profile.tags || []).some((tag) => tag.id === data.tag.id);
        if (!exists) this.playerCard.profile.tags.push(data.tag);
        this.toast(exists ? "Такая метка уже установлена" : "Метка добавлена", exists ? "warn" : "ok");
      } else {
        this.toast(data?.detail || data?.error?.message || "Не удалось добавить метку", "err");
      }
    },
    isPunishmentSelected(item) {
      return this.selectedPunishmentIds.includes(item.id);
    },
    togglePunishmentSelection(item) {
      this.selectedPunishmentIds = this.isPunishmentSelected(item)
        ? this.selectedPunishmentIds.filter((id) => id !== item.id)
        : [...this.selectedPunishmentIds, item.id];
    },
    toggleAllPunishments() {
      const all = this.playerCard.profile?.punishments || [];
      this.selectedPunishmentIds = this.selectedPunishmentIds.length === all.length ? [] : all.map((item) => item.id);
    },
    async hidePunishments(ids) {
      const steamId = this.playerCardSteamId();
      if (!steamId || !ids.length || !this.playerCard.profile?.can_manage_punishments) return;
      const confirmed = await this.confirmAction("Скрыть записи", `Скрыть из истории наказаний записей: ${ids.length}? Журнал аудита сохраняется.`, "Скрыть");
      if (!confirmed) return;
      const { status, data } = await this.api.request(
        "POST",
        `/api/players/${encodeURIComponent(steamId)}/punishments/hide`,
        { ids },
      );
      if (status === 200 && data?.ok) {
        const hidden = new Set(data.hidden || []);
        this.playerCard.profile.punishments = this.playerCard.profile.punishments.filter((entry) => !hidden.has(entry.id));
        this.selectedPunishmentIds = this.selectedPunishmentIds.filter((id) => !hidden.has(id));
        this.toast(`Скрыто записей: ${hidden.size}`, "ok");
      } else {
        this.toast(data?.detail || data?.error?.message || "Не удалось скрыть записи", "err");
      }
    },
    async clearPunishments() {
      const steamId = this.playerCardSteamId();
      if (!steamId || !this.playerCard.profile?.can_manage_punishments) return;
      const confirmed = await this.confirmAction("Очистить историю", "Скрыть всю историю наказаний этого игрока в карточке? Журнал аудита сохраняется.", "Очистить");
      if (!confirmed) return;
      const { status, data } = await this.api.request(
        "DELETE",
        `/api/players/${encodeURIComponent(steamId)}/punishments`,
        {},
      );
      if (status === 200 && data?.ok) {
        this.playerCard.profile.punishments = [];
        this.selectedPunishmentIds = [];
        this.toast("История очищена", "ok");
      } else {
        this.toast(data?.detail || data?.error?.message || "Не удалось очистить историю", "err");
      }
    },
    async deletePlayerTag(item) {
      const steamId = this.playerCardSteamId();
      if (!steamId || !item?.id || !this.playerCard.profile?.can_edit_tags) return;
      const confirmed = await this.confirmAction("Удалить метку", `Удалить метку «${item.label}»?`, "Удалить");
      if (!confirmed) return;
      const { status, data } = await this.api.request(
        "DELETE",
        `/api/players/${encodeURIComponent(steamId)}/tags/${encodeURIComponent(item.id)}`,
        {},
      );
      if (status === 200 && data?.ok) {
        this.playerCard.profile.tags = this.playerCard.profile.tags.filter((tag) => tag.id !== item.id);
        this.toast("Метка удалена", "ok");
      } else {
        this.toast(data?.detail || data?.error?.message || "Не удалось удалить метку", "err");
      }
    },
    async loadClans(force = false) {
      if (this.clans.loading || (this.clans.items.length && !force)) return;
      this.clans.loading = true;
      this.clans.error = "";
      try {
        const { status, data } = await this.api.request("GET", "/api/clans");
        if (status === 200 && data?.ok) {
          this.clans.items = data.clans || [];
        } else {
          this.clans.error = data?.detail || data?.error?.message || "Не удалось загрузить кланы";
        }
      } finally {
        this.clans.loading = false;
      }
    },
    async openClan(clan) {
      const { status, data } = await this.api.request("GET", `/api/clans/${clan.id}`);
      if (status === 200 && data?.ok) this.selectedClan = data.clan;
      else this.toast(data?.detail || "Не удалось открыть карточку клана", "err");
    },
    clanDialogFields(clan = null) {
      return [
        { key: "name", label: "Название", required: true, maxLength: 80, value: clan?.name || "", placeholder: "Полное название клана" },
        { key: "tag", label: "Тег", required: true, maxLength: 16, value: clan?.tag || "", placeholder: "TAG" },
        {
          key: "color", label: "Цвет", type: "select", value: clan?.color || "orange",
          options: [
            { value: "gray", label: "Серый" }, { value: "orange", label: "Оранжевый" },
            { value: "red", label: "Красный" }, { value: "green", label: "Зелёный" },
            { value: "blue", label: "Синий" }, { value: "purple", label: "Фиолетовый" },
            { value: "pink", label: "Розовый" },
          ],
        },
        { key: "description", label: "Описание", type: "textarea", maxLength: 1000, value: clan?.description || "", placeholder: "Необязательно" },
      ];
    },
    async createClan() {
      const values = await this.openActionDialog({
        title: "Создать клан",
        message: "Клан будет доступен для назначения игрокам в их карточках.",
        confirmText: "Создать",
        fields: this.clanDialogFields(),
      });
      if (!values) return;
      const { status, data } = await this.api.request("POST", "/api/clans", values);
      if (status === 200 && data?.ok) {
        this.clans.items.push(data.clan);
        this.toast("Клан создан", "ok");
      } else {
        this.toast(data?.detail || data?.error?.message || "Не удалось создать клан", "err");
      }
    },
    async editClan(clan) {
      const values = await this.openActionDialog({
        title: `Редактировать [${clan.tag}]`,
        confirmText: "Сохранить",
        fields: this.clanDialogFields(clan),
      });
      if (!values) return;
      const { status, data } = await this.api.request("PATCH", `/api/clans/${clan.id}`, values);
      if (status === 200 && data?.ok) {
        await this.loadClans(true);
        if (this.playerCard.profile?.clan?.id === clan.id) {
          Object.assign(this.playerCard.profile.clan, values);
        }
        this.toast("Клан обновлён", "ok");
      } else {
        this.toast(data?.detail || data?.error?.message || "Не удалось обновить клан", "err");
      }
    },
    async addClanMember() {
      if (!this.selectedClan) return;
      const values = await this.openActionDialog({ title: `Добавить в [${this.selectedClan.tag}]`, confirmText: "Добавить", fields: [
        { key: "steam_id", label: "SteamID64", required: true, validator: "steam64", maxLength: 17, placeholder: "7656119…" },
        { key: "member_role", label: "Роль", type: "select", value: "Участник", options: [{value:"Лидер",label:"Лидер"},{value:"Заместитель",label:"Заместитель"},{value:"Участник",label:"Участник"}] },
      ]});
      if (!values) return;
      await this.saveClanMember(values.steam_id, values.member_role);
    },
    async saveClanMember(steamId, memberRole) {
      if (!this.selectedClan) return;
      const { status, data } = await this.api.request("PUT", `/api/clans/${this.selectedClan.id}/members`, { steam_id: steamId, member_role: memberRole });
      if (status === 200 && data?.ok) {
        this.selectedClan = data.clan;
        await this.loadClans(true);
        this.toast(memberRole === "Лидер" ? "Лидер клана назначен" : "Состав клана обновлён", "ok");
      } else this.toast(data?.detail || "Не удалось изменить состав", "err");
    },
    async removeClanMember(member) {
      if (!this.selectedClan || !await this.confirmAction("Исключить из клана", `${member.name || member.steam_id} будет исключён из [${this.selectedClan.tag}].`, "Исключить")) return;
      const { status, data } = await this.api.request("DELETE", `/api/clans/${this.selectedClan.id}/members/${member.steam_id}`);
      if (status === 200 && data?.ok) {
        this.selectedClan = data.clan;
        await this.loadClans(true);
        this.toast("Участник исключён", "ok");
      } else this.toast(data?.detail || "Не удалось исключить участника", "err");
    },
    async uploadClanMedia(event, kind) {
      const file = event.target.files?.[0];
      event.target.value = "";
      if (!file || !this.selectedClan) return;
      if (!['image/png','image/jpeg','image/webp'].includes(file.type) || file.size > 2 * 1024 * 1024) return this.toast("Нужен PNG, JPG или WebP до 2 МБ", "warn");
      const dataUrl = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
      const { status, data } = await this.api.request("POST", `/api/clans/${this.selectedClan.id}/media`, { kind, data_url: dataUrl });
      if (status === 200 && data?.ok) { this.selectedClan = data.clan; await this.loadClans(true); this.toast(kind === "emblem" ? "Эмблема клана обновлена" : "Обложка клана обновлена", "ok"); }
      else this.toast(data?.detail || "Не удалось загрузить изображение", "err");
    },
    async deleteClan(clan) {
      const confirmed = await this.confirmAction(
        "Удалить клан",
        `[${clan.tag}] ${clan.name}. Все участники будут отвязаны от клана.`,
        "Удалить клан",
      );
      if (!confirmed) return;
      const { status, data } = await this.api.request("DELETE", `/api/clans/${clan.id}`, {});
      if (status === 200 && data?.ok) {
        this.clans.items = this.clans.items.filter((item) => item.id !== clan.id);
        if (this.playerCard.profile?.clan?.id === clan.id) {
          this.playerCard.profile.clan = null;
          this.playerCardClanId = "";
        }
        this.toast("Клан удалён", "ok");
      } else {
        this.toast(data?.detail || data?.error?.message || "Не удалось удалить клан", "err");
      }
    },
    async changePlayerClan(clanId) {
      const steamId = this.playerCardSteamId();
      const previous = this.playerCard.profile?.clan?.id || "";
      if (!steamId || clanId === previous || !this.playerCard.profile?.can_edit_clan) return;
      if (!clanId) {
        const confirmed = await this.confirmAction("Убрать из клана", "Игрок будет исключён из текущего клана.", "Убрать");
        if (!confirmed) {
          this.playerCardClanId = previous;
          return;
        }
        const { status, data } = await this.api.request(
          "DELETE", `/api/players/${encodeURIComponent(steamId)}/clan`, {},
        );
        if (status === 200 && data?.ok) {
          this.playerCard.profile.clan = null;
          this.playerCardClanId = "";
          await this.loadClans(true);
          this.toast("Игрок убран из клана", "ok");
        } else {
          this.playerCardClanId = previous;
          this.toast(data?.detail || data?.error?.message || "Не удалось убрать игрока из клана", "err");
        }
        return;
      }
      const { status, data } = await this.api.request(
        "PUT",
        `/api/players/${encodeURIComponent(steamId)}/clan`,
        { clan_id: clanId, member_role: this.playerCard.profile?.clan?.member_role || "Участник" },
      );
      if (status === 200 && data?.ok) {
        this.playerCard.profile.clan = data.clan;
        this.playerCardClanId = data.clan?.id || "";
        await this.loadClans(true);
        this.toast("Клан игрока обновлён", "ok");
      } else {
        this.playerCardClanId = previous;
        this.toast(data?.detail || data?.error?.message || "Не удалось назначить клан", "err");
      }
    },
    playerCardServer() {
      const live = this.playerCard.live;
      return live ? this.state.sid : "";
    },
    playerCardAction(action) {
      const live = this.playerCard.live;
      if (action === "ban") {
        const profile = this.playerCard.profile;
        const target = live || (profile && profile.steam_id ? { steamId: profile.steam_id, name: profile.name || profile.steam_id } : null);
        if (!target) return this.toast("Не удалось определить игрока", "warn");
        return this.banPlayer(target);
      }
      if (!live) return this.toast("Действие доступно, когда игрок находится на сервере", "warn");
      if (action === "message") return this.sendMsg(live);
      if (action === "kick") return this.kickPlayer(live);
      if (action === "kill") return this.killPlayer(live);
      if (action === "swap") return this.swapPlayer(live);
    },
    async swapPlayer(p) {
      const factions = (this.ov?.status?.factionScores || []).map((f) => f.name).filter((name) => name && name !== p.faction);
      if (!factions.length) return this.toast("Нет другой стороны для свапа", "warn");
      const hasVip = (this.playerCard.profile?.vip_slots || []).some((s) => s.active && s.server_id === this.state.sid);
      const limit = hasVip ? 4 : 2;
      const options = factions.map((name) => {
        const diff = Math.abs(this.factionPlayerCount(p.faction) - this.factionPlayerCount(name));
        return { value: name, label: `${name} · перевес ${diff} · ${diff <= limit ? "можно" : "отказ"}` };
      });
      const values = await this.openActionDialog({
        title: `Смена стороны — ${p.name}`,
        message: `Текущая сторона: ${p.faction}. Перевес считается до свапа: штатно до +2, с активным VIP-слотом до +4.`,
        confirmText: "Свапнуть",
        fields: [{ key: "faction", label: "Сторона", type: "select", value: options[0].value, options }],
      });
      if (!values || !values.faction) return;
      return this.act(`/api/server/${this.state.sid}/players/${p.steamId}/swap`, "POST", { faction: values.faction });
    },
    async changePlayerCardFaction(faction) {
      const live = this.playerCard.live;
      if (!live || !faction || faction === live.faction) return;
      const changed = await this.moveFaction(live, faction);
      if (changed) {
        live.faction = faction;
        this.playerCardFaction = faction;
      } else {
        this.playerCardFaction = live.faction || "";
      }
    },
    async saveReservedLimit() {
      const value = Number(this.reservedLimit);
      if (!Number.isInteger(value) || value < 0 || value > 100) return this.toast("Лимит должен быть целым числом от 0 до 100", "warn");
      const { status, data } = await this.api.request("PUT", `/api/server/${this.state.sid}/reserved-slots/limit`, { max_reserved_slots: value });
      if (status === 200) { this.toast("Количество резервных мест применено без рестарта", "ok"); this.loadReserved(); }
      else this.toast((data && (data.detail || data.error?.message)) || "Не удалось изменить лимит резерва", "err");
    },
    async addReserved() {
      const id = this.reservedSteamId.trim();
      if (!/^\d{17}$/.test(id)) return this.toast("Нужен SteamID64 из 17 цифр", "warn");
      const { status, data } = await this.api.request("POST", `/api/server/${this.state.sid}/reserved-slots`, { steam_id: id });
      if (status === 200) { this.reservedSteamId = ""; this.toast("Резерв добавлен", "ok"); this.loadReserved(); }
      else this.toast((data && (data.detail || data.error?.message)) || "Не удалось добавить резерв", "err");
    },
    async removeReserved(id) {
      if (!await this.confirmAction("Убрать из резерва", `SteamID64: ${id}`, "Убрать")) return;
      const { status, data } = await this.api.request("DELETE", `/api/server/${this.state.sid}/reserved-slots/${id}`);
      if (status === 200) { this.toast("Резерв снят", "ok"); this.loadReserved(); }
      else this.toast((data && (data.detail || data.error?.message)) || "Не удалось снять резерв", "err");
    },
    async loadVipSlots() {
      if (!this.state.sid || this.vip.loading) return;
      this.vip.loading = true;
      this.vip.error = "";
      try {
        const { status, data } = await this.api.request("GET", `/api/server/${this.state.sid}/vip-slots`);
        if (status === 200 && data?.ok) {
          this.vip.items = data.items || [];
          this.vip.count = Number(data.count || this.vip.items.length);
          const history = await this.api.request("GET", `/api/server/${this.state.sid}/vip-slots/history`);
          this.vip.history = history.status === 200 ? (history.data?.items || []) : [];
        } else {
          this.vip.error = data?.detail || data?.error?.message || "Не удалось загрузить VIP-слоты";
        }
      } finally {
        this.vip.loading = false;
      }
    },
    async loadPayments() {
      this.payments.loading=true;
      const r=await this.api.request('GET','/api/vip/orders');
      if(r.status===200 && r.data?.ok) Object.assign(this.payments,r.data,{error:''});
      else this.payments.error=r.data?.detail || 'Не удалось загрузить заявки';
      this.payments.loading=false;
    },
    async loadProfile() {
      if (this.profile.loading) return;
      this.profile.loading = true;
      this.profile.error = "";
      const r = await this.api.request("GET", "/api/profile");
      if (r.status === 200 && r.data?.ok) {
        this.profile.data = r.data;
      } else {
        this.profile.error = r.data?.detail || "Не удалось загрузить профиль";
      }
      this.profile.loading = false;
      this.loadSeedShop();
    },
    async loadSeedShop() {
      this.seedShop.loading = true;
      this.seedShop.error = "";
      const r = await this.api.request("GET", "/api/seed/balance");
      if (r.status === 200) {
        this.seedShop.data = r.data;
      } else {
        this.seedShop.error = r.data?.detail || "Не удалось загрузить баланс";
      }
      this.seedShop.loading = false;
    },
    async buySeedVip(sku) {
      if (this.seedShop.buying) return;
      this.seedShop.buying = true;
      this.seedShop.buyResult = "";
      const r = await this.api.request("POST", "/api/seed/vip", { sku });
      if (r.status === 200 && r.data?.ok) {
        this.seedShop.buyResult = `VIP куплен! Баланс: ${r.data.balance} баллов.`;
        this.loadSeedShop();
        this.loadProfile();
      } else {
        this.seedShop.buyResult = r.data?.detail || "Ошибка покупки";
      }
      this.seedShop.buying = false;
    },
    openProfileCard() {
      this.profile.open = true;
      this.loadProfile();
    },
    closeProfileCard() {
      this.profile.open = false;
    },
    async createVipOrder() {
      const values=await this.openActionDialog({title:'Заявка на VIP',message:'Заявка сохраняется со статусом «ожидает оплаты». Ссылка на оплату появится в списке заявок: после перевода слоты выдадутся получателям автоматически.',confirmText:'Создать заявку',fields:[
        {key:'plan_id',label:'Тариф',type:'select',required:true,value:this.payments.plans[0]?.id,options:this.payments.plans.map(p=>({value:p.id,label:`${p.name} · ${p.amount} ₽`}))},
        {key:'buyer_steam_id',label:'SteamID покупателя',required:true,validator:'steam64'},
        {key:'recipients',label:'SteamID получателей (по одному в строке)',type:'textarea',maxLength:600,placeholder:'Для личного VIP можно оставить пустым — получит покупатель'},
      ]});
      if(!values) return;
      const r=await this.api.request('POST','/api/vip/orders',{...values,server_id:this.state.sid,recipients:values.recipients.split(/[\s,;]+/).filter(Boolean)});
      if(r.status===200 && r.data?.ok) { await this.loadPayments(); this.toast(`Заявка ${r.data.item.id} сохранена`,'ok'); }
      else this.toast(r.data?.detail || 'Заявка не создана','err');
    },
    async cancelVipOrder(item) {
      if(!await this.confirmAction('Отменить заявку',item.id,'Отменить заявку')) return;
      const r=await this.api.request('POST',`/api/vip/orders/${item.id}/cancel`,{});
      if(r.status===200) this.loadPayments(); else this.toast(r.data?.detail || 'Не удалось отменить','err');
    },
    orderStatusLabel(order) {
      const labels={awaiting_payment:'Ожидает оплаты',paid:'Оплачен, выдаётся',fulfilled:'Оплачен, выдан',cancelled:'Отменена'};
      return labels[order.status] || order.status;
    },
    orderStatusClass(order) {
      if(order.status==='fulfilled') return 'success';
      if(order.status==='paid') return 'warn';
      if(order.status==='cancelled') return 'danger';
      return '';
    },
    async copyPayUrl(order) {
      if(!order.pay_url) return;
      try {
        await navigator.clipboard.writeText(order.pay_url);
        this.toast('Ссылка оплаты скопирована','ok');
      } catch (e) {
        this.toast(order.pay_url,'warn');
      }
    },
    async extendVip(item) {
      const values=await this.openActionDialog({title:'Продлить VIP',message:`${item.display_name || item.steam_id} · ${this.state.servers.find(s=>s.id===this.state.sid)?.name}`,confirmText:'Продлить',fields:[{key:'months',label:'Срок',type:'select',value:'1',options:[{value:'1',label:'1 месяц'},{value:'3',label:'3 месяца'},{value:'12',label:'12 месяцев'}]}]});
      if(!values) return;
      const date=new Date(Math.max(Date.now(),(item.expires_utc || 0)*1000));
      const day=date.getDate(); date.setDate(1);date.setMonth(date.getMonth()+Number(values.months));date.setDate(Math.min(day,new Date(date.getFullYear(),date.getMonth()+1,0).getDate()));
      const r=await this.api.request('POST',`/api/server/${this.state.sid}/vip-slots`,{steam_id:item.steam_id,display_name:item.display_name,note:item.note,expires_utc:date.toISOString()});
      if(r.status===200 && r.data?.ok) {this.toast(r.data.synced===false?'Продление сохранено, требуется синхронизация':'VIP продлён',r.data.synced===false?'warn':'ok');this.loadVipSlots();} else this.toast(r.data?.detail || 'Не удалось продлить','err');
    },
    startBotWizard() {
      this.botWizard={open:true,step:1,id:'',busy:false}; this.botIssuedToken='';
      this.botDraft={name:'',bot_id:`bot-${crypto.randomUUID().slice(0,8)}`,guild_id:'',scopes:['heartbeat','events.write']};
    },
    async checkBotConnection() {
      await this.loadDiscordAdmin();
      const bot=this.discordAdmin.bots.find(b=>b.bot_id===this.botWizard.id);
      this.toast(bot?.online ? 'Бот прислал heartbeat — соединение подтверждено' : 'Heartbeat пока не получен. Проверьте адрес панели и ключ на хосте бота.',bot?.online?'ok':'warn');
      if(bot?.online) this.botWizard.step=3;
    },
    async showBotEvents(bot) {
      this.botEventsName=bot.name;this.botEvents=[];
      const r=await this.api.request('GET',`/api/discord/bots/${encodeURIComponent(bot.bot_id)}/events?limit=20`);
      if(r.status===200) this.botEvents=r.data.items || []; else this.toast('Журнал бота недоступен','err');
    },
    vipDateInput(timestamp) {
      if (!timestamp) return "";
      const date = new Date(Number(timestamp) * 1000);
      return Number.isNaN(date.getTime()) ? "" : date.toISOString().slice(0, 10);
    },
    vipStateLabel(item) {
      if (item.sync_state === "error") return "ошибка синхронизации";
      if (!item.active || item.sync_state === "expired") return "истёк";
      if (item.sync_state === "synced") return "синхронизирован";
      return "ожидает синхронизации";
    },
    vipTierHint(tier) {
      return {
        2: "VIP и личный тикет с администрацией",
        3: "VIP, личный тикет и объявление о входе в игре",
      }[tier] || "VIP";
    },
    async openVipTicket(item) {
      const { status, data } = await this.api.request("POST", `/api/players/${encodeURIComponent(item.steam_id)}/vip-ticket`, {});
      if (status !== 200 || !data?.ok) return this.toast(data?.detail || "Не удалось открыть тикет", "err");
      if (!data.ticket?.ok) return this.toast(`Личный тикет не создан: ${data.ticket?.error || "бот недоступен"}`, "warn");
      this.toast(data.ticket.created ? "Личный тикет создан в Discord" : "Личный тикет уже открыт", "ok");
    },
    async saveVipSlot(item = null) {
      const values = await this.openActionDialog({
        title: item ? "Изменить VIP-слот" : "Выдать VIP-слот",
        message: "SteamID будет автоматически добавлен в резерв выбранного сервера. Пустая дата — бессрочный VIP.",
        confirmText: item ? "Сохранить" : "Выдать VIP",
        fields: [
          { key: "steam_id", label: "SteamID64", required: true, validator: "steam64", maxLength: 17, value: item?.steam_id || "", placeholder: "7656119…" },
          { key: "display_name", label: "Игрок", maxLength: 120, value: item?.display_name || "", placeholder: "Ник или комментарий" },
          { key: "tier", label: "Уровень VIP", type: "select", value: String(item?.tier || 1), options: [
            { value: "1", label: "Tier 1 — VIP" },
            { value: "2", label: "Tier 2 — VIP и личный тикет с администрацией" },
            { value: "3", label: "Tier 3 — как Tier 2 и объявление о входе в игре" },
          ] },
          { key: "expires_date", label: "Действует до", type: "date", value: this.vipDateInput(item?.expires_utc), placeholder: "пусто = бессрочно" },
          { key: "note", label: "Заметка", type: "textarea", maxLength: 500, value: item?.note || "", placeholder: "Основание выдачи, покупка, событие…" },
        ],
      });
      if (!values) return;
      let expiresUtc = null;
      if (values.expires_date) {
        const expiry = new Date(`${values.expires_date}T23:59:59`);
        if (Number.isNaN(expiry.getTime())) return this.toast("Некорректная дата окончания VIP", "warn");
        expiresUtc = expiry.toISOString();
      }
      const { status, data } = await this.api.request(
        "POST",
        `/api/server/${this.state.sid}/vip-slots`,
        {
          steam_id: values.steam_id,
          display_name: values.display_name,
          expires_utc: expiresUtc,
          note: values.note,
          tier: Number(values.tier) || 1,
        },
      );
      if (status === 200 && data?.ok) {
        await this.loadVipSlots();
        if (data.ticket && !data.ticket.ok) this.toast(`Личный тикет не создан: ${data.ticket.error}`, "warn");
        this.toast(data.synced ? "VIP выдан и синхронизирован" : `VIP сохранён: ${data.warning || "нужна синхронизация"}`, data.synced ? "ok" : "warn");
      } else {
        this.toast(data?.detail || data?.error?.message || "Не удалось сохранить VIP", "err");
      }
    },
    async syncVipSlot(item) {
      const { status, data } = await this.api.request(
        "POST",
        `/api/server/${this.state.sid}/vip-slots/${item.id}/sync`,
        {},
      );
      await this.loadVipSlots();
      if (status === 200 && data?.ok) this.toast("VIP синхронизирован", "ok");
      else this.toast(data?.detail || data?.error?.message || "Синхронизация не удалась", "err");
    },
    async deleteVipSlot(item) {
      const confirmed = await this.confirmAction(
        "Отозвать VIP",
        `${item.display_name || item.steam_id} будет удалён из резерва сервера.`,
        "Отозвать VIP",
      );
      if (!confirmed) return;
      const { status, data } = await this.api.request(
        "DELETE",
        `/api/server/${this.state.sid}/vip-slots/${item.id}`,
        {},
      );
      if (status === 200 && data?.ok) {
        this.vip.items = this.vip.items.filter((vip) => vip.id !== item.id);
        this.vip.count = this.vip.items.length;
        this.toast("VIP отозван", "ok");
      } else {
        await this.loadVipSlots();
        this.toast(data?.detail || data?.error?.message || "Не удалось отозвать VIP", "err");
      }
    },
    ticketStatusLabel(status) {
      return { open: "Открыт", in_progress: "В работе", resolved: "Решён", closed: "Закрыт" }[status] || status;
    },
    ticketPriorityLabel(priority) {
      return { low: "Низкий", normal: "Обычный", high: "Высокий", urgent: "Срочный" }[priority] || priority;
    },
    async loadTickets() {
      if (this.tickets.loading) return;
      this.tickets.loading = true;
      this.tickets.error = "";
      try {
        const { status, data } = await this.api.request("GET", "/api/tickets");
        if (status === 200 && data?.ok) {
          this.tickets.items = data.items || [];
          this.tickets.count = Number(data.count || this.tickets.items.length);
          if (this.tickets.selected) {
            const stillExists = this.tickets.items.some((item) => item.id === this.tickets.selected.id);
            if (!stillExists) this.tickets.selected = null;
          }
        } else {
          this.tickets.error = data?.detail || data?.error?.message || "Не удалось загрузить тикеты";
        }
      } finally {
        this.tickets.loading = false;
      }
    },
    async loadTicketTypes() {
      if (this.ticketTypes.loading) return;
      this.ticketTypes.loading = true;
      this.ticketTypes.error = "";
      try {
        const { status, data } = await this.api.request("GET", "/api/ticket-types");
        if (status === 200 && data?.ok) {
          this.ticketTypes.items = data.types || [];
          if (data.available === false) this.ticketTypes.error = "Типы тикетов появятся после первого запуска Discord-бота";
        }
        else this.ticketTypes.error = data?.detail || "Не удалось загрузить типы тикетов";
      } finally {
        this.ticketTypes.loading = false;
      }
    },
    startTicketTypeDraft(item = null) {
      this.ticketTypeDraft = item
        ? { open: true, isNew: false, type_id: item.type_id, label: item.label, description: item.description, emoji: item.emoji, color: item.color, position: item.position, enabled: item.enabled }
        : { open: true, isNew: true, type_id: "", label: "", description: "", emoji: "", color: "#5865F2", position: this.ticketTypes.items.length, enabled: true };
    },
    async saveTicketType() {
      const draft = this.ticketTypeDraft;
      if (!draft.type_id || !draft.label) return this.toast("ID и название обязательны", "err");
      const payload = {
        label: draft.label, description: draft.description, emoji: draft.emoji,
        color: draft.color, position: Number(draft.position) || 0, enabled: !!draft.enabled,
      };
      this.ticketTypes.busy = true;
      try {
        const { status, data } = draft.isNew
          ? await this.api.request("POST", "/api/ticket-types", { type_id: draft.type_id, ...payload })
          : await this.api.request("PATCH", `/api/ticket-types/${draft.type_id}`, payload);
        if (status === 200 && data?.ok) {
          draft.open = false;
          await this.loadTicketTypes();
          this.toast(draft.isNew ? "Тип добавлен" : "Тип обновлён", "ok");
        } else {
          this.toast(data?.detail || "Не удалось сохранить тип", "err");
        }
      } finally {
        this.ticketTypes.busy = false;
      }
    },
    async deleteTicketType(item) {
      const values = await this.openActionDialog({
        title: `Удалить тип «${item.label}»`,
        message: "Тип исчезнет из меню создания тикетов. Открытые тикеты этого типа не пострадают.",
        confirmText: "Удалить",
        danger: true,
      });
      if (!values) return;
      const { status, data } = await this.api.request("DELETE", `/api/ticket-types/${item.type_id}`);
      if (status === 200 && data?.ok) {
        await this.loadTicketTypes();
        this.toast("Тип удалён", "ok");
      } else {
        this.toast(data?.detail || "Не удалось удалить тип", "err");
      }
    },
    async openTicket(ticket) {
      this.tickets.detailLoading = true;
      this.ticketCommentDraft = "";
      try {
        const { status, data } = await this.api.request("GET", `/api/tickets/${ticket.id}`);
        if (status === 200 && data?.ok) this.tickets.selected = data.ticket;
        else this.toast(data?.detail || "Не удалось открыть тикет", "err");
      } finally {
        this.tickets.detailLoading = false;
      }
    },
    ticketDialogFields(ticket = null) {
      return [
        { key: "title", label: "Заголовок", required: true, maxLength: 160, value: ticket?.title || "", placeholder: "Кратко опишите задачу или проблему" },
        { key: "description", label: "Описание", type: "textarea", maxLength: 5000, value: ticket?.description || "", placeholder: "Подробности, контекст и ожидаемый результат" },
        {
          key: "priority", label: "Приоритет", type: "select", value: ticket?.priority || "normal",
          options: [
            { value: "low", label: "Низкий" }, { value: "normal", label: "Обычный" },
            { value: "high", label: "Высокий" }, { value: "urgent", label: "Срочный" },
          ],
        },
        { key: "player_steam_id", label: "SteamID игрока", validator: "steam64", maxLength: 17, value: ticket?.player_steam_id || "", placeholder: "Необязательно" },
      ];
    },
    async createTicket() {
      const values = await this.openActionDialog({
        title: "Новый тикет",
        message: "Создайте внутреннюю задачу для команды администрации.",
        confirmText: "Создать",
        fields: this.ticketDialogFields(),
      });
      if (!values) return;
      const { status, data } = await this.api.request("POST", "/api/tickets", values);
      if (status === 200 && data?.ok) {
        await this.loadTickets();
        await this.openTicket(data.ticket);
        this.toast("Тикет создан", "ok");
      } else {
        this.toast(data?.detail || data?.error?.message || "Не удалось создать тикет", "err");
      }
    },
    async editTicket(ticket) {
      const values = await this.openActionDialog({
        title: "Редактировать тикет",
        confirmText: "Сохранить",
        fields: this.ticketDialogFields(ticket),
      });
      if (!values) return;
      await this.patchTicket(values);
    },
    async patchTicket(patch) {
      const ticket = this.tickets.selected;
      if (!ticket || !this.has("tickets_edit")) return false;
      const { status, data } = await this.api.request("PATCH", `/api/tickets/${ticket.id}`, patch);
      if (status === 200 && data?.ok) {
        this.tickets.selected = data.ticket;
        await this.loadTickets();
        this.toast("Тикет обновлён", "ok");
        return true;
      }
      this.toast(data?.detail || data?.error?.message || "Не удалось обновить тикет", "err");
      await this.openTicket(ticket);
      return false;
    },
    async addTicketComment() {
      const ticket = this.tickets.selected;
      const body = this.ticketCommentDraft.trim();
      if (!ticket || !body || !this.has("tickets_edit")) return;
      const { status, data } = await this.api.request(
        "POST", `/api/tickets/${ticket.id}/comments`, { body },
      );
      if (status === 200 && data?.ok) {
        ticket.comments.push(data.comment);
        ticket.comment_count = ticket.comments.length;
        this.ticketCommentDraft = "";
        await this.loadTickets();
        this.toast("Комментарий добавлен", "ok");
      } else {
        this.toast(data?.detail || data?.error?.message || "Не удалось добавить комментарий", "err");
      }
    },
    async deleteTicket(ticket) {
      const confirmed = await this.confirmAction(
        "Удалить тикет",
        "Тикет и все комментарии будут удалены без возможности восстановления.",
        "Удалить",
      );
      if (!confirmed) return;
      const { status, data } = await this.api.request("DELETE", `/api/tickets/${ticket.id}`, {});
      if (status === 200 && data?.ok) {
        this.tickets.selected = null;
        await this.loadTickets();
        this.toast("Тикет удалён", "ok");
      } else {
        this.toast(data?.detail || data?.error?.message || "Не удалось удалить тикет", "err");
      }
    },

    async loadStaff() {
      this.staff.loading = true;
      this.staff.error = "";
      try {
        const { status, data } = await this.api.request("GET", "/api/staff");
        if (status === 200 && data?.ok) {
          this.staff.items = data.staff || [];
          this.staff.count = Number(data.count || this.staff.items.length);
        } else {
          this.staff.error = data?.detail || "Не удалось загрузить администрацию";
        }
      } finally {
        this.staff.loading = false;
      }
    },
    async loadDiscordAdmin(silent = false) {
      if (!silent) this.discordAdmin.loading = true;
      this.discordAdmin.error = "";
      try {
        const { status, data } = await this.api.request("GET", "/api/discord/overview");
        if (status === 200 && data?.ok) Object.assign(this.discordAdmin, data);
        else this.discordAdmin.error = data?.detail || "Не удалось загрузить диагностику Discord";
      } finally {
        this.discordAdmin.loading = false;
      }
    },
    async createBotIntegration() {
      if(this.botWizard.busy) return;
      if (!this.botDraft.name.trim() || !this.botDraft.bot_id.trim()) return this.toast("Укажите имя и ID бота", "warn");
      this.botWizard.busy=true;
      const { status, data } = await this.api.request("POST", "/api/discord/bots", this.botDraft);
      this.botWizard.busy=false;
      if (status !== 200 || !data?.ok) return this.toast(data?.detail || "Не удалось зарегистрировать бота", "err");
      this.botIssuedToken = data.token || "";
      this.botWizard.id=this.botDraft.bot_id; this.botWizard.step=2;
      this.botDraft = { name: "", bot_id: "", guild_id: "", scopes: ["heartbeat", "events.write"] };
      this.toast("Бот зарегистрирован. Сохраните токен сейчас", "ok");
      await this.loadDiscordAdmin();
    },
    async toggleBotIntegration(bot) {
      const enabling = !bot.enabled;
      if (!enabling && !await this.confirmAction("Отключить бота", `${bot.name} перестанет принимать и отправлять события. История сохранится.`, "Отключить")) return;
      const { status, data } = await this.api.request("PATCH", `/api/discord/bots/${encodeURIComponent(bot.bot_id)}`, { enabled: enabling });
      if (status === 200 && data?.ok) { this.toast(enabling ? "Бот включён" : "Бот отключён", "ok"); await this.loadDiscordAdmin(); }
      else this.toast(data?.detail || "Не удалось изменить состояние", "err");
    },
    async deleteBotIntegration(bot) {
      if (!await this.confirmAction("Удалить бота", `${bot.name} будет удалён из реестра вместе с историей событий. Действие необратимо.`, "Удалить")) return;
      const { status, data } = await this.api.request("DELETE", `/api/discord/bots/${encodeURIComponent(bot.bot_id)}`, {});
      if (status === 200 && data?.ok) { this.toast("Бот удалён", "ok"); await this.loadDiscordAdmin(true); }
      else this.toast(data?.detail || "Не удалось удалить бота", "err");
    },
    async rotateBotToken(bot) {
      if (!await this.confirmAction("Обновить ключ бота", `Старый ключ ${bot.name} сразу перестанет работать.`, "Обновить ключ")) return;
      const { status, data } = await this.api.request("POST", `/api/discord/bots/${encodeURIComponent(bot.bot_id)}/rotate-token`, {});
      if (status === 200 && data?.ok) { this.botIssuedToken = data.token || ""; this.toast("Новый ключ выпущен", "ok"); await this.loadDiscordAdmin(); }
      else this.toast(data?.detail || "Не удалось обновить ключ", "err");
    },
    async copyBotToken() {
      if (!this.botIssuedToken) return;
      try { await navigator.clipboard.writeText(this.botIssuedToken); this.toast("Ключ скопирован", "ok"); }
      catch (_) { this.toast("Не удалось скопировать ключ", "err"); }
    },
    roomMemberNames(room) {
      const roomId = String(room.id || room.channel_id || "");
      return (this.discordAdmin.tempvoice?.members || [])
        .filter((member) => String(member.channel_id) === roomId)
        .map((member) => member.name || member.user_id);
    },
    async deleteTempVoice(room) {
      const roomId = room.id || room.channel_id;
      if (!await this.confirmAction("Удалить временную комнату", `${room.name || roomId} · владелец ${room.owner_name || room.owner_id}`, "Удалить")) return;
      const { status, data } = await this.api.request("POST", `/api/discord/tempvoice/${roomId}/delete`, {});
      if (status === 200 && data?.ok) { this.toast("Комната удалена", "ok"); this.loadDiscordAdmin(); }
      else this.toast(data?.detail || data?.error || "Не удалось удалить комнату", "err");
    },
    async transferTempVoice(room) {
      const roomId = room.id || room.channel_id;
      const members = (this.discordAdmin.tempvoice?.members || []).filter((member) => String(member.channel_id) === String(roomId));
      const values = await this.openActionDialog({ title: "Передать временную комнату", confirmText: "Передать", fields: [{ key: "owner_id", label: "Новый владелец", type: "select", required: true, options: members.map((member) => ({ value: member.user_id, label: `${member.name} · ${member.user_id}` })) }] });
      if (!values) return;
      const { status, data } = await this.api.request("POST", `/api/discord/tempvoice/${roomId}/transfer`, values);
      if (status === 200 && data?.ok) { this.toast(`Новый владелец: ${data.owner_name || values.owner_id}`, "ok"); this.loadDiscordAdmin(); }
      else this.toast(data?.detail || data?.error || "Не удалось передать комнату", "err");
    },
    async loadSystemStatus() {
      this.systemStatus.loading = true; this.systemStatus.error = "";
      try {
        const { status, data } = await this.api.request("GET", "/api/system/status");
        if (status === 200 && data) Object.assign(this.systemStatus, data);
        else this.systemStatus.error = data?.detail || "Не удалось получить состояние систем";
      } finally { this.systemStatus.loading = false; }
    },
    async testLogChannel() {
      this.busy = true;
      try {
        const { status, data } = await this.api.request("POST", "/api/discord/log-test");
        if (status === 200 && data?.ok) this.toast("Лог-канал работает", "ok");
        this.openActionDialog({ title: "Лог-канал", message: data?.detail || `HTTP ${status}`, confirmText: "Закрыть" });
      } finally { this.busy = false; }
    },
    async previewUserPermissions() {
      const values = await this.openActionDialog({ title: "Итоговые права пользователя", confirmText: "Показать", fields: [{ key: "steam_id", label: "SteamID64", validator: "steam64", required: true }] });
      if (!values) return;
      const { status, data } = await this.api.request("GET", `/api/roles/preview/user/${values.steam_id}`);
      if (status !== 200) return this.toast(data?.detail || "Пользователь не найден", "err");
      this.openActionDialog({ title: data.discord_name || data.steam_id, message: `Роли: ${data.roles.map((role) => role.name).join(", ") || "нет"}.\nИтоговые права: ${data.permissions.map(this.permLabel).join(", ") || "нет доступа"}.`, confirmText: "Закрыть" });
    },
    async grantPersonalAccess() {
      const values = await this.openActionDialog({
        title: "Личный доступ по Discord ID",
        message: "Права выдаются только этому аккаунту Discord: без привязки к ролям и независимо от членства в гильдии.",
        confirmText: "Выдать все права",
        fields: [{ key: "discord_id", label: "Discord ID", validator: "discordid", required: true, maxLength: 20 }],
      });
      if (!values) return;
      this.busy = true;
      try {
        const { status, data } = await this.api.request("PUT", "/api/roles", { role_ref: `user:${values.discord_id}`, perms: [...this.allPerms] });
        if (status === 200 && data.ok) {
          this.toast(`Полный доступ выдан: ${values.discord_id}`, "ok");
          await this.loadRoles(true);
        } else {
          this.toast(data?.detail || data?.error?.message || "Не удалось выдать доступ", "err");
        }
      } finally { this.busy = false; }
    },
    async showRoleHistory(role) {
      const { status, data } = await this.api.request("GET", `/api/roles/${encodeURIComponent(role.id)}/history`);
      if (status !== 200) return this.toast(data?.detail || "История недоступна", "err");
      const text = (data.items || []).slice(0, 20).map((item) => `${this.fmtDate(item.timestamp_utc * 1000)}: +${(item.change.added || []).join(",") || "—"}; −${(item.change.removed || []).join(",") || "—"}`).join("\n");
      this.openActionDialog({ title: `История роли ${role.name}`, message: text || "Изменений пока нет.", confirmText: "Закрыть" });
    },
    staffVisible() {
      const q = this.staffQuery.trim().toLowerCase();
      if (!q) return this.staff.items;
      return this.staff.items.filter((item) => [
        item.persona, item.steam_id, item.discord_name, item.discord_id,
        ...(item.role_names || []), item.group,
      ].some((value) => String(value || "").toLowerCase().includes(q)));
    },
    staffGroups() {
      const groups = new Map();
      for (const item of this.staffVisible()) {
        if (!groups.has(item.group)) groups.set(item.group, []);
        groups.get(item.group).push(item);
      }
      const order = ["Разработчики", "Основатели", "Администраторы", "Модераторы", "Сотрудники"];
      return [...groups.entries()].sort((a, b) => {
        const ai = order.indexOf(a[0]), bi = order.indexOf(b[0]);
        return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi) || a[0].localeCompare(b[0], "ru");
      });
    },
    roleConflicts(role) {
      const perms = new Set(this.draftFor(role));
      const conflicts = [];
      if (perms.has("config_edit") && !perms.has("config_view")) conflicts.push("Запись конфига без просмотра");
      if (perms.has("vip_edit") && !perms.has("vip_view")) conflicts.push("Изменение VIP без просмотра");
      if (perms.has("reserved_edit") && !perms.has("reserved_view")) conflicts.push("Изменение резерва без просмотра");
      if (perms.has("tickets_edit") && !perms.has("tickets_view")) conflicts.push("Изменение тикетов без просмотра");
      if ((perms.has("ban_add") || perms.has("ban_remove")) && !perms.has("ban_view")) conflicts.push("Изменение банов без просмотра");
      return conflicts;
    },
    toggleRoleSelected(roleId) {
      this.roleSelected = this.roleSelected.includes(roleId)
        ? this.roleSelected.filter((id) => id !== roleId)
        : [...this.roleSelected, roleId];
    },
    async bulkRolePreset() {
      if (!this.roleSelected.length) return this.toast("Сначала выберите роли", "warn");
      const values = await this.openActionDialog({
        title: `Шаблон для ${this.roleSelected.length} ролей`, confirmText: "Применить к черновикам",
        fields: [{ key: "preset", label: "Шаблон", type: "select", value: "moderator", options: [
          { value: "observer", label: "Наблюдатель" }, { value: "moderator", label: "Модератор" },
          { value: "administrator", label: "Администратор" },
        ] }],
      });
      if (!values) return;
      for (const role of this.rolesList.filter((item) => this.roleSelected.includes(item.id))) this.roleApplyPreset(role, values.preset);
      this.toast("Шаблон применён к черновикам — проверьте и сохраните", "ok");
    },
    compareSelectedRoles() {
      if (this.roleSelected.length !== 2) return this.toast("Для сравнения выберите ровно две роли", "warn");
      const [left, right] = this.roleSelected.map((id) => this.rolesList.find((role) => role.id === id));
      const leftSet = new Set(this.draftFor(left)), rightSet = new Set(this.draftFor(right));
      const onlyLeft = [...leftSet].filter((permission) => !rightSet.has(permission));
      const onlyRight = [...rightSet].filter((permission) => !leftSet.has(permission));
      this.openActionDialog({
        title: `${left.name} ↔ ${right.name}`,
        message: `Только ${left.name}: ${onlyLeft.map(this.permLabel).join(", ") || "нет"}.\nТолько ${right.name}: ${onlyRight.map(this.permLabel).join(", ") || "нет"}.`,
        confirmText: "Закрыть",
      });
    },

    async ensureCatalog() {
      if (MODE !== "panel" || !(this.has("change_map") || this.has("change_lighting"))) return;
      if (Object.keys(this.catalog.maps).length || !this.state.sid) return;
      const { data } = await this.api.request("GET", `/api/server/${this.state.sid}/catalog`);
      if (data && data.ok) {
        this.catalog.maps = data.maps || {};
        this.catalog.lightings = data.lightings || [];
        if (!this.form.map) this.form.map = Object.keys(this.catalog.maps)[0] || "";
      } else if (data && data.error) {
        this.toast(`Каталог: ${data.error.message}`, "err");
      }
    },

    // ---- форматирование ----
    mapLabel(m) { return m || "—"; },
    ruName(id) { return MAP_RU[id] || ""; },
    lightLabel(l) {
      return l ? (LIGHT_RU[l] ? `${LIGHT_RU[l]} (${l})` : l) : "—";
    },
    modeLabel(list) { return (list && list.length) ? list.join(" · ") : ""; },
    rotMode(r) { return r.mode === "random" ? "Случайно" : "По порядку"; },
    fmtClock(s) {
      s = Math.max(0, Math.floor(s || 0));
      return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
    },
    fmtDur(s) {
      s = Math.floor(s || 0);
      if (!s) return "—";
      const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
      return d ? `${d}д ${h}ч` : `${h}ч`;
    },
    pct(p) { return p.max ? Math.min(100, Math.round((p.current / p.max) * 100)) : 0; },
    pctScore(f) {
      const total = this.sortedFactions.reduce((a, x) => a + (x.score || 0), 0);
      return total ? Math.max(2, Math.round((f.score / total) * 100)) : 0;
    },
    kd(p) {
      const k = p.kills || 0, d = p.deaths || 0;
      return d ? (k / d).toFixed(1) : "—";
    },
    shortSid(s) { return s ? `${String(s).slice(0, 5)}…${String(s).slice(-3)}` : "—"; },
    punishmentLabel(event) {
      return {
        "ban.add": "Бан выдан",
        "rcon.message": "Варн",
        "ban.remove": "Бан снят",
        "rcon.kick": "Кик с сервера",
        "rcon.kill": "Slay",
      }[event] || event || "Действие";
    },
    steamProfileUrl(id) {
      const steamId = String(id || "").trim();
      return /^\d{17}$/.test(steamId) ? `https://steamcommunity.com/profiles/${steamId}` : "";
    },
    fmtPlaytime(value) {
      const seconds = Math.max(0, Number(value) || 0);
      if (seconds < 60) return seconds > 0 ? "< 1 м" : "0 м";
      const totalMinutes = Math.floor(seconds / 60);
      const days = Math.floor(totalMinutes / 1440);
      const hours = Math.floor((totalMinutes % 1440) / 60);
      const minutes = totalMinutes % 60;
      const parts = [];
      if (days) parts.push(`${days} д`);
      if (hours) parts.push(`${hours} ч`);
      if (minutes || !parts.length) parts.push(`${minutes} м`);
      return parts.slice(0, 2).join(" ");
    },
    fmtDate(t) {
      if (!t) return "—";
      const d = new Date(t);
      if (isNaN(d)) return t;
      return d.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "medium" });
    },

    // ---- действия панели ----
    async act(url, method, body) {
      if (this.busy) {
        this.toast("Дождитесь завершения предыдущего действия", "warn");
        return false;
      }
      this.busy = true;
      try {
        const { status, data } = await this.api.request(method, url, body);
        const msg = (data && data.error && data.error.message) || (status === 401 ? "Нужен вход" : status === 403 ? "Недостаточно прав" : (status >= 400 ? `HTTP ${status}` : null));
        if (status < 200 || status >= 300 || !data || data.ok === false || data.error) {
          this.toast(msg || "Ошибка", "err");
          return false;
        }
        if (data.queued) {
          this.toast("Игрок офлайн — бан поставлен в очередь и применится при первом входе", "ok");
        } else {
          this.toast("Команда подтверждена сервером", "ok");
        }
        this.refreshAll(true);
        return true;
      } catch (error) {
        this.toast("Сеть недоступна, действие не подтверждено", "err");
        return false;
      } finally {
        this.busy = false;
      }
    },

    async kickPlayer(p) {
      const values = await this.openActionDialog({
        title: `Кикнуть ${p.name}?`,
        message: `SteamID64: ${p.steamId}`,
        confirmText: "Кикнуть",
        danger: true,
        fields: [{ key: "reason", label: "Причина", placeholder: "Необязательно", maxLength: 500 }],
      });
      if (!values) return;
      this.act(`/api/server/${this.state.sid}/players/${p.steamId}/kick`, "POST",
        values.reason ? { reason: values.reason } : {});
    },
    async killPlayer(p) {
      if (!await this.confirmAction("Убить игрока (slay)", `${p.name} · ${p.steamId}`, "Убить")) return;
      this.act(`/api/server/${this.state.sid}/players/${p.steamId}/kill`, "POST", {});
    },
    async sendMsg(p) {
      const values = await this.openActionDialog({
        title: `Варн для ${p.name}`,
        message: `Игрок получит сообщение на сервере, запись появится в истории наказаний. SteamID64: ${p.steamId}`,
        confirmText: "Отправить",
        fields: [{ key: "message", label: "Сообщение", type: "textarea", required: true, maxLength: 2000, placeholder: "Текст предупреждения" }],
      });
      if (!values) return;
      this.act(`/api/server/${this.state.sid}/players/${p.steamId}/message`, "POST", { message: values.message });
    },
    async moveFaction(p, faction) {
      if (faction === p.faction || !faction) return false;
      if (!await this.confirmAction("Сменить фракцию", `Перевести ${p.name} в команду «${faction}»?`, "Перевести", false)) return false;
      return this.act(`/api/server/${this.state.sid}/players/${p.steamId}`, "PATCH", { faction });
    },
    banExpiry(duration) {
      const seconds = {
        "1h": 3600,
        "1d": 86400,
        "3d": 3 * 86400,
        "7d": 7 * 86400,
        "30d": 30 * 86400,
      }[duration];
      return seconds ? new Date(Date.now() + seconds * 1000).toISOString() : null;
    },
    banDurationField() {
      return {
        key: "duration", label: "Срок", type: "select", value: "permanent",
        options: [
          { value: "1h", label: "1 час" }, { value: "1d", label: "1 день" },
          { value: "3d", label: "3 дня" }, { value: "7d", label: "7 дней" },
          { value: "30d", label: "30 дней" }, { value: "permanent", label: "Навсегда" },
        ],
      };
    },
    async banPlayer(p) {
      const values = await this.openActionDialog({
        title: `Забанить ${p.name}?`,
        message: `SteamID64: ${p.steamId}. Действие попадёт в аудит.`,
        confirmText: "Забанить",
        danger: true,
        fields: [
          this.banDurationField(),
          { key: "reason", label: "Причина", placeholder: "Необязательно", maxLength: 500 },
        ],
      });
      if (!values) return;
      const body = {
        steamId: p.steamId,
        display_name: p.name || "",
        expires_utc: this.banExpiry(values.duration),
      };
      if (values.reason) body.reason = values.reason;
      this.act(`/api/server/${this.state.sid}/bans`, "POST", body);
    },
    async unban(b) {
      if (!await this.confirmAction("Снять бан", `SteamID64: ${b.steamId}`, "Снять бан", false)) return;
      this.act(`/api/server/${this.state.sid}/bans/${b.steamId}`, "DELETE", {});
    },
    toggleBanSelected(steamId) {
      this.banSelected = this.banSelected.includes(steamId) ? this.banSelected.filter((id) => id !== steamId) : [...this.banSelected, steamId];
    },
    async editBan(b) {
      if (!b.managed && !b.queued) return this.toast("Изменять можно баны, выданные через панель", "warn");
      const values = await this.openActionDialog({ title: `Изменить бан ${b.steamId}`, confirmText: "Сохранить", danger: true, fields: [this.banDurationField(), { key: "reason", label: "Причина", value: b.reason || "", maxLength: 500 }] });
      if (!values) return;
      await this.act(`/api/server/${this.state.sid}/bans/${b.steamId}`, "PATCH", { reason: values.reason, expires_utc: this.banExpiry(values.duration) });
    },
    async bulkUnban() {
      if (!this.banSelected.length || !await this.confirmAction("Массово снять баны", `Будет обработано: ${this.banSelected.length}`, "Снять выбранные")) return;
      const { status, data } = await this.api.request("POST", `/api/server/${this.state.sid}/bans/bulk`, { action: "unban", steam_ids: this.banSelected });
      if (status === 200) {
        this.toast(`Снято: ${data.succeeded}, ошибок: ${data.failed}`, data.failed ? "warn" : "ok");
        this.banSelected = []; this.refreshAll(true);
      } else this.toast(data?.detail || "Пакетная операция не выполнена", "err");
    },

    async openBroadcast() {
      const values = await this.openActionDialog({
        title: "Объявление всем игрокам",
        message: "Сообщение будет отправлено на текущий сервер.",
        confirmText: "Отправить",
        fields: [{ key: "message", label: "Текст объявления", type: "textarea", required: true, maxLength: 2000, placeholder: "Введите объявление" }],
      });
      if (!values) return;
      this.act(`/api/server/${this.state.sid}/broadcast`, "POST", { message: values.message });
    },
    async openAddBan() {
      const values = await this.openActionDialog({
        title: "Добавить бан",
        message: "Проверьте SteamID64 перед подтверждением.",
        confirmText: "Забанить",
        danger: true,
        fields: [
          { key: "steam", label: "SteamID64", required: true, validator: "steam64", maxLength: 17, placeholder: "7656119…" },
          { key: "display_name", label: "Игрок", maxLength: 120, placeholder: "Ник или комментарий" },
          this.banDurationField(),
          { key: "reason", label: "Причина", placeholder: "Необязательно", maxLength: 500 },
        ],
      });
      if (!values) return;
      this.act(`/api/server/${this.state.sid}/bans`, "POST", {
        steamId: values.steam,
        display_name: values.display_name,
        expires_utc: this.banExpiry(values.duration),
        ...(values.reason ? { reason: values.reason } : {}),
      });
    },

    async changeMap() {
      const m = this.form.map;
      if (!m) { this.toast("Выберите карту", "warn"); return; }
      const label = `${this.catalog.maps[m].display}${MAP_RU[m] ? ` (${MAP_RU[m]})` : ""}`;
      const msg = `Сменить карту на ${label}?\n` +
        `Режимы: ${this.form.experiences.length ? this.form.experiences.join(", ") : "— по умолчанию —"}\n` +
        `Свет: ${this.form.lighting ? this.lightLabel(this.form.lighting) : "— текущий —"}`;
      if (!await this.confirmAction("Сменить карту", msg, "Сменить карту")) return;
      const body = { map: m };
      if (this.form.experiences.length) body.experiences = this.form.experiences;
      if (this.form.lighting) body.lighting = this.form.lighting;
      if (this.form.zoneAlternator) body.zoneAlternator = this.form.zoneAlternator;
      this.act(`/api/server/${this.state.sid}/match/map`, "POST", body);
    },
    prepareRotationMap(entry) {
      const map = typeof entry === "string" ? entry : entry?.map;
      if (!map) return;
      this.form.map = map;
      this.form.experiences = Array.isArray(entry?.experiences) ? [...entry.experiences] : [];
      this.form.lighting = entry?.lighting || "";
      this.form.zoneAlternator = entry?.zoneAlternator || "";
      this.state.tab = "controls";
      this.toast(`Карта «${this.mapLabel(map)}» выбрана в форме смены карты`, "ok");
    },
    startRotationEdit() {
      const entries = [...(this.ov?.rotation?.entries || [])];
      entries.sort((a, b) => (a.index ?? 1e9) - (b.index ?? 1e9));
      this.rotationDraft = entries.map((entry) => ({
        index: entry.index,
        map: entry.map || "",
        experiences: Array.isArray(entry.experiences) ? [...entry.experiences] : [],
        experiencesText: Array.isArray(entry.experiences) ? entry.experiences.join(", ") : "",
        lighting: entry.lighting || "",
        zoneAlternator: entry.zoneAlternator || "",
      }));
      this.rotationEdit = true;
    },
    reindexRotationDraft() {
      this.rotationDraft.forEach((entry, i) => { entry.index = i; });
    },
    cancelRotationEdit() {
      this.rotationEdit = false;
      this.rotationDraft = [];
    },
    addRotationEntry() {
      const firstMap = Object.keys(this.catalog.maps || {})[0] || "";
      this.rotationDraft.push({ index: this.rotationDraft.length, map: firstMap, experiences: [], experiencesText: "", lighting: "", zoneAlternator: "" });
    },
    moveRotationEntry(index, delta) {
      const target = index + delta;
      if (target < 0 || target >= this.rotationDraft.length) return;
      const [entry] = this.rotationDraft.splice(index, 1);
      this.rotationDraft.splice(target, 0, entry);
      this.reindexRotationDraft();
    },
    removeRotationEntry(index) {
      if (this.rotationDraft.length <= 1) return this.toast("Ротация должна содержать хотя бы одну карту", "warn");
      this.rotationDraft.splice(index, 1);
      this.reindexRotationDraft();
    },
    async saveRotation() {
      if (!this.rotationDraft.length) return this.toast("Добавьте хотя бы одну карту", "warn");
      const entries = this.rotationDraft.map((entry) => ({
        map: entry.map,
        experiences: String(entry.experiencesText || "").split(",").map((value) => value.trim()).filter(Boolean),
        lighting: entry.lighting || "",
        zoneAlternator: entry.zoneAlternator || "",
      }));
      this.rotationSaving = true;
      try {
        const { status, data } = await this.api.request("PUT", `/api/server/${this.state.sid}/rotation`, { entries });
        if (status === 200 && data?.ok) {
          this.rotationEdit = false;
          this.rotationDraft = [];
          this.toast("Ротация сохранена", "ok");
          await this.refreshAll(true);
        } else {
          this.toast(data?.detail || data?.error?.message || `Сервер не принял ротацию (HTTP ${status})`, "err");
        }
      } finally {
        this.rotationSaving = false;
      }
    },
    async changeLighting(l) {
      if (!await this.confirmAction("Сменить освещение", this.lightLabel(l), "Сменить", false)) return;
      this.act(`/api/server/${this.state.sid}/world/lighting`, "PUT", { lighting: l });
    },
    async endMatch() {
      if (!await this.confirmAction("Завершить матч", "Текущий матч будет завершён для всех игроков.", "Завершить")) return;
      this.act(`/api/server/${this.state.sid}/match/end`, "POST", {});
    },
    async restartMatch() {
      if (!await this.confirmAction("Перезапустить матч", "Игроков выкинет из текущего матча.", "Перезапустить")) return;
      this.act(`/api/server/${this.state.sid}/match/restart`, "POST", {});
    },

    // ---- аудит ----
    async loadAudit() {
      const { data } = await this.api.request("GET", `/api/server/${this.state.sid}/audit?limit=${this.auditLimit}`);
      if (data && data.ok) this.audit = data.entries || [];
      else if (data && data.error) this.toast(data.error.message, "err");
    },
    async loadSiteAudit() {
      const { data } = await this.api.request("GET", `/api/audit/site?limit=${this.auditLimit}`);
      if (data && data.ok) this.siteAudit = data.entries || [];
    },

    // ---- килфид ----
    startKillfeedPolling() {
      this.stopKillfeedPolling();
      this.loadKillfeed();
      this.killfeedTimer = setInterval(() => { if (!document.hidden) this.loadKillfeed(); }, 5000);
    },
    stopKillfeedPolling() {
      if (this.killfeedTimer) {
        clearInterval(this.killfeedTimer);
        this.killfeedTimer = null;
      }
    },
    async loadKillfeed() {
      const sid = this.state.sid;
      if (!sid) return;
      this.killfeedLoading = true;
      const [feed, status] = await Promise.all([
        this.api.request("GET", `/api/killfeed?server=${encodeURIComponent(sid)}&limit=200&kind=${encodeURIComponent(this.killfeedKind)}`),
        this.api.request("GET", `/api/killfeed/status?server=${encodeURIComponent(sid)}`),
      ]);
      this.killfeedLoading = false;
      if (sid !== this.state.sid || this.state.tab !== "killfeed") return;
      if (feed.data && feed.data.ok) {
        this.killfeedRows = feed.data.entries || [];
        this.killfeedStats = feed.data.stats || {};
      } else if (feed.data && feed.data.error) {
        this.toast(feed.data.error.message, "err");
      }
      if (status.data && status.data.ok) this.killfeedStatus = status.data;
    },
    async loadKillfeedSetup() {
      const { data } = await this.api.request("GET", `/api/killfeed/setup?server=${encodeURIComponent(this.state.sid)}`);
      if (data && data.ok) this.killfeedSetup = data;
      else if (data && data.error) this.toast(data.error.message, "err");
    },

    // ---- конфиг ----
    async ensureConfig() {
      if (!this.cfg.loaded) await this.loadConfig();
    },
    async loadRoutes() {
      if (!this.state.sid || !this.has("config_view")) return;
      const { status, data } = await this.api.request("GET", `/api/server/${this.state.sid}/routes`);
      if (status === 200 && data?.ok) {
        this.serverApiRoutes = data.routes || [];
        this.serverBuild = data.build || "";
      }
    },
    async loadConfig() {
      this.busy = true;
      const { status, data } = await this.api.request("GET", `/api/server/${this.state.sid}/config`);
      this.busy = false;
      if (status === 200 && data.ok) {
        this.cfg = { text: data.text || "", revision: data.revision, writable: !!data.writable, warnings: data.warnings || [], loaded: true };
        this.cfgResult = null;
      } else if (data && data.error) {
        this.toast(data.error.message, "err");
        this.cfg = { ...this.cfg, loaded: true };
      }
    },
    async applyConfig() {
      if (!this.cfg.loaded) return;
      if (!await this.confirmAction("Применить конфиг", "Конфиг будет применён целиком. Если ревизия устарела, обновите данные и повторите.", "Применить")) return;
      this.busy = true;
      const r = await fetch(`/api/server/${this.state.sid}/config`, {
        method: "PUT",
        credentials: "same-origin",
        headers: {
          "Content-Type": "text/plain",
          "If-Match": this.cfg.revision,
          "X-CSRF-Token": decodeURIComponent(document.cookie.split("; ").find((c) => c.startsWith("wds_csrf=") || c.startsWith("__Host-wds_csrf="))?.split("=")[1] || ""),
          "Idempotency-Key": globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        },
        body: this.cfg.text,
      });
      this.busy = false;
      const data = (await r.json().catch(() => null)) || { status: r.status };
      this.cfgResult = data;
      this.cfgResultStatus = r.status >= 400 ? "err" : "ok";
      if (r.status === 200) {
        this.toast("Конфиг применён", "ok");
        if (data.revision) this.cfg.revision = data.revision;
        this.refreshAll(true);
      } else {
        this.toast("Конфиг не применён — см. результат", "err");
        if (data.revision) this.cfg.revision = data.revision;
      }
    },

    // ---- права ролей ----
    async loadRoles(force = false) {
      if (this.rolesList.length && !force) return;
      const { data } = await this.api.request("GET", "/api/roles");
      if (data && data.ok) {
        this.allPerms = data.all_permissions || [];
        this.rolesList = data.roles || [];
        this.state.roleDrafts = {};
      } else if (data) {
        this.toast(data.detail || data.error?.message || "Не удалось загрузить роли", "err");
      }
    },
    draftFor(r) {
      if (!this.state.roleDrafts[r.id]) this.state.roleDrafts[r.id] = [...(r.perms || [])];
      return this.state.roleDrafts[r.id];
    },
    roleHas(r, p) { return this.draftFor(r).includes(p); },
    roleAllChecked(r) { return this.draftFor(r).length === this.allPerms.length; },
    rolePermissionGroups() {
      const known = new Set();
      const groups = PERM_GROUPS.map((group) => ({
        ...group,
        perms: group.perms.filter((permission) => this.allPerms.includes(permission)),
      })).filter((group) => {
        group.perms.forEach((permission) => known.add(permission));
        return group.perms.length;
      });
      const rest = this.allPerms.filter((permission) => !known.has(permission));
      if (rest.length) groups.push({ id: "other", label: "Прочее", perms: rest });
      return groups;
    },
    visibleRoles() {
      const query = this.roleSearch.trim().toLowerCase();
      return this.rolesList.filter((role) => {
        if (this.rolePermissionFilter === "configured" && !role.configured) return false;
        if (this.rolePermissionFilter === "legacy" && !role.legacy) return false;
        if (this.rolePermissionFilter === "changed" && !this.roleDirty(role)) return false;
        return !query || `${role.name} ${role.id}`.toLowerCase().includes(query);
      });
    },
    roleGroupChecked(role, group) {
      const draft = this.draftFor(role);
      return group.perms.length > 0 && group.perms.every((permission) => draft.includes(permission));
    },
    roleGroupCount(role, group) {
      return group.perms.filter((permission) => this.draftFor(role).includes(permission)).length;
    },
    roleDirty(role) {
      const draft = [...this.draftFor(role)].sort();
      const saved = [...(role.perms || [])].sort();
      return draft.join("|") !== saved.join("|");
    },
    roleReset(role) {
      this.state.roleDrafts[role.id] = [...(role.perms || [])];
    },
    roleToggleGroup(role, group, enabled) {
      const draft = this.draftFor(role);
      for (const permission of group.perms) {
        const index = draft.indexOf(permission);
        if (enabled && index < 0) draft.push(permission);
        if (!enabled && index >= 0) draft.splice(index, 1);
      }
    },
    roleTog(r, p, on) {
      const d = this.draftFor(r);
      const i = d.indexOf(p);
      if (on && i < 0) d.push(p);
      if (!on && i >= 0) d.splice(i, 1);
    },
    roleTogAll(r, on) {
      const d = this.draftFor(r);
      this.state.roleDrafts[r.id] = on ? [...this.allPerms] : [];
    },
    roleApplyPreset(role, presetId) {
      const preset = ROLE_PRESETS[presetId];
      if (!preset) return;
      this.state.roleDrafts[role.id] = preset.perms.filter((permission) => this.allPerms.includes(permission));
      this.toast(`Шаблон «${preset.label}» применён к роли «${role.name}». Не забудьте сохранить.`, "warn");
    },
    roleCopyFrom(role) {
      const sourceId = this.roleCopySources[role.id];
      const source = this.rolesList.find((item) => item.id === sourceId);
      if (!source) return this.toast("Выберите роль-источник", "warn");
      this.state.roleDrafts[role.id] = [...this.draftFor(source)];
      this.toast(`Права скопированы из роли «${source.name}». Не забудьте сохранить.`, "warn");
    },
    async saveAllRoles() {
      const dirty = this.rolesList.filter((role) => this.roleDirty(role));
      if (!dirty.length) return;
      const confirmed = await this.confirmAction(
        "Сохранить изменённые роли",
        `Будет сохранено ролей: ${dirty.length}.`,
        "Сохранить",
        false,
      );
      if (!confirmed) return;
      for (const role of dirty) {
        const ok = await this.saveRole(role, true);
        if (!ok) return;
      }
      this.toast(`Сохранено ролей: ${dirty.length}`, "ok");
    },
    async saveRole(r, silent = false) {
      const perms = this.draftFor(r);
      this.busy = true;
      const { status, data } = await this.api.request("PUT", "/api/roles", { role_ref: r.id, perms });
      this.busy = false;
      if (status === 200 && data.ok) {
        r.configured = true;
        r.perms = [...perms];
        if (!silent) this.toast(`Права роли «${r.name}» сохранены`, "ok");
        return true;
      } else {
        this.toast((data && data.error && data.error.message) || "Не удалось сохранить", "err");
        return false;
      }
    },
  },
});

const vm = app.mount("#app");
