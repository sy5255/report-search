let currentSessionId = null;

// 현재 선택된 “assistant msg_id” 기준 evidence
let currentEvidenceAssistantMsgId = null;

// 최근 로드된 evidence 데이터 (topdocs/citations)
let lastTopDocs = [];
let lastCitations = null;
let activeStreamController = null;
let isSending = false;
let forcedMode = "";                 // "", "[DB_ANALYSIS]", "[RAG_KNOWLEDGE]"
let selectedIndexNames = null;       // null = default, otherwise array of index names

// UI에서 보여줄 topdocs 개수
let topDocsShowN = 5;

// 유저의 마지막 질문을 저장
let lastRealUserQuery = "";

function el(id){ return document.getElementById(id); }

// 문서 검색 결과 유무에 따라 우측 패널을 열고 닫는 함수
let evidenceHasDocs = false;
let evidenceCollapsed = false;  // 기본값 펼침. 사용자가 현재 답변에서 접었을 때만 true.

function _applyEvidenceState(){
  const rightPanel = el("rightPanel");
  const resizer2 = el("resizer2");
  const rail = el("evidenceRail");
  if(!rightPanel) return;

  const open = evidenceHasDocs && !evidenceCollapsed;
  const railOnly = evidenceHasDocs && evidenceCollapsed;

  rightPanel.classList.toggle("hidden", !open);
  if(resizer2) resizer2.classList.toggle("hidden", !open);
  if(rail){
    rail.classList.toggle("hidden", !railOnly);
    const cnt = el("evidenceRailCount");
    if(cnt) cnt.textContent = (Array.isArray(lastTopDocs) && lastTopDocs.length) ? `근거 ${lastTopDocs.length}` : "근거";
  }
}

// hasDocs=true 인 새 답변마다 패널을 '펼침'으로 리셋(기본값 펼침).
function toggleEvidencePanel(hasDocs){
  evidenceHasDocs = !!hasDocs;
  if(hasDocs) evidenceCollapsed = false;
  _applyEvidenceState();
}

function collapseEvidencePanel(){
  evidenceCollapsed = true;
  _applyEvidenceState();
}

function expandEvidencePanel(){
  evidenceCollapsed = false;
  _applyEvidenceState();
}

async function _apiFetch(method, url, body){
  let r;
  try {
    r = await fetch(url, {
      method,
      headers: body ? {"Content-Type":"application/json"} : undefined,
      body: body ? JSON.stringify(body) : undefined,
      credentials:"include"
    });
  } catch(networkErr){
    if(window.Toast) window.Toast.error("네트워크 연결을 확인해주세요.", { title: "연결 실패" });
    throw networkErr;
  }
  if(r.status === 401){
    if(window.Toast) window.Toast.warn("로그인이 만료되었습니다. 다시 로그인합니다.");
    setTimeout(() => { window.location.href = "/"; }, 800);
    throw new Error("Unauthorized");
  }
  if(r.status === 403){
    if(window.Toast) window.Toast.error("권한이 없습니다.");
    throw new Error("Forbidden");
  }
  if(!r.ok){
    let detail = "";
    try { detail = await r.text(); } catch(_) {}
    if(r.status >= 500 && window.Toast){
      window.Toast.error(detail.slice(0, 200) || "서버 오류가 발생했습니다.", { title: `오류 ${r.status}` });
    }
    throw new Error(detail || `HTTP ${r.status}`);
  }
  return await r.json();
}
async function apiGet(url){   return _apiFetch("GET", url, null); }
async function apiPost(url, body){ return _apiFetch("POST", url, body || {}); }
async function apiPatch(url, body){ return _apiFetch("PATCH", url, body || {}); }

function escapeHtml(s){
  return (s||"").replace(/[&<>"']/g, (m)=>({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
  }[m]));
}

function getSelectedIndexNames(){
  return Array.from(document.querySelectorAll('input[name="indexNames"]:checked'))
    .map(x => x.value)
    .filter(Boolean);
}

function formatTimeForUI(t){
  if(!t) return new Date().toLocaleString("ko-KR", { hour12:false });

  if(typeof t === "string" && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(t)){
    return new Date(t.replace(" ", "T")).toLocaleString("ko-KR", { hour12:false });
  }

  return new Date(t).toLocaleString("ko-KR", { hour12:false });
}

/* ---------- markdown render helpers ---------- */
function configureMarked(){
  if(typeof marked === "undefined") return;

  marked.setOptions({
    gfm: true,
    breaks: true
  });
}

function splitInlineMarkdownBullets(text){
  let t = text || "";

  // " - **작성자**" 같은 inline bullet를 줄바꿈으로 분리
  t = t.replace(/\s+-\s+(?=\*\*)/g, "\n- ");
  t = t.replace(/\s+\*\s+(?=\*\*)/g, "\n* ");

  // 숫자 목록도 한 줄에 붙어있으면 분리
  t = t.replace(/\s+(\d+)\.\s+(?=\*\*|[^\s])/g, "\n$1. ");

  return t;
}

