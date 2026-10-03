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
      payments: { plans: [], items: [], loading: false, error: '' },
      botWizard: { open: false, step: 1, id: '', busy: false },
      botScopeChoices: [{id:'heartbeat',label:'Состояние подключения'}, {id:'events.write',label:'Отправка событий'}],
      botEvents: [],
      botEventsName: '',
      noAccess: false,
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
      staffQuery: "",
      clans: { items: [], loading: false, error: "" },
      selectedClan: null,
      clanQuery: "",
      tickets: { items: [], count: 0, loading: false, error: "", selected: null, detailLoading: false },
      ticketQuery: "",
      ticketStatusFilter: "active",
      ticketPriorityFilter: "all",
      ticketCommentDraft: "",
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
      playerCardFaction: "",
      playerCardClanId: "",
      playerCardTab: "overview",
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
      if (any("change_map", "change_lighting", "match_end", "match_restart")) t.push({ id: "controls", label: "Карта / Матч" });
      if (this.perms.length) t.push({ id: "rotation", label: "Ротация" });
      if (any("ban_view", "ban_add", "ban_remove")) t.push({ id: "bans", label: "Баны" });
      if (any("reserved_view", "reserved_edit")) t.push({ id: "reserved", label: "Резервные слоты" });
      if (any("vip_view", "vip_edit")) t.push({ id: "vip", label: "VIP-слоты" });
      if (this.perms.includes("clans")) t.push({ id: "clans", label: "Кланы" });
      if (any("tickets_view", "tickets_edit")) t.push({ id: "tickets", label: "Тикеты" });
      if (this.perms.includes("audit")) t.push({ id: "audit", label: "Аудит" });
      if (any("config_view", "config_edit")) t.push({ id: "config", label: "Конфиг" });
      if (this.perms.includes("roles")) t.push({ id: "staff", label: "Администрация" });
      if (this.perms.includes("roles")) t.push({ id: "discord", label: "Боты и Discord" });
      if (any("audit", "roles", "config_view")) t.push({ id: "system", label: "Состояние" });
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
    playersTableQuery() { this.playersTablePage = 1; },
    playersTableSort() { this.playersTablePage = 1; },
    bansQuery() { this.bansPage = 1; },
    bansSort() { this.bansPage = 1; },
    playerCardTab(tab) {
      if (tab === "notes" && this.playerCard.open) this.loadPlayerNotes();
      if (tab === "clan" && this.playerCard.open && this.has("clans")) this.loadClans();
    },
    "state.tab"(t) {
      if (t === 'status') this.loadAttention();
      if (t === "controls") this.ensureCatalog();
      if (t === "rotation") this.ensureCatalog();
      if (t === "audit") { this.loadAudit(); this.loadSiteAudit(); }
      if (t === "config") this.ensureConfig();
      if (t === "staff") this.loadStaff();
      if (t === "discord") this.loadDiscordAdmin();
      if (t === "system") this.loadSystemStatus();
      if (t === "clans") this.loadClans();
      if (t === "roles") this.loadRoles();
      if (t === "reserved") this.loadReserved();
      if (t === "vip") { this.loadVipSlots(); this.loadPayments(); }
      if (t === "tickets") this.loadTickets();
    },
    "state.sid"() {
      this.ensureCatalog();
      if (!this.state.ovById[this.state.sid]) this.loadOverview(this.state.sid, true);
      if (this.state.tab === "audit") { this.loadAudit(); this.loadSiteAudit(); }
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
        const csrf = document.cookie.split("; ").find((c) => c.startsWith("wds_csrf="))?.split("=")[1];
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
        if (operation) {
          const ok = r.ok && data && data.ok !== false && !data.error;
          Object.assign(operation,{state:ok ? (data.synced === false ? 'pending' : 'confirmed') : 'error', detail: ok ? (data.synced === false ? 'Сохранено, сервер ещё не подтвердил синхронизацию' : 'Запрос подтверждён API') : String(data?.detail || data?.error?.message || `Ошибка ${r.status}`)});
        }
        return { status: r.status, data };
      },
    };
    document.addEventListener("keydown", this.handleGlobalKeydown);
    this._clock = setInterval(()=>{this.clockNow=Date.now();},1000);
      this._attentionTimer=setInterval(()=>{if(this.booted && !this.noAccess && MODE==='panel') {if(this.state.tab==='status') this.loadAttention();if(this.state.tab==='discord' && !this.discordAdmin.loading) this.loadDiscordAdmin(true);}},30000);
    this.init();
  },
  beforeUnmount() {
    document.removeEventListener("keydown", this.handleGlobalKeydown);
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
      if(this.has('tickets_view') || this.has('tickets_edit')) tasks.push(this.api.request('GET','/api/tickets').then(r=>{this.overviewAttention.tickets=r.status===200 ? (r.data?.items || []).filter(t=>!['closed','resolved'].includes(t.status)).length : null;}));
      if(this.has('vip_view') || this.has('vip_edit')) tasks.push(Promise.all(this.state.servers.map(async s=>{const r=await this.api.request('GET',`/api/server/${s.id}/vip-slots`); return r.status===200 ? (r.data.items || []).map(v=>({...v,server_name:s.name,server_id:s.id})) : null;})).then(rows=>{this.overviewAttention.vips=rows.flatMap(r=>r || []);this.overviewAttention.error=rows.some(r=>r===null) ? 'Не все серверы передали сведения VIP' : '';}));
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
    permLabel(p) { return PERM_LABELS[p] || p; },
    permHint(p) { return PERM_HINTS[p] || p; },
    handleGlobalKeydown(event) {
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
      }
    },
    openActionDialog({ title, message = "", confirmText = "Подтвердить", danger = false, fields = [] }) {
      if (this._dialogResolve) this._dialogResolve(null);
      this.actionDialog = {
        open: true,
        title,
        message: danger ? `${message}\nВыбранный сервер: ${this.state.servers.find(s=>s.id===this.state.sid)?.name || this.state.sid}` : message,
        confirmText,
        danger,
        fields: fields.map((field) => ({ ...field, value: field.value ?? "" })),
        error: "",
      };
      return new Promise((resolve) => {
        this._dialogResolve = resolve;
        this.$nextTick(() => document.querySelector(".action-dialog input, .action-dialog textarea")?.focus());
      });
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
        if (field.maxLength && value.length > field.maxLength) {
          this.actionDialog.error = `Поле «${field.label}» длиннее ${field.maxLength} символов`;
          return;
        }
        values[field.key] = value;
      }
      const resolve = this._dialogResolve;
      this._dialogResolve = null;
      this.actionDialog.open = false;
      if (resolve) resolve(values);
    },
    cancelActionDialog() {
      const resolve = this._dialogResolve;
      this._dialogResolve = null;
      this.actionDialog.open = false;
      if (resolve) resolve(null);
    },

    async init() {
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
        return;
      }

      const q = new URLSearchParams(location.search);
      if (q.get("bind") === "discord" && q.get("ok")) this.toast("Discord привязан", "ok");
      else if (q.get("bind") === "discord") this.toast(`Привязка Discord не удалась (${q.get("fail") || "?"})`, "err");
      else if (q.get("auth") === "steam" && q.get("fail")) this.toast("Вход через Steam не удался", "err");
      if (MODE === "panel") this.loadRoles();
      if (MODE === "panel") {
        try { const saved=JSON.parse(sessionStorage.getItem('wardogs-view') || '{}'); for(const k of ['playersTableQuery','playersTableSort','bansQuery','bansSort','vipQuery','vipFilter','ticketQuery','ticketStatusFilter','ticketPriorityFilter']) if(typeof saved[k]==='string') this[k]=saved[k]; } catch {}
        this.$watch(()=>[this.playersTableQuery,this.playersTableSort,this.bansQuery,this.bansSort,this.vipQuery,this.vipFilter,this.ticketQuery,this.ticketStatusFilter,this.ticketPriorityFilter],()=>this.saveViewPreferences());
        this.loadAttention();
      }
      this.booted = true;
      this.startTimer();
      this.refreshAll(true);
    },

    loginSteam() { location.href = "/api/auth/steam/start"; },
    async loginDev() {
      const { status, data } = await this.api.request("POST", "/api/auth/dev");
      if (status === 200 && data && data.ok) location.href = data.redirect || "/panel";
      else this.toast("Локальный вход недоступен", "err");
    },
    bindDiscord() { location.href = "/api/auth/discord/start"; },
    goPanel() { location.href = "/panel"; },
    async refreshRoles(silent = false) {
      this.busy = true;
      const { data } = await this.api.request("POST", "/api/auth/refresh");
      this.busy = false;
      if (data && data.authed) {
        this.state.me = data;
        if (!silent) this.toast(this.perms.length ? "Права обновлены" : "Роли обновлены, права не выданы", this.perms.length ? "ok" : "warn");
        if (MODE === "panel" && !this.hasPerms) this.noAccess = true;
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
      return (this.ov?.status?.factionScores || []).find((faction) => faction.name === name)?.colorHex || "#777";
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
      this._timer = setInterval(() => { if (!this.noAccess) this.refreshAll(false); }, this.state.refresh * 1000);
      if (this.state.authed && this.authConfigured) {
        this._roleTimer = setInterval(() => this.refreshRoles(true), 60 * 1000);
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
      if (this.state.refreshing) return;
      this.state.refreshing = true;
      try {
        await Promise.allSettled(this.state.servers.map((s) => this.loadOverview(s.id, force)));
        this.lastUpdate = new Date().toLocaleTimeString("ru-RU");
      } finally {
        this.state.refreshing = false;
      }
    },

    async loadOverview(id, force) {
      const cached = this.state.ovById[id];
      if (cached && !force && Date.now() - (cached._ts || 0) < 1500) return;
      try {
        const { status, data } = await this.api.request("GET", `/api/server/${id}/overview`);
        const ok=status===200 && data && data.ok!==false && !data.error;
        this.connections[id]={ok:!!ok,success:ok ? Date.now() : this.connections[id]?.success || null,detail:ok ? '' : data?.error?.message || data?.detail || 'Не удалось получить состояние сервера'};
        if(ok || !cached?.status) this.state.ovById[id]=data?.status || data?.error ? {...data,_ts:Date.now()} : {error:{message:'Сервер недоступен'},_ts:Date.now()};
      } catch {
        this.connections[id]={ok:false,success:this.connections[id]?.success || null,detail:'Нет связи с панелью'};
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
    async openPlayerCard(playerOrSteamId, live = null) {
      const steamId = typeof playerOrSteamId === "string" ? playerOrSteamId : playerOrSteamId?.steamId;
      if (!steamId) return;
      this._playerReturnFocus=document.activeElement;
      const requestId=(this._playerRequestId || 0)+1; this._playerRequestId=requestId;
      live=live || (this.ov?.players?.players || []).find(p=>p.steamId===steamId) || null;
      this.playerSearchResults = [];
      this.playerCardTab = "overview";
      this.playerCardClanId = "";
      this.playerNotes = { items: [], loading: false, error: "", canEdit: false, draft: "" };
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
    },
    closePlayerCard() {
      this._playerRequestId=(this._playerRequestId || 0)+1;
      this._playerReturnFocus?.focus?.({preventScroll:true});
      this.playerCardTab = "overview";
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
      if (!live) return this.toast("Действие доступно, когда игрок находится на сервере", "warn");
      if (action === "message") return this.sendMsg(live);
      if (action === "kick") return this.kickPlayer(live);
      if (action === "kill") return this.killPlayer(live);
      if (action === "ban") return this.banPlayer(live);
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
    async createVipOrder() {
      const values=await this.openActionDialog({title:'Заявка на VIP',message:'Черновой режим: заявка сохраняется, но оплата и выдача начнут работать после подключения Donatty.',confirmText:'Создать заявку',fields:[
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
    async saveVipSlot(item = null) {
      const values = await this.openActionDialog({
        title: item ? "Изменить VIP-слот" : "Выдать VIP-слот",
        message: "SteamID будет автоматически добавлен в резерв выбранного сервера.",
        confirmText: item ? "Сохранить" : "Выдать VIP",
        fields: [
          { key: "steam_id", label: "SteamID64", required: true, validator: "steam64", maxLength: 17, value: item?.steam_id || "", placeholder: "7656119…" },
          { key: "display_name", label: "Игрок", maxLength: 120, value: item?.display_name || "", placeholder: "Ник или комментарий" },
          { key: "expires_date", label: "Действует до", type: "date", value: this.vipDateInput(item?.expires_utc) },
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
        },
      );
      if (status === 200 && data?.ok) {
        await this.loadVipSlots();
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
    async deleteTempVoice(room) {
      if (!await this.confirmAction("Удалить временную комнату", `${room.name || room.channel_id} · владелец ${room.owner_name || room.owner_id}`, "Удалить")) return;
      const { status, data } = await this.api.request("POST", `/api/discord/tempvoice/${room.channel_id}/delete`, {});
      if (status === 200 && data?.ok) { this.toast("Комната удалена", "ok"); this.loadDiscordAdmin(); }
      else this.toast(data?.detail || data?.error || "Не удалось удалить комнату", "err");
    },
    async transferTempVoice(room) {
      const members = this.discordAdmin.tempvoice?.members || [];
      const values = await this.openActionDialog({ title: "Передать временную комнату", confirmText: "Передать", fields: [{ key: "owner_id", label: "Новый владелец", type: "select", required: true, options: members.map((member) => ({ value: member.id, label: `${member.name} · ${member.id}` })) }] });
      if (!values) return;
      const { status, data } = await this.api.request("POST", `/api/discord/tempvoice/${room.channel_id}/transfer`, values);
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
    async previewUserPermissions() {
      const values = await this.openActionDialog({ title: "Итоговые права пользователя", confirmText: "Показать", fields: [{ key: "steam_id", label: "SteamID64", validator: "steam64", required: true }] });
      if (!values) return;
      const { status, data } = await this.api.request("GET", `/api/roles/preview/user/${values.steam_id}`);
      if (status !== 200) return this.toast(data?.detail || "Пользователь не найден", "err");
      this.openActionDialog({ title: data.discord_name || data.steam_id, message: `Роли: ${data.roles.map((role) => role.name).join(", ") || "нет"}.\nИтоговые права: ${data.permissions.map(this.permLabel).join(", ") || "нет доступа"}.`, confirmText: "Закрыть" });
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
        this.toast("Команда подтверждена сервером", "ok");
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
        title: `Сообщение для ${p.name}`,
        message: `SteamID64: ${p.steamId}`,
        confirmText: "Отправить",
        fields: [{ key: "message", label: "Сообщение", type: "textarea", required: true, maxLength: 2000, placeholder: "Введите сообщение игроку" }],
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
      if (!b.managed) return this.toast("Изменять можно баны, выданные через панель", "warn");
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
      const entries = this.ov?.rotation?.entries || [];
      this.rotationDraft = entries.map((entry) => ({
        map: entry.map || "",
        experiences: Array.isArray(entry.experiences) ? [...entry.experiences] : [],
        experiencesText: Array.isArray(entry.experiences) ? entry.experiences.join(", ") : "",
        lighting: entry.lighting || "",
        zoneAlternator: entry.zoneAlternator || "",
      }));
      this.rotationEdit = true;
    },
    cancelRotationEdit() {
      this.rotationEdit = false;
      this.rotationDraft = [];
    },
    addRotationEntry() {
      const firstMap = Object.keys(this.catalog.maps || {})[0] || "";
      this.rotationDraft.push({ map: firstMap, experiences: [], experiencesText: "", lighting: "", zoneAlternator: "" });
    },
    moveRotationEntry(index, delta) {
      const target = index + delta;
      if (target < 0 || target >= this.rotationDraft.length) return;
      const [entry] = this.rotationDraft.splice(index, 1);
      this.rotationDraft.splice(target, 0, entry);
    },
    removeRotationEntry(index) {
      if (this.rotationDraft.length <= 1) return this.toast("Ротация должна содержать хотя бы одну карту", "warn");
      this.rotationDraft.splice(index, 1);
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

    // ---- конфиг ----
    async ensureConfig() {
      if (!this.cfg.loaded) await this.loadConfig();
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
          "X-CSRF-Token": decodeURIComponent(document.cookie.split("; ").find((c) => c.startsWith("wds_csrf="))?.split("=")[1] || ""),
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
