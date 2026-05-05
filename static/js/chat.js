let currentSessionId = null;

// 현재 선택된 “assistant msg_id” 기준 evidence
let currentEvidenceAssistantMsgId = null;

// 최근 로드된 evidence 데이터 (topdocs/citations)
let lastTopDocs = [];
let lastCitations = null;

// UI에서 보여줄 topdocs 개수
let topDocsShowN = 5;

// 유저의 마지막 질문을 저장
let lastRealUserQuery = "";

function el(id){ return document.getElementById(id); }

// 문서 검색 결과 유무에 따라 우측 패널을 열고 닫는 함수
function toggleEvidencePanel(hasDocs) {
  const rightPanel = el("rightPanel");
  const resizer2 = el("resizer2");
  if (!rightPanel) return;

  if (hasDocs) {
    rightPanel.classList.remove("hidden");
    if(resizer2) resizer2.classList.remove("hidden");
  } else {
    rightPanel.classList.add("hidden");
    if(resizer2) resizer2.classList.add("hidden");
  }
}

async function apiGet(url){
  const r = await fetch(url, {credentials:"include"});
  if(!r.ok) throw new Error(await r.text());
  return await r.json();
}
async function apiPost(url, body){
  const r = await fetch(url, {
    method:"POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(body),
    credentials:"include"
  });
  if(!r.ok) throw new Error(await r.text());
  return await r.json();
}

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

  // 인용구 변환: [1], [2]를 citation-pill로 교체
  rendered = rendered.replace(/\[(\d+)\]/g, '<span class="citation-pill" onclick="openDocFromCitationIndex($1)">$1</span>');

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

