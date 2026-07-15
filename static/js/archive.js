console.log("🔥 archive.js (다중 선택 커스텀 드롭다운 & 용어사전 관리자 권한 통합본) 로드됨!");

let currentSkip = 0;
const PAGE_LIMIT = 20;
let currentQuery = "";
let currentSort = "desc";

// 💡 1. 다중 필터 및 용어사전용 전역 변수
let currentAuthors = []; 
let currentStartDate = "";
let currentEndDate = "";
let allDictionaryTerms = []; // 모달 드롭다운용 저장소
let currentViewerDoc = null;
let currentViewerMarkdown = "";
let currentViewerTitle = "";
const LS_BOOKMARKS = "rs_archive_bookmarks";

function readBookmarks(){
  try {
    const raw = localStorage.getItem(LS_BOOKMARKS);
    if(!raw) return {};
    return JSON.parse(raw) || {};
  } catch(_){ return {}; }
}
function writeBookmarks(map){
  try { localStorage.setItem(LS_BOOKMARKS, JSON.stringify(map || {})); } catch(_){}
}
function bookmarkKeyFromDoc(doc){
  if(!doc) return "";
  return (doc.storage && doc.storage.parsed_md_rel_path) || doc.doc_id || doc.id || "";
}

function initArchive() {
    if(typeof marked !== "undefined"){
        marked.setOptions({ gfm: true, breaks: true });
    }

    // 서버에서 담당자 목록, 문서 목록, 그리고 용어사전 목록을 모두 초기 로드!
    fetchFilterData();
    fetchDocuments(false);
    loadDictionaryTerms(); // 용어사전 데이터 최초 1회 로딩

    // ----------------------------------------------------
    // [Reports 탭] UI 이벤트 바인딩 (검색, 정렬, 더보기 등)
    // ----------------------------------------------------
    const searchInput = document.getElementById("archiveSearchInput");
    if(searchInput) {
        searchInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                currentQuery = e.target.value.trim();
                fetchDocuments(false);
            }
        });
    }

    const sortBtn = document.getElementById("sortToggleBtn");
    if(sortBtn) {
        sortBtn.addEventListener("click", () => {
            currentSort = currentSort === "desc" ? "asc" : "desc";
            document.getElementById("sortToggleText").innerText = currentSort === "desc" ? "최신순" : "오래된순";
            fetchDocuments(false);
        });
    }

    const loadMoreBtn = document.getElementById("loadMoreBtn");
    if(loadMoreBtn) {
        loadMoreBtn.addEventListener("click", () => fetchDocuments(true));
    }

    const downloadBtn = document.getElementById("downloadMdBtn");
    if(downloadBtn){
        downloadBtn.addEventListener("click", () => {
            if(!currentViewerMarkdown){
                if(window.Toast) window.Toast.warn("먼저 문서를 열어주세요.");
                return;
            }
            const safeTitle = (currentViewerTitle || "document")
                .replace(/[^\w\-가-힣]/g, "_").slice(0, 80) || "document";
            const blob = new Blob([currentViewerMarkdown], { type: "text/markdown;charset=utf-8" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `${safeTitle}.md`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        });
    }

    const bookmarkBtn = document.getElementById("bookmarkBtn");
    if(bookmarkBtn){
        bookmarkBtn.addEventListener("click", () => {
            const key = bookmarkKeyFromDoc(currentViewerDoc);
            if(!key){
                if(window.Toast) window.Toast.warn("이 문서는 즐겨찾기할 수 없습니다.");
                return;
            }
            const marks = readBookmarks();
            if(marks[key]){
                delete marks[key];
                if(window.Toast) window.Toast.info("즐겨찾기에서 제거했습니다.");
            } else {
                marks[key] = {
                    title: currentViewerTitle,
                    saved_at: new Date().toISOString()
                };
                if(window.Toast) window.Toast.success("즐겨찾기에 추가했습니다.");
            }
            writeBookmarks(marks);
            refreshBookmarkButtonState();
            // also refresh card star badges
            refreshCardBookmarkBadges();
        });
    }

    const expandBtn = document.getElementById("expandBtn");
    if(expandBtn) {
        expandBtn.addEventListener("click", () => {
            const leftPanel = document.getElementById("archiveLeftPanel");
            const resizer = document.getElementById("archiveResizer");
            const expandIcon = document.querySelector("#expandBtn span");
            
            if (leftPanel.classList.contains("hidden")) {
                leftPanel.classList.remove("hidden");
                if(resizer) resizer.classList.remove("hidden");
                if(expandIcon) expandIcon.innerText = "open_in_full";
            } else {
                leftPanel.classList.add("hidden");
                if(resizer) resizer.classList.add("hidden");
                if(expandIcon) expandIcon.innerText = "close_fullscreen";
            }
        });
    }
    
    // 필터 토글 이벤트
    const filterToggleBtn = document.getElementById("filterToggleBtn");
    const filterPanel = document.getElementById("filterPanel");
    if(filterToggleBtn && filterPanel) {
        filterToggleBtn.addEventListener("click", () => {
            filterPanel.classList.toggle("hidden");
            filterPanel.classList.toggle("flex");
            filterToggleBtn.classList.toggle("bg-surface-container");
            filterToggleBtn.classList.toggle("text-primary");
        });
    }

    // 커스텀 담당자 드롭다운 이벤트
    const authorBtn = document.getElementById("authorDropdownBtn");
    const authorList = document.getElementById("authorDropdownList");
    if(authorBtn && authorList) {
        authorBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            authorList.classList.toggle("hidden");
            authorBtn.classList.toggle("ring-2");
            authorBtn.classList.toggle("ring-primary/20");
        });
        document.addEventListener("click", () => {
            authorList.classList.add("hidden");
            authorBtn.classList.remove("ring-2", "ring-primary/20");
        });
        authorList.addEventListener("click", (e) => e.stopPropagation());
    }

    const startDateInput = document.getElementById("filterStartDate");
    if(startDateInput) {
        startDateInput.addEventListener("change", (e) => {
            currentStartDate = e.target.value;
            fetchDocuments(false);
        });
    }

    const endDateInput = document.getElementById("filterEndDate");
    if(endDateInput) {
        endDateInput.addEventListener("change", (e) => {
            currentEndDate = e.target.value;
            fetchDocuments(false);
        });
    }

    setupArchiveResizer();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initArchive);
} else {
    initArchive();
}