// GFM 표가 앞 문단과 붙어 있으면 marked가 표로 인식하지 못해 깨진다.
// 표 블록(헤더행 + |---| 구분행) 앞뒤에 빈 줄을 보장한다. (코드펜스 내부는 건드리지 않음)
function ensureTableSpacing(text){
  const lines = String(text || "").split("\n");
  const out = [];
  let inFence = false;
  const isSep = (s) => s.includes("-") && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(s);
  const isRow = (s) => s.includes("|");
  for(let i = 0; i < lines.length; i++){
    const line = lines[i];
    if(/^\s*```/.test(line)){ inFence = !inFence; out.push(line); continue; }
    if(inFence){ out.push(line); continue; }

    const next = (i + 1 < lines.length) ? lines[i + 1] : "";
    if(isRow(line.trim()) && isSep(next.trim())){
      // 헤더 앞 빈 줄 보장
      if(out.length && out[out.length - 1].trim() !== "") out.push("");
      out.push(line);          // header
      out.push(lines[++i]);    // separator
      // 이어지는 표 본문 행 복사
      while(i + 1 < lines.length && lines[i + 1].includes("|") && lines[i + 1].trim() !== ""){
        out.push(lines[++i]);
      }
      // 표 뒤 빈 줄 보장
      if(i + 1 < lines.length && lines[i + 1].trim() !== "") out.push("");
      continue;
    }
    out.push(line);
  }
  return out.join("\n");
}

function normalizeMarkdownInput(text){
  let t = String(text || "").trim();

  // 전체가 quoted string처럼 감싸져 있으면 제거
  if(
    t.length >= 2 &&
    (
      (t.startsWith('"') && t.endsWith('"')) ||
      (t.startsWith("'") && t.endsWith("'"))
    )
  ){
    t = t.slice(1, -1);
  }

  // JSON string escape 복원
  t = t
    .replace(/\\"/g, '"')
    .replace(/\\'/g, "'")
    .replace(/\\\\/g, "\\")
    .replace(/\\r\\n/g, "\n")
    .replace(/\\n/g, "\n")
    .replace(/\\r/g, "\n")
    .replace(/\\t/g, "\t");

  // 실제 CRLF/LF 정규화
  t = t.replace(/\r\n/g, "\n").replace(/\r/g, "\n");

  // <br> 류가 섞여 오면 줄바꿈으로 변환
  t = t.replace(/<br\s*\/?>/gi, "\n");

  // markdown code fence 제거
  t = t.replace(/^```(?:markdown|md|text)?\s*\n?/i, "");
  t = t.replace(/\n?```$/i, "");

  // 한 줄에 붙은 bullet 강제 분리
  t = splitInlineMarkdownBullets(t);

  // 표 블록 앞뒤 빈 줄 보장 (표 깨짐 방지)
  t = ensureTableSpacing(t);

  // heading/table/list 앞뒤 여유 줄 확보
  t = t.replace(/\n{3,}/g, "\n\n").trim();

  return t;
}

function renderMarkdownSafe(mdText){
  const raw = normalizeMarkdownInput(mdText);

  if(typeof marked === "undefined"){
    return escapeHtml(raw).replace(/\n/g, "<br>");
  }

  let rendered = marked.parse(raw);

  if(typeof DOMPurify !== "undefined"){
    rendered = DOMPurify.sanitize(rendered, {
      USE_PROFILES: { html: true },
      ADD_TAGS: ['details', 'summary'] // 추가 SQL 접기/펴기 태그 허용
    });
  }

  // 인용구 [n] → pill 변환은 DOM 렌더 후 convertCitationPills()에서 수행
  // (문자열 정규식 전역 치환은 표/코드 안의 무관한 [3]까지 오변환하는 버그가 있었음)

  return rendered;
}

// document 모달 호출 시 인덱스로 찾기 위해 래핑 함수를 하나 만듦
window.openDocFromCitationIndex = function(idxStr) {
  // citations에서 idx 인덱스에 해당하는 것을 찾아서 열어줍니다.
  const idx = parseInt(idxStr, 10) - 1;
  const ans = (lastCitations && lastCitations.answer) ? lastCitations.answer : [];
  if (ans[idx] && ans[idx].citations && ans[idx].citations.length > 0) {
     const c = ans[idx].citations[0];
     openDocFromCitation(c.doc_id, c.chunk_id, c.quote || "");
  } else {
    // topDocs에서 idx로 바로 찾기 (fallback)
    if(lastTopDocs && lastTopDocs[idx]) {
       openDocModal(lastTopDocs[idx], null);
    } else {
       console.log("No document found for index: " + idxStr);
    }
  }
};

function renderDocumentMarkdown(mdText){
  const raw = String(mdText || "");

  if(typeof marked === "undefined"){
    return escapeHtml(raw).replace(/\n/g, "<br>");
  }

  let rendered = marked.parse(raw);

  if(typeof DOMPurify !== "undefined"){
    rendered = DOMPurify.sanitize(rendered, {
      USE_PROFILES: { html: true },
      ADD_TAGS: ['details', 'summary'] // 추가 SQL 접기/펴기 태그 허용
    });
  }

  return rendered;
}

function renderMessageContent(role, content){
  const text = content || "";

  if(role === "assistant"){
    console.log("[assistant raw content]", text);
    console.log("[assistant normalized content]", normalizeMarkdownInput(text));
    return renderMarkdownSafe(text);
  }

  return escapeHtml(text).replace(/\n/g, "<br>");
}

function autoLinkPlainUrls(container){
  if(!container) return;

  const urlRe = /\bhttps?:\/\/[^\s<]+/gi;
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null);
  const targets = [];
  let node;

  while((node = walker.nextNode())){
    const parent = node.parentElement;
    if(!parent) continue;

    const tag = parent.tagName;
    if(["A", "CODE", "PRE", "SCRIPT", "STYLE", "TEXTAREA"].includes(tag)) continue;
    if(!node.nodeValue || !urlRe.test(node.nodeValue)) continue;

    targets.push(node);
  }

  targets.forEach(textNode => {
    const text = textNode.nodeValue || "";
    const frag = document.createDocumentFragment();

    let lastIdx = 0;
    text.replace(urlRe, (match, offset) => {
      const before = text.slice(lastIdx, offset);
      if(before) frag.appendChild(document.createTextNode(before));

      const a = document.createElement("a");
      a.href = match;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = match;
      frag.appendChild(a);

      lastIdx = offset + match.length;
      return match;
    });

    const tail = text.slice(lastIdx);
    if(tail) frag.appendChild(document.createTextNode(tail));

    if(textNode.parentNode){
      textNode.parentNode.replaceChild(frag, textNode);
    }
  });
}

function addCodeCopyButtons(container){
  if(!container) return;

  container.querySelectorAll("pre > code").forEach(code => {
    const pre = code.parentElement;
    if(!pre || pre.dataset.copyBound === "1") return;

    pre.dataset.copyBound = "1";

    const wrap = document.createElement("div");
    wrap.className = "code-block-wrap";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-code-btn";
    btn.textContent = "복사";

    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      try{
        await navigator.clipboard.writeText(code.innerText || code.textContent || "");
        const prev = btn.textContent;
        btn.textContent = "복사됨";
        setTimeout(() => { btn.textContent = prev; }, 1200);
      } catch(err){
        const prev = btn.textContent;
        btn.textContent = "실패";
        setTimeout(() => { btn.textContent = prev; }, 1200);
      }
    });

    const parent = pre.parentNode;
    if(!parent) return;

    parent.insertBefore(wrap, pre);
    wrap.appendChild(btn);
    wrap.appendChild(pre);
  });
}

function convertCitationPills(container){
  // [n] 텍스트를 citation pill로 변환 (DOM 기반).
  // 조건: 유효한 citation 번호(1..N)일 때만, 표/코드/링크 내부는 제외 → 오변환 방지.
  if(!container) return;
  const total = (lastCitations && Array.isArray(lastCitations.answer)) ? lastCitations.answer.length : 0;
  if(!total) return;

  const SKIP_TAGS = ["A", "CODE", "PRE", "SCRIPT", "STYLE", "BUTTON", "TABLE", "THEAD", "TBODY", "TH", "TD", "SUMMARY", "KBD"];
  const re = /\[(\d{1,2})\]/g;
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null);
  const targets = [];
  let node;
  while((node = walker.nextNode())){
    let p = node.parentElement, skip = false;
    while(p && p !== container){
      if(SKIP_TAGS.includes(p.tagName)){ skip = true; break; }
      p = p.parentElement;
    }
    if(skip) continue;
    re.lastIndex = 0;
    if(node.nodeValue && re.test(node.nodeValue)) targets.push(node);
  }

  targets.forEach(textNode => {
    const text = textNode.nodeValue || "";
    const frag = document.createDocumentFragment();
    let last = 0, changed = false, m;
    re.lastIndex = 0;
    while((m = re.exec(text))){
      const n = parseInt(m[1], 10);
      if(n < 1 || n > total) continue; // 무효 번호는 텍스트 그대로 유지
      frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const pill = document.createElement("span");
      pill.className = "citation-pill";
      pill.textContent = String(n);
      pill.addEventListener("click", () => window.openDocFromCitationIndex(String(n)));
      frag.appendChild(pill);
      last = m.index + m[0].length;
      changed = true;
    }
    if(!changed) return;
    frag.appendChild(document.createTextNode(text.slice(last)));
    if(textNode.parentNode) textNode.parentNode.replaceChild(frag, textNode);
  });
}

function enhanceRenderedMessage(scope){
  if(!scope) return;

  const contentNodes = scope.querySelectorAll(".content.markdown-body");
  contentNodes.forEach(node => {
    autoLinkPlainUrls(node);
    addCodeCopyButtons(node);
    // 마크다운 링크([t](url))는 marked가 target 없이 렌더 → 무조건 새 창에서 열리도록 강제
    node.querySelectorAll("a[href]").forEach(a => {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    });
    convertCitationPills(node);
  });
  attachCitationHovers(scope);
}

/* ---------- citation pill hover preview ---------- */
function attachCitationHovers(scope){
  if(!scope) return;
  if(!attachCitationHovers._tip){
    const tip = document.createElement("div");
    tip.className = "citation-tooltip";
    tip.setAttribute("role", "tooltip");
    document.body.appendChild(tip);
    attachCitationHovers._tip = tip;
  }
  const tip = attachCitationHovers._tip;
  scope.querySelectorAll(".citation-pill").forEach(pill => {
    if(pill.dataset.hoverBound === "1") return;
    pill.dataset.hoverBound = "1";

    pill.addEventListener("mouseenter", () => {
      const idx = parseInt(pill.textContent.trim(), 10);
      if(!idx) return;
      const ans = (lastCitations && Array.isArray(lastCitations.answer)) ? lastCitations.answer : [];
      const item = ans[idx - 1];
      let quote = "";
      let docHint = "";
      if(item){
        quote = (item.quote || item.cited_quote || item.sentence || item.text || "").toString();
        docHint = (item.doc_id || item.title || "").toString();
      }
      if(!quote){
        const td = Array.isArray(lastTopDocs) ? lastTopDocs[idx - 1] : null;
        if(td){
          quote = td.title || td.snippet || "(인용 문장이 없습니다)";
          docHint = td.doc_id || td.index || "";
        } else {
          quote = "(인용 문장을 찾을 수 없습니다)";
        }
      }
      tip.innerHTML = `
        <span class="ct-label">인용 [${idx}]</span>
        <div class="ct-quote">${escapeHtml(quote.length > 280 ? quote.slice(0, 280) + "…" : quote)}</div>
        ${docHint ? `<span class="ct-doc">${escapeHtml(docHint)}</span>` : ""}
      `;
      // Position above the pill (or below if near top)
      const rect = pill.getBoundingClientRect();
      tip.style.visibility = "hidden";
      tip.classList.add("is-visible");
      const tipRect = tip.getBoundingClientRect();
      let top = rect.top - tipRect.height - 10;
      if (top < 8) top = rect.bottom + 10;
      let left = rect.left + rect.width / 2 - tipRect.width / 2;
      left = Math.max(8, Math.min(left, window.innerWidth - tipRect.width - 8));
      tip.style.top = `${top}px`;
      tip.style.left = `${left}px`;
      tip.style.visibility = "";
    });
    pill.addEventListener("mouseleave", () => {
      tip.classList.remove("is-visible");
    });
  });
}

/* ---------- query interpretation card ---------- */
function buildDetectedTermTags(detectedTerms){
  if(!Array.isArray(detectedTerms) || !detectedTerms.length) return "";
  const uniq = [];
  const seen = new Set();

  detectedTerms.forEach(t => {
    const name = (t && t.canonical_name) ? String(t.canonical_name).trim() : "";
    if(!name) return;
    if(seen.has(name)) return;
    seen.add(name);
    uniq.push(name);
  });

  return uniq.map(x => `<span class="query-chip">#${escapeHtml(x)}</span>`).join("");
}

function buildQueryInterpretationCard(data){
  if(!data) return "";

  const rewritten = (data.rewritten_query || "").trim();
  const normalized = (data.normalized_query || "").trim();
  const expanded = (data.expanded_query || "").trim();
  const detectedTerms = Array.isArray(data.detected_terms) ? data.detected_terms : [];

  const hasMeaningful =
    rewritten || normalized || expanded || detectedTerms.length > 0;

  if(!hasMeaningful) return "";

  const tagsHtml = buildDetectedTermTags(detectedTerms);
  const summaryText = detectedTerms.length
    ? `🔎 검색 해석 적용됨 · ${detectedTerms.map(x => "#" + (x.canonical_name || x.matched_text || "")).filter(Boolean).join(" ")}`
    : "🔎 검색 해석 적용됨";

  const detailsRows = [];

  if(rewritten){
    detailsRows.push(`
      <div class="query-row">
        <div class="query-label">재작성 질의</div>
        <div class="query-value">${escapeHtml(rewritten)}</div>
      </div>
    `);
  }
  if(normalized){
    detailsRows.push(`
      <div class="query-row">
        <div class="query-label">정규화 질의</div>
        <div class="query-value">${escapeHtml(normalized)}</div>
      </div>
    `);
  }
  if(expanded){
    detailsRows.push(`
      <div class="query-row">
        <div class="query-label">확장 질의</div>
        <div class="query-value">${escapeHtml(expanded)}</div>
      </div>
    `);
  }
  if(tagsHtml){
    detailsRows.push(`
      <div class="query-row">
        <div class="query-label">인식 용어</div>
        <div class="query-value query-tags">${tagsHtml}</div>
      </div>
    `);
  }

  return `
    <div class="query-interpret-card">
      <button class="query-interpret-toggle" type="button">
        <span class="query-interpret-summary">${escapeHtml(summaryText)}</span>
        <span class="query-interpret-arrow">▾</span>
      </button>
      <div class="query-interpret-body" style="display:none;">
        ${detailsRows.join("")}
      </div>
    </div>
  `;
}

function wireQueryInterpretCard(scope){
  const cards = (scope || document).querySelectorAll(".query-interpret-card");
  cards.forEach(card => {
    const btn = card.querySelector(".query-interpret-toggle");
    const body = card.querySelector(".query-interpret-body");
    const arrow = card.querySelector(".query-interpret-arrow");
    if(!btn || !body || btn.dataset.bound === "1") return;

    btn.dataset.bound = "1";
    btn.addEventListener("click", () => {
      const open = body.style.display !== "none";
      body.style.display = open ? "none" : "block";
      if(arrow) arrow.textContent = open ? "▾" : "▴";
    });
  });
}

/* ---------- theme ---------- */
function applyTheme(theme){
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("theme", theme);
}
function toggleTheme(){
  const cur = document.documentElement.getAttribute("data-theme") || "dark";
  applyTheme(cur === "dark" ? "light" : "dark");
}

/* ---------- sidebar ---------- */
function setSidebarCollapsed(collapsed){
  const sb = el("sidebar");
  if(!sb) return;
  sb.classList.toggle("collapsed", !!collapsed);
  const btn = el("toggleSidebar");
  const icon = btn && btn.querySelector("span");
  // 접힘 = 펼치기(side_navigation) 아이콘, 펼침 = 접기(menu_open) 아이콘
  if(icon) icon.textContent = collapsed ? "side_navigation" : "menu_open";
}
function setupSidebarToggle(){
  const btn = el("toggleSidebar");
  if(!btn) return;
  btn.onclick = () => {
    const sb = el("sidebar");
    setSidebarCollapsed(!(sb && sb.classList.contains("collapsed")));
  };
}

/* ---------- resizers ---------- */
function setupVerticalResizer(resizerId, leftSelector, rightSelector){
  const resizer = el(resizerId);
  const left = document.querySelector(leftSelector);
  const right = document.querySelector(rightSelector);
  let dragging = false;

  resizer.addEventListener("mousedown", ()=>{
    dragging = true;
    document.body.classList.add("dragging");
    document.body.style.cursor = "col-resize";
  });

  window.addEventListener("mousemove", (e)=>{
    if(!dragging) return;
    const x = e.clientX;
    const total = window.innerWidth;

    if(resizerId === "resizer1"){
      const min = 60;
      const max = total * 0.5;
      const w = Math.max(min, Math.min(max, x));
      left.style.width = w + "px";
    } else {
      const min = 520;
      const max = total * 0.7;
      const w = Math.max(min, Math.min(max, total - x));
      right.style.width = w + "px";
    }
  });

  window.addEventListener("mouseup", ()=>{
    dragging = false;
    document.body.classList.remove("dragging");
    document.body.style.cursor = "";
  });
}

function setupHorizontalResizer(resizerId, topPanelSelector, bottomPanelSelector){
  const resizer = el(resizerId);
  const topPanel = document.querySelector(topPanelSelector);
  const bottomPanel = document.querySelector(bottomPanelSelector);
  let dragging = false;

  resizer.addEventListener("mousedown", ()=>{
    dragging = true;
    document.body.classList.add("dragging");
    document.body.style.cursor = "row-resize";
  });

  window.addEventListener("mousemove", (e)=>{
    if(!dragging) return;
    const rect = document.querySelector(".right-body").getBoundingClientRect();
    const y = e.clientY - rect.top;

    const minTop = 120;
    const minBottom = 120;
    const total = rect.height;

    const topH = Math.max(minTop, Math.min(total - minBottom, y));
    topPanel.style.flex = "0 0 auto";
    bottomPanel.style.flex = "1 1 auto";
    topPanel.style.height = topH + "px";
  });

  window.addEventListener("mouseup", ()=>{
    dragging = false;
    document.body.classList.remove("dragging");
    document.body.style.cursor = "";
  });
}

/* ---------- panel maximize ---------- */
function setupPanelMaxButtons(){
  document.querySelectorAll(".panel-max").forEach(btn => {
    btn.onclick = () => {
        const target = btn.dataset.target;
        document.querySelectorAll(".panel").forEach(p => {
            if(p.dataset.panel === target) p.classList.toggle("maximized");
            else p.classList.remove("maximized");
        });
    };
  });
}

// ✨ [수정] 한 줄로 들어오든 여러 줄이든 [MAIL_META] 블록을 정규식으로 유연하고 완벽하게 제거
function stripLeadingMailMetaBlock(mdText){
  let t = String(mdText || "").replace(/\r\n/g, "\n");

  // 1) 맨 앞 fenced code block 자체가 MAIL_META를 담고 있으면 통째로 제거
  t = t.replace(
    /^\s*```[^\n]*\n([\s\S]*?)\n```[\t ]*\n*/i,
    (full, inner) => {
      if(/\[MAIL_META\]/i.test(String(inner || ""))) return "";
      return full;
    }
  );

  // 2) [MAIL_META] 부터 시작해서, 다음 빈 줄(\n\n)이 나오거나 문서가 끝날때까지 통째로 날림
  t = t.replace(/\[MAIL_META\][\s\S]*?(?=\n\s*\n|$)/i, "");

  return t.trimStart(); 
}

function injectImagesIntoMarkdown(mdText, assets){
  if(!mdText) return mdText || "";
  
  // 1. 첨부된 에셋(이미지)이 아예 없는 경우: 
  // 원본을 그대로 반환하지 않고 모든 [Image_position]를 빈 문자열로 지워줍니다.
  if(!assets || !assets.length) {
    return mdText.replace(/\[Image_position\]/gi, "");
  }

  const imgs = assets
    .map(a => (a && a.path) ? a : null)
    .filter(Boolean);

  // 유효한 이미지 경로가 없는 경우에도 모두 지워줍니다.
  if(!imgs.length) {
    return mdText.replace(/\[Image_position\]/gi, "");
  }

  let i = 0;

  return mdText.replace(/\[Image_position\]/gi, () => {
    // 2. 이미지를 순차적으로 매핑하다가, 준비된 이미지를 모두 소진한 경우:
    // 기존의 return "[Image_position]"; 대신 return ""; 를 사용하여 남은 문구를 화면에서 지웁니다.
    if(i >= imgs.length) return "";

    const a = imgs[i++];
    const url = `/api/view/asset?rel=${encodeURIComponent(a.path)}`;
    const alt = (a.file_name || a.path || "image").replace(/[\r\n]+/g, " ");

    // 정상적으로 매핑된 이미지는 HTML 태그로 렌더링
    return `
<div class="md-embed-img-wrap">
  <img class="md-embed-img" src="${url}" alt="${escapeHtml(alt)}" loading="lazy"
       onclick="showImgPreview('${url}')">
</div>
`;
  });
}

/* ---------- sessions ---------- */
function showSessionListSkeleton(){
  const box = el("sessionList");
  if(!box) return;
  let html = "";
  for(let i = 0; i < 5; i++){
    html += `
      <div class="session-card flex flex-col gap-2 p-3 mx-2 mb-2 rounded-xl border border-surface-container bg-surface-container-low">
        <div class="skeleton" style="height:10px; width:${60 + Math.random()*30}%;"></div>
        <div class="skeleton" style="height:8px; width:${30 + Math.random()*20}%;"></div>
      </div>`;
  }
  box.innerHTML = html;
}

async function refreshSessions(){
  const box = el("sessionList");
  if(box && !box.children.length) showSessionListSkeleton();
  try {
    const data = await apiGet("/api/sessions");
    renderSessions(data.sessions || []);
    applySessionFilter();
  } catch(err){
    if(box) box.innerHTML = `
      <div class="p-4 mx-2 mb-2 rounded-xl border border-error/30 bg-error/5 text-error text-xs">
        세션을 불러오지 못했습니다.
        <button type="button" id="sessionListRetry" class="ml-2 underline font-bold">다시 시도</button>
      </div>`;
    const retry = el("sessionListRetry");
    if(retry) retry.addEventListener("click", refreshSessions);
  }
}

function renderSessions(sessions){
  const box = el("sessionList");
  box.innerHTML = "";
  sessions.forEach(s => {
    const div = document.createElement("div");
    div.className = "session-card group flex flex-col gap-1 p-3 mx-2 mb-2 rounded-xl bg-surface-container-low hover:bg-surface-container border border-surface-container cursor-pointer";
    div.dataset.sessionId = s.session_id;
    div.dataset.title = (s.title || "").toLowerCase();
    if(s.session_id === currentSessionId) div.classList.add("is-active");
    div.innerHTML = `
      <div class="flex items-center justify-between gap-2 overflow-hidden">
        <div class="session-title text-xs font-bold text-on-surface truncate flex-1" title="더블클릭으로 이름 변경">${escapeHtml(s.title || "Untitled")}</div>
        <button class="session-rename-btn icon-btn opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-surface-container-high text-secondary shrink-0" title="이름 변경" aria-label="이름 변경">
          <span class="material-symbols-outlined text-[14px]">edit</span>
        </button>
        <button class="session-delete-btn icon-btn danger opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-error/10 text-error shrink-0" title="Delete" aria-label="삭제">
          <span class="material-symbols-outlined text-[14px]">delete</span>
        </button>
        <button class="session-pin-btn icon-btn p-1 rounded hover:bg-surface-container-high shrink-0 ${s.pinned ? '' : 'opacity-0 group-hover:opacity-100'} ${s.pinned ? 'text-rag-strong' : 'text-secondary'}" title="${s.pinned ? '핀 해제' : '핀'}" aria-label="${s.pinned ? '핀 해제' : '핀'}">
          <span class="material-symbols-outlined text-[14px]">${s.pinned ? 'push_pin' : 'keep'}</span>
        </button>
      </div>
      <div class="session-date text-[10px] text-secondary truncate">${escapeHtml(s.updated_at || "")}</div>
    `;

    div.addEventListener("click", (ev) => {
      if(ev.target && ev.target.closest("button")) return;
      if(div.dataset.renaming === "1") return;
      loadSession(s.session_id);
    });

    const titleEl = div.querySelector(".session-title");
    if(titleEl){
      titleEl.addEventListener("dblclick", (ev) => {
        ev.stopPropagation();
        enterSessionRenameMode(div, s);
      });
    }
    const renameBtn = div.querySelector(".session-rename-btn");
    if(renameBtn){
      renameBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        enterSessionRenameMode(div, s);
      });
    }

    const pinBtn = div.querySelector(".session-pin-btn");
    if(pinBtn){
      pinBtn.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        try {
          const res = await apiPost(`/api/sessions/${encodeURIComponent(s.session_id)}/pin`, { pinned: !s.pinned });
          s.pinned = !!(res && res.pinned);
          await refreshSessions();
        } catch(err){
          if(window.Toast) window.Toast.error("핀 처리에 실패했습니다.");
        }
      });
    }

    const deleteBtn = div.querySelector(".session-delete-btn");
    if(deleteBtn) {
      deleteBtn.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const ok = window.Toast
          ? await window.Toast.confirm("이 대화를 목록에서 제거할까요?", { okText: "제거", destructive: true })
          : confirm("이 대화를 목록에서 제거할까요?");
        if(!ok) return;
        await apiPost(`/api/sessions/${encodeURIComponent(s.session_id)}/archive`, {});
        if(currentSessionId === s.session_id){
          currentSessionId = null;
          el("chatArea").innerHTML = "";
          clearEvidencePanels();
          if (typeof toggleEvidencePanel === "function") toggleEvidencePanel(false);
        }
        await refreshSessions();
      });
    }
    box.appendChild(div);
  });
}