function enhanceRenderedMessage(scope){
  if(!scope) return;

  const contentNodes = scope.querySelectorAll(".content.markdown-body");
  contentNodes.forEach(node => {
    autoLinkPlainUrls(node);
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
function setupSidebarToggle(){
  el("toggleSidebar").onclick = () => {
    const sb = el("sidebar");
    sb.classList.toggle("collapsed");
    el("toggleSidebar").textContent = sb.classList.contains("collapsed") ? "»" : "«";
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
async function refreshSessions(){
  const data = await apiGet("/api/sessions");
  renderSessions(data.sessions || []);
}

function renderSessions(sessions){
  const box = el("sessionList");
  box.innerHTML = "";
  sessions.forEach(s => {
    const div = document.createElement("div");
    // transition-all 제거
    div.className = "session-card group flex flex-col gap-1 p-3 mx-2 mb-2 rounded-xl bg-surface-container-low hover:bg-surface-container border border-surface-container cursor-pointer";
    div.innerHTML = `
      <div class="flex items-center justify-between gap-2 overflow-hidden">
        <div class="session-title text-xs font-bold text-on-surface truncate flex-1">${escapeHtml(s.title || "Untitled")}</div>
        <button class="session-delete-btn icon-btn danger opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-error/10 text-error shrink-0" title="Delete">
          <span class="material-symbols-outlined text-[14px]">delete</span>
        </button>
      </div>
      <div class="session-date text-[10px] text-secondary truncate">${escapeHtml(s.updated_at || "")}</div>
    `;

    div.addEventListener("click", (ev) => {
      if(ev.target && ev.target.closest("button")) return;
      loadSession(s.session_id);
    });

    const deleteBtn = div.querySelector("button");
    if(deleteBtn) {
      deleteBtn.addEventListener("click", async (ev) => {
        ev.stopPropagation(); 
        if(!confirm("이 대화를 목록에서 제거할까요?")) return;
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

async function loadSession(sessionId){
  currentSessionId = sessionId;
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
      if (m.intent || (m.suggested_actions && m.suggested_actions.length > 0)) {
        extra = {
          intent: m.intent,
          suggested_actions: m.suggested_actions,
          agent_steps: m.agent_steps
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
  el("chatArea").innerHTML = "";
  clearEvidencePanels();

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
  const div = document.createElement("div");
  
  div.className = role === "user" 
    ? "msg user flex justify-end mb-6 w-full" 
    : "msg assistant flex justify-start mb-8 w-full";
  
  if(msgId) div.dataset.msgId = msgId;

  let extraHtml = "";
  let intentHtml = "";
  let chipsHtml = "";
  let stepsHtml = "";

  if(role === "user" && extra) {
    extraHtml = buildQueryInterpretationCard(extra); 
  }

  if(role === "assistant" && extra) {
    let agentName = "Intellectual Curator";
    let agentIcon = "robot_2";
    let agentColor = "text-secondary dark:text-[#94a3b8]";

    if(extra.intent) {
      if(extra.intent === "DB_ANALYSIS") { agentName = "DB Stats Agent"; agentIcon = "monitoring"; agentColor = "text-primary dark:text-[#60a5fa]"; }
      else if(extra.intent === "RAG_KNOWLEDGE") { agentName = "Document Search Agent"; agentIcon = "description"; agentColor = "text-primary dark:text-[#60a5fa]"; }
      else if(extra.intent === "HYBRID_DB_RAG") { agentName = "Hybrid Analysis Agent"; agentIcon = "sync"; agentColor = "text-[#b45309] dark:text-[#fbbf24]"; }
      
      intentHtml = `
        <div class="flex items-center gap-3 mb-4">
            <div class="w-8 h-8 rounded bg-surface-container-high dark:bg-[#1f2b4a] flex items-center justify-center">
                <span class="material-symbols-outlined ${agentColor} text-sm">${agentIcon}</span>
            </div>
            <div>
                <span class="text-xs font-bold font-headline ${agentColor}">${agentName}</span>
                <span class="mx-2 text-[10px] text-outline-variant dark:text-[#475569]">•</span>
                <span class="text-[10px] text-outline-variant dark:text-[#475569]">${escapeHtml(formatTimeForUI(metaText))}</span>
            </div>
        </div>`;
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

    if(extra.suggested_actions && extra.suggested_actions.length > 0) {
      const chips = extra.suggested_actions.map(chip => {
        if (chip.disabled) return `<button class="px-4 py-2 bg-surface-container dark:bg-[#1f2b4a] text-outline dark:text-[#94a3b8] rounded-full text-[11px] font-semibold flex items-center gap-2 cursor-not-allowed opacity-60" disabled><span class="material-symbols-outlined text-sm">block</span> ${escapeHtml(chip.label)}</button>`;
        return `<button class="action-chip px-4 py-2 border border-outline-variant dark:border-[#475569] hover:bg-surface-container dark:hover:bg-[#1f2b4a] dark:text-[#e7eefc] rounded-full text-[11px] font-semibold flex items-center gap-2 hover:-translate-y-0.5" data-action="${escapeHtml(chip.action)}"><span class="material-symbols-outlined text-sm">bolt</span> ${escapeHtml(chip.label)}</button>`;
      }).join("");
      chipsHtml = `<div class="pt-4 mt-4 border-t border-surface-container dark:border-[#1f2b4a] flex flex-wrap items-center gap-3">${chips}</div>`;
    }
  }

  if (role === "user") {
    div.innerHTML = `
      <div class="max-w-[85%] flex flex-col items-end">
        <div class="user-bubble-inner bg-primary text-on-primary p-4 rounded-2xl rounded-tr-none shadow-sm flex flex-col gap-3 w-full">
          <div class="content text-sm leading-relaxed whitespace-pre-wrap">${escapeHtml(content)}</div>
          ${extraHtml}
        </div>
        <div class="text-[10px] text-outline-variant dark:text-[#94a3b8] mt-1">${escapeHtml(formatTimeForUI(metaText))}</div>
      </div>`;
  } else {
    div.innerHTML = `
      <div class="assistant-card w-full max-w-[90%] bg-white dark:bg-[#0f1a33] border border-surface-container dark:border-[#1f2b4a] rounded-2xl p-6 hover:shadow-md cursor-pointer group/ai-card">
        ${intentHtml}
        <div class="pl-11">
            ${stepsHtml}
            <div class="content markdown-body text-sm leading-relaxed text-on-surface dark:text-[#e7eefc]">
                ${renderMessageContent(role, content)}
            </div>
            ${chipsHtml}
        </div>
      </div>`;
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
    
    // 💡 [복구 완료] 분석보고서 URL(edmLinks) 클릭 기능 복구 (이슈 4 해결)
    if(meta.edmLinks && meta.edmLinks.length){
      meta.edmLinks.forEach(u => {
        // transition-colors 제거
        tagsHtml += `<span class="px-2 py-0.5 bg-surface-container-high dark:bg-[#1f2b4a] text-secondary dark:text-[#94a3b8] text-[9px] rounded hover:text-primary dark:hover:text-[#60a5fa] cursor-pointer"><a href="${escapeHtml(u)}" target="_blank" rel="noopener noreferrer">#${escapeHtml(u)}</a></span>`;
      });
    }

    const card = document.createElement("div");
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

    const div = document.createElement("div");
    // 💡 [색상 수정] 다크 모드 배경색 직접 주입
    div.className = "bg-white dark:bg-[#0f1a33] dark:text-[#e7eefc] p-3 border border-surface-container dark:border-[#1f2b4a] rounded-lg mb-3 shadow-sm";
    div.innerHTML = `
      <div class="text-[12px] leading-relaxed mb-2"><span class="font-bold text-primary dark:text-[#60a5fa]">${idx+1}.</span> ${escapeHtml(sentence)}</div>
      <button class="cite-btn px-2 py-1 bg-surface-container dark:bg-[#1f2b4a] hover:bg-surface-container-high dark:hover:bg-[#334155] rounded text-[10px] font-semibold">근거 문서 보기</button>
      <div class="cite-list hidden mt-3 space-y-2 border-t border-surface-container dark:border-[#1f2b4a] pt-2"></div>
    `;

    const btn = div.querySelector(".cite-btn");
    const list = div.querySelector(".cite-list");

    btn.onclick = () => {
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
    alert("현재 TopDocs에 없는 문서입니다. (다음 개선: doc_id로 재조회)");
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
async function sendMessage(overrideActionTag = null, specificQuery = null){
  let rawSendText = "";     
  let displayUserText = ""; 

  if (overrideActionTag) {
    const queryToUse = specificQuery || lastRealUserQuery;
    if (overrideActionTag === "retry") {
      rawSendText = "[DB_ANALYSIS] 이전 검색 결과가 부족하거나 사용자가 더 넓은 범위를 원합니다. 기존에 적용했던 엄격한 일치 조건 (공, 모, 라 등)을 최소화하거나 제거하고, 가장 핵심이 되는 키워드만 사용하여 'LIKE' 검색 위주로 조건을 넓혀서 다음 질문에 대해 다시 쿼리를 작성해줘: " + lastRealUserQuery;
      displayUserText = "🔄 조건을 넓혀서 다시 검색 중...";
    } else {
      rawSendText = overrideActionTag + " " + queryToUse;
      if (overrideActionTag === "[DB_ANALYSIS]") displayUserText = "📊 DB 통계 Agent 호출 중...";
      else if (overrideActionTag === "[RAG_KNOWLEDGE]") displayUserText = "📖 문서 검색 Agent 호출 중...";
      else displayUserText = "🔄 다시 검색 중...";
    }
    lastRealUserQuery = queryToUse;
  } else {
    rawSendText = el("userInput").value.trim();
    if(!rawSendText) return;
    lastRealUserQuery = rawSendText;
    displayUserText = rawSendText;
    el("userInput").value = "";
  }

  appendMessage("user", displayUserText, null, null);

  const pendingId = "PENDING_" + Date.now();
  const loadingHtml = `
    <div class="agent-status-wrapper flex items-center gap-2 text-sm text-secondary dark:text-[#94a3b8] font-medium px-2 py-2 mt-2">
       <span class="material-symbols-outlined text-[18px] animate-spin">progress_activity</span>
       <span class="agent-status-text transition-opacity duration-300 opacity-100 tracking-wide">시스템 에이전트 연결 중...</span>
    </div>
  `;
  appendMessage("assistant", loadingHtml, null, pendingId);

  el("sendBtn").disabled = true;
  el("userInput").disabled = true;

  const payload = {
    session_id: currentSessionId,
    user_text: rawSendText,
    index_names: [window.__BOOT__.defaultIndex],
    top_k: 5,
    filters: null
  };

  try{
    const response = await fetch("/api/chat_stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let done = false;

    const pendNode = document.querySelector(`.msg.assistant[data-msg-id="${pendingId}"]`);
    const statusTextNode = pendNode ? pendNode.querySelector(".agent-status-text") : null;
    let finalRes = null;

    while (!done) {
      const { value, done: readerDone } = await reader.read();
      done = readerDone;
      if (value) {
        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split("\n").filter(line => line.trim() !== "");
        
        for (const line of lines) {
          try {
            const parsed = JSON.parse(line);
            
            if (parsed.type === "step") {
              // 💡 객체로 변경된 데이터에서 'thought'만 추출하여 라이브 로딩 텍스트로 사용
              const displayThought = parsed.data.thought || parsed.message || "로딩 중...";
              
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
        }
      }
    }

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
          agent_steps: finalRes.agent_steps
        };

        appendMessage("assistant", finalRes.assistant_text, new Date().toISOString(), finalRes.assistant_msg_id || null, extraData);
        setSelectedAssistantMsg(finalRes.assistant_msg_id || "");

        currentEvidenceAssistantMsgId = finalRes.assistant_msg_id || null;
        lastTopDocs = finalRes.top_docs || [];
        renderTopDocsFiltered();

        lastCitations = finalRes.citations || {answer:[], final:finalRes.assistant_text};
        renderCitations(lastCitations);

        await refreshSessions();
    }

  } catch(e){
    const pend = document.querySelector(`.msg.assistant[data-msg-id="${pendingId}"]`);
    if(pend){
      const contentDiv = pend.querySelector(".content") || pend;
      contentDiv.innerHTML = `<div class="text-error p-4">ERROR: ${escapeHtml(String(e))}</div>`;
    } else {
      appendMessage("assistant", "ERROR: " + String(e), new Date().toISOString(), null);
    }
  } finally {
    el("sendBtn").disabled = false;
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

/* ---------- init ---------- */
document.addEventListener("DOMContentLoaded", async () => {
  if (typeof configureMarked === "function") configureMarked();

  // 테마 초기화
  const saved = localStorage.getItem("theme") || "light";
  if (saved === "dark") document.documentElement.classList.add("dark");

  // 테마 토글
  const themeBtn = el("themeToggleGlobal");
  if(themeBtn){
      themeBtn.onclick = () => {
          // html 태그에 dark 클래스 토글
          const isDark = document.documentElement.classList.toggle("dark");
          // 아이콘 변경 (해 <-> 달)
          themeBtn.querySelector('span').textContent = isDark ? "dark_mode" : "light_mode";
          localStorage.setItem("theme", isDark ? "dark" : "light");
      };
  }

  // 사이드바 토글
  const sidebarBtn = el("toggleSidebar");
  if(sidebarBtn) {
      sidebarBtn.onclick = () => {
          const sidebar = el("sidebar");
          sidebar.classList.toggle("collapsed");
          // 접혔을 때는 '펼치기(side_navigation)' 아이콘, 펴졌을 때는 '접기(menu_open)' 아이콘
          sidebarBtn.querySelector('span').textContent = sidebar.classList.contains("collapsed") ? "side_navigation" : "menu_open";
      };
  }

  // 로그아웃
  const logoutBtn = el("logoutGlobal");
  if(logoutBtn){
    logoutBtn.onclick = async () => {
      await fetch("/logout", {method:"POST", credentials:"include"});
      window.location.href = "/";
    };
  }

  // 모달 제어
  el("docModalClose").onclick = closeModal;
  el("docModalBackdrop").onclick = closeModal;
  document.querySelectorAll(".modal .tab").forEach(t => {
    t.onclick = () => activateModalTab(t.dataset.tab);
  });

  // 세션 및 전송
  el("newSession").onclick = newSession;
  el("sendBtn").onclick = () => sendMessage();

  el("userInput").addEventListener("keydown", (e)=>{
    if(e.key === "Enter" && !e.shiftKey){
      e.preventDefault();
      sendMessage();
    }
  });

  // 패널 조절기 초기화 (오류 방지를 위해 존재 여부 체크)
  if (typeof setupSidebarToggle === "function") setupSidebarToggle();
  if (typeof setupPanelMaxButtons === "function") setupPanelMaxButtons();
  
  if(el("resizer1")) setupVerticalResizer("resizer1", "#sidebar", ".main");
  // 우측 패널 선택자를 기존 ".right"에서 Tailwind가 적용된 "#rightPanel"로 수정!
  if(el("resizer2")) setupVerticalResizer("resizer2", ".main", "#rightPanel"); 
  if(el("hresizer1")) setupHorizontalResizer("hresizer1", '.panel[data-panel="topdocs"]', '.panel[data-panel="citations"]');

  await refreshSessions();
  newSession();
});