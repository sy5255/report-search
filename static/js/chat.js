let currentSessionId = null;

// 현재 선택된 “assistant msg_id” 기준 evidence
let currentEvidenceAssistantMsgId = null;

// 최근 로드된 evidence 데이터 (topdocs/citations)
let lastTopDocs = [];
let lastCitations = null;

// UI에서 보여줄 topdocs 개수
let topDocsShowN = 5;

function el(id){ return document.getElementById(id); }

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
      USE_PROFILES: { html: true }
    });
  }

  return rendered;
}

function renderDocumentMarkdown(mdText){
  const raw = String(mdText || "");

  if(typeof marked === "undefined"){
    return escapeHtml(raw).replace(/\n/g, "<br>");
  }

  let rendered = marked.parse(raw);

  if(typeof DOMPurify !== "undefined"){
    rendered = DOMPurify.sanitize(rendered, {
      USE_PROFILES: { html: true }
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
  document.querySelectorAll(".panel-max").forEach(btn=>{
    btn.addEventListener("click", ()=>{
      const target = btn.dataset.target;
      document.querySelectorAll(".panel").forEach(p=>{
        if(p.dataset.panel === target){
          p.classList.toggle("maximized");
        } else {
          p.classList.remove("maximized");
        }
      });
    });
  });
}

function stripLeadingMailMetaBlock(mdText){
  let t = String(mdText || "").replace(/\r\n/g, "\n");

  // 1) 맨 앞 fenced code block 자체가 MAIL_META를 담고 있으면 통째로 제거
  t = t.replace(
    /^\s*```[^\n]*\n([\s\S]*?)\n```[\t ]*\n*/i,
    (full, inner) => {
      const body = String(inner || "");
      if(/\[MAIL_META\]/i.test(body)){
        return "";
      }
      return full;
    }
  );

  // 2) fenced block이 아니더라도, 맨 앞 MAIL_META 라인 블록 제거
  const lines = t.split("\n");
  let i = 0;

  while(i < lines.length && !lines[i].trim()){
    i++;
  }

  if(i < lines.length && lines[i].trim().toUpperCase() === "[MAIL_META]"){
    i++;

    while(i < lines.length){
      const s = lines[i].trim();

      if(!s){
        i++;
        continue;
      }

      if(
        s.toUpperCase() === "[EDM_LINKS]" ||
        /^From\s*:/i.test(s) ||
        /^Date\s*:/i.test(s) ||
        /^To\s*:/i.test(s) ||
        /^Cc\s*:/i.test(s) ||
        /^Bcc\s*:/i.test(s) ||
        /^Subject\s*:/i.test(s) ||
        /^EDM\s*링크\s*:/i.test(s)
      ){
        i++;
        continue;
      }

      break;
    }

    while(i < lines.length && !lines[i].trim()){
      i++;
    }

    t = lines.slice(i).join("\n");
  }

  return t.trimStart();
}

function injectImagesIntoMarkdown(mdText, assets){
  if(!mdText) return mdText || "";
  if(!assets || !assets.length) return mdText;

  const imgs = assets
    .map(a => (a && a.path) ? a : null)
    .filter(Boolean);

  if(!imgs.length) return mdText;

  let i = 0;

  return mdText.replace(/\[placeholder\]/gi, () => {
    if(i >= imgs.length) return "[placeholder]";

    const a = imgs[i++];
    const url = `/api/view/asset?rel=${encodeURIComponent(a.path)}`;
    const alt = (a.file_name || a.path || "image").replace(/[\r\n]+/g, " ");

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
    div.className = "session-item";
    div.innerHTML = `
      <div class="session-row">
        <div class="title">${escapeHtml(s.title || "Untitled")}</div>
        <button class="icon-btn danger" title="Archive">🗑</button>
      </div>
      <div class="time">${escapeHtml(s.updated_at || "")}</div>
    `;

    div.onclick = (ev) => {
      if(ev.target && ev.target.classList.contains("icon-btn")) return;
      loadSession(s.session_id);
    };

    div.querySelector(".icon-btn").onclick = async (ev) => {
      ev.stopPropagation();
      if(!confirm("이 대화를 목록에서 제거할까요?")) return;
      await apiPost(`/api/sessions/${encodeURIComponent(s.session_id)}/archive`, {});
      if(currentSessionId === s.session_id){
        currentSessionId = null;
        el("chatArea").innerHTML = "";
        clearEvidencePanels();
      }
      await refreshSessions();
    };

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
  el("userInput").focus();
}

function setSelectedAssistantMsg(msgId){
  document.querySelectorAll(".msg.assistant.selected").forEach(x => x.classList.remove("selected"));
  if(!msgId) return;

  const node = document.querySelector(`.msg.assistant[data-msg-id="${CSS.escape(msgId)}"]`);
  if(node) node.classList.add("selected");
}

/* ---------- chat messages ---------- */
function appendMessage(role, content, metaText, msgId, extra = null){
  const chat = el("chatArea");
  const div = document.createElement("div");
  div.className = "msg " + role;
  if(msgId) div.dataset.msgId = msgId;

  let extraHtml = "";
  if(role === "user" && extra){
    extraHtml = buildQueryInterpretationCard(extra);
  }

  const contentClass = role === "assistant" ? "content markdown-body" : "content";

  div.innerHTML = `
    <div class="meta">
      <span>${role.toUpperCase()}</span>
      <span>${escapeHtml(formatTimeForUI(metaText))}</span>
    </div>
    <div class="${contentClass}">${renderMessageContent(role, content)}</div>
    ${extraHtml}
  `;

  if(role === "assistant" && msgId){
    div.addEventListener("click", async (e)=>{
      const target = e.target;
      if(target && (target.closest("a") || target.closest("button"))) return;

      setSelectedAssistantMsg(msgId);
      await loadEvidenceByAssistantMsgId(msgId);
    });
  }

  chat.appendChild(div);
  wireQueryInterpretCard(div);
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

  docs.forEach((d, i) => {
    const title = stripEnriched(d.title || "(no title)");
    const score = (d.score == null) ? "" : Number(d.score).toFixed(5);

    const meta = pickMailMeta(d.additionalField || {});
    const tags = [];

    if(meta.mailFrom) tags.push(`<span class="tag">#${escapeHtml(meta.mailFrom)}</span>`);
    if(meta.edmLinks && meta.edmLinks.length){
      meta.edmLinks.forEach(u=>{
        tags.push(`<span class="tag">#<a href="${escapeHtml(u)}" target="_blank" rel="noopener noreferrer">${escapeHtml(u)}</a></span>`);
      });
    }
    if(meta.mailDate) tags.push(`<span class="tag">#${escapeHtml(meta.mailDate)}</span>`);

    const card = document.createElement("div");
    const idxName = d._index || "";

    card.className = "doc-card";
    card.innerHTML = `
      <div class="doc-line1">
        <div class="doc-title">${escapeHtml(title)}</div>
        <div class="doc-badges">
          <div class="badge top">🏅 Top-${escapeHtml(String(d.rank || (i+1)))}</div>
          ${score ? `<div class="badge score">📌 Score:${escapeHtml(score)}</div>` : ``}
          ${idxName ? `<div class="badge idx">🗂️ ${escapeHtml(idxName)}</div>` : ``}
        </div>
      </div>
      <div class="doc-line2">
        ${tags.join("")}
      </div>
    `;

    card.onclick = () => openDocModal(d, null);

    box.appendChild(card);
  });
}

/* ---------- citations ---------- */
function renderCitations(citations){
  const box = el("citations");
  box.innerHTML = "";

  const ans = (citations && citations.answer) ? citations.answer : [];
  if(!ans.length){
    box.innerHTML = `<div class="cite-sentence"><div class="sentence">(근거 정보 없음)</div></div>`;
    return;
  }

  ans.forEach((a, idx) => {
    const sentence = (a.sentence || "").trim();
    const cites = a.citations || [];

    const div = document.createElement("div");
    div.className = "cite-sentence";
    div.innerHTML = `
      <div class="sentence">${idx+1}. ${escapeHtml(sentence)}</div>
      <button class="btn small cite-btn">근거 보기</button>
      <div class="cite-list" style="display:none;"></div>
    `;

    const btn = div.querySelector(".cite-btn");
    const list = div.querySelector(".cite-list");

    btn.onclick = () => {
      if(list.style.display === "none"){
        list.style.display = "block";
        list.innerHTML = "";

        if(cites.length === 0){
          const it = document.createElement("div");
          it.className = "cite-item";
          it.innerHTML = `<div class="q">(근거 없음)</div>`;
          list.appendChild(it);
          return;
        }

        cites.forEach(c => {
          const quote = (c.quote || "").trim();
          const meta = `${c.doc_id||""} | ${c.chunk_id||""}${c.score!=null ? (" | score=" + c.score) : ""}`;

          const item = document.createElement("div");
          item.className = "cite-item";
          item.innerHTML = quote
            ? `
              <div class="q">${escapeHtml(quote)}</div>
              <div class="m">${escapeHtml(meta)}</div>
            `
            : `
              <div class="q">(선택한 주장에 연결된 근거 문서)</div>
              <div class="m">${escapeHtml(meta)}</div>
            `;

          item.onclick = () => openDocFromCitation(c.doc_id, c.chunk_id, quote || "");
          list.appendChild(item);
        });
      } else {
        list.style.display = "none";
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
  document.querySelectorAll(".modal .tab").forEach(t=>{
    const on = (t.dataset.tab === name);
    t.classList.toggle("active", on);
  });
  el("docModalMd").classList.toggle("viewer-active", name==="md");
  el("docModalImages").classList.toggle("viewer-active", name==="images");
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

async function openDocModal(d, highlightQuote){
  const title = stripEnriched(d.title || "(no title)");
  el("docModalTitle").textContent = title;

  const af = d.additionalField || {};
  const storage = af.storage || {};
  const assets = af.assets || [];

  const mdRel = storage.parsed_md_rel_path;
  if(mdRel){
    try{
      const mdText = await fetch(`/api/view/md?rel=${encodeURIComponent(mdRel)}`, {credentials:"include"}).then(r=>r.text());

      const mdNoMeta = stripLeadingMailMetaBlock(mdText);
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

/* ---------- send message ---------- */
async function sendMessage(){
  const txt = el("userInput").value.trim();
  if(!txt) return;

  el("userInput").value = "";
  appendMessage("user", txt, null, null);

  const pendingId = "PENDING_" + Date.now();
  appendMessage("assistant", "⏳ 답변 생성 중...", null, pendingId);

  const indexNames = getSelectedIndexNames();
  if(indexNames.length === 0){
    alert("최소 1개 인덱스 선택 필수");
    return;
  }

  el("sendBtn").disabled = true;
  el("userInput").disabled = true;

  const topK = parseInt(el("topK").value || "5", 10);

  const payload = {
    session_id: currentSessionId,
    user_text: txt,
    index_names: indexNames,
    top_k: topK,
    filters: null
  };

  try{
    const res = await apiPost("/api/chat", payload);
    currentSessionId = res.session_id;

    const pend = document.querySelector(`.msg.assistant[data-msg-id="${pendingId}"]`);
    if(pend){
      pend.remove();
    }

    const userMsgs = Array.from(document.querySelectorAll(".msg.user"));
    const lastUserMsg = userMsgs[userMsgs.length - 1];
    if(lastUserMsg && !lastUserMsg.querySelector(".query-interpret-card")){
      const html = buildQueryInterpretationCard({
        rewritten_query: res.rewritten_query,
        normalized_query: res.normalized_query,
        expanded_query: res.expanded_query,
        detected_terms: res.detected_terms || []
      });
      if(html){
        lastUserMsg.insertAdjacentHTML("beforeend", html);
        wireQueryInterpretCard(lastUserMsg);
      }
    }

    appendMessage("assistant", res.assistant_text, new Date().toISOString(), res.assistant_msg_id || null);
    setSelectedAssistantMsg(res.assistant_msg_id || "");

    currentEvidenceAssistantMsgId = res.assistant_msg_id || null;
    lastTopDocs = res.top_docs || [];
    renderTopDocsFiltered();

    lastCitations = res.citations || {answer:[], final:res.assistant_text};
    renderCitations(lastCitations);

    await refreshSessions();
  } catch(e){
    const pend = document.querySelector(`.msg.assistant[data-msg-id="${pendingId}"]`);
    if(pend){
      const contentDiv = pend.querySelector(".content");
      if(contentDiv) contentDiv.textContent = "ERROR: " + String(e);
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
  configureMarked();

  const saved = localStorage.getItem("theme") || "dark";
  applyTheme(saved);
  const themeBtn = el("themeToggle");
  if(themeBtn){
    themeBtn.onclick = toggleTheme;
  }

  const showInput = el("topDocsShowN");
  showInput.addEventListener("input", ()=>{
    const v = parseInt(showInput.value || "5", 10);
    if(Number.isFinite(v) && v >= 1){
      topDocsShowN = v;
      renderTopDocsFiltered();
    }
  });

  el("docModalClose").onclick = closeModal;
  el("docModalBackdrop").onclick = closeModal;
  document.querySelectorAll(".modal .tab").forEach(t => {
    t.onclick = () => activateModalTab(t.dataset.tab);
  });

  el("logout").onclick = async () => {
    await fetch("/logout", {method:"POST", credentials:"include"});
    window.location.href = "/";
  };

  el("newSession").onclick = newSession;
  el("sendBtn").onclick = sendMessage;

  const pickerBtn = el("indexPickerBtn");
  const pickerMenu = el("indexPickerMenu");
  const pickerLabel = el("indexPickerLabel");

  function refreshIndexPickerLabel(){
    const picked = getSelectedIndexNames();
    if(picked.length === 0){
      pickerLabel.textContent = "Select indexes";
    } else if(picked.length <= 2){
      pickerLabel.textContent = picked.join(", ");
    } else {
      pickerLabel.textContent = `${picked[0]}, ${picked[1]} +${picked.length - 2}`;
    }
  }

  const pickerRoot = el("indexPicker");

  function closePicker(){
    pickerRoot.classList.remove("open");
  }

  pickerBtn.addEventListener("click", (e)=>{
    e.stopPropagation();
    pickerRoot.classList.toggle("open");
  });

  document.addEventListener("click", closePicker);

  pickerMenu.addEventListener("click", (e)=>{
    e.stopPropagation();
  });

  document.querySelectorAll('input[name="indexNames"]').forEach(chk=>{
    chk.addEventListener("change", refreshIndexPickerLabel);
  });

  const allBtn = el("idxAll");
  const noneBtn = el("idxNone");

  allBtn.onclick = () => {
    document.querySelectorAll('input[name="indexNames"]').forEach(x => x.checked = true);
    refreshIndexPickerLabel();
  };

  noneBtn.onclick = () => {
    document.querySelectorAll('input[name="indexNames"]').forEach(x => x.checked = false);
    refreshIndexPickerLabel();
  };

  refreshIndexPickerLabel();

  el("userInput").addEventListener("keydown", (e)=>{
    if(e.key === "Enter" && !e.shiftKey){
      e.preventDefault();
      sendMessage();
    }
  });

  setupSidebarToggle();
  setupPanelMaxButtons();
  setupVerticalResizer("resizer1", "#sidebar", ".main");
  setupVerticalResizer("resizer2", ".main", ".right");
  setupHorizontalResizer("hresizer1", '.panel[data-panel="topdocs"]', '.panel[data-panel="citations"]');

  await refreshSessions();
  newSession();
});