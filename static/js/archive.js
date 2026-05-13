console.log("🔥 archive.js (다중 선택 커스텀 드롭다운 & 용어사전 듀얼모드 적용 완료) 로드됨!");

let currentSkip = 0;
const PAGE_LIMIT = 20;
let currentQuery = "";
let currentSort = "desc";

// 💡 1. 다중 필터 및 용어사전용 전역 변수
let currentAuthors = []; 
let currentStartDate = "";
let currentEndDate = "";
let allDictionaryTerms = []; // 모달 드롭다운용 저장소

function initArchive() {
    if(typeof marked !== "undefined"){
        marked.setOptions({ gfm: true, breaks: true });
    }

    // 서버에서 담당자 목록, 문서 목록, 그리고 용어사전 목록을 모두 초기 로드!
    fetchFilterData();
    fetchDocuments(false);
    loadDictionaryTerms(); // 💡 누락되었던 용어사전 로딩 함수 추가!

    // 검색창 이벤트
    const searchInput = document.getElementById("archiveSearchInput");
    if(searchInput) {
        searchInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                currentQuery = e.target.value.trim();
                fetchDocuments(false);
            }
        });
    }

    // 정렬 이벤트
    const sortBtn = document.getElementById("sortToggleBtn");
    if(sortBtn) {
        sortBtn.addEventListener("click", () => {
            currentSort = currentSort === "desc" ? "asc" : "desc";
            document.getElementById("sortToggleText").innerText = currentSort === "desc" ? "최신순" : "오래된순";
            fetchDocuments(false);
        });
    }

    // Load More 이벤트
    const loadMoreBtn = document.getElementById("loadMoreBtn");
    if(loadMoreBtn) {
        loadMoreBtn.addEventListener("click", () => {
            fetchDocuments(true);
        });
    }

    // 우측 화면 확장 토글 이벤트
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
    
    // ============================================
    // 💡 2. 필터 패널 확장 토글 이벤트
    // ============================================
    const filterToggleBtn = document.getElementById("filterToggleBtn");
    const filterPanel = document.getElementById("filterPanel");
    
    if(filterToggleBtn && filterPanel) {
        filterToggleBtn.addEventListener("click", () => {
            if (filterPanel.classList.contains("hidden")) {
                filterPanel.classList.remove("hidden");
                filterPanel.classList.add("flex");
                filterToggleBtn.classList.add("bg-surface-container", "text-primary");
            } else {
                filterPanel.classList.add("hidden");
                filterPanel.classList.remove("flex");
                filterToggleBtn.classList.remove("bg-surface-container", "text-primary");
            }
        });
    }

    // ============================================
    // 💡 3. 커스텀 담당자 드롭다운 이벤트
    // ============================================
    const authorBtn = document.getElementById("authorDropdownBtn");
    const authorList = document.getElementById("authorDropdownList");
    
    if(authorBtn && authorList) {
        // 버튼 클릭 시 리스트 열고 닫기
        authorBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            authorList.classList.toggle("hidden");
            authorBtn.classList.toggle("ring-2");
            authorBtn.classList.toggle("ring-primary/20");
        });
        
        // 리스트 바깥쪽(배경) 클릭 시 리스트 닫기
        document.addEventListener("click", () => {
            authorList.classList.add("hidden");
            authorBtn.classList.remove("ring-2", "ring-primary/20");
        });

        // 리스트 내부를 클릭했을 때는 닫히지 않도록 이벤트 전파 막기
        authorList.addEventListener("click", (e) => {
            e.stopPropagation();
        });
    }

    // 날짜 시작일 변경 이벤트
    const startDateInput = document.getElementById("filterStartDate");
    if(startDateInput) {
        startDateInput.addEventListener("change", (e) => {
            currentStartDate = e.target.value;
            fetchDocuments(false);
        });
    }

    // 날짜 종료일 변경 이벤트
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

// 💡 4. 담당자 데이터 로드 및 드롭다운 체크박스 생성
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
            
            // 한 줄로 통일하고 전체 bold 처리
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

// 💡 5. 선택된 담당자 수에 따라 드롭다운 버튼 텍스트 변경
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

// 💡 6. 필터 파라미터를 담아 문서 검색 API 호출
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
            author: currentAuthors.join(","), // 배열을 문자열(콤마 구분)로 변환
            start_date: currentStartDate,
            end_date: currentEndDate,
            skip: currentSkip,
            limit: PAGE_LIMIT,
            sort: currentSort
        });

        const url = `/api/archive/documents?${params.toString()}`;
        const res = await fetch(url, { credentials: "include" });
        if (!res.ok) throw new Error("서버 응답 오류 (상태 코드: " + res.status + ")");
        
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

// 문서 카드 렌더링 함수
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
        const summary = af.summary || ""; 
        
        const sender = af.mail_from || doc.mail_from; 
        const links = Array.isArray(af.report_links) ? af.report_links : (Array.isArray(doc.report_links) ? doc.report_links : []);
        
        let tagsHtml = "";
        
        if (sender) {
            tagsHtml += `
                <span class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-primary/10 text-primary text-[10px] font-bold truncate max-w-full" title="${escapeHtml(sender)}">
                    <span class="material-symbols-outlined text-[14px]">person</span>
                    ${escapeHtml(sender)}
                </span>
            `;
        }
        
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