// ==========================================
// 💡 [Reports 탭] 핵심 기능 함수들
// ==========================================
function showLoading(listEl) {
    const countEl = document.getElementById("totalDocsCount");
    if (countEl) countEl.innerText = "문서 불러오는 중...";
    listEl.innerHTML = `
        <div class="col-span-2 flex flex-col items-center justify-center h-40 text-secondary">
            <span class="material-symbols-outlined animate-spin text-4xl mb-3 text-primary">progress_activity</span>
            <span class="text-sm font-bold">문서를 검색하고 있습니다...</span>
        </div>
    `;
}

async function fetchFilterData() {
    try {
        const res = await fetch("/api/archive/filters");
        if (!res.ok) return;
        const data = await res.json();
        const listContainer = document.getElementById("authorDropdownList");
        if (!listContainer) return;
        listContainer.innerHTML = "";

        data.authors.forEach(auth => {
            if (!auth) return;
            const label = document.createElement("label");
            label.className = "flex items-center gap-3 px-3 py-2 hover:bg-primary/5 rounded-lg cursor-pointer transition-colors group";
            label.innerHTML = `
                <input type="checkbox" value="${escapeHtml(auth)}" class="author-checkbox w-4 h-4 rounded border-surface-container text-primary focus:ring-primary/30 cursor-pointer">
                <span class="text-xs font-bold text-on-surface group-hover:text-primary truncate" title="${escapeHtml(auth)}">
                    ${escapeHtml(auth)}
                </span>
            `;
            const checkbox = label.querySelector("input");
            checkbox.addEventListener("change", () => {
                const checkedNodes = document.querySelectorAll(".author-checkbox:checked");
                currentAuthors = Array.from(checkedNodes).map(cb => cb.value);
                updateAuthorButtonText();
                fetchDocuments(false);
            });
            listContainer.appendChild(label);
        });
    } catch (e) { console.error("필터 데이터 로드 실패", e); }
}

function updateAuthorButtonText() {
    const textEl = document.getElementById("authorSelectionText");
    if (!textEl) return;
    if (currentAuthors.length === 0) {
        textEl.innerText = "전체 담당자 (All)";
        textEl.classList.remove("text-primary", "font-bold");
    } else {
        const firstAuthorName = currentAuthors[0].split(" <")[0];
        if (currentAuthors.length === 1) {
            textEl.innerText = firstAuthorName;
        } else {
            textEl.innerText = `${firstAuthorName} 외 ${currentAuthors.length - 1}명`;
        }
        textEl.classList.add("text-primary", "font-bold");
    }
}

async function fetchDocuments(isAppend = false) {
    const listEl = document.getElementById("archiveList");
    const moreBtn = document.getElementById("loadMoreBtn");

    if (!isAppend) {
        currentSkip = 0;
        if(listEl) showLoading(listEl);
        if(moreBtn) moreBtn.classList.add("hidden");
    }

    try {
        const params = new URLSearchParams({
            q: currentQuery,
            author: currentAuthors.join(","),
            start_date: currentStartDate,
            end_date: currentEndDate,
            skip: currentSkip,
            limit: PAGE_LIMIT,
            sort: currentSort
        });

        const res = await fetch(`/api/archive/documents?${params.toString()}`, { credentials: "include" });
        if (!res.ok) throw new Error("서버 응답 오류");
        const data = await res.json();
        
        if (data.total_fetched !== undefined && !isAppend) {
            const countEl = document.getElementById("totalDocsCount");
            if(countEl) countEl.innerText = `검색 결과: ${data.total_fetched}건`;
        }

        const docsArray = Array.isArray(data.documents) ? data.documents : [];
        renderCards(docsArray, isAppend);
        
        if (data.has_more && moreBtn) {
            moreBtn.classList.remove("hidden");
            currentSkip += PAGE_LIMIT;
        } else if (moreBtn) {
            moreBtn.classList.add("hidden");
        }
    } catch (err) {
        console.error("🔥 fetchDocuments 에러:", err);
        const countEl = document.getElementById("totalDocsCount");
        if(countEl) countEl.innerText = "로딩 실패";
        if(listEl) {
            listEl.innerHTML = `<div class="col-span-2 text-center text-error mt-10 p-4">데이터 로딩 오류: ${err.message}</div>`;
        }
    }
}

