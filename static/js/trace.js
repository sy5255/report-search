/* Cognitive Trace — 성능 평가 대시보드 */
(function(){
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const pct = (v) => (v === null || v === undefined) ? "—" : `${Math.round(v * 1000) / 10}%`;
  const num = (v) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString();

  const INTENT_META = {
    "RAG_KNOWLEDGE": { label: "사내 문서 검색", color: "var(--chart-1)" },
    "DB_ANALYSIS":   { label: "DB 통계 분석",   color: "var(--chart-2)" },
    "HYBRID_DB_RAG": { label: "통계+문서 통합", color: "var(--chart-3)" },
    "GENERAL_CHAT":  { label: "일반 대화",      color: "var(--chart-4)" },
  };
  const KG_SOURCE_META = {
    "lot_wf":    { label: "Lot+WF 정밀 매칭", color: "var(--chart-1)" },
    "lot":       { label: "Lot 단위 매칭",     color: "var(--chart-2)" },
    "edm_token": { label: "EDM 링크 보조",     color: "var(--chart-3)" },
  };

  /* ── 공유 hover 툴팁 ─────────────────────────────── */
  const tip = () => $("traceTip");
  function bindTip(el, html){
    el.addEventListener("mouseenter", (e) => {
      const t = tip(); if(!t) return;
      t.innerHTML = html;
      t.style.display = "block";
      position(e);
    });
    el.addEventListener("mousemove", position);
    el.addEventListener("mouseleave", () => { const t = tip(); if(t) t.style.display = "none"; });
    function position(e){
      const t = tip(); if(!t) return;
      let x = e.clientX + 12, y = e.clientY - 34;
      const r = t.getBoundingClientRect();
      if(x + r.width > window.innerWidth - 8) x = e.clientX - r.width - 12;
      if(y < 8) y = e.clientY + 16;
      t.style.left = `${x}px`; t.style.top = `${y}px`;
    }
  }

  /* ── 렌더 프리미티브 ─────────────────────────────── */
  function statTile(label, value, sub){
    return `<div class="stat-tile"><div class="stat-label">${label}</div><div class="stat-value">${value}</div>${sub ? `<div class="stat-sub">${sub}</div>` : ""}</div>`;
  }

  function tableView(headers, rows){
    if(!rows.length) return "";
    const th = headers.map(h => `<th>${esc(h)}</th>`).join("");
    const trs = rows.map(r => `<tr>${r.map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("");
    return `<details class="chart-table"><summary>표로 보기</summary><table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table></details>`;
  }

  // 세로 바 (일별 시리즈 — series: [{key,color,label}])
  function renderVBars(el, daily, series, opts = {}){
    if(!el) return;
    if(!daily.length){ el.innerHTML = `<div class="empty-note">아직 집계할 데이터가 없습니다</div>`; return; }
    const max = Math.max(1, ...daily.flatMap(d => series.map(s => d[s.key] || 0)));
    const wrap = document.createElement("div");
    wrap.className = "vbar-chart";
    daily.forEach(d => {
      const col = document.createElement("div");
      col.className = "vbar-col";
      series.forEach(s => {
        const v = d[s.key] || 0;
        const bar = document.createElement("div");
        bar.className = "vbar";
        bar.style.background = s.color;
        bar.style.height = `${Math.max(2, (v / max) * 100)}%`;
        if(v === 0) bar.style.opacity = "0.15";
        bindTip(bar, `<b>${esc(d.d)}</b><br>${esc(s.label)}: ${num(v)}${opts.unit || ""}`);
        col.appendChild(bar);
      });
      wrap.appendChild(col);
    });
    el.innerHTML = "";
    el.appendChild(wrap);
    const axis = document.createElement("div");
    axis.style.cssText = "display:flex;justify-content:space-between;font-size:9px;color:var(--color-secondary);margin-top:4px;";
    axis.innerHTML = `<span>${esc(daily[0].d)}</span><span>${esc(daily[daily.length-1].d)}</span>`;
    el.appendChild(axis);
    el.insertAdjacentHTML("beforeend",
      tableView(["날짜", ...series.map(s => s.label)], daily.map(d => [d.d, ...series.map(s => num(d[s.key] || 0))])));
  }

  // 가로 바 (분포)
  function renderHBars(el, items, opts = {}){
    if(!el) return;
    if(!items.length){ el.innerHTML = `<div class="empty-note">아직 집계할 데이터가 없습니다</div>`; return; }
    const max = Math.max(1, ...items.map(i => i.value));
    el.innerHTML = "";
    items.forEach(i => {
      const row = document.createElement("div");
      row.className = "hbar-row";
      row.innerHTML = `<div class="hbar-label" title="${esc(i.label)}">${esc(i.label)}</div>
        <div class="hbar-track"><div class="hbar" style="width:${Math.max(2,(i.value/max)*100)}%;background:${i.color}"></div>
        <span class="hbar-value">${num(i.value)}${opts.unit || ""}</span></div>`;
      bindTip(row.querySelector(".hbar"), `<b>${esc(i.label)}</b><br>${num(i.value)}${opts.unit || ""}${i.sub ? `<br>${esc(i.sub)}` : ""}`);
      el.appendChild(row);
    });
    el.insertAdjacentHTML("beforeend",
      tableView([opts.nameHeader || "항목", opts.valueHeader || "값"], items.map(i => [i.label, num(i.value)])));
  }

  /* ── 데이터 로드 & 렌더 ──────────────────────────── */
  async function loadEval(days){
    let data;
    try {
      const r = await fetch(`/api/eval/summary?days=${days}`, { credentials: "include" });
      data = await r.json();
    } catch(e){
      $("statTiles").innerHTML = `<div class="empty-note" style="grid-column:1/-1">평가 데이터를 불러오지 못했습니다</div>`;
      return;
    }

    const t = data.totals || {}, q = data.quality || {}, s = data.search || {};
    const fbTotal = (t.fb_up || 0) + (t.fb_down || 0);
    const satisfaction = fbTotal ? t.fb_up / fbTotal : null;

    $("statTiles").innerHTML = [
      statTile("질문 수", num(t.questions), `${num(t.sessions)}개 세션 · ${data.days}일`),
      statTile("사용자 만족도", pct(satisfaction), fbTotal ? `👍 ${num(t.fb_up)} / 👎 ${num(t.fb_down)}` : "피드백 없음"),
      statTile("근거 충족도", pct(q.groundedness), q.claims_rows ? `검증된 턴 ${num(q.claims_rows)}건 평균` : "검증 데이터 없음"),
      statTile("수치 검증 통과율", pct(q.numeric_ok_rate), q.numeric_rows ? `DB 답변 ${num(q.numeric_rows)}건` : "DB 답변 없음"),
      statTile("근거 게이트 발동", num(q.gate_count), "근거 부족으로 답변 중단"),
      statTile("검색 0건 비율", pct(s.zero_hit_rate), s.logs ? `검색 ${num(s.logs)}회 · 용어 평균 ${s.avg_terms ?? "—"}개` : "검색 로그 없음"),
    ].join("");

    const daily = data.daily || [];
    renderVBars($("chartDaily"), daily, [{ key: "questions", color: "var(--chart-1)", label: "질문 수" }]);
    renderVBars($("chartFeedback"), daily, [
      { key: "up",   color: "var(--status-good)", label: "👍 좋아요" },
      { key: "down", color: "var(--status-bad)",  label: "👎 별로예요" },
    ]);
    renderVBars($("chartGrounded"),
      daily.map(d => ({ d: d.d, g: d.grounded === null || d.grounded === undefined ? 0 : Math.round(d.grounded * 100) })),
      [{ key: "g", color: "var(--chart-1)", label: "근거 충족도" }], { unit: "%" });

    renderHBars($("chartIntents"), (data.intents || []).map(i => {
      const meta = INTENT_META[i.intent] || { label: i.intent, color: "var(--chart-4)" };
      return { label: meta.label, value: i.cnt, color: meta.color };
    }), { nameHeader: "분석 방식", valueHeader: "대화 수", unit: "건" });
  }

  async function loadKg(){
    let data;
    try {
      const r = await fetch(`/api/kg/stats`, { credentials: "include" });
      data = await r.json();
    } catch(e){
      $("kgTiles").innerHTML = `<div class="empty-note" style="grid-column:1/-1">KG 상태를 불러오지 못했습니다</div>`;
      return;
    }

    const b = data.built || {}, e = data.edges || {}, c = data.coverage || {};
    $("kgTiles").innerHTML = [
      statTile("마지막 빌드", b.last_built_at ? esc(String(b.last_built_at).slice(0, 16)) : "미빌드", "24h 주기 자동 갱신"),
      statTile("색인 문서", num(b.docs_indexed), "아카이브 문서"),
      statTile("색인 DB 행", num(b.reports_indexed), "v_ai_defect_search"),
      statTile("문서↔보고서 연결", num(e.doc_report), `커버리지 ${pct(c.docs_linked_report_pct)}`),
      statTile("문서↔용어 연결", num(e.doc_term), `커버리지 ${pct(c.docs_with_terms_pct)}`),
      statTile("용어 동시출현 엣지", num(e.term_edge), `보고서↔용어 ${num(e.report_term)}건`),
    ].join("");

    renderHBars($("chartKgSources"), (data.sources || []).map(s => {
      const meta = KG_SOURCE_META[s.source] || { label: s.source, color: "var(--chart-4)" };
      return { label: meta.label, value: s.cnt, color: meta.color };
    }), { nameHeader: "연결 방식", valueHeader: "엣지 수", unit: "건" });

    renderHBars($("chartKgTerms"), (data.top_terms || []).map(tm => ({
      label: tm.canonical_name || `#${tm.term_id}`,
      value: tm.docs,
      color: "var(--chart-2)",
      sub: `유형: ${tm.term_type || "-"}`,
    })), { nameHeader: "용어", valueHeader: "문서 수", unit: "건" });
  }

  /* ── init ────────────────────────────────────────── */
  document.addEventListener("DOMContentLoaded", () => {
    let days = 30;
    document.querySelectorAll(".range-chip").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".range-chip").forEach(x => x.classList.remove("is-active"));
        btn.classList.add("is-active");
        days = parseInt(btn.dataset.days, 10) || 30;
        loadEval(days);
      });
    });
    loadEval(days);
    loadKg();
  });
})();
