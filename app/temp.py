chat.js에서 추가한 함수인데 문제 생길 요소를 평가해줘.

// ✨ [업그레이드] 마크다운 전처리 함수 (다중 절단 조건 지원)
function preProcessMarkdown(mdText) {
  let t = String(mdText || "");

  // 1) '[placeholder]' 완벽 제거 (대소문자, 공백 무시)
  t = t.replace(/\[\s*placeholder\s*\]/gi, "");

  // 2) 특정 문자열 이후 텍스트 모두 날리기 (조건 통합 방어)
  const truncRegex = /\.\/images\/\[(?:i|l)nline\s*FA\s*Report\]|attachments\/inline/i;
  const match = t.match(truncRegex);
  
  if (match) {
      t = t.substring(0, match.index); // 가장 먼저 매칭된 문자열 시작점 앞까지만 남기고 싹둑
  }

  return t;
}

아래는 변경한 함수야. 문제 없는지 이것도 체크해줘.
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

아래도 변경한 함수야.
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