function renderCards(docs, isAppend) {
    const listEl = document.getElementById("archiveList");
    if(!listEl) return;
    if (!isAppend) listEl.innerHTML = "";
    if (docs.length === 0 && !isAppend) {
        listEl.innerHTML = `
          <div class="col-span-2 welcome-screen" style="padding:32px 16px;">
            <img src="/static/images/empty-archive.svg" alt=""/>
            <h2>조건에 맞는 보고서가 없습니다</h2>
            <p>검색어 · 담당자 · 기간 필터를 조정하거나 초기화해 다시 시도해보세요.</p>
            <div class="welcome-chips">
              <button class="welcome-chip" id="emptyArchiveResetBtn" type="button">
                <span class="material-symbols-outlined">restart_alt</span>
                <span>필터 전부 초기화</span>
              </button>
            </div>
          </div>`;
        const resetBtn = document.getElementById("emptyArchiveResetBtn");
        if(resetBtn){
          resetBtn.addEventListener("click", () => {
            const search = document.getElementById("archiveSearchInput");
            if(search) search.value = "";
            document.querySelectorAll('input[name="authorFilter"]').forEach(cb => { cb.checked = false; });
            const sd = document.getElementById("startDate"); if(sd) sd.value = "";
            const ed = document.getElementById("endDate");   if(ed) ed.value = "";
            if(typeof fetchDocuments === "function"){
              currentSkip = 0;
              currentQuery = "";
              currentAuthors = [];
              currentStartDate = "";
              currentEndDate = "";
              fetchDocuments(false);
            }
          });
        }
        return;
    }

    docs.forEach(doc => {
        const af = doc.additionalField || {};
        let displayTitle = (doc.title || "(제목 없음)").replace(/\.enriched$/i, "").replace(/_/g, " ");
        const dateRaw = af.mail_date || doc.mail_date;
        const dateStr = dateRaw ? new Date(dateRaw).toLocaleDateString('ko-KR') : "날짜 없음";
        const versionTag = af.version_tag || "DOC";
        const summary = af.summary || ""; 
        const sender = af.mail_from || doc.mail_from; 
        const links = Array.isArray(af.report_links) ? af.report_links : (Array.isArray(doc.report_links) ? doc.report_links : []);
        
        let tagsHtml = "";
        if (sender) {
            tagsHtml += `<span class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-primary/10 text-primary text-[10px] font-bold"><span class="material-symbols-outlined text-[14px]">person</span>${escapeHtml(sender)}</span>`;
        }
        links.forEach(link => {
            tagsHtml += `<a href="${escapeHtml(link)}" target="_blank" onclick="event.stopPropagation()" class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-secondary/10 hover:bg-secondary/20 text-secondary text-[10px] font-bold"><span class="material-symbols-outlined text-[14px]">link</span>${escapeHtml(link)}</a>`;
        });

        const card = document.createElement("div");
        const bmKey = bookmarkKeyFromDoc(doc);
        card.dataset.bookmarkKey = bmKey;
        const isBookmarked = !!(readBookmarks())[bmKey];
        card.className = "archive-card group bg-surface-container-lowest border border-surface-container rounded-2xl p-6 shadow-sm hover:border-primary/40 transition-all cursor-pointer flex flex-col min-h-[255px] relative";
        card.innerHTML = `
            <span class="archive-bookmark-badge material-symbols-outlined absolute top-3 right-3 text-yellow-500 ${isBookmarked ? '' : 'hidden'}" style="font-variation-settings:'FILL' 1;font-size:18px;" title="즐겨찾은 문서">star</span>
            <div class="flex justify-between items-start mb-4 gap-2">
                <span class="text-[10px] font-extrabold text-secondary uppercase tracking-tighter bg-surface-container-highest px-2 py-0.5 rounded-md">${escapeHtml(versionTag)}</span>
                <span class="text-[10px] font-semibold text-outline">${escapeHtml(dateStr)}</span>
            </div>
            <h3 class="text-[15px] font-extrabold text-on-surface mb-3 line-clamp-3 group-hover:text-primary transition-colors font-manrope">${escapeHtml(displayTitle)}</h3>
            ${summary ? `<p class="text-[11px] text-secondary mb-5 line-clamp-3 leading-relaxed flex-1 opacity-80">${escapeHtml(summary)}</p>` : '<div class="flex-1"></div>'}
            <div class="mt-auto pt-4 border-t border-surface-container flex flex-wrap gap-2">${tagsHtml}</div>
        `;

        card.addEventListener("click", () => {
            document.querySelectorAll(".archive-card").forEach(c => c.classList.remove("ring-2", "ring-primary", "bg-primary/5"));
            card.classList.add("ring-2", "ring-primary", "bg-primary/5");
            openViewer(doc, displayTitle);
        });
        listEl.appendChild(card);
    });
}

