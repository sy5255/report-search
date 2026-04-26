console.log("🔥 archive.js (Top Docs 전처리 로직 완벽 복원 버전) 로드됨!");

let currentSkip = 0;
const PAGE_LIMIT = 20;
let currentQuery = "";
let currentSort = "desc";

function initArchive() {
    if(typeof marked !== "undefined"){
        marked.setOptions({ gfm: true, breaks: true });
    }

    fetchDocuments(false);

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
        loadMoreBtn.addEventListener("click", () => {
            fetchDocuments(true);
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
                expandIcon.innerText = "open_in_full";
            } else {
                leftPanel.classList.add("hidden");
                if(resizer) resizer.classList.add("hidden");
                expandIcon.innerText = "close_fullscreen";
            }
        });
    }
    setupArchiveResizer();
}


if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initArchive);
} else {
    initArchive();
}

function showLoading(listEl) {
    listEl.innerHTML = `
        <div class="flex flex-col items-center justify-center h-32 text-secondary">
            <span class="material-symbols-outlined animate-spin text-3xl mb-2 text-primary">progress_activity</span>
            <span class="text-xs font-bold">문서를 검색 중입니다...</span>
        </div>
    `;
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
        const url = `/api/archive/documents?q=${encodeURIComponent(currentQuery)}&skip=${currentSkip}&limit=${PAGE_LIMIT}&sort=${currentSort}`;
        const res = await fetch(url, { credentials: "include" });
        if (!res.ok) throw new Error("서버 응답 오류");
        
        const data = await res.json();
        
        if (data.total_fetched !== undefined && !isAppend) {
            const countEl = document.getElementById("totalDocsCount");
            if(countEl) countEl.innerText = `검색 결과: ${data.total_fetched}건`;
        }

        renderCards(data.documents || [], isAppend);
        
        if (data.has_more && moreBtn) {
            moreBtn.classList.remove("hidden");
            currentSkip += PAGE_LIMIT;
        } else if (moreBtn) {
            moreBtn.classList.add("hidden");
        }
    } catch (err) {
        if(listEl) listEl.innerHTML = `<div class="text-center text-error mt-10">데이터를 불러오지 못했습니다.</div>`;
    }
}


function renderCards(docs, isAppend) {
    const listEl = document.getElementById("archiveList");
    if(!listEl) return;
    
    if (!isAppend) listEl.innerHTML = "";

    if (docs.length === 0 && !isAppend) {
        listEl.innerHTML = `<div class="text-center text-xs text-secondary mt-10">결과가 없습니다.</div>`;
        return;
    }

    docs.forEach(doc => {
        let displayTitle = (doc.title || "(제목 없음)")
            .replace(/\.enriched$/i, "")
            .replace(/_/g, " ");

        const dateStr = doc.mail_date ? new Date(doc.mail_date).toLocaleDateString('ko-KR') : "";
        
        let tagsHtml = "";
        if (doc.mail_from) {
            tagsHtml += `<span class="query-chip text-[10px] mr-1 mb-1">#${escapeHtml(doc.mail_from)}</span>`;
        }
        if (doc.report_links && doc.report_links.length > 0) {
            doc.report_links.forEach(link => {
                tagsHtml += `<a href="${escapeHtml(link)}" target="_blank" class="query-chip text-primary border-primary/30 bg-primary/5 text-[10px] mr-1 mb-1 hover:bg-primary/10 transition-colors">#${escapeHtml(link)}</a>`;
            });
        }

        const card = document.createElement("div");
        card.className = "archive-card bg-surface p-4 rounded-xl border border-surface-container cursor-pointer hover:border-primary/50 hover:shadow-sm transition-all group";
        
        card.innerHTML = `
            <div class="flex justify-between items-start mb-2 gap-2">
                <h3 class="text-[13px] font-bold text-on-surface line-clamp-2 group-hover:text-primary transition-colors">${escapeHtml(displayTitle)}</h3>
                <span class="text-[10px] font-medium text-secondary whitespace-nowrap bg-surface-container-low px-1.5 py-0.5 rounded">${dateStr}</span>
            </div>
            <div class="flex flex-wrap mt-2">${tagsHtml}</div>
        `;

        card.addEventListener("click", () => {
            document.querySelectorAll(".archive-card").forEach(c => c.classList.remove("ring-1", "ring-primary", "bg-primary/5"));
            card.classList.add("ring-1", "ring-primary", "bg-primary/5");
            openViewer(doc, displayTitle);
        });
        
        listEl.appendChild(card);
    });
}