// 뷰어 창 오픈 함수
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

// 유틸리티 함수들
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

// ==========================================
// 💡 Term Dictionary & Add Term Modal 로직 (신규 반영 부분)
// ==========================================

// 1. 용어 사전 데이터 로드 및 초기 렌더링
async function loadDictionaryTerms() {
    const tbody = document.getElementById("dictionaryTableBody");
    const targetSelect = document.getElementById("targetTermSelect");
    if (!tbody) return;

    try {
        const res = await fetch("/api/dictionary/terms");
        if (!res.ok) throw new Error("서버 응답 오류");
        
        const data = await res.json();
        allDictionaryTerms = data.terms || [];
        
        // A. 모달의 대상 용어 드롭다운 채우기 (처음 한 번만)
        if (targetSelect) {
            targetSelect.innerHTML = "";
            allDictionaryTerms.forEach(term => {
                const opt = document.createElement("option");
                opt.value = term.term_id;
                opt.textContent = `[${term.term_type.toUpperCase()}] ${term.canonical_name}`;
                targetSelect.appendChild(opt);
            });
        }

        // B. 전체 데이터 렌더링
        renderDictionaryTable(allDictionaryTerms);

    } catch (error) {
        console.error("🔥 사전 데이터 로딩 실패:", error);
        tbody.innerHTML = `<tr><td colspan="4" class="px-8 py-8 text-center text-error text-sm font-bold">데이터 로딩 오류</td></tr>`;
    }
}

// 💡 1-1. 검색 필터링을 위해 테이블 그리는 로직을 별도 함수로 분리
function renderDictionaryTable(termsToRender) {
    const tbody = document.getElementById("dictionaryTableBody");
    if (!tbody) return;

    tbody.innerHTML = "";

    if (termsToRender.length === 0) {
        tbody.innerHTML = `<tr><td colspan="4" class="px-8 py-12 text-center text-secondary text-sm font-bold bg-surface-container-lowest/50 rounded-xl">검색 조건에 맞는 용어가 없습니다.</td></tr>`;
        return;
    }

    termsToRender.forEach(term => {
        const description = term.description ? escapeHtml(term.description) : `<span class="text-outline italic">설명 없음</span>`;
        const aliases = term.aliases ? `<div class="mt-2 text-[11px] text-primary bg-primary/5 inline-block px-2 py-0.5 rounded border border-primary/10 font-semibold">Aliases: ${escapeHtml(term.aliases)}</div>` : '';

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
                <button class="p-2 text-outline-variant hover:text-error hover:bg-error/5 rounded-lg transition-all" ON-CLICK="alert('용어 삭제는 관리자 권한이 필요합니다.')" title="Delete Term">
                    <span class="material-symbols-outlined">delete</span>
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

// 2. 모달 및 사전 검색 이벤트 설정
document.addEventListener("DOMContentLoaded", () => {
    // 💡 2-1. 용어 사전 실시간 검색 이벤트 추가!
    const dictSearchInput = document.getElementById("dictSearchInput");
    if (dictSearchInput) {
        dictSearchInput.addEventListener("input", (e) => {
            const query = e.target.value.toLowerCase().trim();
            
            // 검색어가 없으면 전체 보여주기
            if (!query) {
                renderDictionaryTable(allDictionaryTerms);
                return;
            }

            // 검색어가 있으면 Term(이름), Type(카테고리), Aliases, 설명 중에서 찾아서 필터링
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

    // 2-2. 듀얼 모드 라디오 버튼 전환 로직
    const radios = document.querySelectorAll('input[name="proposal_mode"]');
    const sectionTarget = document.getElementById("sectionTargetTerm");
    const sectionNew = document.getElementById("sectionNewTermDetails");
    const rawLabel = document.getElementById("rawTextLabel");
    const rawInput = document.getElementById("rawTextInput");

    radios.forEach(radio => {
        radio.addEventListener('change', (e) => {
            const mode = e.target.value;
            if(mode === "new_term") {
                sectionTarget.classList.add("hidden");
                sectionNew.classList.remove("hidden");
                rawLabel.innerText = "Candidate Term (원문/대표 유의어)";
                rawInput.placeholder = "e.g. SF2_defect";
            } else {
                sectionTarget.classList.remove("hidden");
                sectionNew.classList.add("hidden");
                rawLabel.innerText = "New Alias (새로 추가할 유의어)";
                rawInput.placeholder = "e.g. SF2불량";
            }
        });
    });

    // 3. 폼 제출 로직 (백엔드 파이프라인 규격에 맞게 전송)
    const proposeForm = document.getElementById("proposeTermForm");
    if(proposeForm) {
        proposeForm.addEventListener("submit", async (e) => {
            e.preventDefault();
            const formData = new FormData(proposeForm);
            
            const kind = formData.get("proposal_mode"); 
            const aliasesStr = formData.get("aliases") || "";
            const aliasesArr = aliasesStr.split(",").map(s => s.trim()).filter(s => s.length > 0);

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
                
                alert("성공적으로 제안되었습니다. 관리자 승인 후 반영됩니다.");
                if(typeof closeTermModal === "function") closeTermModal();
            } catch (err) {
                console.error(err);
                alert("제안 중 오류가 발생했습니다.");
            }
        });
    }
});