/* ---------- session rename + sidebar search ---------- */
function rebindSessionTitle(card, session){
  const oldTitleEl = card.querySelector(".session-title");
  if(!oldTitleEl) return;
  const fresh = oldTitleEl.cloneNode(true);
  oldTitleEl.replaceWith(fresh);
  fresh.addEventListener("dblclick", (ev) => {
    ev.stopPropagation();
    enterSessionRenameMode(card, session);
  });
}

function enterSessionRenameMode(card, session){
  if(!card || card.dataset.renaming === "1") return;
  const titleEl = card.querySelector(".session-title");
  if(!titleEl) return;
  card.dataset.renaming = "1";

  const oldText = (session && session.title) || titleEl.textContent || "";
  const input = document.createElement("input");
  input.type = "text";
  input.value = oldText;
  input.className = "session-title-edit text-xs font-bold text-on-surface bg-transparent border-b border-rag flex-1 outline-none px-1";
  input.maxLength = 255;
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  let finished = false;
  let committing = false;
  const restore = (newText) => {
    if(finished) return;
    finished = true;
    card.dataset.renaming = "0";
    const replacement = document.createElement("div");
    replacement.className = "session-title text-xs font-bold text-on-surface truncate flex-1";
    replacement.setAttribute("title", "더블클릭으로 이름 변경");
    replacement.textContent = newText;
    input.replaceWith(replacement);
    replacement.addEventListener("dblclick", (ev) => {
      ev.stopPropagation();
      enterSessionRenameMode(card, session);
    });
    card.dataset.title = (newText || "").toLowerCase();
    applySessionFilter();
  };

  const commit = async () => {
    // Enter 키다운과 (input 제거로 유발되는) blur가 각각 commit을 호출해
    // 두 번 실행되던 것을 방지 — 성공/실패 토스트 동시 발생 버그 차단.
    if(finished || committing) return;
    committing = true;
    const newTitle = (input.value || "").trim();
    if(!newTitle || newTitle === oldText){
      restore(oldText);
      return;
    }
    try{
      await apiPatch(`/api/sessions/${encodeURIComponent(session.session_id)}`, { title: newTitle });
      if(session) session.title = newTitle;
      restore(newTitle);
      if(window.Toast) window.Toast.success("세션 이름을 변경했습니다.");
    } catch(err){
      restore(oldText);
      if(window.Toast) window.Toast.error("이름 변경에 실패했습니다.");
    }
  };

  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if(e.key === "Enter"){ e.preventDefault(); commit(); }
    else if(e.key === "Escape"){ e.preventDefault(); restore(oldText); }
  });
  input.addEventListener("blur", commit);
  input.addEventListener("click", (e) => e.stopPropagation());
}

function applySessionFilter(){
  const input = el("sessionSearchInput");
  const q = (input && input.value || "").trim().toLowerCase();
  const cards = document.querySelectorAll("#sessionList .session-card");
  cards.forEach(card => {
    const title = card.dataset.title || "";
    card.style.display = (!q || title.includes(q)) ? "" : "none";
  });
}

/* ---------- assistant message action bar ---------- */
function openDownFeedbackPopover(bar, submit){
  // 이미 열려있으면 재사용
  const existing = bar.parentElement && bar.parentElement.querySelector(".fb-comment-pop");
  if(existing){ existing.querySelector("textarea").focus(); return; }

  const pop = document.createElement("div");
  pop.className = "fb-comment-pop";
  pop.innerHTML = `
    <div class="fb-comment-title">어떤 점이 아쉬웠는지 알려주세요 <span>(필수 · 5자 이상)</span></div>
    <textarea class="fb-comment-input" rows="2" maxlength="500"
      placeholder="예: 통계 수치가 실제와 다릅니다 / 엉뚱한 문서를 인용했어요"></textarea>
    <div class="fb-comment-actions">
      <button type="button" class="fb-cancel">취소</button>
      <button type="button" class="fb-submit">피드백 보내기</button>
    </div>`;
  bar.insertAdjacentElement("afterend", pop);

  const ta = pop.querySelector("textarea");
  ta.focus();
  pop.querySelector(".fb-cancel").addEventListener("click", (e) => { e.stopPropagation(); pop.remove(); });
  pop.querySelector(".fb-submit").addEventListener("click", async (e) => {
    e.stopPropagation();
    const comment = ta.value.trim();
    if(comment.length < 5){
      if(window.Toast) window.Toast.warn("어떤 점이 아쉬웠는지 5자 이상 적어주세요.");
      ta.focus();
      return;
    }
    await submit("down", comment);
    pop.remove();
  });
  pop.addEventListener("click", (e) => e.stopPropagation());
  ta.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if(e.key === "Escape") pop.remove();
  });
}

function wireMessageActionBar(scope, assistantMsgId){
  if(!scope || !assistantMsgId) return;
  const bar = scope.querySelector(".msg-action-bar");
  if(!bar || bar.dataset.bound === "1") return;
  bar.dataset.bound = "1";

  bar.querySelectorAll(".msg-action-btn").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const act = btn.getAttribute("data-action");
      if(act === "copy"){
        const raw = scope.dataset.rawMarkdown || (scope.querySelector(".content")?.innerText || "");
        try{
          await navigator.clipboard.writeText(raw);
          btn.classList.add("is-copied");
          const label = btn.querySelector("span:not(.material-symbols-outlined)");
          const prev = label ? label.textContent : null;
          if(label) label.textContent = "복사됨";
          if(window.Toast) window.Toast.success("응답을 복사했습니다.");
          setTimeout(() => {
            btn.classList.remove("is-copied");
            if(label && prev !== null) label.textContent = prev;
          }, 1500);
        } catch(err){
          if(window.Toast) window.Toast.error("복사에 실패했습니다.");
        }
        return;
      }
      if(act === "regen"){
        let node = scope.previousElementSibling;
        let userText = "";
        while(node){
          if(node.classList && node.classList.contains("user")){
            const c = node.querySelector(".content");
            userText = c ? (c.innerText || c.textContent || "") : "";
            break;
          }
          node = node.previousElementSibling;
        }
        userText = userText.trim();
        if(!userText){
          if(window.Toast) window.Toast.warn("재생성할 질문을 찾지 못했습니다.");
          return;
        }
        const input = el("userInput");
        if(input){ input.value = userText; autosizeInput(); }
        sendMessage();
        return;
      }
      if(act === "up" || act === "down"){
        const submitFeedback = async (rating, comment) => {
          try{
            const res = await apiPost("/api/feedback", {
              assistant_msg_id: assistantMsgId,
              rating,
              comment: comment || null,
              session_id: currentSessionId || null
            });
            const newRating = res && res.rating;
            bar.querySelectorAll(".msg-action-btn[data-action='up'],.msg-action-btn[data-action='down']").forEach(b => {
              const a = b.getAttribute("data-action");
              if(a === newRating) b.classList.add("is-active");
              else b.classList.remove("is-active");
            });
            if(window.Toast){
              if(newRating === "up") window.Toast.success("좋은 응답으로 기록했습니다.");
              else if(newRating === "down") window.Toast.info("소중한 피드백 감사합니다. 개선에 활용할게요.");
              else window.Toast.info("피드백을 취소했습니다.");
            }
          } catch(err){
            if(window.Toast) window.Toast.error("피드백 저장에 실패했습니다.");
          }
        };

        if(act === "up"){
          await submitFeedback("up");
          return;
        }
        // 👎는 이유(코멘트)를 필수로 받는다 — 가볍게 누르는 노이즈를 거르고,
        // '왜 별로였는지'가 있어야 개선 분석(드릴다운)이 가능하기 때문.
        // 이미 👎 상태에서 다시 누르는 것(취소)은 마찰 없이 바로 처리.
        if(btn.classList.contains("is-active")){
          await submitFeedback("down");
          return;
        }
        openDownFeedbackPopover(bar, submitFeedback);
        return;
      }
    });
  });
}

/* ---------- welcome / empty state ---------- */
function renderWelcomeScreen(){
  const chat = el("chatArea");
  if(!chat) return;

  const suggestions = [
    { icon: "monitoring",   text: "최근 3개월 동안 가장 많이 발생한 불량명 순위 5개",          hint: "DB 통계" },
    { icon: "description",  text: "Wet Etch 공정에서 파티클 불량의 주요 원인과 가이드를 찾아줘", hint: "문서 검색" },
    { icon: "auto_awesome", text: "어제 등록된 IFA 보고서 중 양산 관련된 내용을 요약해줘",      hint: "하이브리드" }
  ];

  const chipsHtml = suggestions.map(s => `
    <button class="welcome-chip" data-suggestion="${escapeHtml(s.text)}" type="button" title="${escapeHtml(s.hint)}">
      <span class="material-symbols-outlined">${s.icon}</span>
      <span>${escapeHtml(s.text)}</span>
    </button>
  `).join("");

  chat.innerHTML = `
    <div class="welcome-screen">
      <img src="/static/images/empty-chat.svg" alt=""/>
      <h2>무엇을 분석해드릴까요?</h2>
      <p>사내 보고서와 불량 통계 DB를 함께 활용해<br/>원인 파악 · 근거 인용 · 다음 액션 제안까지 한 번에 도와드립니다.</p>
      <div class="welcome-chips">${chipsHtml}</div>
    </div>
  `;

  chat.querySelectorAll(".welcome-chip").forEach(btn => {
    btn.addEventListener("click", () => {
      const text = btn.getAttribute("data-suggestion") || "";
      const input = el("userInput");
      if(input){
        input.value = text;
        autosizeInput();
      }
      // 예시 프롬프트는 클릭 즉시 전송
      sendMessage();
    });
  });
}

function markActiveSessionCard(sessionId){
  document.querySelectorAll("#sessionList .session-card").forEach(card => {
    if(card.dataset.sessionId === sessionId) card.classList.add("is-active");
    else card.classList.remove("is-active");
  });
}

async function loadSession(sessionId){
  currentSessionId = sessionId;
  if(typeof setSidebarCollapsed === "function") setSidebarCollapsed(false);
  markActiveSessionCard(sessionId);
  el("chatArea").innerHTML = "";

  const data = await apiGet(`/api/sessions/${encodeURIComponent(sessionId)}`);
  const searchLogsByUserMsgId = data.search_logs_by_user_msg_id || {};

  (data.messages || []).forEach(m => {
    let extra = null;
    if(m.role === "user" && m.msg_id && searchLogsByUserMsgId[m.msg_id]){
      const log = searchLogsByUserMsgId[m.msg_id];
      extra = {
        rewritten_query: log.rewritten_query,
        normalized_query: log.normalized_query,
        expanded_query: log.expanded_query,
        detected_terms: log.detected_terms || []
      };
    }
    // 어시스턴트(LLM)의 과거 메시지일 경우 인텐트/액션 데이터 복구
    else if (m.role === "assistant") {
      if (m.intent || (m.suggested_actions && m.suggested_actions.length > 0) || (m.related_docs && m.related_docs.length > 0) || m.feedback) {
        extra = {
          intent: m.intent,
          suggested_actions: m.suggested_actions,
          agent_steps: m.agent_steps,
          related_docs: m.related_docs || [],
          feedback: m.feedback || null,
          raw_markdown: m.content
        };
      }
    }
    appendMessage(m.role, m.content, m.created_at, m.msg_id, extra);
  });

  clearEvidencePanels();
  try{
    const a = await apiGet(`/api/sessions/${encodeURIComponent(sessionId)}/latest-artifact`);
    const art = a.artifact;

    const docs = extractTopDocsFromArtifact(art);
    lastTopDocs = docs;
    renderTopDocsFiltered();

    if(art && art.citations){
      lastCitations = art.citations;
      renderCitations(lastCitations);
    }
    currentEvidenceAssistantMsgId = null;
  } catch(e){
    // ignore
  }
}