async function openViewer(doc, displayTitle) {
    const titleEl = document.getElementById("viewerTitle");
    if(titleEl) titleEl.innerText = displayTitle;
    
    const contentEl = document.getElementById("viewerContent");
    if(!contentEl) return;
    
    contentEl.innerHTML = `
        <div class="flex flex-col items-center justify-center h-full text-secondary">
            <span class="material-symbols-outlined animate-spin text-4xl mb-3 text-primary">autorenew</span>
            <span class="text-sm font-bold">문서를 렌더링하는 중입니다...</span>
        </div>
    `;

    const mdRel = doc.storage?.parsed_md_rel_path;
    const assets = doc.assets || [];

    if (!mdRel) {
        contentEl.innerHTML = `<div class="text-error text-center mt-10 font-bold">문서 원본 경로를 찾을 수 없습니다.</div>`;
        return;
    }

    try {
        const res = await fetch(`/api/view/md?rel=${encodeURIComponent(mdRel)}`, { credentials: "include" });
        if (!res.ok) throw new Error("마크다운 파일을 읽어올 수 없습니다.");
        
        const mdText = await res.text();
        
        // 💡 chat.js의 검증된 로직 100% 동일하게 사용!
        const mdNoMeta = stripLeadingMailMetaBlock(mdText);
        const mdWithImgs = injectImagesIntoMarkdown(mdNoMeta, assets);
        
        contentEl.innerHTML = renderDocumentMarkdown(mdWithImgs);

    } catch (e) {
        contentEl.innerHTML = `<div class="text-error text-center mt-10">${escapeHtml(String(e))}</div>`;
    }
} 


// ==========================================
// 💡 아래부터는 chat.js에서 그대로 가져온 완벽한 유틸리티 함수들입니다.
// ==========================================

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
  
    return t.trimStart(); // 💡 이 부분 때문에 코드 블록에 갇히는 현상이 사라집니다!
}


function renderDocumentMarkdown(mdText){
    const raw = String(mdText || "");
  
    if(typeof marked === "undefined"){
      return escapeHtml(raw).replace(/\n/g, "<br>");
    }
  
    let rendered = marked.parse(raw);
  
    if(typeof DOMPurify !== "undefined"){
      rendered = DOMPurify.sanitize(rendered, {
        USE_PROFILES: { html: true },
        ADD_TAGS: ['details', 'summary']
      });
    }
  
    return rendered;
}

function injectImagesIntoMarkdown(mdText, assets){
    if(!mdText) return mdText || "";
    if(!assets || !assets.length) return mdText.replace(/\[placeholder\]/gi, "");
  
    const imgs = assets.map(a => (a && a.path) ? a : null).filter(Boolean);
    if(!imgs.length) return mdText.replace(/\[placeholder\]/gi, "");
  
    let i = 0;
    return mdText.replace(/\[placeholder\]/gi, () => {
        if(i >= imgs.length) return ""; 
  
        const a = imgs[i++];
        const url = `/api/view/asset?rel=${encodeURIComponent(a.path)}`;
        const alt = (a.file_name || a.path || "image").replace(/[\r\n]+/g, " ");
  
        return `
            <div class="my-4 md-embed-img-wrap rounded-lg overflow-hidden border border-surface-container bg-surface-container-lowest">
                <img src="${url}" alt="${escapeHtml(alt)}" loading="lazy" class="max-w-full h-auto mx-auto cursor-pointer hover:opacity-90 transition-opacity" onclick="showImgPreview('${url}')">
            </div>
        `;
    });
}

function showImgPreview(src){
    const pv = document.getElementById("imgPreview");
    if(!pv) return;
    pv.innerHTML = `<img src="${src}" class="max-h-full max-w-full object-contain" />`;
    pv.classList.remove("hidden");
    pv.onclick = () => { pv.classList.add("hidden"); pv.innerHTML = ""; };
}

function escapeHtml(s){
    return (s||"").replace(/[&<>"']/g, (m)=>({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;" }[m]));
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
        const x = e.clientX;
        const max = window.innerWidth * 0.6;
        const w = Math.max(300, Math.min(max, x));
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