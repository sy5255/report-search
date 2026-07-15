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
    "REPORT_ANALYSIS": { label: "보고서 심층분석", color: "var(--chart-5)" },
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
      statTile("근거 게이트 발동", num(q.gate_count), "근거 부족으로 답변 중단"),
      statTile("문서검색 0건 비율", pct(s.zero_hit_rate), s.rag_turns ? `문서검색 시도 ${num(s.rag_turns)}턴 기준 · 용어 평균 ${s.avg_terms ?? "—"}개` : "문서검색 턴 없음"),
      // 보조 지표: DB 답변에서 결과·근거에 없는 숫자가 등장하지 않은 비율(수치 환각 차단). 대표 품질 지표는 아님.
      statTile("DB 수치 환각 차단율", pct(q.numeric_ok_rate), q.numeric_rows ? `보조 지표 · DB 답변 ${num(q.numeric_rows)}건` : "DB 답변 없음"),
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

    renderHBars($("chartIntents"), (data.intents || []).filter(i => i.intent !== "REPORT_ANALYSIS").map(i => {
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
      statTile("마지막 빌드", b.last_built_at ? esc(String(b.last_built_at).slice(2, 16)) : "미빌드", "24h 주기 자동 갱신"),
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

    renderHBars($("chartKgTerms"), (data.top_terms || []).slice(0, 8).map(tm => ({
      label: tm.canonical_name || `#${tm.term_id}`,
      value: tm.docs,
      color: "var(--chart-2)",
      sub: `유형: ${tm.term_type || "-"}`,
    })), { nameHeader: "용어", valueHeader: "문서 수", unit: "건" });

    // 그래프 탐색기 초기화 (상위 용어 칩 + 1위 용어 자동 선택)
    setupExplorer(data.top_terms || []);
  }

  /* ── 골든셋 평가 ─────────────────────────────────── */
  const GS_PALETTE = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)"];
  let gsRuns = [];          // 최신순
  let gsLabelColor = {};    // label → color (고정 순서)
  let gsSelectedRunId = null;

  function gsColorForLabel(label){
    const key = label || "(no label)";
    if(!(key in gsLabelColor)){
      gsLabelColor[key] = GS_PALETTE[Object.keys(gsLabelColor).length % GS_PALETTE.length];
    }
    return gsLabelColor[key];
  }

  async function loadGoldenset(){
    let data;
    try {
      const r = await fetch("/api/eval/goldenset/runs?limit=60", { credentials: "include" });
      data = await r.json();
    } catch(e){
      $("gsTiles").innerHTML = `<div class="empty-note" style="grid-column:1/-1">골든셋 이력을 불러오지 못했습니다</div>`;
      return;
    }
    gsRuns = data.runs || [];
    if(!gsRuns.length){
      $("gsTiles").innerHTML = `<div class="empty-note" style="grid-column:1/-1">아직 평가 실행이 없습니다. 서버에서 <span class="mono">python -m app.goldenset_runner</span> 를 실행하세요.</div>`;
      ["gsTrend","gsRuns","gsTable"].forEach(id => { const el = $(id); if(el) el.innerHTML = ""; });
      const lg = $("gsTrendLegend"); if(lg) lg.innerHTML = "";
      return;
    }
    // 라벨 색 고정 순서 배정(오래된 순서 기준으로 안정적)
    gsLabelColor = {};
    [...gsRuns].reverse().forEach(r => gsColorForLabel(r.label || ""));

    renderGsTrend();
    renderGsRunList();
    // 기본 선택: 최신 run
    selectRun(gsRuns[0].run_id);
  }

  function renderGsTrend(){
    const box = $("gsTrend");
    if(!box) return;
    const chron = [...gsRuns].reverse(); // 오래된→최신
    const H = 150, barMax = Math.max(0.001, ...chron.map(r => r.hit_at_5 || 0));

    // 골든셋 변경 지점(직전과 hash 다름)
    const changed = chron.map((r, i) => i > 0 && r.goldenset_hash && r.goldenset_hash !== chron[i-1].goldenset_hash);

    const wrap = document.createElement("div");
    wrap.className = "vbar-chart";
    wrap.style.height = H + "px";
    wrap.style.position = "relative";
    chron.forEach((r, i) => {
      const col = document.createElement("div");
      col.className = "vbar-col";
      col.style.position = "relative";
      col.style.cursor = "pointer";
      const h5 = r.hit_at_5 || 0;
      const bar = document.createElement("div");
      bar.className = "vbar";
      bar.style.background = gsColorForLabel(r.label || "");
      bar.style.height = `${Math.max(2, (h5 / barMax) * 100)}%`;
      if(r.run_id === gsSelectedRunId) bar.style.outline = "2px solid var(--color-on-surface)";
      col.appendChild(bar);
      // MRR 점 (0~1 → 높이비율)
      if(r.mrr != null){
        const dot = document.createElement("div");
        dot.style.cssText = `position:absolute;left:50%;transform:translate(-50%,50%);width:5px;height:5px;border-radius:50%;background:var(--color-on-surface);bottom:${Math.min(100,(r.mrr*100))}%;`;
        col.appendChild(dot);
      }
      // 골든셋 변경 마커
      if(changed[i]){
        const dm = document.createElement("div");
        dm.textContent = "◆";
        dm.title = "골든셋 문항이 변경된 지점";
        dm.style.cssText = "position:absolute;top:-14px;left:50%;transform:translateX(-50%);font-size:9px;color:var(--chart-3);";
        col.appendChild(dm);
      }
      bindTip(col, `<b>${esc(String(r.created_at).slice(0,16))}</b><br>라벨: ${esc(r.label||"—")} · 인덱스: ${esc(r.index_name||"—")}<br>hit@5 ${pct(r.hit_at_5)} · MRR ${r.mrr ?? "—"} · 문항 ${num(r.total)}${changed[i] ? "<br>◆ 골든셋 변경됨" : ""}`);
      col.addEventListener("click", () => selectRun(r.run_id));
      wrap.appendChild(col);
    });
    box.innerHTML = "";
    box.appendChild(wrap);

    // 범례: 라벨별 색 + MRR점 + 변경마커
    const lg = $("gsTrendLegend");
    if(lg){
      const labelDots = Object.keys(gsLabelColor).map(k =>
        `<span><span class="dot" style="background:${gsColorForLabel(k)}"></span>${esc(k)}</span>`).join("");
      lg.innerHTML = `${labelDots}<span><span class="dot" style="background:var(--color-on-surface)"></span>MRR(점)</span><span style="color:var(--chart-3)">◆ 골든셋 변경</span>`;
    }
  }

  function renderGsRunList(){
    const box = $("gsRuns");
    if(!box) return;
    const admin = (typeof window !== "undefined" && window.__IS_ADMIN__ === true);
    const rows = gsRuns.map(r => `
      <tr data-run="${esc(r.run_id)}" class="gs-run-row ${r.run_id === gsSelectedRunId ? "is-sel" : ""}">
        <td>${esc(String(r.created_at).slice(0,16))}</td>
        <td><span class="gs-label-chip" style="border-color:${gsColorForLabel(r.label||"")};color:${gsColorForLabel(r.label||"")}">${esc(r.label||"—")}</span></td>
        <td class="mono" style="font-size:10px">${esc(r.index_name||"—")}</td>
        <td>${num(r.total)}</td>
        <td>${pct(r.hit_at_5)}</td>
        <td>${r.mrr ?? "—"}</td>
        <td>${pct(r.intent_accuracy)}</td>
        <td>${admin ? `<button class="gs-del" data-run="${esc(r.run_id)}" title="이 실행 삭제">🗑</button>` : ""}</td>
      </tr>`).join("");
    box.innerHTML = `<div class="gs-table"><table>
      <thead><tr><th>실행 시각</th><th>라벨</th><th>인덱스</th><th>문항</th><th>hit@5</th><th>MRR</th><th>인텐트</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;

    box.querySelectorAll(".gs-run-row").forEach(tr => {
      tr.addEventListener("click", (e) => {
        if(e.target.closest(".gs-del")) return;
        selectRun(tr.getAttribute("data-run"));
      });
    });
    box.querySelectorAll(".gs-del").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const id = btn.getAttribute("data-run");
        const ok = window.Toast
          ? await window.Toast.confirm("이 평가 실행을 삭제할까요? 되돌릴 수 없습니다.", { okText: "삭제", destructive: true })
          : confirm("이 평가 실행을 삭제할까요?");
        if(!ok) return;
        try {
          const r = await fetch(`/api/eval/goldenset/runs/${encodeURIComponent(id)}`, { method: "DELETE", credentials: "include" });
          if(!r.ok) throw new Error(String(r.status));
          if(window.Toast) window.Toast.success("삭제했습니다.");
          if(gsSelectedRunId === id) gsSelectedRunId = null;
          loadGoldenset();
        } catch(err){
          if(window.Toast) window.Toast.error("삭제에 실패했습니다. (관리자만 삭제 가능)");
        }
      });
    });
  }

  function renderGsTiles(L){
    $("gsTiles").innerHTML = [
      statTile("문항 수", num(L.total), `${esc(L.label||"—")} · ${esc(String(L.created_at).slice(0,16))}`),
      statTile("검색 hit@5", pct(L.hit_at_5), L.scored_retrieval ? `채점 ${num(L.scored_retrieval)}문항` : "정답 문서 미지정"),
      statTile("검색 MRR", L.mrr ?? "—", "정답 문서 평균 역순위"),
      statTile("검색 hit@1 / @10", `${pct(L.hit_at_1)} / ${pct(L.hit_at_10)}`, "1위 / 10위 내 적중"),
      statTile("인텐트 정확도", pct(L.intent_accuracy), L.scored_intent ? `채점 ${num(L.scored_intent)}문항` : "기대 인텐트 미지정"),
      statTile("용어 감지율", pct(L.term_detect_rate), L.scored_terms ? `채점 ${num(L.scored_terms)}문항` : "기대 용어 미지정"),
    ].join("");
  }

  function renderGsItems(items){
    const box = $("gsTable");
    if(!box) return;
    if(!items || !items.length){ box.innerHTML = `<div class="empty-note">문항 상세가 없습니다</div>`; return; }
    const mark = (ok, okText, badText) => ok ? `<span class="gs-mark ok">${okText}</span>` : `<span class="gs-mark bad">${badText}</span>`;
    const rows = items.map(it => {
      const retCell = it.scored_retrieval
        ? (it.found_rank ? mark(it.hit5, `rank ${it.found_rank}`, `rank ${it.found_rank}`) : `<span class="gs-mark bad">MISS</span>`)
        : `<span class="gs-mark na">—</span>`;
      const intCell = it.scored_intent
        ? mark(it.intent_ok, "정답", `${esc(it.router_intent||"?")}`)
        : `<span class="gs-mark na">—</span>`;
      const termCell = it.scored_terms ? pct(it.term_rate) : "—";
      return `<tr>
        <td class="gs-q">${esc(it.question||"")}</td>
        <td>${retCell}</td>
        <td>${it.scored_intent ? `<span style="color:var(--color-secondary)">${esc(it.expected_intent||"")}</span> → ${intCell}` : intCell}</td>
        <td>${termCell}</td>
        <td style="color:var(--color-secondary)">${esc((it.detected||[]).join(", "))}</td>
      </tr>`;
    }).join("");
    box.innerHTML = `<div class="gs-table"><table>
      <thead><tr><th>질문</th><th>검색(found-rank)</th><th>인텐트(기대→실측)</th><th>용어</th><th>감지된 용어</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  async function selectRun(runId){
    gsSelectedRunId = runId;
    document.querySelectorAll(".gs-run-row").forEach(tr => tr.classList.toggle("is-sel", tr.getAttribute("data-run") === runId));
    renderGsTrend();
    const meta = gsRuns.find(r => r.run_id === runId);
    if(meta) renderGsTiles(meta);
    const lbl = $("gsTableRunLabel");
    if(lbl && meta) lbl.textContent = `· ${String(meta.created_at).slice(0,16)} (${meta.label || "라벨 없음"})`;
    const box = $("gsTable");
    if(box) box.innerHTML = `<div class="empty-note">불러오는 중...</div>`;
    try {
      const r = await fetch(`/api/eval/goldenset/runs/${encodeURIComponent(runId)}`, { credentials: "include" });
      const detail = await r.json();
      renderGsItems(detail.items || []);
    } catch(e){
      if(box) box.innerHTML = `<div class="empty-note">문항 상세를 불러오지 못했습니다</div>`;
    }
  }

  /* ── KG 그래프 탐색기 ─────────────────────────────── */
  const SVG_NS = "http://www.w3.org/2000/svg";
  const TYPE_COLORS = { defect: "var(--chart-1)", chemistry: "var(--chart-2)", process: "var(--chart-3)", node: "var(--chart-4)" };
  const typeColor = (t) => TYPE_COLORS[t] || "var(--color-secondary)";
  let allTerms = null;
  let currentTermId = null;

  async function ensureTerms(){
    if(allTerms) return allTerms;
    try {
      const r = await fetch("/api/dictionary/terms", { credentials: "include" });
      const d = await r.json();
      allTerms = d.terms || [];
    } catch(e){ allTerms = []; }
    return allTerms;
  }

  function svgEl(tag, attrs){
    const el = document.createElementNS(SVG_NS, tag);
    for(const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  // 그래프 탐색기 상태: 마지막 네트워크 데이터 + 유형 필터
  let lastNetwork = null;          // { center, nodes, edges }
  let activeTypes = null;          // null = 전체 표시, Set = 표시할 유형

  function _typeKey(t){ return TYPE_COLORS[t] ? t : "기타"; }

  function renderTypeFilter(nodes){
    const host = $("kgTypeFilter");
    if(!host) return;
    const present = [];
    const seen = new Set();
    (nodes || []).forEach(n => { const k = _typeKey(n.term_type); if(!seen.has(k)){ seen.add(k); present.push(k); } });
    if(!present.length){ host.innerHTML = ""; return; }
    const nameOf = { defect:"불량", chemistry:"성분", process:"공정", node:"노드", "기타":"기타" };
    host.innerHTML = present.map(k => {
      const on = !activeTypes || activeTypes.has(k);
      const col = k === "기타" ? "var(--color-secondary)" : TYPE_COLORS[k];
      return `<button class="kg-type-chip ${on?"is-on":""}" data-type="${esc(k)}" type="button">
        <span class="dot" style="background:${col}"></span>${esc(nameOf[k]||k)}</button>`;
    }).join("");
    host.querySelectorAll(".kg-type-chip").forEach(btn => {
      btn.addEventListener("click", () => {
        const k = btn.getAttribute("data-type");
        const keys = present.slice();
        if(!activeTypes) activeTypes = new Set(keys);   // 첫 클릭: 전체에서 시작
        if(activeTypes.has(k)) activeTypes.delete(k); else activeTypes.add(k);
        if(activeTypes.size === 0 || activeTypes.size === keys.length) activeTypes = null; // 전체
        if(lastNetwork) renderNetworkGraph(lastNetwork, { keepFade: false });
      });
    });
  }

  function _visible(type){ return !activeTypes || activeTypes.has(_typeKey(type)); }

  function renderNetworkGraph(net, opts){
    const box = $("kgGraph");
    if(!box) return;
    lastNetwork = net;
    box.innerHTML = "";
    const center = net.center || {};
    const nodes = (net.nodes || []).slice().sort((a,b) => (b.weight||0) - (a.weight||0)).slice(0, 16);
    if(!nodes.length){
      box.innerHTML = `<div class="empty-note" style="padding-top:170px">'${esc(center.canonical_name||"")}'와 함께 등장하는 용어가 아직 없습니다</div>`;
      renderTypeFilter([]);
      return;
    }
    const W = 680, H = 420, CX = W/2, CY = H/2;
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "kg-net-svg", role: "img",
      "aria-label": `${center.canonical_name||""} 중심 지식그래프` });
    if(!opts || opts.keepFade !== false) svg.classList.add("kg-net-enter");

    // 2중 링 배치: 강도 상위=안쪽, 하위=바깥
    const inner = nodes.slice(0, Math.min(8, nodes.length));
    const outer = nodes.slice(8);
    const pos = { [center.term_id]: { x: CX, y: CY } };
    const place = (arr, rx, ry) => arr.forEach((n, i) => {
      const ang = (2*Math.PI*i)/Math.max(1,arr.length) - Math.PI/2 + (arr === outer ? Math.PI/arr.length : 0);
      pos[n.term_id] = { x: CX + rx*Math.cos(ang), y: CY + ry*Math.sin(ang) };
    });
    place(inner, 150, 96);
    place(outer, 285, 176);

    const wMaxNode = Math.max(1, ...nodes.map(n => n.weight||0));
    const nodeR = w => 7 + 13*Math.sqrt((w||0)/wMaxNode);
    const wMaxEdge = Math.max(1, ...(net.edges||[]).map(e => e.weight||0));
    const idType = {}; nodes.forEach(n => idType[n.term_id] = n.term_type);
    idType[center.term_id] = center.term_type;

    // ── 엣지 (노드 아래) : 중심↔이웃 = 실선 굵게 / 이웃↔이웃 = 얇고 반투명(2-hop 클러스터) ──
    (net.edges||[]).forEach(e => {
      const p1 = pos[e.a], p2 = pos[e.b];
      if(!p1 || !p2) return;
      const isCenter = (e.a === center.term_id || e.b === center.term_id);
      const dim = !(_visible(idType[e.a]) && _visible(idType[e.b]));
      // 곡선(quadratic): 중점에서 수직으로 살짝 휘게
      const mx = (p1.x+p2.x)/2, my = (p1.y+p2.y)/2;
      const dx = p2.x-p1.x, dy = p2.y-p1.y, len = Math.hypot(dx,dy)||1;
      const off = isCenter ? 0 : 18;
      const cxp = mx + (-dy/len)*off, cyp = my + (dx/len)*off;
      const path = svgEl("path", {
        class: isCenter ? "kg-edge-c" : "kg-edge-2hop",
        d: `M${p1.x.toFixed(1)},${p1.y.toFixed(1)} Q${cxp.toFixed(1)},${cyp.toFixed(1)} ${p2.x.toFixed(1)},${p2.y.toFixed(1)}`,
        fill: "none", "stroke-linecap": "round",
        "stroke-width": isCenter ? (1.4 + 4.6*((e.weight||0)/wMaxEdge)).toFixed(1) : (0.6 + 1.4*((e.weight||0)/wMaxEdge)).toFixed(1),
        opacity: dim ? 0.06 : (isCenter ? 0.55 : 0.28),
      });
      svg.appendChild(path);
    });

    // ── 이웃 노드 ──
    nodes.forEach(n => {
      const p = pos[n.term_id]; if(!p) return;
      const dim = !_visible(n.term_type);
      const r = nodeR(n.weight);
      const g = svgEl("g", { class: "kg-node", opacity: dim ? 0.18 : 1 });
      g.appendChild(svgEl("circle", { cx: p.x, cy: p.y, r: r.toFixed(1), fill: typeColor(n.term_type),
        stroke: "var(--color-surface)", "stroke-width": 1.5 }));
      const name = String(n.canonical_name || `#${n.term_id}`);
      const lbl = svgEl("text", { x: p.x, y: (p.y + (p.y>=CY ? r+13 : -r-6)).toFixed(1), "text-anchor": "middle", class: "kg-node-label" });
      lbl.textContent = name.length > 9 ? name.slice(0,9)+"…" : name;
      g.appendChild(lbl);
      bindTip(g, `<b>${esc(name)}</b> <span style="color:var(--color-secondary)">(${esc(n.term_type||"-")})</span><br>함께 등장: 문서 ${num(n.docs_count)}건 · 보고서 ${num(n.reports_count)}건 · 강도 ${num(n.weight)}<br><span style="color:var(--color-secondary)">클릭하면 이 용어 중심으로 이동</span>`);
      if(!dim) g.addEventListener("click", () => selectTerm(n.term_id, name, n.term_type));
      svg.appendChild(g);
    });

    // ── 중심 노드 ──
    const cname = center.canonical_name || `#${center.term_id}`;
    const cg = svgEl("g", { class: "kg-node kg-node-center" });
    cg.appendChild(svgEl("circle", { cx: CX, cy: CY, r: 19, fill: typeColor(center.term_type),
      stroke: "var(--color-surface)", "stroke-width": 3.5 }));
    const cl = svgEl("text", { class: "kg-center-label", x: CX, y: CY + 37, "text-anchor": "middle" });
    cl.textContent = cname;
    cg.appendChild(cl);
    bindTip(cg, `<b>${esc(cname)}</b><br>연결 문서 ${num(center.docs_count)}건 · 연결 보고서 ${num(center.reports_count)}건`);
    svg.appendChild(cg);

    box.appendChild(svg);
    renderTypeFilter(nodes);
  }

  /* ── 문서 뷰어 모달 (채팅 문서 모달과 동일한 렌더링 뷰) ── */
  function preProcessDocMd(mdText){
    let t = String(mdText || "").replace(/\r\n/g, "\n");
    // [placeholder] 제거 + 이미지 마커 이후 절단 (chat.js preProcessMarkdown과 동일 규칙)
    t = t.replace(/\[\s*placeholder\s*\]/gi, "");
    const m = t.match(/\.\/images\/\|attachments\/inline|<img\s+src=/i);
    if(m) t = t.substring(0, m.index);
    // 선두 [MAIL_META] 블록 제거 (chat.js stripLeadingMailMetaBlock과 동일 규칙)
    t = t.replace(/^\s*```[^\n]*\n([\s\S]*?)\n```[\t ]*\n*/i,
      (full, inner) => /\[MAIL_META\]/i.test(String(inner || "")) ? "" : full);
    t = t.replace(/\[MAIL_META\][\s\S]*?(?=\n\s*\n|$)/i, "");
    return t.trimStart();
  }

  // chat.js injectImagesIntoMarkdown 이식: [Image_position] 마커를 첨부 이미지로 순차 치환
  // (assets 없으면 마커 제거). Knowledge Base의 문서 모달과 동일하게 이미지를 함께 보여준다.
  function injectImagesIntoMarkdown(mdText, assets){
    if(!mdText) return mdText || "";
    const imgs = (assets || []).map(a => (a && a.path) ? a : null).filter(Boolean);
    if(!imgs.length) return mdText.replace(/\[Image_position\]/gi, "");
    let i = 0;
    return mdText.replace(/\[Image_position\]/gi, () => {
      if(i >= imgs.length) return "";
      const a = imgs[i++];
      const url = `/api/view/asset?rel=${encodeURIComponent(a.path)}`;
      const alt = esc((a.file_name || a.path || "image").replace(/[\r\n]+/g, " "));
      return `\n<div class="md-embed-img-wrap"><img class="md-embed-img" src="${url}" alt="${alt}" loading="lazy" onclick="window.open('${url}','_blank')"></div>\n`;
    });
  }

  async function openDocViewer(title, rel, assets){
    const modal = $("kgDocModal"), body = $("kgDocBody"), head = $("kgDocTitle");
    if(!modal || !body) return;
    head.textContent = title || "(제목 없음)";
    body.innerHTML = `<div class="empty-note">불러오는 중...</div>`;
    modal.style.display = "flex";
    modal.setAttribute("aria-hidden", "false");
    try {
      const raw = await fetch(`/api/view/md?rel=${encodeURIComponent(rel)}`, { credentials: "include" }).then(r => r.text());
      const md = injectImagesIntoMarkdown(preProcessDocMd(raw), assets);
      body.innerHTML = (typeof marked !== "undefined")
        ? marked.parse(md)
        : `<pre style="white-space:pre-wrap">${esc(md)}</pre>`;
      body.scrollTop = 0;
    } catch(e){
      body.innerHTML = `<div class="empty-note">문서를 불러오지 못했습니다</div>`;
    }
  }

  function closeDocViewer(){
    const modal = $("kgDocModal");
    if(!modal) return;
    modal.style.display = "none";
    modal.setAttribute("aria-hidden", "true");
  }

  function setupDocViewer(){
    const close = $("kgDocClose"), backdrop = $("kgDocBackdrop");
    if(close) close.addEventListener("click", closeDocViewer);
    if(backdrop) backdrop.addEventListener("click", closeDocViewer);
    document.addEventListener("keydown", (e) => { if(e.key === "Escape") closeDocViewer(); });
  }

  function renderTermSide(center, data){
    const side = $("kgSide");
    if(!side) return;
    const docs = data.top_docs || [];
    const reports = data.top_reports || [];
    const useReportFallback = !docs.length && reports.length;

    side.innerHTML = `
      <div class="kg-side-head">
        <span class="kg-side-title">${esc(center.name)}</span>
        <span class="kg-badge">유형: ${esc(center.type || "-")}</span>
        <span class="kg-badge">연결 문서 ${num(data.docs_count)}건</span>
        <span class="kg-badge">연결 보고서 ${num(data.reports_count)}건</span>
      </div>
      <div class="chart-desc" style="margin:0">${
        useReportFallback
          ? "본문에서 매칭된 문서는 없지만, DB에 기록된 분석 건(보고서)이 있습니다"
          : "이 용어가 가장 많이 언급된 문서 (클릭 시 원문 열기)"
      }</div>
      <div class="kg-doc-list">
        ${(docs.length || reports.length) ? "" : `<div class="empty-note">연결된 문서/보고서가 없습니다</div>`}
      </div>`;
    const list = side.querySelector(".kg-doc-list");

    docs.forEach(d => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "kg-doc-item";
      item.innerHTML = `<span class="t">${esc(d.title || d.doc_id)}</span>
        <span class="m">${esc(d.mail_date || "")}${d.freq ? ` · 언급 ${num(d.freq)}회` : ""}</span>`;
      item.addEventListener("click", () => {
        const rel = (((d.additionalField || {}).storage) || {}).parsed_md_rel_path;
        if(rel) openDocViewer(d.title || d.doc_id, rel, ((d.additionalField || {}).assets) || []);
      });
      list.appendChild(item);
    });

    if(useReportFallback){
      reports.forEach(r => {
        const item = document.createElement("div");
        item.className = "kg-doc-item";
        item.style.cursor = "default";
        item.innerHTML = `<span class="t">📊 보고서 #${esc(r.report_index)}${r.defect ? ` · ${esc(r.defect)}` : ""}</span>
          <span class="m">${esc(String(r.date || "").slice(0, 10))}${r.src_cols ? ` · 연결 컬럼: ${esc(r.src_cols)}` : ""}</span>`;
        list.appendChild(item);
      });
    }
  }

  /* ── 연결 상세 드릴다운 (문서↔보고서, evidence 포함) ── */
  let linksSource = "", linksLoaded = false;

  async function loadLinks(){
    const box = $("kgLinksTable");
    if(!box) return;
    box.innerHTML = `<div class="empty-note">불러오는 중...</div>`;
    const q = ($("kgLinksSearch") && $("kgLinksSearch").value.trim()) || "";
    let data;
    try {
      const r = await fetch(`/api/kg/links?source=${encodeURIComponent(linksSource)}&q=${encodeURIComponent(q)}&limit=100`, { credentials: "include" });
      data = await r.json();
    } catch(e){
      box.innerHTML = `<div class="empty-note">연결 상세를 불러오지 못했습니다</div>`;
      return;
    }
    const links = data.links || [];
    if(!links.length){
      box.innerHTML = `<div class="empty-note">조건에 맞는 연결이 없습니다</div>`;
      return;
    }
    const rows = links.map((l, i) => `
      <tr>
        <td><span class="doc-link" data-rel="${esc(l.rel_path)}" data-idx="${i}" title="${esc(l.title)}">${esc(l.title)}</span>
            <span style="font-size:9px;color:var(--color-secondary)">${esc(l.mail_date || "")}</span></td>
        <td>#${esc(l.report_index)}</td>
        <td><span class="kg-src-badge ${esc(l.source)}">${esc(l.source)}</span></td>
        <td><span class="kg-evidence">${esc(l.evidence || "—")}</span></td>
        <td>${(l.confidence ?? 0).toFixed(2)}</td>
      </tr>`).join("");
    box.innerHTML = `<table>
      <thead><tr><th>문서 (클릭 시 원문)</th><th>보고서</th><th>방식</th><th>매칭 근거</th><th>conf</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
    box.querySelectorAll(".doc-link").forEach(a => {
      a.addEventListener("click", () => {
        const rel = a.getAttribute("data-rel");
        const l = links[parseInt(a.getAttribute("data-idx"), 10)] || {};
        if(rel) openDocViewer(a.getAttribute("title") || "", rel, l.assets || []);
      });
    });
  }

  function setupLinksPanel(){
    const toggle = $("kgLinksToggle"), panel = $("kgLinksPanel");
    if(!toggle || !panel) return;
    toggle.addEventListener("click", () => {
      const open = panel.style.display !== "none";
      panel.style.display = open ? "none" : "block";
      if(!open && !linksLoaded){ linksLoaded = true; loadLinks(); }
    });
    panel.querySelectorAll(".kg-chip[data-src]").forEach(chip => {
      chip.addEventListener("click", () => {
        panel.querySelectorAll(".kg-chip[data-src]").forEach(c => c.classList.remove("is-active"));
        chip.classList.add("is-active");
        linksSource = chip.dataset.src || "";
        loadLinks();
      });
    });
    const search = $("kgLinksSearch");
    if(search){
      let t = null;
      search.addEventListener("input", () => { clearTimeout(t); t = setTimeout(loadLinks, 350); });
    }
  }

  async function selectTerm(termId, name, type){
    currentTermId = termId;
    activeTypes = null;  // 새 중심으로 이동 시 필터 리셋
    document.querySelectorAll(".kg-chip").forEach(c => c.classList.toggle("is-active", c.dataset.termId == String(termId)));
    const box = $("kgGraph");
    if(box) box.innerHTML = `<div class="empty-note" style="padding-top:170px">불러오는 중...</div>`;
    let net, ov;
    try {
      // 2-hop 네트워크(그래프) + 용어 개요(사이드 패널) 병렬 조회
      const [rn, ro] = await Promise.all([
        fetch(`/api/kg/network?term_id=${termId}`, { credentials: "include" }),
        fetch(`/api/kg/term/${termId}`, { credentials: "include" }),
      ]);
      net = await rn.json();
      ov = await ro.json();
    } catch(e){
      if(box) box.innerHTML = `<div class="empty-note" style="padding-top:170px">용어 정보를 불러오지 못했습니다</div>`;
      return;
    }
    // 네트워크 center에 이름/유형 보강(백엔드는 canonical_name을 채우지 않음)
    net.center = Object.assign({ term_id: termId, canonical_name: name || `#${termId}`, term_type: type || "",
      docs_count: (ov && ov.docs_count) || 0, reports_count: (ov && ov.reports_count) || 0 }, net.center || {},
      { canonical_name: name || `#${termId}`, term_type: type || "" });
    renderNetworkGraph(net, { keepFade: true });
    renderTermSide({ name: name || `#${termId}`, type: type || "" }, ov || {});
  }

  function setupExplorer(topTerms){
    const chips = $("kgTopChips");
    if(chips){
      chips.innerHTML = "";
      (topTerms || []).forEach(t => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "kg-chip";
        b.dataset.termId = String(t.term_id);
        b.title = `유형: ${t.term_type || "-"}`;
        b.innerHTML = `<span class="kg-chip-dot" style="background:${typeColor(t.term_type)}"></span>${esc(t.canonical_name || `#${t.term_id}`)}`;
        b.addEventListener("click", () => selectTerm(t.term_id, t.canonical_name, t.term_type));
        chips.appendChild(b);
      });
    }

    const input = $("kgTermSearch");
    const sug = $("kgTermSuggest");
    if(input && sug){
      const hide = () => { sug.style.display = "none"; };
      input.addEventListener("input", async () => {
        const q = input.value.trim().toLowerCase();
        if(!q){ hide(); return; }
        const terms = await ensureTerms();
        const hits = terms.filter(t =>
          String(t.canonical_name || "").toLowerCase().includes(q) ||
          String(t.aliases || "").toLowerCase().includes(q)
        ).slice(0, 8);
        if(!hits.length){ hide(); return; }
        sug.innerHTML = "";
        hits.forEach(t => {
          const row = document.createElement("div");
          row.className = "kg-suggest-item";
          row.innerHTML = `<span>${esc(t.canonical_name)}</span><span class="tt">${esc(t.term_type || "")}</span>`;
          row.addEventListener("mousedown", () => {
            input.value = t.canonical_name;
            hide();
            selectTerm(t.term_id, t.canonical_name, t.term_type);
          });
          sug.appendChild(row);
        });
        sug.style.display = "block";
      });
      input.addEventListener("blur", () => setTimeout(hide, 150));
      input.addEventListener("keydown", (e) => {
        if(e.key === "Enter"){
          const first = sug.querySelector(".kg-suggest-item");
          if(first) first.dispatchEvent(new MouseEvent("mousedown"));
        }
        if(e.key === "Escape") hide();
      });
    }

    // 초기 선택: 최다 매칭 용어 1위
    if(topTerms && topTerms.length){
      selectTerm(topTerms[0].term_id, topTerms[0].canonical_name, topTerms[0].term_type);
    }
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
    setupDocViewer();
    setupLinksPanel();
    loadEval(days);
    loadGoldenset();
    loadKg();
    if(window.__IS_ADMIN__ === true) loadFeedbackCases();
  });

  /* ── 실패 사례 (👎 피드백, 관리자 전용) ───────────────── */
  async function loadFeedbackCases(){
    const box = $("fbCases");
    if(!box) return;
    let cases = [];
    try {
      const r = await fetch("/api/eval/feedback-cases?limit=50", { credentials: "include" });
      if(!r.ok) throw new Error(String(r.status));
      cases = (await r.json()).cases || [];
    } catch(e){
      box.innerHTML = `<div class="empty-note">실패 사례를 불러오지 못했습니다.</div>`;
      return;
    }
    if(!cases.length){
      box.innerHTML = `<div class="empty-note">👎 피드백이 아직 없습니다. 채팅 답변의 엄지 아래 버튼으로 수집됩니다.</div>`;
      return;
    }

    const intentLabel = (it) => (INTENT_META[it] ? INTENT_META[it].label : (it || "—"));
    const rows = cases.map((c, i) => `
      <tr class="fb-case-row" data-i="${i}">
        <td style="white-space:nowrap">${esc(String(c.created_at).slice(0,16))}</td>
        <td>${esc(intentLabel(c.intent))}</td>
        <td style="max-width:340px">${esc((c.question||"").slice(0,120))}</td>
        <td style="max-width:220px;color:var(--color-secondary)">${esc((c.comment||"").slice(0,80) || "—")}</td>
        <td><button class="fb-promote" data-i="${i}" title="goldenset.json에 비활성 문항으로 추가">골든셋 후보</button></td>
      </tr>
      <tr class="fb-case-answer" data-for="${i}" style="display:none">
        <td colspan="5"><div class="fb-answer-body">${esc((c.answer||"(답변 없음)").slice(0,2000))}</div></td>
      </tr>`).join("");

    box.innerHTML = `<div class="gs-table"><table>
      <thead><tr><th>시각</th><th>인텐트</th><th>질문</th><th>👎 코멘트</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;

    box.querySelectorAll(".fb-case-row").forEach(tr => {
      tr.addEventListener("click", (e) => {
        if(e.target.closest(".fb-promote")) return;
        const ans = box.querySelector(`.fb-case-answer[data-for="${tr.dataset.i}"]`);
        if(ans) ans.style.display = (ans.style.display === "none") ? "" : "none";
      });
    });
    box.querySelectorAll(".fb-promote").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const c = cases[parseInt(btn.dataset.i, 10)];
        if(!c || !c.question) return;
        btn.disabled = true;
        try {
          const r = await fetch("/api/eval/goldenset/candidates", {
            method: "POST", credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: c.question, expected_intent: c.intent || "", note: c.comment || "" }),
          });
          const res = await r.json();
          if(!r.ok) throw new Error(res.detail || String(r.status));
          if(res.duplicated){
            if(window.Toast) window.Toast.info ? window.Toast.info(res.message) : window.Toast.success(res.message);
          } else {
            if(window.Toast) window.Toast.success("골든셋 후보로 추가했습니다. goldenset.json에서 정답 문서를 채워 활성화하세요.");
            btn.textContent = "추가됨 ✓";
          }
        } catch(err){
          btn.disabled = false;
          if(window.Toast) window.Toast.error("추가에 실패했습니다.");
        }
      });
    });
  }
})();