function newSession(){
  currentSessionId = null;
  markActiveSessionCard(null);
  el("chatArea").innerHTML = "";
  clearEvidencePanels();
  if (typeof renderWelcomeScreen === "function") renderWelcomeScreen();

  // 완전히 새로운 대화를 시작할 때만 패널 닫기.
  if (typeof toggleEvidencePanel === "function") {
    toggleEvidencePanel(false)
  }
  el("userInput").focus();
}

// 선택된 카드 스타일링 업데이트 함수도 수정
function setSelectedAssistantMsg(msgId){
  document.querySelectorAll(".assistant-card").forEach(x => {
      x.classList.remove("ring-2", "ring-primary", "shadow-md", "bg-primary/5");
  });
  if(!msgId) return;

  const node = document.querySelector(`.msg.assistant[data-msg-id="${CSS.escape(msgId)}"] .assistant-card`);
  if(node) {
      node.classList.add("ring-2", "ring-primary", "shadow-md", "bg-primary/5");
  }
}

/* ---------- chat messages ---------- */
function appendMessage(role, content, metaText, msgId, extra = null){
  const chat = el("chatArea");
  // First real message clears the welcome screen
  const welcome = chat.querySelector(".welcome-screen");
  if(welcome) welcome.remove();
  const div = document.createElement("div");
  
  div.className = role === "user" 
    ? "msg user flex justify-end mb-6 w-full" 
    : "msg assistant flex justify-start mb-8 w-full";
  
  if(msgId) div.dataset.msgId = msgId;
  if(role === "assistant" && extra && extra.intent) div.dataset.intent = extra.intent;

  let extraHtml = "";
  let intentHtml = "";
  let chipsHtml = "";
  let stepsHtml = "";
  let actionBarHtml = "";
  let verifyHtml = "";
  const isPending = typeof msgId === "string" && msgId.startsWith("PENDING_");

  if(role === "user" && extra) {
    extraHtml = buildQueryInterpretationCard(extra); 
  }

  if(role === "assistant" && extra) {
    let agentName = "Intellectual Curator";
    let agentIcon = "robot_2";
    let agentColor = "text-secondary dark:text-[#94a3b8]";

    if(extra.intent) {
      let badgeClass = "is-default";
      if(extra.intent === "DB_ANALYSIS")        { agentName = "DB Stats Agent";        agentIcon = "monitoring";    badgeClass = "is-db"; }
      else if(extra.intent === "RAG_KNOWLEDGE") { agentName = "Document Search Agent"; agentIcon = "description";   badgeClass = "is-rag"; }
      else if(extra.intent === "HYBRID_DB_RAG") { agentName = "Hybrid Analysis Agent"; agentIcon = "auto_awesome";  badgeClass = "is-hybrid"; }

      intentHtml = `
        <div class="agent-badge ${badgeClass} mb-4">
          <span class="agent-badge-icon"><span class="material-symbols-outlined">${agentIcon}</span></span>
          <span class="agent-badge-name">${escapeHtml(agentName)}</span>
          <span class="agent-badge-divider">·</span>
          <span class="agent-badge-time">${escapeHtml(formatTimeForUI(metaText))}</span>
        </div>`;
    }

    // 💡 검증 배지: claim 근거 지원율 + DB 수치 에코 체크 결과
    if(extra.verification){
      const v = extra.verification;
      const parts = [];
      if(typeof v.claims_total === "number" && v.claims_total > 0){
        const ok = v.claims_supported || 0;
        const cls = ok === v.claims_total ? "is-good" : (ok > 0 ? "is-mid" : "is-bad");
        parts.push(`<span class="verify-badge ${cls}" title="답변 속 주장 중 근거 문서로 뒷받침된 비율">근거 ${ok}/${v.claims_total}</span>`);
      }
      if(v.numeric_ok === false){
        const nums = (v.unmatched || []).join(", ");
        parts.push(`<span class="verify-badge is-bad" title="답변 속 일부 수치가 DB 조회 결과에서 확인되지 않았습니다: ${escapeHtml(nums)}">⚠ 수치 확인 필요</span>`);
      }
      if(parts.length) verifyHtml = `<div class="verify-badges mb-3">${parts.join("")}</div>`;
    }

    // 💡 [개편] 계층형 개별 토글 UI 적용 (숫자 뱃지 + 양끝 정렬)
    if (extra.agent_steps && extra.agent_steps.length > 0) {
      const stepsList = extra.agent_steps.map((step, index) => {
        const stepNum = index + 1;
        
        // 1. 하위 호환성 (문자열만 있을 경우)
        if (typeof step === 'string') {
           return `
           <div class="py-2.5 px-3 border-b border-surface-container/50 dark:border-[#1f2b4a]/50 last:border-0 flex items-start gap-3">
               <span class="flex-shrink-0 w-5 h-5 rounded-full bg-surface-container-high dark:bg-[#1f2b4a] text-secondary dark:text-[#94a3b8] flex items-center justify-center text-[10px] font-bold mt-0.5">${stepNum}</span>
               <span class="text-on-surface dark:text-[#e7eefc] leading-relaxed">${escapeHtml(step)}</span>
           </div>`;
        } 
        
        // 2. 객체 형태인데 기술 로그(SQL)가 없는 경우
        if (!step.technical_detail) {
           return `
           <div class="py-2.5 px-3 border-b border-surface-container/50 dark:border-[#1f2b4a]/50 last:border-0 flex items-start gap-3">
               <span class="flex-shrink-0 w-5 h-5 rounded-full bg-surface-container-high dark:bg-[#1f2b4a] text-secondary dark:text-[#94a3b8] flex items-center justify-center text-[10px] font-bold mt-0.5">${stepNum}</span>
               <span class="text-on-surface dark:text-[#e7eefc] leading-relaxed">${escapeHtml(step.thought)}</span>
           </div>`;
        }

        // 3. 객체 형태이고 기술 로그(SQL)가 있는 경우 (개별 details 토글 생성)
        // [주의] summary 태그 안에서 list-none과 [&::-webkit-details-marker]:hidden 을 사용하여 기본 화살표를 숨깁니다.
        return `
        <details class="group/step border-b border-surface-container/50 dark:border-[#1f2b4a]/50 last:border-0">
            <summary class="py-2.5 px-3 flex justify-between items-start gap-4 cursor-pointer hover:bg-surface-container dark:hover:bg-[#1f2b4a] transition-colors list-none [&::-webkit-details-marker]:hidden outline-none">
                <div class="flex items-start gap-3">
                    <span class="flex-shrink-0 w-5 h-5 rounded-full bg-primary/10 text-primary dark:bg-[#60a5fa]/20 dark:text-[#60a5fa] flex items-center justify-center text-[10px] font-bold mt-0.5">${stepNum}</span>
                    <span class="text-on-surface dark:text-[#e7eefc] font-medium leading-relaxed">${escapeHtml(step.thought)}</span>
                </div>
                <div class="flex-shrink-0 flex items-center gap-1 text-[10px] font-bold text-outline-variant dark:text-[#475569] group-hover/step:text-primary dark:group-hover/step:text-[#60a5fa] transition-colors mt-0.5 px-2 py-1 rounded border border-surface-container dark:border-[#1f2b4a]">
                    <span>로그 보기</span>
                    <span class="material-symbols-outlined text-[12px] transition-transform group-open/step:rotate-180">keyboard_arrow_down</span>
                </div>
            </summary>
            <div class="px-3 pb-3 pl-11">
                <pre class="p-3 bg-surface-container-highest dark:bg-[#0b1220] rounded text-[11px] text-secondary dark:text-[#94a3b8] overflow-x-auto border border-surface-container/30 dark:border-[#1f2b4a]/30"><code>${escapeHtml(step.technical_detail)}</code></pre>
            </div>
        </details>`;
      }).join("");

      stepsHtml = `
        <details class="mb-5 bg-surface-container-lowest dark:bg-[#0f1a33] border border-surface-container dark:border-[#1f2b4a] rounded-lg overflow-hidden group shadow-sm">
            <summary class="text-xs font-bold text-secondary dark:text-[#94a3b8] cursor-pointer p-3 outline-none hover:bg-surface-container-low dark:hover:bg-[#101f3f] transition-colors flex items-center gap-2 select-none">
                <span class="material-symbols-outlined text-sm transition-transform group-open:rotate-90">chevron_right</span>
                <span class="material-symbols-outlined text-sm">memory</span> 
                Agent 추론 과정 및 도구 실행 로그 보기
            </summary>
            <div class="pt-1 text-[12px] bg-surface-container-low dark:bg-[#101f3f] border-t border-surface-container dark:border-[#1f2b4a]">
                ${stepsList}
            </div>
        </details>
      `;
    }

    // ─── 액션바 (응답 복사 + 👍/👎) ──────────────────────────────
    actionBarHtml = "";
    if(msgId && !isPending){
      const fb = extra.feedback || null;
      actionBarHtml = `
        <div class="msg-action-bar" data-msg-id="${escapeHtml(msgId)}">
          <button class="msg-action-btn" data-action="copy" type="button" aria-label="응답 복사" title="응답 복사">
            <span class="material-symbols-outlined">content_copy</span>
            <span>복사</span>
          </button>
          <button class="msg-action-btn" data-action="regen" type="button" aria-label="재생성" title="동일 질문으로 재생성">
            <span class="material-symbols-outlined">refresh</span>
            <span>재생성</span>
          </button>
          <button class="msg-action-btn ${fb === 'up' ? 'is-active' : ''}" data-action="up" type="button" aria-label="좋아요" title="좋아요">
            <span class="material-symbols-outlined">thumb_up</span>
          </button>
          <button class="msg-action-btn ${fb === 'down' ? 'is-active' : ''}" data-action="down" type="button" aria-label="별로예요" title="별로예요">
            <span class="material-symbols-outlined">thumb_down</span>
          </button>
        </div>`;
    }

    // 🔗 KG: DB 통계와 연결된 원본 보고서 문서 칩
    let kgChipHtml = "";
    if(extra.related_docs && extra.related_docs.length){
      kgChipHtml = `<button class="kg-related-chip px-4 py-2 border border-rag/50 text-rag-strong dark:text-rag hover:bg-rag/10 rounded-full text-[11px] font-semibold flex items-center gap-2 hover:-translate-y-0.5" type="button" title="이 통계와 연결된 원본 보고서 문서를 우측 패널에서 보기"><span class="material-symbols-outlined text-sm">link</span> 연결된 보고서 문서 ${extra.related_docs.length}건 보기</button>`;
    }

    if((extra.suggested_actions && extra.suggested_actions.length > 0) || kgChipHtml) {
      const chips = (extra.suggested_actions || []).map(chip => {
        if (chip.disabled) return `<button class="px-4 py-2 bg-surface-container dark:bg-[#1f2b4a] text-outline dark:text-[#94a3b8] rounded-full text-[11px] font-semibold flex items-center gap-2 cursor-not-allowed opacity-60" disabled><span class="material-symbols-outlined text-sm">block</span> ${escapeHtml(chip.label)}</button>`;
        return `<button class="action-chip px-4 py-2 border border-outline-variant dark:border-[#475569] hover:bg-surface-container dark:hover:bg-[#1f2b4a] dark:text-[#e7eefc] rounded-full text-[11px] font-semibold flex items-center gap-2 hover:-translate-y-0.5" data-action="${escapeHtml(chip.action)}"><span class="material-symbols-outlined text-sm">bolt</span> ${escapeHtml(chip.label)}</button>`;
      }).join("");
      chipsHtml = `<div class="pt-4 mt-4 border-t border-surface-container dark:border-[#1f2b4a] flex flex-wrap items-center gap-3">${kgChipHtml}${chips}</div>`;
    }
  }

  if (role === "user") {
    div.innerHTML = `
      <div class="max-w-[85%] flex flex-col items-end group/user">
        <div class="user-bubble-inner bg-primary text-on-primary p-4 rounded-2xl rounded-tr-none shadow-sm flex flex-col gap-3 w-full relative">
          <button class="user-edit-btn opacity-0 group-hover/user:opacity-100 absolute -top-2 -left-2 w-7 h-7 rounded-full bg-surface border border-outline-variant text-secondary hover:text-on-surface hover:border-rag shadow-sm flex items-center justify-center transition-all" type="button" title="이 질문 편집 / 재전송" aria-label="이 질문 편집">
            <span class="material-symbols-outlined" style="font-size:14px;">edit</span>
          </button>
          <div class="content text-sm leading-relaxed whitespace-pre-wrap">${escapeHtml(content)}</div>
          ${extraHtml}
        </div>
        <div class="text-[10px] text-outline-variant dark:text-[#94a3b8] mt-1">${escapeHtml(formatTimeForUI(metaText))}</div>
      </div>`;
    const editBtn = div.querySelector(".user-edit-btn");
    if(editBtn){
      editBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const contentEl = div.querySelector(".content");
        const text = contentEl ? (contentEl.innerText || contentEl.textContent || "") : "";
        const input = el("userInput");
        if(input){
          input.value = text.trim();
          autosizeInput();
          input.focus();
          input.setSelectionRange(input.value.length, input.value.length);
          input.scrollIntoView({ behavior: "smooth", block: "end" });
        }
      });
    }
  } else {
    div.innerHTML = `
      <div class="assistant-card w-full max-w-[90%] bg-white dark:bg-[#0f1a33] border border-surface-container dark:border-[#1f2b4a] rounded-2xl p-6 hover:shadow-md cursor-pointer group/ai-card">
        ${intentHtml}
        <div class="pl-11">
            ${verifyHtml}
            ${stepsHtml}
            <div class="content markdown-body text-sm leading-relaxed text-on-surface dark:text-[#e7eefc]">
                ${renderMessageContent(role, content)}
            </div>
            ${actionBarHtml}
            ${chipsHtml}
        </div>
      </div>`;
    if(msgId && !isPending){
      const rawContent = (extra && extra.raw_markdown) ? extra.raw_markdown : content;
      div.dataset.rawMarkdown = rawContent;
    }
  }

  if(role === "assistant" && extra && extra.suggested_actions) {
    div.querySelectorAll(".action-chip").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const actionTag = btn.getAttribute("data-action");
        let targetQuery = lastRealUserQuery;
        let prevNode = div.previousElementSibling;
        while(prevNode) {
          if(prevNode.classList.contains("user")) {
            const contentEl = prevNode.querySelector(".content");
            if (contentEl) targetQuery = contentEl.innerText || contentEl.textContent;
            break;
          }
          prevNode = prevNode.previousElementSibling;
        }
        sendMessage(actionTag, targetQuery.trim());
      });
    });
  } 

  if(role === "assistant"){
    const kgBtn = div.querySelector(".kg-related-chip");
    if(kgBtn){
      kgBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        // 복원된(또는 이전) 메시지의 버튼일 수 있어 우측 패널이 다른 메시지의 근거를 담고 있을 수 있음.
        // → 이 메시지의 근거를 먼저 로드해 lastTopDocs를 맞춘다.
        if(msgId && currentEvidenceAssistantMsgId !== msgId){
          setSelectedAssistantMsg(msgId);
          try { await loadEvidenceByAssistantMsgId(msgId); } catch(_) {}
        }
        // 연결 문서는 백엔드가 top_docs 뒤쪽에 병합해 내려주는데, 기본 표시 개수(5)에 잘려 안 보일 수 있음.
        // → 전부 렌더하고 패널을 펼친 뒤 첫 KG 연결문서로 스크롤/강조해 확실히 보이게 한다.
        if(Array.isArray(lastTopDocs) && lastTopDocs.length){
          topDocsShowN = Math.max(topDocsShowN, lastTopDocs.length);
        }
        evidenceHasDocs = true;
        expandEvidencePanel();      // 접혀 있으면 펼침 (열려 있으면 그대로)
        renderTopDocsFiltered();
        requestAnimationFrame(() => {
          const box = el("topDocs");
          const target = box ? box.querySelector('[data-kg="1"]') : null;
          if(target){
            target.scrollIntoView({ behavior: "smooth", block: "center" });
            target.classList.add("kg-flash");
            setTimeout(() => target.classList.remove("kg-flash"), 1600);
          }
        });
      });
    }
  }

  if(role === "assistant" && msgId && !isPending){
    wireMessageActionBar(div, msgId);
  }

  if(role === "assistant" && msgId){
    div.addEventListener("click", async (e)=>{
      const target = e.target;
      // 💡 details 태그 내부를 클릭할 때는 상위 카드의 클릭 이벤트(증거 보기)가 트리거되지 않도록 방어
      if(target && (target.closest("a") || target.closest("button") || target.closest("details") || target.closest("summary") || target.closest("pre") || target.closest("code"))) return;
      setSelectedAssistantMsg(msgId);
      await loadEvidenceByAssistantMsgId(msgId);
    });
  }

  chat.appendChild(div);
  if(role === "user" && extra) wireQueryInterpretCard(div);
  enhanceRenderedMessage(div);
  chat.scrollTop = chat.scrollHeight;
}

