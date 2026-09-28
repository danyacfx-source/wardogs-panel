(() => {
  const page = document.body.dataset.page;
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const fmtDate = (v) => v ? new Date(v * 1000).toLocaleString("ru-RU") : "—";
  const fmtPlaytime = (value) => {
    const totalMinutes = Math.floor(Math.max(0, Number(value) || 0) / 60);
    if (!totalMinutes) return "0 м";
    const days = Math.floor(totalMinutes / 1440);
    const hours = Math.floor((totalMinutes % 1440) / 60);
    const minutes = totalMinutes % 60;
    const parts = [];
    if (days) parts.push(`${days} д`);
    if (hours) parts.push(`${hours} ч`);
    if (minutes || !parts.length) parts.push(`${minutes} м`);
    return parts.slice(0, 2).join(" ");
  };
  const fmtKd = (player) => {
    const kills = Number(player?.kills || 0);
    const deaths = Number(player?.deaths || 0);
    return deaths > 0 ? (kills / deaths).toFixed(2) : "—";
  };
  async function json(url) { const r = await fetch(url, {credentials:"same-origin"}); return {status:r.status, data:await r.json().catch(()=>null)}; }

  let loadedPlayers = [];
  const playerSearch = document.querySelector("#playerSearch");
  const playerSort = document.querySelector("#playerSort");

  function renderPlayers(players) {
    const notice = document.querySelector("#playersNotice"), table = document.querySelector("#playersTable");
    if (!players) { notice.textContent = "Список доступен после входа через Steam и только роли с правом просмотра игроков."; return; }
    loadedPlayers = players;
    const query = String(playerSearch?.value || "").trim().toLocaleLowerCase("ru");
    const visible = query
      ? players.filter((p) => String(p.name || "").toLocaleLowerCase("ru").includes(query) || String(p.steam_id || "").includes(query))
      : players;
    notice.hidden = !!visible.length;
    notice.textContent = visible.length ? "" : "По вашему запросу игроки не найдены.";
    table.hidden = !visible.length;
    table.querySelector("tbody").innerHTML = visible.map((p, i) => {
      const steamId = /^\d{17}$/.test(String(p.steam_id || "")) ? String(p.steam_id) : "";
      const profile = steamId
        ? `<a class="portal-player-link" href="https://steamcommunity.com/profiles/${steamId}" target="_blank" rel="noopener noreferrer"><b>${esc(p.name)}</b><small>${steamId}</small></a>`
        : `<b>${esc(p.name)}</b>`;
      const kd = fmtKd(p);
      return `<tr><td>${i + 1}</td><td>${profile}${p.online ? '<span class="portal-online">онлайн</span>' : ""}</td><td class="num">${Number(p.server_count || 0)}</td><td class="num">${Number(p.kills || 0)}</td><td class="num">${Number(p.deaths || 0)}</td><td class="num">${kd}</td><td>${fmtPlaytime(p.total_seconds)}</td><td class="num">${Number(p.session_count || 0)}</td><td>${fmtDate(p.last_seen_utc)}</td></tr>`;
    }).join("");
    if (page === "activity") {
      const metric = playerSort?.value || "kills";
      const labels = {
        kills: (p) => `${p.kills} K`,
        kd: (p) => `K/D ${fmtKd(p)}`,
        playtime: (p) => fmtPlaytime(p.total_seconds),
        sessions: (p) => `${p.session_count || 0} сессий`,
        recent: (p) => fmtDate(p.last_seen_utc),
      };
      document.querySelector("#podium").innerHTML = visible.slice(0, 3).map((p, i) => `<article class="podium-card rank-${i + 1}"><span>#${i + 1}</span><strong>${esc(p.name)}</strong><b>${labels[metric](p)}</b><small>${p.online ? "сейчас онлайн" : `${p.server_count || 0} серверов`}</small></article>`).join("");
    }
  }

  async function loadPlayers() {
    const notice = document.querySelector("#playersNotice");
    if (notice) { notice.hidden = false; notice.textContent = "Загрузка…"; }
    const sort = playerSort?.value || "kills";
    const r = await json(`/api/stats/players?limit=200&sort=${encodeURIComponent(sort)}`);
    renderPlayers(r.status === 200 && r.data ? r.data.players : null);
  }

  function drawChart(samples) {
    const canvas = document.querySelector("#populationChart"); if (!canvas) return;
    const empty = document.querySelector("#chartEmpty");
    if (!samples.length) { canvas.hidden=true; empty.hidden=false; return; }
    canvas.hidden=false; empty.hidden=true;
    const ratio=devicePixelRatio||1, w=canvas.clientWidth||900, h=260; canvas.width=w*ratio; canvas.height=h*ratio;
    const c=canvas.getContext("2d"); c.scale(ratio,ratio); c.clearRect(0,0,w,h);
    const byTs=new Map(); for(const s of samples) byTs.set(s.ts,(byTs.get(s.ts)||0)+s.players);
    const pts=[...byTs].sort((a,b)=>a[0]-b[0]), max=Math.max(1,...pts.map(x=>x[1]));
    c.strokeStyle="#343536"; c.fillStyle="#97938b"; c.font="11px Segoe UI";
    for(let i=0;i<=4;i++){const y=20+(h-45)*i/4;c.beginPath();c.moveTo(42,y);c.lineTo(w-10,y);c.stroke();c.fillText(String(Math.round(max*(4-i)/4)),8,y+4)}
    c.strokeStyle="#f6a817";c.lineWidth=3;c.beginPath();pts.forEach((p,i)=>{const x=42+(w-55)*(i/Math.max(1,pts.length-1)),y=20+(h-45)*(1-p[1]/max);i?c.lineTo(x,y):c.moveTo(x,y)});c.stroke();
  }
  async function loadPopulation(hours=24){const r=await json(`/api/stats/population?hours=${hours}`);drawChart((r.data&&r.data.samples)||[])}
  playerSearch?.addEventListener("input", () => renderPlayers(loadedPlayers));
  playerSort?.addEventListener("change", loadPlayers);
  document.querySelector("#refreshPlayers")?.addEventListener("click", loadPlayers);
  if(page === "stats") { loadPopulation(); loadPlayers(); document.querySelectorAll("[data-hours]").forEach(b=>b.addEventListener("click",()=>{document.querySelectorAll("[data-hours]").forEach(x=>x.classList.remove("active"));b.classList.add("active");loadPopulation(b.dataset.hours)})); window.addEventListener("resize",()=>loadPopulation(document.querySelector("[data-hours].active").dataset.hours)); }
  if(page === "activity") loadPlayers();
})();