function refreshCardBookmarkBadges(){
    const marks = readBookmarks();
    document.querySelectorAll(".archive-card").forEach(card => {
        const key = card.dataset.bookmarkKey;
        const badge = card.querySelector(".archive-bookmark-badge");
        if(!badge) return;
        if(marks[key]) badge.classList.remove("hidden");
        else badge.classList.add("hidden");
    });
}

function highlightSearchTermsIn(rootEl, query){
  if(!rootEl || !query) return;
  const terms = query.split(/\s+/).filter(t => t && t.length >= 2);
  if(!terms.length) return;
  const escaped = terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp("(" + escaped.join("|") + ")", "gi");
  const walker = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT, {
    acceptNode(n){
      if(!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
      const p = n.parentElement;
      if(!p) return NodeFilter.FILTER_REJECT;
      const tag = p.tagName;
      if(tag === "SCRIPT" || tag === "STYLE" || tag === "CODE" || tag === "PRE") return NodeFilter.FILTER_REJECT;
      if(p.closest && p.closest("mark.archive-highlight")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  const targets = [];
  let n; while((n = walker.nextNode())) targets.push(n);
  targets.forEach(node => {
    if(!re.test(node.nodeValue)) { re.lastIndex = 0; return; }
    re.lastIndex = 0;
    const frag = document.createDocumentFragment();
    const parts = node.nodeValue.split(re);
    parts.forEach((seg, i) => {
      if(i % 2 === 1){
        const mark = document.createElement("mark");
        mark.className = "archive-highlight";
        mark.textContent = seg;
        frag.appendChild(mark);
      } else if(seg){
        frag.appendChild(document.createTextNode(seg));
      }
    });
    node.parentNode.replaceChild(frag, node);
  });
}

function setViewerToolbarVisible(visible){
  const bm = document.getElementById("bookmarkBtn");
  const dl = document.getElementById("downloadMdBtn");
  [bm, dl].forEach(b => { if(b) b.classList.toggle("hidden", !visible); });
}

function refreshBookmarkButtonState(){
  const btn = document.getElementById("bookmarkBtn");
  if(!btn) return;
  const key = bookmarkKeyFromDoc(currentViewerDoc);
  const marks = readBookmarks();
  const on = !!marks[key];
  btn.classList.toggle("text-yellow-500", on);
  btn.classList.toggle("text-secondary", !on);
  const icon = btn.querySelector(".material-symbols-outlined");
  if(icon){
    icon.textContent = on ? "star" : "star_outline";
    icon.style.fontVariationSettings = on ? "'FILL' 1" : "'FILL' 0";
  }
  btn.title = on ? "즐겨찾기 해제" : "즐겨찾기에 추가";
}

async function openViewer(doc, displayTitle) {
    currentViewerDoc = doc;
    currentViewerMarkdown = "";
    currentViewerTitle = displayTitle || "";
    const titleEl = document.getElementById("viewerTitle");
    if(titleEl) titleEl.innerText = displayTitle;
    setViewerToolbarVisible(true);
    refreshBookmarkButtonState();

    const contentEl = document.getElementById("viewerContent");
    if(!contentEl) return;
    
    contentEl.innerHTML = `
        <div class="flex flex-col items-center justify-center h-full text-secondary">
            <span class="material-symbols-outlined animate-spin text-4xl mb-3 text-primary">autorenew</span>
            <span class="text-sm font-bold">문서를 렌더링하는 중입니다...</span>
        </div>
    `;

    const mdRel = doc.storage?.parsed_md_rel_path;
    const assets = Array.isArray(doc.assets) ? doc.assets : [];

    if (!mdRel) {
        contentEl.innerHTML = `<div class="flex flex-col items-center justify-center h-full text-error opacity-80"><span class="material-symbols-outlined text-5xl mb-4">broken_image</span><div class="font-bold">원본 경로를 찾을 수 없습니다.</div></div>`;
        return;
    }

    try {
        const res = await fetch(`/api/view/md?rel=${encodeURIComponent(mdRel)}`, { credentials: "include" });
        if (!res.ok) throw new Error("마크다운 파일을 읽어올 수 없습니다.");
        
        const rawMdText = await res.text();
        currentViewerMarkdown = rawMdText;
        const processedText = preProcessMarkdown(rawMdText);
        const mdNoMeta = stripLeadingMailMetaBlock(processedText);
        const mdWithImgs = injectImagesIntoMarkdown(mdNoMeta, assets);
        contentEl.innerHTML = renderDocumentMarkdown(mdWithImgs);

        contentEl.querySelectorAll("img").forEach(img => {
            if(img.classList.contains("max-w-full")) return;
            img.className = "max-w-full h-auto rounded-xl cursor-pointer hover:opacity-90 transition-opacity my-6 shadow-sm border border-outline/10";
            img.onclick = () => showImgPreview(img.src);
        });

        // 검색어 하이라이트
        if(currentQuery) highlightSearchTermsIn(contentEl, currentQuery);
    } catch (e) {
        contentEl.innerHTML = `<div class="text-error text-center mt-10 font-bold">${escapeHtml(String(e))}</div>`;
    }
}

function stripLeadingMailMetaBlock(mdText){
    let t = String(mdText || "").replace(/\r\n/g, "\n");
    t = t.replace(/^\s*```[^\n]*\n([\s\S]*?)\n```[\t ]*\n*/i, (full, inner) => {
        if(/\[MAIL_META\]/i.test(String(inner || ""))) return "";
        return full;
    });
    t = t.replace(/\[MAIL_META\][\s\S]*?(?=\n\s*\n|$)/i, "");
    return t.trimStart(); 
}

function renderDocumentMarkdown(mdText){
    const raw = String(mdText || "");
    if(typeof marked === "undefined") return escapeHtml(raw).replace(/\n/g, "<br>");
    let rendered = marked.parse(raw);
    if(typeof DOMPurify !== "undefined"){
      rendered = DOMPurify.sanitize(rendered, { USE_PROFILES: { html: true }, ADD_TAGS: ['details', 'summary'] });
    }
    return rendered;
}

function injectImagesIntoMarkdown(mdText, assets){
    if(!mdText) return mdText || "";
    if(!assets || !assets.length) return mdText.replace(/`?\[Image_position\]`?/gi, "");
 
    const imgs = assets.map(a => (a && a.path) ? a : null).filter(Boolean);
    if(!imgs.length) return mdText.replace(/`?\[Image_position\]`?/gi, "");
 
    let i = 0;
    return mdText.replace(/`?\[Image_position\]`?/gi, () => {
        if(i >= imgs.length) return ""; 
        const a = imgs[i++];
        const url = `/api/view/asset?rel=${encodeURIComponent(a.path)}`;
        const alt = (a.file_name || a.path || "image").replace(/[\r\n]+/g, " ");
        return `\n\n![${escapeHtml(alt)}](${url})\n\n`;
    });
}

function showImgPreview(src){
    const pv = document.getElementById("imgPreview");
    if(!pv) return;
    pv.innerHTML = `<img src="${src}" class="max-h-full max-w-full object-contain rounded-xl shadow-2xl" />`;
    pv.classList.remove("hidden");
    pv.onclick = () => { pv.classList.add("hidden"); pv.innerHTML = ""; };
}

function escapeHtml(s){
    return String(s || "").replace(/[&<>"']/g, (m)=>({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;" }[m]));
}

function setupArchiveResizer(){
    const resizer = document.getElementById("archiveResizer");
    const leftPanel = document.getElementById("archiveLeftPanel");
    if(!resizer || !leftPanel) return;
    let dragging = false;
    resizer.addEventListener("mousedown", ()=>{
        dragging = true;
        document.body.classList.add("dragging");
        document.body.style.cursor = "col-resize";
    });
    window.addEventListener("mousemove", (e)=>{
        if(!dragging) return;
        const w = Math.max(300, Math.min(window.innerWidth * 0.6, e.clientX));
        leftPanel.style.width = w + "px";
    });
    window.addEventListener("mouseup", ()=>{
        if(dragging){
            dragging = false;
            document.body.classList.remove("dragging");
            document.body.style.cursor = "";
        }
    });
} 


function preProcessMarkdown(mdText) {
    let t = String(mdText || "");
    t = t.replace(/\[\s*placeholder\s*\]/gi, "");
    const match = t.match(/\.\/images\/\|attachments\/inline|<img\s+src=/i);
    if (match) t = t.substring(0, match.index);
    return t;
}

// ==========================================
// 💡 [Term Dictionary 탭] 핵심 기능 및 모달 이벤트
// ==========================================

// 1. 용어 사전 데이터 로드 및 렌더링
async function loadDictionaryTerms() {
    const tbody = document.getElementById("dictionaryTableBody");
    const targetSelect = document.getElementById("targetTermSelect");
    if (!tbody) return;

    try {
        const res = await fetch("/api/dictionary/terms");
        if (!res.ok) throw new Error("서버 응답 오류");
        
        const data = await res.json();
        allDictionaryTerms = data.terms || [];
        
        // 타겟 선택 드롭다운 채우기 (유의어 추가 모드용)
        if (targetSelect) {
            targetSelect.innerHTML = "";
            allDictionaryTerms.forEach(term => {
                const opt = document.createElement("option");
                opt.value = term.term_id;
                opt.textContent = `[${term.term_type.toUpperCase()}] ${term.canonical_name}`;
                targetSelect.appendChild(opt);
            });
        }
        renderDictionaryTable(allDictionaryTerms);

    } catch (error) {
        tbody.innerHTML = `<tr><td colspan="4" class="px-8 py-8 text-center text-error text-sm font-bold">데이터 로딩 오류</td></tr>`;
    }
}

// 페이지네이션 상태 (페이지당 5개)
const DICT_PAGE_SIZE = 5;
let dictPageList = [];
let dictPage = 1;

// 렌더 진입점: 리스트를 받아 1페이지부터 페이지네이션해 그린다 (검색 필터도 이 함수를 호출)
function renderDictionaryTable(termsToRender) {
    dictPageList = termsToRender || [];
    dictPage = 1;
    _renderDictPage();
}

function _renderDictPage() {
    const tbody = document.getElementById("dictionaryTableBody");
    if (!tbody) return;
    tbody.innerHTML = "";

    if (dictPageList.length === 0) {
        tbody.innerHTML = `<tr><td colspan="4" class="px-8 py-12 text-center text-secondary text-sm font-bold bg-surface-container-lowest/50 rounded-xl">검색 조건에 맞는 용어가 없습니다.</td></tr>`;
        _renderDictPagination();
        return;
    }

    const totalPages = Math.ceil(dictPageList.length / DICT_PAGE_SIZE);
    if (dictPage > totalPages) dictPage = totalPages;
    if (dictPage < 1) dictPage = 1;
    const start = (dictPage - 1) * DICT_PAGE_SIZE;
    const pageTerms = dictPageList.slice(start, start + DICT_PAGE_SIZE);

    pageTerms.forEach(term => {
        const description = term.description ? escapeHtml(term.description) : `<span class="text-outline italic">설명 없음</span>`;
        const aliases = term.aliases ? `<div class="mt-2 text-[11px] text-primary bg-primary/5 inline-block px-2 py-0.5 rounded border border-primary/10 font-semibold">Aliases: ${escapeHtml(term.aliases)}</div>` : '';

        // 💡 관리자(IS_ADMIN)인 경우에만 실제 수정/삭제 버튼 렌더링 (IS_ADMIN은 HTML에서 전역변수로 전달됨)
        const actionHtml = typeof IS_ADMIN !== "undefined" && IS_ADMIN ? `
            <div class="flex justify-end gap-2">
                <button onclick="openEditTermModal(${term.term_id})" class="p-2 text-outline-variant hover:text-primary hover:bg-primary/5 rounded-lg transition-all" title="Edit Term">
                    <span class="material-symbols-outlined">edit</span>
                </button>
                <button onclick="deleteTerm(${term.term_id})" class="p-2 text-outline-variant hover:text-error hover:bg-error/5 rounded-lg transition-all" title="Delete Term">
                    <span class="material-symbols-outlined">delete</span>
                </button>
            </div>
        ` : `<span class="text-xs text-secondary italic">권한 없음</span>`;

        const tr = document.createElement("tr");
        tr.className = "hover:bg-surface-container-low/50 transition-colors group border-b border-surface-container/50";
        tr.innerHTML = `
            <td class="px-8 py-6">
                <div class="font-bold text-on-surface font-manrope">${escapeHtml(term.canonical_name)}</div>
                <div class="text-[11px] text-outline uppercase tracking-tighter mt-1">ID: ${term.term_id}</div>
            </td>
            <td class="px-8 py-6">
                <span class="px-3 py-1 bg-surface-container-highest text-on-surface-variant text-[10px] font-extrabold rounded-full uppercase tracking-widest shadow-sm">
                    ${escapeHtml(term.term_type)}
                </span>
            </td>
            <td class="px-8 py-6 text-sm text-on-surface-variant leading-relaxed font-body">
                ${description}
                <br>${aliases}
            </td>
            <td class="px-8 py-6 text-right">
                ${actionHtml}
            </td>
        `;
        tbody.appendChild(tr);
    });

    _renderDictPagination();
}

// 번호 페이지네이션 컨트롤 (현재±2 윈도우 + 처음/끝 + ‹ ›). 총 1페이지면 숨김.
function _renderDictPagination() {
    const box = document.getElementById("dictPagination");
    if (!box) return;
    const totalPages = Math.ceil(dictPageList.length / DICT_PAGE_SIZE);
    if (totalPages <= 1) { box.innerHTML = ""; return; }

    const btn = (label, page, opts = {}) => {
        const { active = false, disabled = false, ellipsis = false } = opts;
        if (ellipsis) return `<span class="px-2 text-secondary text-sm select-none">…</span>`;
        const base = "min-w-[32px] h-8 px-2 rounded-lg text-sm font-bold transition-colors border";
        const cls = active
            ? "bg-primary text-on-primary border-primary"
            : (disabled
                ? "text-outline border-transparent cursor-not-allowed opacity-50"
                : "text-on-surface border-surface-container hover:bg-surface-container-high");
        return `<button type="button" class="dict-page-btn ${base} ${cls}" ${disabled ? "disabled" : `data-page="${page}"`}>${label}</button>`;
    };

    const pages = [];
    pages.push(btn("‹", dictPage - 1, { disabled: dictPage <= 1 }));
    const win = 2;
    let last = 0;
    for (let p = 1; p <= totalPages; p++) {
        if (p === 1 || p === totalPages || (p >= dictPage - win && p <= dictPage + win)) {
            if (last && p - last > 1) pages.push(btn("", 0, { ellipsis: true }));
            pages.push(btn(String(p), p, { active: p === dictPage }));
            last = p;
        }
    }
    pages.push(btn("›", dictPage + 1, { disabled: dictPage >= totalPages }));
    box.innerHTML = pages.join("");

    box.querySelectorAll(".dict-page-btn[data-page]").forEach(b => {
        b.addEventListener("click", () => {
            const p = parseInt(b.getAttribute("data-page"), 10);
            if (!isNaN(p) && p !== dictPage) {
                dictPage = p;
                _renderDictPage();
                const tbody = document.getElementById("dictionaryTableBody");
                if (tbody && tbody.scrollIntoView) tbody.scrollIntoView({ behavior: "smooth", block: "start" });
            }
        });
    });
}

// 용어 사전 내 검색 & 폼 이벤트 바인딩
document.addEventListener("DOMContentLoaded", () => {
    // 2-1. 용어 사전 실시간 검색 필터
    const dictSearchInput = document.getElementById("dictSearchInput");
    const editSlider = document.getElementById("editSearchBoost");
    const editValDisplay = document.getElementById("editBoostVal");

    if(editSlider && editValDisplay) {
        editSlider.addEventListener("input", (e) => {
            editValDisplay.innerText = parseFloat(e.target.value).toFixed(1);
        });
    }

    if (dictSearchInput) {
        dictSearchInput.addEventListener("input", (e) => {
            const query = e.target.value.toLowerCase().trim();
            if (!query) {
                renderDictionaryTable(allDictionaryTerms);
                return;
            }
            const filteredTerms = allDictionaryTerms.filter(term => {
                const cName = (term.canonical_name || "").toLowerCase();
                const tType = (term.term_type || "").toLowerCase();
                const desc = (term.description || "").toLowerCase();
                const als = (term.aliases || "").toLowerCase();
                return cName.includes(query) || tType.includes(query) || desc.includes(query) || als.includes(query);
            });
            renderDictionaryTable(filteredTerms);
        });
    }

    // 2-2. 제안 모드(라디오 버튼) 전환 시 입력 필드 변경 로직
    const radios = document.querySelectorAll('input[name="proposal_mode"]');
    const sectionTarget = document.getElementById("sectionTargetTerm");
    const sectionNew = document.getElementById("sectionNewTermDetails");
    const rawLabel = document.getElementById("rawTextLabel");
    const rawInput = document.getElementById("rawTextInput");

    radios.forEach(radio => {
        radio.addEventListener('change', (e) => {
            const mode = e.target.value;
            if(mode === "new_term") {
                if(sectionTarget) sectionTarget.classList.add("hidden");
                if(sectionNew) sectionNew.classList.remove("hidden");
                if(rawLabel) rawLabel.innerText = "Candidate Term (원문/대표 유의어)";
                if(rawInput) rawInput.placeholder = "e.g. SF2_defect";
            } else {
                if(sectionTarget) sectionTarget.classList.remove("hidden");
                if(sectionNew) sectionNew.classList.add("hidden");
                if(rawLabel) rawLabel.innerText = "New Alias (새로 추가할 유의어)";
                if(rawInput) rawInput.placeholder = "e.g. SF2불량";
            }
        });
    });

    // 💡 2-3. 제안/수정 폼 제출 통합 처리 (POST vs PUT)
    const proposeForm = document.getElementById("proposeTermForm");
    if(proposeForm) {
        proposeForm.addEventListener("submit", async (e) => {
            e.preventDefault();
            const formData = new FormData(proposeForm);
            
            // 숨김 필드(editTermId)에 값이 있으면 '관리자 수정 모드(PUT)'
            const editTermId = formData.get("edit_term_id");
            const aliasesStr = formData.get("aliases") || "";
            const aliasesArr = aliasesStr.split(",").map(s => s.trim()).filter(s => s.length > 0);

            // 🟢 [PUT] 관리자 수정 모드
            if (editTermId) {
                const payload = {
                    term_type: formData.get("type"),
                    canonical_name: formData.get("canonical"),
                    description: formData.get("description"),
                    aliases: aliasesArr,
                    // 💡 파라미터 값들을 함께 묶어서 보냅니다.
                    priority: parseInt(formData.get("priority") || 100),
                    search_boost: parseFloat(formData.get("search_boost") || 1.0),
                    expand_to_aliases: document.getElementById("editExpandAliases").checked ? 1 : 0
                };
                
                try {
                    const res = await fetch(`/api/dictionary/terms/${editTermId}`, {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(payload)
                    });
                    
                    if (!res.ok) {
                        let errMsg = "수정 실패";
                        try { const errData = await res.json(); errMsg = errData.detail || errMsg; } catch(e){}
                        throw new Error(errMsg);
                    }
                    
                    (window.Toast ? window.Toast.success("수정 완료되었습니다.") : alert("수정 완료되었습니다."));
                    if(typeof closeTermModal === "function") closeTermModal();
                    loadDictionaryTerms();
                } catch (err) {
                    (window.Toast
                      ? window.Toast.error(err.message || "수정 중 오류가 발생했습니다.", { title: "수정 실패" })
                      : alert("수정 중 오류가 발생했습니다:\n" + err.message));
                }
            }

            // 🔵 [POST] 사용자 제안 모드 (기존 기능)
            else {
                const kind = formData.get("proposal_mode"); 
                const payload = {
                    kind: kind,
                    raw_text: formData.get("raw_text"),
                    aliases: aliasesArr
                };

                if (kind === "new_term") {
                    payload.type = formData.get("type");
                    payload.canonical = formData.get("canonical") || payload.raw_text;
                } else {
                    const targetId = formData.get("target_id");
                    payload.target_id = parseInt(targetId);
                    const targetTerm = allDictionaryTerms.find(t => t.term_id === payload.target_id);
                    payload.type = targetTerm ? targetTerm.term_type : "unknown";
                }

                try {
                    const res = await fetch("/api/dictionary/propose", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(payload)
                    });
                    if (!res.ok) throw new Error("제안 실패");

                    (window.Toast
                      ? window.Toast.success("관리자 승인 후 반영됩니다.", { title: "제안 등록 완료" })
                      : alert("성공적으로 제안되었습니다. 관리자 승인 후 반영됩니다."));
                    if(typeof closeTermModal === "function") closeTermModal();
                } catch (err) {
                    (window.Toast
                      ? window.Toast.error("제안 중 오류가 발생했습니다.")
                      : alert("제안 중 오류가 발생했습니다."));
                }
            }
        });
    }
});

// 💡 3. 관리자 전용 삭제 API 호출
window.deleteTerm = async function(termId) {
    const ok = window.Toast
      ? await window.Toast.confirm(
          "비활성화되어 검색 엔진에서 즉시 제외됩니다.",
          { title: "이 용어를 삭제하시겠습니까?", okText: "삭제", destructive: true })
      : confirm("정말 이 용어를 삭제하시겠습니까?\n(비활성화되어 검색 엔진에서 즉시 제외됩니다)");
    if (!ok) return;
    try {
        const res = await fetch(`/api/dictionary/terms/${termId}`, { method: 'DELETE' });
        if (!res.ok) throw new Error('삭제 처리 실패');
        (window.Toast ? window.Toast.success("삭제 완료되었습니다.") : alert("삭제 완료되었습니다."));
        loadDictionaryTerms();
    } catch(e) {
        (window.Toast
          ? window.Toast.error(e.message || "삭제 처리 실패", { title: "오류" })
          : alert("오류가 발생했습니다: " + e.message));
    }
}

// 💡 4. 관리자 전용 수정 모달 열기 (기존 Add Modal 재활용)
window.openEditTermModal = function(termId) {
    const term = allDictionaryTerms.find(t => t.term_id === termId);
    if(!term) return;

    const modal = document.getElementById('addTermModal');
    const form = document.getElementById('proposeTermForm');
    if(form) form.reset();

    // UI 모드 세팅 (생략)
    document.getElementById('termModalTitle').innerText = "Edit Term (용어 수정)";
    document.getElementById('proposalModeSection').classList.add('hidden'); 
    document.getElementById('sectionCandidateRaw').classList.add('hidden'); 
    document.getElementById('rawTextInput').required = false; 
    document.getElementById('sectionNewTermDetails').classList.remove('hidden');
    document.getElementById('sectionAdminEdit').classList.remove('hidden'); 

    // 기존 데이터 폼에 주입
    document.getElementById('editTermId').value = term.term_id;
    document.querySelector('#proposeTermForm select[name="type"]').value = term.term_type;
    document.querySelector('#proposeTermForm input[name="canonical"]').value = term.canonical_name;
    document.getElementById('termAliasesInput').value = term.aliases || "";
    
    // 💡 어드민 파라미터 주입! (undefined 일 경우 기본값 처리)
    const editDesc = document.getElementById('editDescription');
    if(editDesc) editDesc.value = term.description || "";
    
    document.getElementById('editPriority').value = term.priority !== undefined ? term.priority : 100;
    
    const boostVal = term.search_boost !== undefined ? term.search_boost : 1.0;
    document.getElementById('editSearchBoost').value = boostVal;
    document.getElementById('editBoostVal').innerText = parseFloat(boostVal).toFixed(1);
    
    document.getElementById('editExpandAliases').checked = (term.expand_to_aliases !== 0); // 0이 아니면 무조건 체크

    document.getElementById('submitProposalBtn').innerHTML = `<span class="material-symbols-outlined text-sm">save</span> 수정 완료`;
    modal.classList.remove('hidden');
    modal.classList.add('flex');
}