/* ---------- evidence ---------- */
async function loadEvidenceByAssistantMsgId(assistantMsgId){
  if(!currentSessionId) return;
  currentEvidenceAssistantMsgId = assistantMsgId;

  clearEvidencePanels();

  try{
    const data = await apiGet(`/api/artifacts/by-assistant/${encodeURIComponent(assistantMsgId)}?session_id=${encodeURIComponent(currentSessionId)}`);
    const art = data.artifact;
    if(!art) return;

    const docs = extractTopDocsFromArtifact(art);
    lastTopDocs = docs;
    renderTopDocsFiltered();

    if(art.citations){
      lastCitations = art.citations;
      renderCitations(lastCitations);
      // 히스토리 로드 시점엔 citation 개수를 몰라 [n]이 평문으로 남아 있음 → 지금 pill로 변환
      const msgNode = document.querySelector(`.msg.assistant[data-msg-id="${CSS.escape(assistantMsgId)}"]`);
      if(msgNode) enhanceRenderedMessage(msgNode);
    }
  } catch(e){
  }
}

function clearEvidencePanels(){
  lastTopDocs = [];
  lastCitations = null;
  el("topDocs").innerHTML = "";
  el("citations").innerHTML = "";
}

/* ---------- transform RAG hit -> UI topdoc ---------- */
function toTopDoc(hit, idx){
  const src = hit._source || {};
  return {
    rank: (hit._rank || (idx+1)),
    score: hit._score,
    doc_id: src.doc_id,
    chunk_id: src.chunk_id || hit._id,
    title: src.title || "",
    merge_title_content: src.merge_title_content || "",
    additionalField: src.additionalField || {},
    _index: hit._index
  };
}

function isUiTopDocShape(x){
  return !!x && (
    Object.prototype.hasOwnProperty.call(x, "doc_id") ||
    Object.prototype.hasOwnProperty.call(x, "chunk_id") ||
    Object.prototype.hasOwnProperty.call(x, "additionalField")
  );
}

function normalizeTopDocs(rawDocs){
  if(!Array.isArray(rawDocs)) return [];

  return rawDocs.map((d, idx) => {
    if(isUiTopDocShape(d) && !d._source){
      return {
        rank: d.rank || (idx + 1),
        score: d.score,
        doc_id: d.doc_id,
        chunk_id: d.chunk_id,
        title: d.title || "",
        merge_title_content: d.merge_title_content || "",
        additionalField: d.additionalField || {},
        _index: d._index || ""
      };
    }

    return toTopDoc(d, idx);
  });
}

function extractTopDocsFromArtifact(art){
  if(!art) return [];

  if(Array.isArray(art.top_docs) && art.top_docs.length){
    return normalizeTopDocs(art.top_docs);
  }

  const rr = art.rag_response || {};

  if(Array.isArray(rr.top_docs) && rr.top_docs.length){
    return normalizeTopDocs(rr.top_docs);
  }

  const hits1 = (((rr || {}).hits || {}).hits || []);
  if(Array.isArray(hits1) && hits1.length){
    return normalizeTopDocs(hits1);
  }

  const hits2 = (((rr || {}).retrieval || {}).hits || {}).hits || [];
  if(Array.isArray(hits2) && hits2.length){
    return normalizeTopDocs(hits2);
  }

  return [];
}

function stripEnriched(title){
  if(!title) return title;
  return title.replace(/\.enriched(\.eml)?$/i, "").trim();
}

function pickMailMeta(af){
  const mailFrom = af?.mail_from || null;
  const mailDate = af?.mail_date || null;

  const links = af?.report_links || [];
  const edmLinks = Array.isArray(links)
    ? links
        .filter(x => typeof x === "string")
        .map(x => x.trim())
        .filter(x => x.startsWith("http://gw."))
    : [];

  return { mailFrom, mailDate, edmLinks };
}

/* ---------- TopDocs render ---------- */
function renderTopDocsFiltered(){
  const box = el("topDocs");
  box.innerHTML = "";
  const n = Math.max(1, Math.min(topDocsShowN, lastTopDocs.length || 0));
  const docs = (lastTopDocs || []).slice(0, n);

  if (typeof toggleEvidencePanel === "function") toggleEvidencePanel(docs.length > 0);

  docs.forEach((d, i) => {
    const title = stripEnriched(d.title || "(no title)");
    const score = (d.score == null) ? "" : Number(d.score).toFixed(5);
    const meta = pickMailMeta(d.additionalField || {});
    
    let tagsHtml = "";
    if(meta.mailFrom) tagsHtml += `<span class="px-2 py-0.5 bg-surface-container-high dark:bg-[#1f2b4a] text-secondary dark:text-[#94a3b8] text-[9px] rounded">#${escapeHtml(meta.mailFrom)}</span>`;
    if(meta.mailDate) tagsHtml += `<span class="px-2 py-0.5 bg-surface-container-high dark:bg-[#1f2b4a] text-secondary dark:text-[#94a3b8] text-[9px] rounded">#${escapeHtml(meta.mailDate)}</span>`;
    
    // 💡 [Phase 3] KG 연결 근거 배지 (심층분석 근거 문서의 provenance 표시)
    const kgSrc = d.kg_source || (d._index === "kg-related" ? "kg" : "");
    if(kgSrc === "search"){
      tagsHtml += `<span class="px-2 py-0.5 bg-surface-container-high dark:bg-[#1f2b4a] text-secondary dark:text-[#94a3b8] text-[9px] rounded" title="KG 연결 문서가 없어 시맨틱 검색으로 보완된 근거">🔎 검색 보완</span>`;
    }else if(kgSrc && kgSrc !== "db"){
      const tipParts = [];
      if(d.kg_evidence) tipParts.push(d.kg_evidence);
      if(d.kg_confidence != null) tipParts.push(`신뢰도 ${Number(d.kg_confidence).toFixed(2)}`);
      const tip = tipParts.join(" · ") || "지식그래프로 연결된 원본 보고서 문서";
      tagsHtml += `<span class="px-2 py-0.5 bg-rag/10 text-rag-strong dark:text-rag text-[9px] font-semibold rounded" title="${escapeHtml(tip)}">🧩 KG 연결</span>`;
    }

    // 💡 [복구 완료] 분석보고서 URL(edmLinks) 클릭 기능 복구 (이슈 4 해결)
    if(meta.edmLinks && meta.edmLinks.length){
      meta.edmLinks.forEach(u => {
        // transition-colors 제거
        tagsHtml += `<span class="px-2 py-0.5 bg-surface-container-high dark:bg-[#1f2b4a] text-secondary dark:text-[#94a3b8] text-[9px] rounded hover:text-primary dark:hover:text-[#60a5fa] cursor-pointer"><a href="${escapeHtml(u)}" target="_blank" rel="noopener noreferrer">#${escapeHtml(u)}</a></span>`;
      });
    }

    // 💡 KG 연결문서 여부 (연결문서 버튼 스크롤 타깃 표식)
    const isKgDoc = (d._index === "kg-related") || (d.kg_source && d.kg_source !== "db" && d.kg_source !== "search");

    const card = document.createElement("div");
    if(isKgDoc) card.setAttribute("data-kg", "1");
    // 💡 [색상 수정] 다크 모드 배경색(dark:bg-[#0f1a33]) 직접 주입
    card.className = "bg-white dark:bg-[#0f1a33] dark:text-[#e7eefc] rounded-lg p-3 shadow-sm border border-surface-container dark:border-[#1f2b4a] border-l-4 border-l-primary dark:border-l-[#60a5fa] cursor-pointer hover:-translate-y-0.5";
    card.innerHTML = `
      <div class="flex justify-between items-start mb-2">
        <span class="px-2 py-0.5 bg-surface-container-highest dark:bg-[#334155] text-[9px] font-bold rounded">TOP ${escapeHtml(String(d.rank || (i+1)))}</span>
        ${score ? `<span class="text-[10px] font-bold text-primary dark:text-[#60a5fa]">Score: ${escapeHtml(score)}</span>` : ''}
      </div>
      <h3 class="text-[12px] font-bold mb-1 leading-tight line-clamp-2">${escapeHtml(title)}</h3>
      ${d._index ? `<div class="text-[9px] text-secondary dark:text-[#94a3b8] mb-2">🗂️ ${escapeHtml(d._index)}</div>` : ''}
      <div class="flex flex-wrap gap-1 mt-2">${tagsHtml}</div>
    `;
    card.onclick = (e) => {
      // 링크 클릭 시 문서 뷰어 모달이 열리지 않도록 방어
      if(e.target.tagName === 'A') return;
      openDocModal(d, null);
    };
    box.appendChild(card);
  });
}

/* ---------- citations ---------- */
function renderCitations(citations){
  const box = el("citations");
  box.innerHTML = "";
  const ans = (citations && citations.answer) ? citations.answer : [];
  
  if(!ans.length){
    box.innerHTML = `<div class="text-xs text-secondary dark:text-[#94a3b8] p-3 bg-surface-container-low dark:bg-[#101f3f] rounded">(근거 정보 없음)</div>`;
    return;
  }

  ans.forEach((a, idx) => {
    const sentence = (a.sentence || "").trim();
    const cites = a.citations || [];

    // 💡 claim 지원 상태 아이콘 (✓ 근거 확인 / ◐ 부분 / ⚠ 근거 없음)
    const support = (a.support || "").toLowerCase();
    let supIcon = "";
    if(support === "supported")        supIcon = `<span class="cite-support is-good" title="근거 문서로 명확히 뒷받침됨">✓</span>`;
    else if(support === "unsupported") supIcon = `<span class="cite-support is-bad" title="근거 문서에서 확인되지 않은 주장입니다">⚠</span>`;
    else if(support)                   supIcon = `<span class="cite-support is-mid" title="부분적으로만 뒷받침됨">◐</span>`;

    const div = document.createElement("div");
    // 💡 [색상 수정] 다크 모드 배경색 직접 주입
    div.className = "bg-white dark:bg-[#0f1a33] dark:text-[#e7eefc] p-3 border border-surface-container dark:border-[#1f2b4a] rounded-lg mb-3 shadow-sm";
    div.innerHTML = `
      <div class="text-[12px] leading-relaxed mb-2"><span class="font-bold text-primary dark:text-[#60a5fa]">${idx+1}.</span> ${supIcon} ${escapeHtml(sentence)}</div>
      ${cites.length ? `<button class="cite-btn px-2 py-1 bg-surface-container dark:bg-[#1f2b4a] hover:bg-surface-container-high dark:hover:bg-[#334155] rounded text-[10px] font-semibold">근거 문서 보기</button>` : `<div class="text-[10px] text-error/80">연결된 근거 문서 없음</div>`}
      <div class="cite-list hidden mt-3 space-y-2 border-t border-surface-container dark:border-[#1f2b4a] pt-2"></div>
    `;

    const btn = div.querySelector(".cite-btn");
    const list = div.querySelector(".cite-list");

    if(btn) btn.onclick = () => {
      if(list.classList.contains("hidden")){
        list.classList.remove("hidden");
        list.innerHTML = "";
        cites.forEach(c => {
          const quote = (c.quote || "").trim();
          const item = document.createElement("div");
          item.className = "p-2 bg-surface-container-low dark:bg-[#101f3f] border border-surface-container dark:border-[#1f2b4a] border-dashed rounded cursor-pointer hover:bg-surface-container dark:hover:bg-[#1f2b4a]";
          item.innerHTML = `
            <div class="text-[11px] mb-1 font-mono break-words">${quote ? escapeHtml(quote) : '(원본 문서로 이동)'}</div>
            <div class="text-[9px] text-secondary dark:text-[#94a3b8]">${escapeHtml(c.doc_id||"")}</div>
          `;
          item.onclick = () => openDocFromCitation(c.doc_id, c.chunk_id, quote || "");
          list.appendChild(item);
        });
      } else {
        list.classList.add("hidden");
      }
    };
    box.appendChild(div);
  });
}


