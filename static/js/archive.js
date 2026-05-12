console.log("🔥 archive.js (Dashboard-Style Archive + Bulletproof 렌더링) 로드됨!");

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
                if(expandIcon) expandIcon.innerText = "open_in_full";
            } else {
                leftPanel.classList.add("hidden");
                if(resizer) resizer.classList.add("hidden");
                if(expandIcon) expandIcon.innerText = "close_fullscreen";
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
    const countEl = document.getElementById("totalDocsCount");
    if (countEl) countEl.innerText = "문서 불러오는 중...";
    
    listEl.innerHTML = `
        <div class="col-span-2 flex flex-col items-center justify-center h-40 text-secondary">
            <span class="material-symbols-outlined animate-spin text-4xl mb-3 text-primary">progress_activity</span>
            <span class="text-sm font-bold">문서를 검색하고 있습니다...</span>
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
        if (!res.ok) throw new Error("서버 응답 오류 (상태 코드: " + res.status + ")");
        
        const data = await res.json();
        
        if (data.total_fetched !== undefined && !isAppend) {
            const countEl = document.getElementById("totalDocsCount");
            if(countEl) countEl.innerText = `검색 결과: ${data.total_fetched}건`;
        }

        // 데이터가 배열인지 안전하게 확인 후 렌더링
        const docsArray = Array.isArray(data.documents) ? data.documents : [];
        renderCards(docsArray, isAppend);
        
        if (data.has_more && moreBtn) {
            moreBtn.classList.remove("hidden");
            currentSkip += PAGE_LIMIT;
        } else if (moreBtn) {
            moreBtn.classList.add("hidden");
        }
    } catch (err) {
        console.error("🔥 fetchDocuments 에러 발생:", err);
        const countEl = document.getElementById("totalDocsCount");
        if(countEl) countEl.innerText = "로딩 실패";
        
        if(listEl) {
            listEl.innerHTML = `
                <div class="col-span-2 text-center text-error mt-10 bg-error/10 p-4 rounded-xl border border-error/20">
                    <span class="material-symbols-outlined text-3xl mb-2">error</span>
                    <p class="font-bold">데이터를 불러오거나 화면에 그리는 중 문제가 발생했습니다.</p>
                    <p class="text-xs opacity-80 mt-1">${err.message}</p>
                </div>
            `;
        }
    }
}


// archive.js 내 renderCards 함수 교체
function renderCards(docs, isAppend) {
    const listEl = document.getElementById("archiveList");
    if(!listEl) return;
    
    if (!isAppend) listEl.innerHTML = "";

    if (docs.length === 0 && !isAppend) {
        listEl.innerHTML = `<div class="col-span-2 text-center text-sm font-bold text-secondary mt-10 p-8 bg-surface-container-highest rounded-2xl">조건에 맞는 검색 결과가 없습니다.</div>`;
        return;
    }

    docs.forEach(doc => {
        const af = doc.additionalField || {};
        
        let displayTitle = (doc.title || "(제목 없음)")
            .replace(/\.enriched$/i, "")
            .replace(/_/g, " ");

        const dateRaw = af.mail_date || doc.mail_date;
        const dateStr = dateRaw ? new Date(dateRaw).toLocaleDateString('ko-KR') : "날짜 없음";
        
        const versionTag = af.version_tag || "DOC";
        // 💡 요약 문구 기본값 제거 (데이터가 있을 때만 노출)
        const summary = af.summary || ""; 
        
        const sender = af.mail_from || doc.mail_from; 
        const links = Array.isArray(af.report_links) ? af.report_links : (Array.isArray(doc.report_links) ? doc.report_links : []);
        
        let tagsHtml = "";
        
        // 💡 분석 담당자: '#' 제거, 사람 아이콘 유지
        if (sender) {
            tagsHtml += `
                <span class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-primary/10 text-primary text-[10px] font-bold truncate max-w-full" title="${escapeHtml(sender)}">
                    <span class="material-symbols-outlined text-[14px]">person</span>
                    ${escapeHtml(sender)}
                </span>
            `;
        }
        
        // 💡 리포트 URL: '#' 제거, 링크 아이콘 유지, URL 원본 노출
        if (links.length > 0) {
            links.forEach(link => {
                tagsHtml += `
                    <a href="${escapeHtml(link)}" target="_blank" onclick="event.stopPropagation()" 
                       class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-secondary/10 hover:bg-secondary/20 text-secondary text-[10px] font-bold truncate max-w-full transition-all" title="${escapeHtml(link)}">
                        <span class="material-symbols-outlined text-[14px]">link</span>
                        ${escapeHtml(link)}
                    </a>
                `;
            });
        }

        const card = document.createElement("div");
        // 💡 min-h-[255px] 적용 및 p-6로 내부 여백 확보
        card.className = "archive-card group bg-surface-container-lowest border border-surface-container rounded-2xl p-6 shadow-sm hover:shadow-md hover:border-primary/40 transition-all cursor-pointer flex flex-col min-h-[255px] overflow-hidden";
        
        card.innerHTML = `
            <div class="flex justify-between items-start mb-4 gap-2">
                <span class="text-[10px] font-extrabold text-secondary uppercase tracking-tighter bg-surface-container-highest px-2 py-0.5 rounded-md shrink-0">
                    ${escapeHtml(versionTag)}
                </span>
                <span class="text-[10px] font-semibold text-outline shrink-0">${escapeHtml(dateStr)}</span>
            </div>

            <h3 class="text-[15px] font-extrabold text-on-surface mb-3 line-clamp-3 group-hover:text-primary transition-colors font-manrope leading-tight">
                ${escapeHtml(displayTitle)}
            </h3>

            ${summary ? `
                <p class="text-[11px] text-secondary mb-5 line-clamp-3 leading-relaxed flex-1 opacity-80">
                    ${escapeHtml(summary)}
                </p>
            ` : '<div class="flex-1"></div>'}

            <div class="mt-auto pt-4 border-t border-surface-container flex flex-wrap gap-2">
                ${tagsHtml}
            </div>
        `;

        card.addEventListener("click", () => {
            document.querySelectorAll(".archive-card").forEach(c => c.classList.remove("ring-2", "ring-primary", "bg-primary/5"));
            card.classList.add("ring-2", "ring-primary", "bg-primary/5");
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
    const assets = Array.isArray(doc.assets) ? doc.assets : [];

    if (!mdRel) {
        contentEl.innerHTML = `<div class="flex flex-col items-center justify-center h-full text-error opacity-80"><span class="material-symbols-outlined text-5xl mb-4">broken_image</span><div class="font-bold">문서 원본(Markdown) 경로를 찾을 수 없습니다.</div></div>`;
        return;
    }

    try {
        const res = await fetch(`/api/view/md?rel=${encodeURIComponent(mdRel)}`, { credentials: "include" });
        if (!res.ok) throw new Error("마크다운 파일을 읽어올 수 없습니다.");
        
        const rawMdText = await res.text();
        
        const processedText = preProcessMarkdown(rawMdText);
        const mdNoMeta = stripLeadingMailMetaBlock(processedText);
        const mdWithImgs = injectImagesIntoMarkdown(mdNoMeta, assets);
        
        contentEl.innerHTML = renderDocumentMarkdown(mdWithImgs);

        contentEl.querySelectorAll("img").forEach(img => {
            if(img.classList.contains("max-w-full")) return; 
            img.className = "max-w-full h-auto rounded-xl cursor-pointer hover:opacity-90 transition-opacity my-6 shadow-sm border border-outline/10";
            img.onclick = () => showImgPreview(img.src);
        });

    } catch (e) {
        contentEl.innerHTML = `<div class="text-error text-center mt-10 font-bold">${escapeHtml(String(e))}</div>`;
    }
}

// ==========================================
// 유틸리티 함수 모음
// ==========================================

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

// 💡 핵심 픽스: 어떤 타입이 들어와도 안전하게 문자열(String)로 변환 후 이스케이프 처리
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

function preProcessMarkdown(mdText) {
    let t = String(mdText || "");
    t = t.replace(/\[\s*placeholder\s*\]/gi, "");
    const truncRegex = /\.\/images\/\|attachments\/inline|<img\s+src=/i;
    const match = t.match(truncRegex);
    if (match) {
        t = t.substring(0, match.index);
    }
    return t;
}