function openDocFromCitation(docId, chunkId, quote){
  let d = (lastTopDocs || []).find(x => x.doc_id === docId && (x.chunk_id === chunkId));
  if(!d) d = (lastTopDocs || []).find(x => x.doc_id === docId);
  if(!d){
    (window.Toast
      ? window.Toast.warn("현재 Top Docs에 없는 문서입니다.", { title: "원본 조회 불가" })
      : alert("현재 TopDocs에 없는 문서입니다."));
    return;
  }
  openDocModal(d, quote);
}

/* ---------- modal viewer ---------- */
function activateModalTab(name){
  document.querySelectorAll(".modal .tab").forEach(t => {
    const on = (t.dataset.tab === name);
    t.classList.toggle("active", on);
    if(on) {
        // 활성화된 탭 (Primary 색상)
        t.className = "tab active px-4 py-2 rounded text-xs font-bold bg-primary text-on-primary";
    } else {
        // 비활성화된 탭 (다크모드에 맞춰 반전되는 Surface 색상)
        t.className = "tab px-4 py-2 rounded text-xs font-bold bg-surface text-on-surface border border-surface-container hover:bg-surface-container-low transition-colors";
    }
  });
  el("docModalMd").classList.toggle("hidden", name !== "md");
  el("docModalImages").classList.toggle("hidden", name !== "images");
}

function openModal(){
  el("docModal").classList.remove("hidden");
  el("docModal").setAttribute("aria-hidden", "false");
}
function closeModal(){
  el("docModal").classList.add("hidden");
  el("docModal").setAttribute("aria-hidden", "true");
  el("docModalTitle").textContent = "(Document)";
  el("docModalMd").innerHTML = "";
  el("docModalImages").innerHTML = "";
}

function clearMarks(container){
  if(!container) return;
  const marks = container.querySelectorAll("mark.__cite_mark");
  marks.forEach(m=>{
    const parent = m.parentNode;
    if(!parent) return;
    parent.replaceChild(document.createTextNode(m.textContent || ""), m);
    parent.normalize();
  });
}

function normalizeQuoteForSearch(q){
  return (q || "")
    .replace(/\s+/g, " ")
    .replace(/\u00A0/g, " ")
    .trim();
}

function markFirstOccurrence(container, quote){
  if(!container || !quote) return null;

  const q = normalizeQuoteForSearch(quote);
  if(!q) return null;

  const walker = document.createTreeWalker(
    container,
    NodeFilter.SHOW_TEXT,
    {
      acceptNode(node){
        if(!node || !node.nodeValue) return NodeFilter.FILTER_REJECT;
        if(node.nodeValue.trim().length < 2) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    }
  );

  let node;
  while((node = walker.nextNode())){
    const hay = normalizeQuoteForSearch(node.nodeValue);
    const idx = hay.indexOf(q);
    if(idx >= 0){
      const seed = (quote || "").replace(/\s+/g, " ").trim();
      const seedShort = seed.length > 180 ? seed.slice(0, 180).trim() : seed;

      const raw = node.nodeValue;
      const rawNorm = normalizeQuoteForSearch(raw);
      const seedIdxNorm = rawNorm.indexOf(normalizeQuoteForSearch(seedShort));
      if(seedIdxNorm < 0){
        const seedIdxRaw = raw.indexOf(seedShort);
        if(seedIdxRaw >= 0){
          return wrapRangeInSingleTextNode(node, seedIdxRaw, seedIdxRaw + seedShort.length);
        }
      } else {
        const firstToken = seedShort.split(" ").filter(Boolean)[0];
        if(firstToken){
          const near = raw.indexOf(firstToken);
          if(near >= 0){
            const start = near;
            const end = Math.min(raw.length, start + seedShort.length);
            return wrapRangeInSingleTextNode(node, start, end);
          }
        }
      }

      return wrapRangeInSingleTextNode(node, 0, Math.min(node.nodeValue.length, q.length));
    }
  }

  return markAcrossTextNodes(container, q);
}

function wrapRangeInSingleTextNode(textNode, start, end){
  try{
    const len = (textNode.nodeValue || "").length;
    const s = Math.max(0, Math.min(start, len));
    const e = Math.max(0, Math.min(end, len));
    if(e <= s) return null;

    const mid = textNode.splitText(s);
    mid.splitText(e - s);

    const mark = document.createElement("mark");
    mark.className = "__cite_mark";
    mark.dataset.cite = "1";
    mark.textContent = mid.nodeValue;

    mid.parentNode.replaceChild(mark, mid);
    return mark;
  } catch(e){
    return null;
  }
}

function isKeepChar(ch){
  return /[0-9A-Za-z가-힣\-_\/]/.test(ch);
}

function normalizeWithMap(raw){
  const map = [];
  let norm = "";

  for(let i=0; i<raw.length; i++){
    const ch = raw[i];

    if(ch === "\u00A0" || ch === "\u200B" || ch === "\u200C" || ch === "\u200D") continue;

    if(isKeepChar(ch)){
      norm += ch.toLowerCase();
      map.push(i);
    }
  }
  return { norm, map };
}

function buildFlatTextAndNodes(container){
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null);
  const nodes = [];
  const starts = [];
  let flat = "";
  let n;

  while((n = walker.nextNode())){
    nodes.push(n);
    starts.push(flat.length);
    flat += (n.nodeValue || "");
  }
  return { flat, nodes, starts };
}

function markAcrossTextNodes(container, quote){
  if(!quote) return null;

  const { flat, nodes, starts } = buildFlatTextAndNodes(container);

  const { norm: flatNorm, map: flatMap } = normalizeWithMap(flat);
  const { norm: quoteNorm } = normalizeWithMap(String(quote));

  if(!quoteNorm) return null;

  const idx = flatNorm.indexOf(quoteNorm);
  if(idx < 0) return null;

  const rawStart = flatMap[idx];
  const rawEndInclusive = flatMap[idx + quoteNorm.length - 1];
  if(rawStart == null || rawEndInclusive == null) return null;

  const rawEnd = rawEndInclusive + 1;

  return wrapRangeBySplitting(nodes, starts, rawStart, rawEnd);
}

function wrapRangeBySplitting(nodes, starts, startPos, endPos){
  let firstMark = null;

  for(let i = nodes.length - 1; i >= 0; i--){
    const node = nodes[i];
    const nodeStart = starts[i];
    const text = (node.nodeValue || "");
    const nodeEnd = nodeStart + text.length;

    const s = Math.max(startPos, nodeStart);
    const e = Math.min(endPos, nodeEnd);
    if(e <= s) continue;

    const localS = s - nodeStart;
    const localE = e - nodeStart;

    const mark = wrapRangeInSingleTextNode(node, localS, localE);
    if(mark) firstMark = mark;
  }

  return firstMark;
}

function locateTextOffset(nodes, starts, pos){
  for(let i = nodes.length - 1; i >= 0; i--){
    if(pos >= starts[i]){
      const node = nodes[i];
      const offset = pos - starts[i];
      const safeOffset = Math.max(0, Math.min(offset, (node.nodeValue || "").length));
      return { node, offset: safeOffset };
    }
  }
  return null;
}

function highlightInViewer(quote){
  const container = el("docModalMd");
  if(!container || !quote) return;

  clearMarks(container);

  const mark = markFirstOccurrence(container, quote);
  if(mark){
    mark.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
  }
}

// ✨ [업그레이드] 마크다운 전처리 함수 (다중 절단 조건 지원)
function preProcessMarkdown(mdText) {
  let t = String(mdText || "");

  // 1) '[placeholder]' 완벽 제거 (대소문자, 공백 무시)
  t = t.replace(/\[\s*placeholder\s*\]/gi, "");

  // 2) 특정 문자열 이후 텍스트 모두 날리기 (조건 통합 방어)
  const truncRegex = /\.\/images\/\|attachments\/inline|<img\s+src=/i;
  const match = t.match(truncRegex);
  
  if (match) {
      t = t.substring(0, match.index); // 가장 먼저 매칭된 문자열 시작점 앞까지만 남기고 싹둑
  }

  return t;
}

async function openDocModal(d, highlightQuote){
  const title = stripEnriched(d.title || "(no title)");
  el("docModalTitle").textContent = title;

  const af = d.additionalField || {};
  const storage = af.storage || {};
  const assets = af.assets || [];

  const mdRel = storage.parsed_md_rel_path;
  if(mdRel){
    try{
      const rawMdText = await fetch(`/api/view/md?rel=${encodeURIComponent(mdRel)}`, {credentials:"include"}).then(r=>r.text());

      // ✨ 전처리 파이프라인 통과
      const processedText = preProcessMarkdown(rawMdText);
      const mdNoMeta = stripLeadingMailMetaBlock(processedText);
      const mdWithImgs = injectImagesIntoMarkdown(mdNoMeta, assets);

      el("docModalMd").innerHTML = renderDocumentMarkdown(mdWithImgs);
      if(highlightQuote) highlightInViewer(highlightQuote);

    } catch(e){
      el("docModalMd").innerHTML = `<pre>${escapeHtml(String(e))}</pre>`;
    }
  } else {
    el("docModalMd").innerHTML = `<pre>(parsed_md_rel_path 없음)</pre>`;
  }

  const imgBox = el("docModalImages");
  imgBox.innerHTML = "";
  if(assets && assets.length > 0){
    assets.forEach(a => {
      const p = a.path;
      if(!p) return;
      const wrap = document.createElement("div");
      wrap.className = "img-wrap";
      const img = document.createElement("img");
      img.src = `/api/view/asset?rel=${encodeURIComponent(p)}`;
      img.title = a.file_name || p;

      img.addEventListener("click", () => showImgPreview(img.src));
      wrap.appendChild(img);
      imgBox.appendChild(wrap);
    });
  } else {
    imgBox.innerHTML = `<pre>(assets 없음)</pre>`;
  }

  autoLinkPlainUrls(el("docModalMd"));

  activateModalTab("md");
  openModal();
}

/* ---------- full screen image preview ---------- */
function showImgPreview(src){
  const pv = el("imgPreview");
  pv.innerHTML = `<img src="${src}" />`;
  pv.classList.remove("hidden");
  pv.onclick = hideImgPreview;
}
function hideImgPreview(){
  const pv = el("imgPreview");
  pv.classList.add("hidden");
  pv.innerHTML = "";
}

/* ---------- send message (sendMessage) ---------- */
function setSendButtonState(state){
  const btn = el("sendBtn");
  if(!btn) return;
  const icon = btn.querySelector(".material-symbols-outlined");
  if(state === "sending"){
    btn.classList.add("is-stop");
    btn.disabled = false;
    btn.setAttribute("aria-label", "응답 중단");
    btn.title = "응답 중단";
    if(icon) icon.textContent = "stop";
  } else {
    btn.classList.remove("is-stop");
    btn.disabled = false;
    btn.setAttribute("aria-label", "전송");
    btn.title = "전송 (Enter)";
    if(icon) icon.textContent = "send";
  }
}

function abortActiveStream(){
  if(activeStreamController){
    try { activeStreamController.abort(); } catch(_){}
    activeStreamController = null;
  }
}

/* ---------- composer toolbar (intent mode + index multiselect) ---------- */
const LS_MODE = "rs_forced_mode";
const LS_INDEXES = "rs_selected_indexes";

function loadComposerState(){
  try {
    const m = localStorage.getItem(LS_MODE);
    if(m === "[DB_ANALYSIS]" || m === "[RAG_KNOWLEDGE]" || m === "") forcedMode = m;
  } catch(_) {}
  try {
    const raw = localStorage.getItem(LS_INDEXES);
    if(raw){
      const arr = JSON.parse(raw);
      if(Array.isArray(arr) && arr.length > 0) selectedIndexNames = arr;
    }
  } catch(_) {}
}

function persistMode(){
  try { localStorage.setItem(LS_MODE, forcedMode || ""); } catch(_) {}
}
function persistIndexes(){
  try {
    if(selectedIndexNames && selectedIndexNames.length > 0){
      localStorage.setItem(LS_INDEXES, JSON.stringify(selectedIndexNames));
    } else {
      localStorage.removeItem(LS_INDEXES);
    }
  } catch(_) {}
}

function getEffectiveIndexNames(){
  if(selectedIndexNames && selectedIndexNames.length > 0) return selectedIndexNames;
  try {
    const userDefault = localStorage.getItem("rs_default_index");
    if(userDefault) return [userDefault];
  } catch(_) {}
  const d = window.__BOOT__ && window.__BOOT__.defaultIndex;
  return d ? [d] : [];
}

function updateModeChips(){
  const map = { "": "modeAuto", "[DB_ANALYSIS]": "modeDb", "[RAG_KNOWLEDGE]": "modeRag" };
  ["modeAuto","modeDb","modeRag"].forEach(id => {
    const btn = el(id);
    if(!btn) return;
    btn.classList.toggle("is-active", map[forcedMode] === id);
  });
}

function updateIndexLabel(){
  const lbl = el("indexBtnLabel");
  if(!lbl) return;
  const defaultIdx = window.__BOOT__ && window.__BOOT__.defaultIndex;
  if(!selectedIndexNames || selectedIndexNames.length === 0){
    lbl.textContent = defaultIdx || "인덱스";
  } else if(selectedIndexNames.length === 1){
    lbl.textContent = selectedIndexNames[0];
  } else {
    lbl.textContent = `${selectedIndexNames[0]} 외 ${selectedIndexNames.length - 1}개`;
  }
}

function renderIndexPopupList(){
  const host = el("indexPopupList");
  if(!host) return;
  const opts = (window.__BOOT__ && window.__BOOT__.indexOptions) || [];
  const defaultIdx = window.__BOOT__ && window.__BOOT__.defaultIndex;
  if(!opts.length){
    host.innerHTML = `<div class="composer-popup-row" style="opacity:0.6">사용 가능한 인덱스가 없습니다</div>`;
    return;
  }
  const set = new Set(selectedIndexNames || []);
  host.innerHTML = opts.map(name => `
    <label class="composer-popup-row">
      <input type="checkbox" data-idx="${escapeHtml(name)}" ${set.has(name) ? "checked" : ""}/>
      <span class="row-name">${escapeHtml(name)}</span>
      ${name === defaultIdx ? `<span class="row-default">default</span>` : ""}
    </label>
  `).join("");
  host.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    cb.addEventListener("change", () => {
      const checked = Array.from(host.querySelectorAll('input[type="checkbox"]:checked'))
        .map(x => x.getAttribute("data-idx"));
      selectedIndexNames = checked.length ? checked : null;
      persistIndexes();
      updateIndexLabel();
    });
  });
}

function openIndexPopup(){
  const p = el("indexPopup");
  if(!p) return;
  renderIndexPopupList();
  p.classList.remove("hidden");
  const btn = el("indexBtn"); if(btn) btn.setAttribute("aria-expanded","true");
  setTimeout(() => document.addEventListener("click", outsideIndexPopupHandler, true), 0);
}
function closeIndexPopup(){
  const p = el("indexPopup");
  if(!p) return;
  p.classList.add("hidden");
  const btn = el("indexBtn"); if(btn) btn.setAttribute("aria-expanded","false");
  document.removeEventListener("click", outsideIndexPopupHandler, true);
}
function outsideIndexPopupHandler(e){
  const p = el("indexPopup");
  const btn = el("indexBtn");
  if(!p || p.classList.contains("hidden")) return;
  if(p.contains(e.target) || (btn && btn.contains(e.target))) return;
  closeIndexPopup();
}

function setupComposerToolbar(){
  loadComposerState();
  updateModeChips();
  updateIndexLabel();

  [["modeAuto",""], ["modeDb","[DB_ANALYSIS]"], ["modeRag","[RAG_KNOWLEDGE]"]].forEach(([id, mode]) => {
    const btn = el(id);
    if(!btn) return;
    btn.addEventListener("click", () => {
      forcedMode = (forcedMode === mode && mode !== "") ? "" : mode;
      persistMode();
      updateModeChips();
    });
  });
  const indexBtn = el("indexBtn");
  if(indexBtn){
    indexBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const p = el("indexPopup");
      if(p && p.classList.contains("hidden")) openIndexPopup();
      else closeIndexPopup();
    });
  }
  const closeBtn = el("indexCloseBtn");
  if(closeBtn) closeBtn.addEventListener("click", closeIndexPopup);
  const resetBtn = el("indexResetBtn");
  if(resetBtn) resetBtn.addEventListener("click", () => {
    selectedIndexNames = null;
    persistIndexes();
    renderIndexPopupList();
    updateIndexLabel();
  });
}

/* ---------- input aids: slash / @ / ↑ history ---------- */
const SLASH_COMMANDS = [
  { cmd: "/help",  label: "도움말", desc: "키보드 단축키 모달 열기",     icon: "help" },
  { cmd: "/clear", label: "지우기", desc: "현재 채팅 화면 비우기 (세션 유지)", icon: "delete_sweep" },
  { cmd: "/new",   label: "새 세션", desc: "새 대화 시작",                icon: "add_circle" },
];

let _dictTermsCache = null;
let _dictTermsFetching = null;
function loadDictTerms(){
  if(_dictTermsCache) return Promise.resolve(_dictTermsCache);
  if(_dictTermsFetching) return _dictTermsFetching;
  _dictTermsFetching = apiGet("/api/dictionary/terms")
    .then(data => {
      _dictTermsCache = Array.isArray(data && data.terms) ? data.terms : (Array.isArray(data) ? data : []);
      return _dictTermsCache;
    })
    .catch(() => { _dictTermsCache = []; return _dictTermsCache; })
    .finally(() => { _dictTermsFetching = null; });
  return _dictTermsFetching;
}

let _aidActiveIndex = -1;
let _aidRows = [];
function showInputAidPopup(rows, onPick){
  const pop = el("inputAidPopup");
  if(!pop) return;
  _aidRows = rows.slice(0, 8);
  _aidActiveIndex = _aidRows.length ? 0 : -1;
  if(!_aidRows.length){ hideInputAidPopup(); return; }
  pop.innerHTML = _aidRows.map((r, i) => `
    <div class="input-aid-row ${i === _aidActiveIndex ? 'is-active' : ''}" data-aid-idx="${i}">
      <span class="aid-icon"><span class="material-symbols-outlined">${r.icon || "tag"}</span></span>
      <span class="flex-1">
        <div><span class="aid-name">${escapeHtml(r.name || r.cmd || "")}</span>
          ${r.mono ? `<span class="aid-mono ml-2">${escapeHtml(r.mono)}</span>` : ""}
        </div>
        ${r.desc ? `<div class="aid-desc">${escapeHtml(r.desc)}</div>` : ""}
      </span>
    </div>
  `).join("");
  pop.classList.remove("hidden");
  pop.querySelectorAll(".input-aid-row").forEach(rowEl => {
    rowEl.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const idx = parseInt(rowEl.getAttribute("data-aid-idx") || "-1", 10);
      if(idx >= 0 && onPick) onPick(_aidRows[idx]);
      hideInputAidPopup();
    });
  });
}
function hideInputAidPopup(){
  const pop = el("inputAidPopup");
  if(pop) pop.classList.add("hidden");
  _aidActiveIndex = -1;
  _aidRows = [];
}
function moveAidActive(delta){
  if(!_aidRows.length) return;
  _aidActiveIndex = (_aidActiveIndex + delta + _aidRows.length) % _aidRows.length;
  const pop = el("inputAidPopup");
  if(!pop) return;
  pop.querySelectorAll(".input-aid-row").forEach((r, i) => {
    r.classList.toggle("is-active", i === _aidActiveIndex);
  });
}

function handleSlashCommand(cmd){
  if(cmd === "/help"){
    const m = el("shortcutModal");
    if(m){ m.classList.remove("hidden"); m.setAttribute("aria-hidden","false"); }
  } else if(cmd === "/clear"){
    el("chatArea").innerHTML = "";
    if(typeof renderWelcomeScreen === "function") renderWelcomeScreen();
  } else if(cmd === "/new"){
    if(typeof newSession === "function") newSession();
  }
  const input = el("userInput");
  if(input){ input.value = ""; autosizeInput(); }
}

function refreshInputAids(){
  const input = el("userInput");
  if(!input) return;
  const val = input.value || "";
  // slash command suggestions (input starts with "/")
  if(/^\/\w*$/.test(val.trim()) && val.trim() === val){
    const q = val.trim().toLowerCase();
    const matches = SLASH_COMMANDS.filter(c => c.cmd.toLowerCase().startsWith(q));
    if(matches.length){
      showInputAidPopup(matches.map(c => ({
        cmd: c.cmd, name: c.cmd, desc: c.desc, icon: c.icon, mono: c.label
      })), (row) => handleSlashCommand(row.cmd));
      return;
    }
  }
  // @ term autocomplete: detect @<query> at caret position
  const caret = input.selectionStart || val.length;
  const before = val.slice(0, caret);
  const m = before.match(/@([\w가-힣]{0,40})$/);
  if(m){
    const q = m[1].toLowerCase();
    loadDictTerms().then(terms => {
      if(input.selectionStart !== caret) return; // user moved on
      const current = (input.value || "").slice(0, input.selectionStart || 0).match(/@([\w가-힣]{0,40})$/);
      if(!current) { hideInputAidPopup(); return; }
      const matches = terms
        .filter(t => {
          const n = (t.canonical_name || t.display_name || "").toLowerCase();
          if(!n) return false;
          if(!q) return true;
          if(n.includes(q)) return true;
          const aliases = Array.isArray(t.aliases) ? t.aliases : [];
          return aliases.some(a => (a || "").toLowerCase().includes(q));
        })
        .slice(0, 8);
      if(!matches.length){ hideInputAidPopup(); return; }
      const rows = matches.map(t => ({
        name: t.canonical_name || t.display_name || "",
        desc: (t.description || "").slice(0, 80),
        mono: t.term_type || "",
        icon: "book_2",
        insertText: t.canonical_name || t.display_name || ""
      }));
      showInputAidPopup(rows, (row) => {
        const v = input.value || "";
        const cur = (v.slice(0, input.selectionStart || 0)).match(/@([\w가-힣]{0,40})$/);
        if(!cur) return;
        const startIdx = (input.selectionStart || 0) - cur[0].length;
        const newVal = v.slice(0, startIdx) + row.insertText + " " + v.slice(input.selectionStart || 0);
        input.value = newVal;
        const newCaret = startIdx + row.insertText.length + 1;
        input.setSelectionRange(newCaret, newCaret);
        autosizeInput();
        input.focus();
      });
    });
    return;
  }
  hideInputAidPopup();
}

function autosizeInput(){
  const input = el("userInput");
  if(!input) return;
  input.style.height = "auto";
  const max = 200;
  input.style.height = Math.min(input.scrollHeight, max) + "px";
}

async function sendMessage(overrideActionTag = null, specificQuery = null){
  if(isSending) return;
  let rawSendText = "";
  let displayUserText = "";

  if (overrideActionTag) {
    const queryToUse = specificQuery || lastRealUserQuery;
    if (overrideActionTag === "retry") {
      rawSendText = "[DB_ANALYSIS] 이전 검색 결과가 부족하거나 사용자가 더 넓은 범위를 원합니다. 기존에 적용했던 엄격한 일치 조건 (공, 모, 라 등)을 최소화하거나 제거하고, 가장 핵심이 되는 키워드만 사용하여 'LIKE' 검색 위주로 조건을 넓혀서 다음 질문에 대해 다시 쿼리를 작성해줘: " + lastRealUserQuery;
      displayUserText = "🔄 조건을 넓혀서 다시 검색 중...";
    } else {
      rawSendText = overrideActionTag + " " + queryToUse;
      if (overrideActionTag === "[DB_ANALYSIS]") displayUserText = "📊 같은 질문을 DB 통계로 확인하고 있어요...";
      else if (overrideActionTag === "[RAG_KNOWLEDGE]") displayUserText = "📖 같은 질문을 사내 문서에서 찾아보고 있어요...";
      else displayUserText = "🔄 다시 검색 중...";
    }
    lastRealUserQuery = queryToUse;
  } else {
    const typedText = el("userInput").value.trim();
    if(!typedText) return;
    // 화면 표시용: 강제 라우팅 태그는 숨긴다 (혹시 사용자가 직접 입력한 경우도 제거)
    const cleanText = typedText.replace(/^\[(DB_ANALYSIS|RAG_KNOWLEDGE)\]\s*/, "").trim();
    // 백엔드 전송용: 강제 라우팅 태그를 앞에 붙인다 (적절한 에이전트 라우팅에 필요)
    if(forcedMode && !typedText.startsWith("[DB_ANALYSIS]") && !typedText.startsWith("[RAG_KNOWLEDGE]")){
      rawSendText = `${forcedMode} ${cleanText}`;
    } else {
      rawSendText = typedText;
    }
    lastRealUserQuery = cleanText;
    displayUserText = cleanText;
    el("userInput").value = "";
    autosizeInput();
  }

  // 실제 전송이 확정된 시점에 사이드바 자동 펼침 (빈 입력 조기 반환 이후)
  if(typeof setSidebarCollapsed === "function") setSidebarCollapsed(false);

  appendMessage("user", displayUserText, null, null);

  const pendingId = "PENDING_" + Date.now();
  const loadingHtml = `
    <div class="agent-status-wrapper inline-flex items-center gap-3 text-sm text-secondary dark:text-[#94a3b8] font-medium px-2 py-2 mt-2">
       <span class="agent-badge is-default" style="padding:4px 12px 4px 4px;">
         <span class="agent-badge-icon"><span class="material-symbols-outlined animate-spin" style="font-size:14px;">progress_activity</span></span>
         <span class="agent-badge-name">분석 중</span>
       </span>
       <span class="agent-status-text streaming-caret transition-opacity duration-300 opacity-100 tracking-wide">답변 준비를 시작하고 있어요...</span>
    </div>
  `;
  appendMessage("assistant", loadingHtml, null, pendingId);

  isSending = true;
  setSendButtonState("sending");
  el("userInput").disabled = true;

  activeStreamController = new AbortController();

  const payload = {
    session_id: currentSessionId,
    user_text: rawSendText,
    index_names: getEffectiveIndexNames(),
    top_k: (window.__BOOT__ && window.__BOOT__.defaultTopK) || 5,
    filters: null
  };

  try{
    const response = await fetch("/api/chat_stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: activeStreamController.signal
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let done = false;

    const pendNode = document.querySelector(`.msg.assistant[data-msg-id="${pendingId}"]`);
    const statusTextNode = pendNode ? pendNode.querySelector(".agent-status-text") : null;
    let finalRes = null;
    let buf = "";

    const processLine = (line) => {
      const t = (line || "").trim();
      if(!t) return;
      try {
        const parsed = JSON.parse(t);
        if (parsed.type === "step") {
          // 💡 객체형(data.thought)과 메시지형(message) step 모두 안전 처리
          const displayThought = (parsed.data && parsed.data.thought) || parsed.message || "로딩 중...";
          if (statusTextNode && statusTextNode.textContent !== displayThought) {
            statusTextNode.style.opacity = '0';
            setTimeout(() => {
              statusTextNode.textContent = displayThought;
              statusTextNode.style.opacity = '1';
            }, 150);
          }
        } else if (parsed.type === "final") {
          finalRes = parsed.data;
        }
      } catch (e) {
        console.error("Chunk parse error:", e);
      }
    };

    while (!done) {
      const { value, done: readerDone } = await reader.read();
      done = readerDone;
      if (value) {
        // ⚠️ NDJSON 한 줄(특히 대용량 final)이 여러 read() 조각으로 쪼개져 도착할 수 있으므로
        // 반드시 라인 버퍼로 이어붙인 뒤 완전한 줄만 파싱한다. (잘린 조각을 그대로 parse하면
        // final을 통째로 잃어 답변이 빈 화면으로 남는 버그가 있었음)
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop(); // 마지막 불완전 조각은 다음 read와 이어붙임
        for (const line of lines) processLine(line);
      }
    }
    buf += decoder.decode(); // 디코더 잔여 바이트 flush
    if (buf.trim()) processLine(buf); // 개행 없이 끝난 마지막 라인 처리

    if (pendNode) pendNode.remove(); 
    
    if (finalRes) {
        currentSessionId = finalRes.session_id;

        const userMsgs = Array.from(document.querySelectorAll(".msg.user"));
        const lastUserMsg = userMsgs[userMsgs.length - 1];
        const innerBubble = lastUserMsg ? lastUserMsg.querySelector(".user-bubble-inner") : null;

        if(innerBubble && !innerBubble.querySelector(".query-interpret-card")){
          const html = buildQueryInterpretationCard({
            rewritten_query: finalRes.rewritten_query,
            normalized_query: finalRes.normalized_query,
            expanded_query: finalRes.expanded_query,
            detected_terms: finalRes.detected_terms || []
          });
          if(html){
            innerBubble.insertAdjacentHTML("beforeend", html);
            wireQueryInterpretCard(innerBubble);
          }
        }

        const extraData = {
          intent: finalRes.intent,
          suggested_actions: finalRes.suggested_actions,
          agent_steps: finalRes.agent_steps,
          verification: finalRes.verification || null,
          related_docs: finalRes.related_docs || []
        };

        // 인용 pill 변환(convertCitationPills)이 렌더 시점에 citation 개수를 참조하므로
        // appendMessage보다 먼저 세팅해야 한다.
        currentEvidenceAssistantMsgId = finalRes.assistant_msg_id || null;
        lastTopDocs = finalRes.top_docs || [];
        lastCitations = finalRes.citations || {answer:[], final:finalRes.assistant_text};

        appendMessage("assistant", finalRes.assistant_text, new Date().toISOString(), finalRes.assistant_msg_id || null, extraData);
        setSelectedAssistantMsg(finalRes.assistant_msg_id || "");

        renderTopDocsFiltered();
        renderCitations(lastCitations);

        await refreshSessions();
    } else {
      // 2차 방어: 스트림은 끝났지만 final을 못 받은 경우 — 답변은 이미 DB에 저장돼 있으므로
      // 빈 화면 대신 저장된 메시지를 다시 불러와 자동 복구한다 (수동 F5와 동일 효과).
      console.error("[chat] final chunk missing — recovering from persisted messages");
      await refreshSessions();
      if (currentSessionId) {
        await loadSession(currentSessionId);
        if (window.Toast) window.Toast.warn("연결이 불안정해 생성된 답변을 다시 불러왔어요.");
      } else {
        appendMessage("assistant",
          `<div class="rounded-xl border border-error/30 bg-error/5 text-error p-4 text-sm">
             응답을 받는 중 연결이 끊겼습니다. 좌측 대화 목록에서 방금 대화를 선택하면 생성된 답변을 확인할 수 있어요.
           </div>`, new Date().toISOString(), null);
      }
    }

  } catch(e){
    const aborted = (e && (e.name === "AbortError" || String(e).includes("aborted")));
    const pend = document.querySelector(`.msg.assistant[data-msg-id="${pendingId}"]`);
    if(aborted){
      if(pend) pend.remove();
      if(window.Toast) window.Toast.info("응답을 중단했습니다.");
    } else {
      const retryId = "RETRY_" + Date.now();
      const errBlock = `
        <div class="rounded-xl border border-error/30 bg-error/5 text-error p-4 text-sm">
          <div class="font-bold mb-1 flex items-center gap-1">
            <span class="material-symbols-outlined" style="font-size:16px;">error</span>
            응답 생성 중 오류가 발생했습니다
          </div>
          <div class="text-xs opacity-80 mb-2">${escapeHtml(String(e).slice(0, 280))}</div>
          <button type="button" data-retry-id="${retryId}" class="msg-action-btn" style="border-color:currentColor;">
            <span class="material-symbols-outlined">refresh</span><span>다시 시도</span>
          </button>
        </div>`;
      if(pend){
        const contentDiv = pend.querySelector(".content") || pend;
        contentDiv.innerHTML = errBlock;
      } else {
        appendMessage("assistant", errBlock, new Date().toISOString(), null);
      }
      // wire retry
      setTimeout(() => {
        const btn = document.querySelector(`[data-retry-id="${retryId}"]`);
        if(btn){
          btn.addEventListener("click", () => {
            const failedQuery = (lastRealUserQuery || "").trim();
            if(!failedQuery) return;
            // restore input + clear error block by re-sending
            if(pend) pend.remove();
            sendMessage(null, failedQuery);
          });
        }
      }, 50);
    }
  } finally {
    activeStreamController = null;
    isSending = false;
    setSendButtonState("idle");
    el("userInput").disabled = false;
    el("userInput").focus();
  }
}

/* ---------- topdocs show N ---------- */
function applyTopDocsN(){
  const v = parseInt(el("topDocsShowN").value || "5", 10);
  if(!Number.isFinite(v) || v < 1) return;
  topDocsShowN = v;
  renderTopDocsFiltered();
}

/* ---------- announcements (login notice) ---------- */
async function showAnnouncementsOnLoad(){
  let items = [];
  try {
    const data = await apiGet("/api/announcements/active");
    items = (data && data.items) || [];
  } catch(_) { return; }

  const now = Date.now();
  // '일주일간 보지 않기'로 숨긴 공지는 제외 (단, important는 항상 표시)
  const visible = items.filter(it => {
    if(it.important) return true;
    try {
      const until = parseInt(localStorage.getItem("rs_ann_dismiss_" + it.id) || "0", 10);
      return !(until && now < until);
    } catch(_) { return true; }
  });
  if(!visible.length) return;
  renderAnnouncementModal(visible);
}

function renderAnnouncementModal(items){
  if(document.getElementById("annModal")) return;

  const cards = items.map(it => `
    <div class="ann-item" data-ann-id="${escapeHtml(it.id)}">
      <div class="ann-item-head">
        ${it.important ? '<span class="ann-badge">중요</span>' : ''}
        <h3>${escapeHtml(it.title)}</h3>
      </div>
      <div class="ann-body">${escapeHtml(it.body).replace(/\n/g, "<br>")}</div>
      ${it.important ? '' : `<button class="ann-dismiss-btn" data-ann-id="${escapeHtml(it.id)}" type="button">일주일간 보지 않기</button>`}
    </div>
  `).join("");

  const modal = document.createElement("div");
  modal.id = "annModal";
  modal.className = "ann-modal";
  modal.innerHTML = `
    <div class="ann-backdrop"></div>
    <div class="ann-panel" role="dialog" aria-modal="true" aria-label="공지사항">
      <div class="ann-panel-head">
        <span class="material-symbols-outlined">campaign</span>
        <span class="ann-panel-title">공지사항</span>
        <button class="ann-close" type="button" aria-label="닫기"><span class="material-symbols-outlined">close</span></button>
      </div>
      <div class="ann-scroll">${cards}</div>
    </div>`;
  document.body.appendChild(modal);

  const close = () => modal.remove();
  modal.querySelector(".ann-close").addEventListener("click", close);
  modal.querySelector(".ann-backdrop").addEventListener("click", close);
  modal.querySelectorAll(".ann-dismiss-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-ann-id");
      try { localStorage.setItem("rs_ann_dismiss_" + id, String(Date.now() + 7 * 24 * 60 * 60 * 1000)); } catch(_){}
      const item = modal.querySelector(`.ann-item[data-ann-id="${CSS.escape(id)}"]`);
      if(item) item.remove();
      if(!modal.querySelector(".ann-item")) close();
    });
  });
}

/* ---------- init ---------- */
document.addEventListener("DOMContentLoaded", async () => {
  if (typeof configureMarked === "function") configureMarked();

  // ❌ 테마 초기화, 테마 토글, 로그아웃 로직 완벽히 삭제함 (base.html에서 전역 처리)

  // 사이드바 토글은 setupSidebarToggle()에서 일괄 처리 (setSidebarCollapsed 기반)

  // 모달 제어
  el("docModalClose").onclick = closeModal;
  el("docModalBackdrop").onclick = closeModal;
  document.querySelectorAll(".modal .tab").forEach(t => {
    t.onclick = () => activateModalTab(t.dataset.tab);
  });

  // 세션 및 전송
  el("newSession").onclick = newSession;
  el("sendBtn").onclick = () => {
    if(isSending){ abortActiveStream(); return; }
    sendMessage();
  };
  setSendButtonState("idle");

  el("userInput").addEventListener("keydown", (e)=>{
    // IME composition (한글 조합) 중에는 Enter를 가로채지 않는다
    if(e.isComposing || e.keyCode === 229) return;
    // 입력 보조 팝업이 열려있을 때 ↑↓/Enter/Esc 처리
    const aidPop = el("inputAidPopup");
    const aidOpen = aidPop && !aidPop.classList.contains("hidden");
    if(aidOpen){
      if(e.key === "ArrowDown"){ e.preventDefault(); moveAidActive(1); return; }
      if(e.key === "ArrowUp"){ e.preventDefault(); moveAidActive(-1); return; }
      if(e.key === "Escape"){ e.preventDefault(); hideInputAidPopup(); return; }
      if(e.key === "Enter" && !e.shiftKey){
        // pick the active row
        if(_aidActiveIndex >= 0 && _aidRows[_aidActiveIndex]){
          e.preventDefault();
          const row = aidPop.querySelector(`.input-aid-row[data-aid-idx="${_aidActiveIndex}"]`);
          if(row){
            // simulate mousedown handler
            row.dispatchEvent(new MouseEvent("mousedown"));
          }
          return;
        }
      }
    }
    // ↑ 단축: 입력이 비어있을 때 이전 질문 호출
    if(e.key === "ArrowUp" && !aidOpen){
      const input = el("userInput");
      if(input && !input.value && lastRealUserQuery){
        e.preventDefault();
        input.value = lastRealUserQuery;
        autosizeInput();
        input.setSelectionRange(input.value.length, input.value.length);
        return;
      }
    }
    if(e.key === "Enter"){
      if(e.shiftKey || e.altKey) return;
      e.preventDefault();
      sendMessage();
    }
  });

  el("userInput").addEventListener("input", () => {
    autosizeInput();
    refreshInputAids();
  });
  el("userInput").addEventListener("blur", () => {
    setTimeout(hideInputAidPopup, 150);
  });

  setupComposerToolbar();

  // ─── Global keyboard shortcuts ───────────────────────────────
  const openShortcutModal = () => {
    const m = el("shortcutModal");
    if(!m) return;
    m.classList.remove("hidden");
    m.setAttribute("aria-hidden", "false");
  };
  const closeShortcutModal = () => {
    const m = el("shortcutModal");
    if(!m) return;
    m.classList.add("hidden");
    m.setAttribute("aria-hidden", "true");
  };
  const closeBtn = el("shortcutModalClose");
  if(closeBtn) closeBtn.addEventListener("click", closeShortcutModal);
  const backdrop = el("shortcutModalBackdrop");
  if(backdrop) backdrop.addEventListener("click", closeShortcutModal);

  document.addEventListener("keydown", (e) => {
    const cmd = e.ctrlKey || e.metaKey;
    // Esc: close modal / abort stream / close img preview
    if(e.key === "Escape"){
      const docModalOpen = el("docModal") && !el("docModal").classList.contains("hidden");
      const shortcutModalOpen = el("shortcutModal") && !el("shortcutModal").classList.contains("hidden");
      const imgPreviewOpen = el("imgPreview") && !el("imgPreview").classList.contains("hidden");
      const indexPopupOpen = el("indexPopup") && !el("indexPopup").classList.contains("hidden");
      if(indexPopupOpen){ closeIndexPopup(); return; }
      if(shortcutModalOpen){ closeShortcutModal(); return; }
      if(docModalOpen){ if(typeof closeModal === "function") closeModal(); return; }
      if(imgPreviewOpen){
        el("imgPreview").classList.add("hidden");
        el("imgPreview").innerHTML = "";
        return;
      }
      if(isSending){ abortActiveStream(); return; }
      return;
    }
    if(!cmd) return;
    // Ctrl/Cmd+K — focus session search
    if(e.key.toLowerCase() === "k"){
      e.preventDefault();
      const inp = el("sessionSearchInput");
      if(inp){ inp.focus(); inp.select(); }
      return;
    }
    // Ctrl/Cmd+N — new session
    if(e.key.toLowerCase() === "n"){
      e.preventDefault();
      if(typeof newSession === "function") newSession();
      return;
    }
    // Ctrl/Cmd+/ — help modal
    if(e.key === "/"){
      e.preventDefault();
      openShortcutModal();
      return;
    }
    // Ctrl/Cmd+\ — sidebar toggle
    if(e.key === "\\"){
      e.preventDefault();
      const sb = el("sidebar");
      if(sb){
        sb.classList.toggle("collapsed");
        const tBtn = el("toggleSidebar");
        if(tBtn) tBtn.querySelector("span").textContent = sb.classList.contains("collapsed") ? "side_navigation" : "menu_open";
      }
      return;
    }
  });

  const sessionSearch = el("sessionSearchInput");
  if(sessionSearch){
    sessionSearch.addEventListener("input", applySessionFilter);
    sessionSearch.addEventListener("keydown", (e) => {
      if(e.key === "Escape"){
        sessionSearch.value = "";
        applySessionFilter();
      }
    });
  }

  // 패널 조절기 초기화 (오류 방지를 위해 존재 여부 체크)
  if (typeof setupSidebarToggle === "function") setupSidebarToggle();
  if (typeof setupPanelMaxButtons === "function") setupPanelMaxButtons();
  
  if(el("resizer1")) setupVerticalResizer("resizer1", "#sidebar", ".main");
  // 우측 패널 선택자를 기존 ".right"에서 Tailwind가 적용된 "#rightPanel"로 수정!
  if(el("resizer2")) setupVerticalResizer("resizer2", ".main", "#rightPanel");
  // Top Documents / Sentence Citations는 균등 50/50 + 각자 스크롤 (가로 리사이저 제거)

  // 근거 패널 접기/펴기
  if(el("evidenceCollapse")) el("evidenceCollapse").addEventListener("click", collapseEvidencePanel);
  if(el("evidenceRail")) el("evidenceRail").addEventListener("click", expandEvidencePanel);

  await refreshSessions();
  newSession();

  // 대문(초기 로드): 사이드바를 접어 채팅을 중앙에 크게 보여줌.
  // 세션 선택 또는 첫 메시지 전송 시 setSidebarCollapsed(false)로 자동 펼쳐짐.
  setSidebarCollapsed(true);

  // 로그인 후 공지 팝업
  showAnnouncementsOnLoad();
});