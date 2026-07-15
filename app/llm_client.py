import json
import re
import uuid
from openai import OpenAI
from app.config import (
    LLM_API_BASE_URL,
    LLM_TICKET,
    SEND_SYSTEM_NAME,
    USER_TYPE,
)
# build_glossary_from_texts는 함수 내부에서 지연 import한다(모듈 로드 시 app.db/mysql 의존을 끌지 않게).

ANSWER_MAX_TOKENS = 3000
CITATION_MAX_TOKENS = 4000  # 인용 JSON은 claim×quote로 커질 수 있어 여유 확보(잘림 방지)
REWRITE_MAX_TOKENS = 3000 #220

def _safe_json_extract(text: str) -> dict:
    t = (text or "").strip()
    i = t.find("{")
    j = t.rfind("}")
    if i == -1 or j == -1 or j <= i:
        raise ValueError(f"LLM output is not JSON: {t[:1000]}")

    candidate = t[i:j+1]
    try:
        return json.loads(candidate)
    except Exception:
        print("=== LLM RAW OUTPUT BEGIN ===")
        print(candidate[:5000])
        print("=== LLM RAW OUTPUT END ===")
        raise


def _make_client(user_id: str) -> OpenAI:
    return OpenAI(
        base_url=LLM_API_BASE_URL,
        api_key="EMPTY",
        default_headers={
            "x-dep-ticket": LLM_TICKET,
            "Send-System-Name": SEND_SYSTEM_NAME,
            "User-Id": user_id,
            "User-Type": USER_TYPE,
            "Prompt-Msg-Id": str(uuid.uuid4()),
            "Completion-Msg-Id": str(uuid.uuid4()),
        }
    )


def rewrite_query_with_history(
    user_id: str,
    user_question: str,
    previous_messages: list[dict] | None = None
) -> str:
    client = _make_client(user_id)

    system = (
        "You rewrite a follow-up user question into a standalone search query for RAG retrieval.\n"
        "Use the previous conversation only to resolve references such as 'that document', 'it', 'above', etc.\n"
        "Do NOT answer the question.\n"
        "Do NOT add unsupported facts.\n"
        "Return STRICT JSON only.\n"
    )

    payload = {
        "task": "Rewrite the user's latest question into a standalone search query.",
        "rules": [
            "Return JSON only.",
            "Preserve the user's intent exactly.",
            "Resolve ambiguous references using previous conversation when possible.",
            "Keep the rewritten query concise but specific.",
            "If the latest question is already standalone, keep it almost unchanged."
        ],
        "output_schema": {
            "standalone_query": "string"
        },
        "conversation": previous_messages or [],
        "latest_user_question": user_question
    }

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
    ]

    completion = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=messages,
        temperature=0.0,
        stream=False,
        max_tokens=REWRITE_MAX_TOKENS,
    )

    text = completion.choices[0].message.content
    parsed = _safe_json_extract(text)
    rewritten = (parsed.get("standalone_query") or "").strip()
    return rewritten or user_question


def _build_context_summary(previous_messages: list[dict] | None, max_items: int = 4) -> list[dict]:
    if not previous_messages:
        return []

    trimmed = previous_messages[-max_items:]
    out = []
    for m in trimmed:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        out.append({
            "role": role,
            "content": content[:1200]
        })
    return out


def _format_glossary_block(glossary: list[dict], unknown_acronyms: list[str] | None = None) -> str:
    """감지된 사내 용어 목록(+미등록 약어 전개 금지 목록)을 답변 LLM용 system 메시지로 조립."""
    parts = []
    lines = []
    for g in (glossary or []):
        canon = (g.get("canonical_name") or "").strip()
        if not canon:
            continue
        typ = (g.get("term_type") or "").strip()
        desc = (g.get("description") or "").strip()
        forms = [f for f in (g.get("surface_forms") or []) if f]
        alias_part = f" [문서 표기: {', '.join(forms)}]" if forms else ""
        desc_part = f" — {desc}" if desc else ""
        type_part = f" ({typ})" if typ else ""
        lines.append(f"- 정식명칭 '{canon}'{type_part}{alias_part}{desc_part}")
    if lines:
        parts.append(
            "[사내 용어 GLOSSARY — 권위 기준]\n"
            "아래는 사내 표준 용어사전입니다. EVIDENCE(근거 문서)에는 약어가 비일관적이거나 잘못 표기될 수 "
            "있습니다. 이 GLOSSARY를 권위 있는 기준으로 삼으세요. 해당 용어를 답변에 쓸 때는 정식 명칭을 "
            "사용하고, 최초 언급 시 약어를 괄호로 병기하세요(예: 정식명칭(약어)). 근거 문서의 약어를 그대로 "
            "베끼지 마세요. 이 용어 정규화는 필수이며, 사실을 지어내는 것이 아닙니다.\n\n"
            + "\n".join(lines)
        )
    acr = [a for a in (unknown_acronyms or []) if a]
    if acr:
        parts.append(
            "[전개 금지 — 뜻 미확인 약어]\n"
            "다음 약어들은 사내 전용 용어로 보이지만 GLOSSARY에 등록되어 있지 않아 뜻을 확인할 수 없습니다. "
            "반드시 원문 표기 그대로 사용하고, 절대 풀어쓰거나(전개하거나) 뜻을 추측해 괄호로 병기하지 "
            "마세요. 일반 상식의 동명 약어와 다른 사내 고유 의미일 수 있습니다. 뜻 설명이 꼭 필요하면 "
            "'사내 용어사전에 미등록된 약어'라고만 명시하세요.\n"
            f"목록: {', '.join(acr)}"
        )
    return "\n\n".join(parts)


# ---------- 용어 가드: 근거 없는 약어 전개(할루시네이션) 결정적 탐지/제거 ----------

def _norm_guard_text(s: str) -> str:
    return re.sub(r"[\s\-_·/]+", " ", str(s or "").lower()).strip()


_ACRONYM_SIDE_RE = re.compile(r"^[A-Z][A-Z0-9\-]{1,7}$")
# 패턴 A: 약어(풀이) — 예: PID(Plasma Induced Damage), PID(비례적분미분)
_EXPANSION_A_RE = re.compile(r"\b([A-Z][A-Z0-9\-]{1,7})\s*\(([^()\n]{2,80})\)")
# 패턴 B: 풀이(약어) — 예: 플라즈마 유발 손상(PID). 괄호 앞 최대 6단어 캡처
_EXPANSION_B_RE = re.compile(
    r"((?:[A-Za-z가-힣0-9\-·]+\s){0,5}[A-Za-z가-힣0-9\-·]+)\s*\(([A-Z][A-Z0-9\-]{1,7})\)"
)


def _find_unverified_expansions(answer: str, evidence_texts: list[str], glossary: list[dict]) -> list[dict]:
    """
    답변 속 '약어(풀이)'/'풀이(약어)' 병기 중, 약어가 GLOSSARY에 없고 풀이도 근거 원문에 없는
    쌍(=LLM이 임의로 지어낸 전개)을 찾아 반환. [{match, abbr, expansion, pattern}]
    """
    text = str(answer or "")
    if not text:
        return []

    evidence_blob = _norm_guard_text(" ".join(evidence_texts or []))

    allowed_abbrs = set()
    allowed_names = set()
    for g in (glossary or []):
        cn = _norm_guard_text(g.get("canonical_name") or "")
        if cn:
            allowed_names.add(cn)
            allowed_abbrs.add(cn)
        for f in (g.get("surface_forms") or []):
            nf = _norm_guard_text(f)
            if nf:
                allowed_abbrs.add(nf)

    def _abbr_ok(abbr: str) -> bool:
        return _norm_guard_text(abbr) in allowed_abbrs

    def _phrase_in_evidence(phrase: str) -> bool:
        np = _norm_guard_text(phrase)
        return bool(np) and (np in evidence_blob or np in allowed_names)

    violations = []
    seen_spans = set()

    # 패턴 A: 약어(풀이)
    for m in _EXPANSION_A_RE.finditer(text):
        abbr, exp = m.group(1), m.group(2).strip()
        # 풀이로 보이지 않는 괄호(수치·URL·전대문자 토큰·인용마커 등)는 스킵
        if "http" in exp.lower() or "/" in exp:
            continue
        if not re.search(r"[a-z가-힣]", exp) and " " not in exp:
            continue
        if len(re.findall(r"[A-Za-z가-힣]", exp)) < 3:
            continue
        if _abbr_ok(abbr):
            continue
        if _phrase_in_evidence(exp):
            continue
        span = m.span()
        if span in seen_spans:
            continue
        seen_spans.add(span)
        violations.append({"match": m.group(0), "abbr": abbr, "expansion": exp, "pattern": "A"})

    # 패턴 B: 풀이(약어) — 캡처가 문장 앞부분을 더 물 수 있어 접미(마지막 k단어) 축소 검증
    for m in _EXPANSION_B_RE.finditer(text):
        exp, abbr = m.group(1).strip(), m.group(2)
        if _ACRONYM_SIDE_RE.fullmatch(exp):
            continue  # 전대문자 토큰(코드명 쌍 등)은 풀이로 취급하지 않음
        if _abbr_ok(abbr):
            continue
        words = exp.split()
        verified = False
        for k in range(len(words), 0, -1):
            if _phrase_in_evidence(" ".join(words[-k:])):
                verified = True
                break
        if verified:
            continue
        span = m.span()
        if span in seen_spans:
            continue
        seen_spans.add(span)
        violations.append({"match": m.group(0), "abbr": abbr, "expansion": exp, "pattern": "B"})

    return violations


def _strip_unverified_expansions(answer: str, violations: list[dict]) -> str:
    """
    교정 재생성 후에도 남은 위반을 결정적으로 제거. 패턴 A('약어(풀이)')만 안전하게 치환
    (약어 원문만 남김). 패턴 B는 캡처 경계가 문장 일부를 물 수 있어 치환하지 않고 로그만 남긴다.
    """
    out = str(answer or "")
    for v in (violations or []):
        if v.get("pattern") == "A":
            out = out.replace(v["match"], v["abbr"])
        else:
            print(f"[term-guard] pattern-B violation left as-is (unsafe to strip): {v['match']!r}")
    return out


def _build_answer_prompt(
    user_question: str,
    rag_chunks: list[dict],
    previous_messages: list[dict] | None = None,
    style_rules: str | None = None,
    glossary: list[dict] | None = None,
    unknown_acronyms: list[str] | None = None
) -> list[dict]:
    system = (
        "You are a helpful RAG answerer.\n"
        "You must use ONLY the provided EVIDENCE for factual claims.\n"
        "Do NOT invent facts.\n"
        "If evidence is insufficient, clearly say what is uncertain.\n"
        "Write a thorough, natural, well-connected Korean explanation that reads like an expert "
        "analyst: connect sentences into flowing paragraphs, and explain the mechanism(원리), "
        "cause(원인), and implication(함의) when the evidence supports it. "
        "Do NOT write terse one-fact-per-line lists.\n"
        "If a GLOSSARY system message is provided, follow its terminology-normalization instructions: "
        "use the official canonical name and expand abbreviations; this is REQUIRED and is not "
        "considered inventing facts.\n"
        "CRITICAL: abbreviations in this domain are company-internal. NEVER expand or spell out an "
        "abbreviation that is not defined in the GLOSSARY or verbatim in the EVIDENCE — guessing an "
        "expansion from general knowledge is a serious error. Keep unknown abbreviations exactly as "
        "written in the evidence.\n"
        "Return STRICT JSON only.\n"
    )

    evidence_lines = []
    for idx, c in enumerate(rag_chunks, 1):
        evidence_lines.append({
            "no": idx,
            "doc_id": c.get("doc_id"),
            "chunk_id": c.get("chunk_id"),
            "title": c.get("title"),
            "text": (c.get("merge_title_content") or "")[:2500],
            "score": c.get("score"),
        })

    user_payload = {
        "task": "Answer the question naturally in Korean using ONLY the evidence.",
        "output_schema": {
            "answer_markdown": "Markdown formatted answer string"
        },
        "rules": [
            "Return JSON only.",
            "Do not wrap the JSON with markdown fences.",
            "No trailing commas.",
            "All strings must be valid JSON strings.",
            "Answer in Korean.",
            "Put the full answer into 'answer_markdown' as a single markdown string.",
            "The markdown must be inside a JSON string, not printed as raw markdown.",
            "If you do not return valid JSON, the response will be rejected.",
            "Example valid output: {\"answer_markdown\":\"## 제목\\n\\n- 항목1\\n- 항목2\"}",
            "Use markdown headings, bullet lists, or tables when they improve readability.",
            "If the user asks for a table, output a markdown table.",
            "If the user asks for code, output a markdown code block inside the answer_markdown string.",
            "Write in natural, connected Korean paragraphs. Cite each major factual claim by "
            "appending an inline citation marker like [1] at the end of the sentence/clause that "
            "relies on a specific evidence item, so the reader can trace the key statements to "
            "sources (a well-cited answer usually has several markers across it). Do NOT force a "
            "marker on every sentence, and do NOT fragment the writing into one-fact-per-line just "
            "to add markers. "
            "Use ONLY evidence numbers that exist. Do not put markers inside tables or code blocks.",
            "If a GLOSSARY is provided, normalize terms to the official canonical name and expand "
            "abbreviations (put the abbreviation in parentheses on first mention). Do not blindly "
            "copy an abbreviation from evidence when the glossary gives its official name. This "
            "normalization is required and is not inventing facts.",
            "NEVER expand, spell out, or guess the meaning of an abbreviation that is not in the "
            "GLOSSARY and not spelled out verbatim in the evidence. Company-internal abbreviations "
            "often differ from their common textbook meaning. Keep such abbreviations exactly as "
            "written. If asked what one means and no evidence defines it, answer that it is not "
            "registered in the internal term dictionary.",
            "If evidence is insufficient, say so clearly."
        ],
        "question": user_question,
        "evidence": evidence_lines
    }

    if style_rules:
        user_payload["style_rules"] = style_rules

    messages = [{"role": "system", "content": system}]
    # 💡 권위 용어사전 + 전개 금지 약어 목록을 별도 system 메시지로 직접 주입한다.
    #    (_build_context_summary는 system role을 제거하므로 그 경로를 절대 태우지 않는다 —
    #     이게 기존 사전이 답변 LLM에 도달 못 하던 원인이었음.)
    glossary_block = _format_glossary_block(glossary or [], unknown_acronyms)
    if glossary_block:
        messages.append({"role": "system", "content": glossary_block})
    messages.extend(_build_context_summary(previous_messages))
    messages.append({
        "role": "user",
        "content": json.dumps(user_payload, ensure_ascii=False)
    })
    return messages


def _build_citation_prompt(
    user_question: str,
    final_answer: str,
    rag_chunks: list[dict],
) -> list[dict]:
    system = (
        "You are a strict citation mapper.\n"
        "Use ONLY the provided EVIDENCE.\n"
        "Do NOT rewrite the answer.\n"
        "Do NOT invent claims.\n"
        "Return STRICT JSON only.\n"
    )

    evidence_lines = []
    for idx, c in enumerate(rag_chunks, 1):
        evidence_lines.append({
            "no": idx,
            "doc_id": c.get("doc_id"),
            "chunk_id": c.get("chunk_id"),
            "title": c.get("title"),
            "text": (c.get("merge_title_content") or "")[:2500],
            "score": c.get("score"),
        })

    user_payload = {
        "task": (
            "Extract the factual claims from the answer (sentence-level) and attach citations "
            "from the evidence. Also judge how well each claim is supported."
        ),
        "output_schema": {
            "claims": [
                {
                    "claim": "Short factual claim in Korean",
                    "support": "supported | partial | unsupported",
                    "citations": [
                        {
                            "doc_id": "string",
                            "chunk_id": "string",
                            "quote": "verbatim passage copied EXACTLY from the evidence text that supports the claim",
                            "score": "number or null"
                        }
                    ]
                }
            ]
        },
        "rules": [
            "Return JSON only.",
            "No markdown fences.",
            "No trailing commas.",
            "All strings must be valid JSON strings.",
            "Return up to 6 claims, covering the main factual statements of the answer.",
            "Each claim must be short and concise.",
            "Each claim should have 1 to 2 citations.",
            "Use only doc_id and chunk_id that exist in evidence.",
            "'quote' MUST be copied verbatim (character-for-character) from the evidence text, "
            "but keep it a SHORT snippet (at most ~120 characters). Never paraphrase the quote.",
            "Set support='supported' only when the evidence clearly states the claim; "
            "'partial' when related but not exact; 'unsupported' when no evidence backs it.",
            "For unsupported claims, return an empty citations array.",
            "If the answer contains uncertainty, only cite the supported factual parts."
        ],
        "question": user_question,
        "answer": final_answer,
        "evidence": evidence_lines
    }

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}
    ]


def _normalize_claims_to_answer_list(claims: list[dict] | None) -> list[dict]:
    out = []
    for c in claims or []:
        claim = (c.get("claim") or "").strip()
        support = (c.get("support") or "").strip().lower()
        if support not in ("supported", "partial", "unsupported"):
            support = "partial"
        cites = []
        for x in (c.get("citations") or []):
            cites.append({
                "doc_id": x.get("doc_id"),
                "chunk_id": x.get("chunk_id"),
                "quote": (x.get("quote") or "").strip(),
                "score": x.get("score"),
            })
        if claim:
            out.append({
                "sentence": claim,
                "support": support,
                "citations": cites
            })
    return out


def _normalize_ws(s: str) -> str:
    return " ".join(str(s or "").split()).lower()


def validate_citations(claims: list[dict] | None, rag_chunks: list[dict]) -> list[dict]:
    """LLM이 뽑은 citation을 코드로 검증한다 (지어낸 인용 차단).
    - 존재하지 않는 doc_id/chunk_id 인용은 제거.
    - quote가 해당 청크 원문의 substring이 아니면 quote를 비우고 support를 'partial'로 강등.
    - citation이 전부 제거된 claim은 support를 'unsupported'로 강등.
    """
    chunk_texts = {}
    for c in rag_chunks or []:
        key = (str(c.get("doc_id") or ""), str(c.get("chunk_id") or ""))
        chunk_texts[key] = _normalize_ws(c.get("merge_title_content") or "")
    doc_texts = {}
    for (doc_id, _), text in chunk_texts.items():
        doc_texts.setdefault(doc_id, []).append(text)

    out = []
    for c in claims or []:
        claim = (c.get("claim") or "").strip()
        if not claim:
            continue
        support = (c.get("support") or "").strip().lower()
        if support not in ("supported", "partial", "unsupported"):
            support = "partial"

        valid_cites = []
        for x in (c.get("citations") or []):
            doc_id = str(x.get("doc_id") or "")
            chunk_id = str(x.get("chunk_id") or "")
            key = (doc_id, chunk_id)
            # 존재하지 않는 문서/청크 인용 제거 (chunk_id 불일치는 doc 단위로 한 번 더 허용)
            if key in chunk_texts:
                candidate_texts = [chunk_texts[key]]
            elif doc_id in doc_texts:
                candidate_texts = doc_texts[doc_id]
            else:
                continue

            quote = (x.get("quote") or "").strip()
            if quote:
                nq = _normalize_ws(quote)
                if not nq or not any(nq in t for t in candidate_texts):
                    # 원문에 없는 인용문 → 인용문만 제거하고 지원도 강등
                    quote = ""
                    if support == "supported":
                        support = "partial"
            valid_cites.append({
                "doc_id": x.get("doc_id"),
                "chunk_id": x.get("chunk_id"),
                "quote": quote,
                "score": x.get("score"),
            })

        if not valid_cites and support != "unsupported":
            support = "unsupported"

        out.append({
            "claim": claim,
            "support": support,
            "citations": valid_cites,
        })
    return out


def _salvage_claims(text: str) -> list[dict]:
    """토큰 상한으로 잘린 인용 JSON에서 **완성된 claim 객체만** 복구한다.
    `{"claims":[ {..}, {..}, <잘림> ]}` 형태에서 균형 잡힌 {...} 객체를 순서대로 파싱하고,
    처음으로 미완성(중괄호 불균형)인 객체를 만나면 멈춘다. 마지막 1개만 잘려도 나머지는 살린다."""
    if not text:
        return []
    m = re.search(r'"claims"\s*:\s*\[', text)
    if not m:
        return []
    i, n = m.end(), len(text)
    claims = []
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] == "]":
            break
        if text[i] != "{":
            break
        depth, j, in_str, esc = 0, i, False, False
        while j < n:
            ch = text[j]
            if in_str:
                if esc: esc = False
                elif ch == "\\": esc = True
                elif ch == '"': in_str = False
            else:
                if ch == '"': in_str = True
                elif ch == "{": depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
            j += 1
        if depth != 0:
            break  # 미완성(잘림) 객체 → 여기서 중단
        try:
            claims.append(json.loads(text[i:j]))
        except Exception:
            break
        i = j
    return claims


def _call_claims(client: OpenAI, messages: list[dict], max_tokens: int) -> list[dict]:
    """인용(claims) 생성 전용 호출. 잘린 JSON도 완성된 claim만 관대하게 복구한다."""
    completion = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=messages,
        temperature=0.0,
        stream=False,
        max_tokens=max_tokens,
    )
    text = completion.choices[0].message.content or ""
    # 1) 엄격 파싱 우선
    try:
        return _safe_json_extract(text).get("claims") or []
    except Exception as e:
        print(f"[citation-json-truncated? salvaging] {e}")
    # 2) 잘린 JSON에서 완성된 claim만 복구
    salvaged = _salvage_claims(text)
    if salvaged:
        print(f"[citation-salvage] {len(salvaged)} claims 복구")
    return salvaged


def _call_json(client: OpenAI, messages: list[dict], max_tokens: int, temperature: float = 0.0) -> dict:
    completion = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=messages,
        temperature=temperature,
        stream=False,
        max_tokens=max_tokens,
    )

    try:
        print("finish_reason =", completion.choices[0].finish_reason)
    except Exception:
        pass

    text = completion.choices[0].message.content
    return _safe_json_extract(text)


_MARKER_RE = re.compile(r"\[(\d{1,2})\]")


def _derive_claims_from_markers(final_answer: str, rag_chunks: list[dict]) -> list[dict]:
    """답변의 인라인 [n] 마커를 근거 청크(1-indexed)에 결정적으로 매핑해 claims를 만든다.
    2차 인용 LLM이 빈 결과를 낼 때의 폴백 — 패널이 화면의 [n]과 항상 일치하게 한다.
    코드펜스/표 줄은 건너뛰고, 존재하는 번호만 인용한다."""
    text = str(final_answer or "")
    if not rag_chunks or "[" not in text:
        return []

    out = []
    in_fence = False
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.startswith("|"):  # 코드블록·표 줄 제외
            continue
        # 한 줄 안에서 문장 단위로 분리(마침표/물음표/느낌표 뒤 공백)
        for seg in re.split(r"(?<=[.!?。])\s+", line):
            nums = _MARKER_RE.findall(seg)
            if not nums:
                continue
            cites = []
            seen = set()
            for ns in nums:
                idx = int(ns) - 1
                if idx in seen or idx < 0 or idx >= len(rag_chunks):
                    continue
                seen.add(idx)
                c = rag_chunks[idx]
                cites.append({
                    "doc_id": c.get("doc_id"),
                    "chunk_id": c.get("chunk_id"),
                    "quote": "",
                    "score": c.get("score"),
                })
            if not cites:
                continue
            sentence = _MARKER_RE.sub("", seg).strip().lstrip("#>-*• ").strip()
            if sentence:
                out.append({"claim": sentence, "support": "supported", "citations": cites})
    return out


def _sentence_tokens(s: str) -> set:
    return set(re.findall(r"[0-9A-Za-z가-힣]+", _normalize_ws(s)))


def _best_claim_for_sentence(sentence: str, claims: list[dict]) -> dict | None:
    """답변 문장과 토큰이 가장 많이 겹치는 claim을 반환(Jaccard). 임계 미만이면 None.
    각주 문장에 2차 인용 LLM의 verbatim quote·지원등급을 붙이기 위한 매칭."""
    st = _sentence_tokens(sentence)
    if not st:
        return None
    best, best_score = None, 0.0
    for claim in claims or []:
        ct = _sentence_tokens(claim.get("claim") or "")
        if not ct:
            continue
        inter = len(st & ct)
        if not inter:
            continue
        score = inter / len(st | ct)
        if score > best_score:
            best_score, best = score, claim
    return best if best_score >= 0.34 else None


def _best_snippet_from_doc(sentence: str, doc_text: str, max_len: int = 180):
    """각주 문장과 가장 겹치는 **문서 원문 구절**을 결정적으로 골라 (스니펫, 점수) 반환(verbatim substring).
    각주마다 그 문장에 특화된 서로 다른 근거 스니펫을 만들어 툴팁 중복을 막는다.
    관련성이 낮으면 ("", 0.0)."""
    st = _sentence_tokens(sentence)
    text = str(doc_text or "").strip()
    if not st or not text:
        return "", 0.0
    best, best_score = "", 0.0
    for seg in re.split(r"(?<=[.!?。\n])\s*", text):
        seg = seg.strip()
        if len(seg) < 4:
            continue
        ct = _sentence_tokens(seg)
        if not ct:
            continue
        inter = len(st & ct)
        if not inter:
            continue
        score = inter / len(st | ct)
        if score > best_score:
            best_score, best = score, seg
    if best_score < 0.12:
        return "", 0.0
    return best[:max_len].strip(), best_score


def _best_doc_for_sentence(sentence: str, rag_chunks: list[dict], min_score: float = 0.12):
    """문장을 가장 잘 뒷받침하는 문서를 고른다(모든 근거 문서 원문과 겹침 비교).
    반환: (doc_index, snippet). 임계 미만이면 (None, "")."""
    st = _sentence_tokens(sentence)
    if not st:
        return None, ""
    best_i, best_score, best_snip = None, 0.0, ""
    for i, c in enumerate(rag_chunks or []):
        snip, score = _best_snippet_from_doc(sentence, c.get("merge_title_content") or "")
        if score > best_score:
            best_i, best_score, best_snip = i, score, snip
    if best_i is None or best_score < min_score:
        return None, ""
    return best_i, best_snip


def _footnotes_by_matching(text: str, rag_chunks: list[dict], max_notes: int = 12):
    """마커가 없는 답변(HYBRID 등)에 대해 문장↔문서 매칭으로 각주와 인라인 [k] 마커를 결정적으로 생성.
    코드펜스/표/헤딩/짧은 줄은 건너뛰고, 문서와 충분히 겹치는 문장에만 마커를 붙인다(과잉표시 방지).
    반환: (footnote_answer, footnotes)."""
    footnotes = []
    n = 0
    out_lines = []
    in_fence = False
    for raw_line in text.split("\n"):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out_lines.append(raw_line)
            continue
        if in_fence or stripped.startswith("|") or stripped.startswith("#") or len(stripped) < 8:
            out_lines.append(raw_line)
            continue
        # 라인을 문장 단위로 나눠, 매칭 문서가 있는 문장에만 마커 삽입
        new_parts = []
        for seg in re.split(r"(?<=[.!?。])\s+", raw_line):
            clean = _MARKER_RE.sub("", seg).strip().lstrip("#>-*•· ").strip()
            if n >= max_notes or len(clean) < 8:
                new_parts.append(seg)
                continue
            di, snip = _best_doc_for_sentence(clean, rag_chunks)
            # 임계를 낮춘 만큼(0.12) 단일 공통어 오탐을 막기 위해 공유 콘텐츠 토큰 2개 이상 요구
            if di is None or len(_sentence_tokens(clean) & _sentence_tokens(snip)) < 2:
                new_parts.append(seg)
                continue
            n += 1
            new_parts.append(seg + f"[{n}]")
            c = rag_chunks[di]
            footnotes.append({
                "claim": clean,
                "support": "supported" if snip else "partial",
                "citations": [{
                    "doc_id": c.get("doc_id"),
                    "chunk_id": c.get("chunk_id"),
                    "quote": snip,
                    "score": c.get("score"),
                }],
            })
        out_lines.append(" ".join(new_parts))
    return "\n".join(out_lines), footnotes


def _build_footnotes(final_answer: str, claims: list[dict], rag_chunks: list[dict]):
    """답변의 인라인 [doc] 마커(=근거 문서 번호)를 **읽는 순서대로 각주 [1..M]로 재번호**하고,
    각주별 근거 목록을 만든다. 인라인 [i] ↔ 우측 패널 i번 = 그 문장이 되도록 정합.

    반환: (footnote_answer, footnotes)
      - footnote_answer: 마커가 [1],[2],[3]… 읽는 순서로 치환된 답변 마크다운.
      - footnotes: [{claim(문장), support, citations:[{doc_id,chunk_id,quote,score}]}] (읽는 순서).
    LLM 마커가 있으면 그걸 읽는 순서로 재번호, 없으면(HYBRID 등) 문장↔문서 매칭으로 각주를 생성한다.
    코드펜스/표 줄은 건드리지 않는다."""
    text = str(final_answer or "")
    if not rag_chunks:
        return text, []
    # LLM이 붙인 [doc] 마커가 아예 없으면(HYBRID 툴 루프 답변) 문장↔문서 매칭으로 각주 생성
    if "[" not in text:
        return _footnotes_by_matching(text, rag_chunks)

    group_re = re.compile(r"\[\d{1,2}\](?:\s*\[\d{1,2}\])*")
    footnotes = []
    n_counter = 0
    out_lines = []
    in_fence = False
    for raw_line in text.split("\n"):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out_lines.append(raw_line)
            continue
        if in_fence or stripped.startswith("|") or "[" not in raw_line:
            out_lines.append(raw_line)
            continue

        pieces = []
        last = 0
        for m in group_re.finditer(raw_line):
            before = raw_line[last:m.start()]
            pieces.append(before)
            last = m.end()

            nums = _MARKER_RE.findall(m.group(0))
            doc_idxs, seen = [], set()
            for ns in nums:
                di = int(ns) - 1
                if 0 <= di < len(rag_chunks) and di not in seen:
                    seen.add(di)
                    doc_idxs.append(di)
            if not doc_idxs:
                continue  # 유효 문서 없는 마커 → 제거(치환 없음)

            n_counter += 1
            pieces.append(f"[{n_counter}]")

            # 문장 텍스트: 마커 앞 텍스트에서 마지막 종결부호 이후 조각
            seg = re.split(r"(?<=[.!?。])\s+", before.strip())
            sentence_raw = seg[-1] if seg else before
            sentence = _MARKER_RE.sub("", sentence_raw).strip().lstrip("#>-*•· ").strip()

            best = _best_claim_for_sentence(sentence, claims)
            cites, has_quote = [], False
            for di in doc_idxs:
                c = rag_chunks[di]
                did = c.get("doc_id")
                # 1) 그 문장에 특화된 근거 스니펫을 문서 원문에서 결정적으로 추출(각주별로 다름 → 툴팁 중복 방지)
                quote = _best_snippet_from_doc(sentence, c.get("merge_title_content") or "")[0]
                # 2) 없으면 매칭된 claim의 verbatim quote(2차 인용 LLM)로 보완
                if not quote and best:
                    for x in (best.get("citations") or []):
                        if x.get("doc_id") == did and (x.get("quote") or "").strip():
                            quote = x["quote"].strip()
                            break
                if quote:
                    has_quote = True
                cites.append({
                    "doc_id": did,
                    "chunk_id": c.get("chunk_id"),
                    "quote": quote,
                    "score": c.get("score"),
                })
            support = "supported" if ((best and best.get("support") == "supported") or has_quote) else "partial"
            footnotes.append({
                "claim": sentence or (best.get("claim") if best else ""),
                "support": support,
                "citations": cites,
            })
        pieces.append(raw_line[last:])
        out_lines.append("".join(pieces))

    # 마커가 하나도 없었으면(HYBRID 등 툴 루프 답변) 문장↔문서 매칭으로 각주를 결정적으로 생성
    if n_counter == 0:
        return _footnotes_by_matching(text, rag_chunks)

    return "\n".join(out_lines), footnotes


def llm_answer_with_citations(
    user_id: str,
    user_question: str,
    rag_chunks: list[dict],
    previous_messages: list[dict] | None = None,
    style_rules: str | None = None,
    client: OpenAI | None = None
) -> dict:
    if client is None:
        client = _make_client(user_id)

    # 💡 [권위 용어사전 + 전개 금지 목록] 질문 + 근거 문서에서 ① 등록 용어를 감지해 사전을 만들고
    #    ② 사전에 없는 약어는 '전개 금지 목록'으로 수집해 함께 주입한다. DB 이슈 시엔 조용히 생략.
    evidence_texts = [(c.get("merge_title_content") or "") for c in (rag_chunks or [])]
    glossary, unknown_acronyms = [], []
    try:
        from app.query_normalizer import build_glossary_from_texts, extract_unknown_acronyms
        glossary = build_glossary_from_texts([user_question] + evidence_texts)
        unknown_acronyms = extract_unknown_acronyms([user_question] + evidence_texts)
        if unknown_acronyms:
            # 사전 등록 플라이휠의 씨앗 — 이 로그의 약어를 term_aliases에 등록하면 정규화 대상이 된다.
            print(f"[unknown-acronyms] {', '.join(unknown_acronyms)}")
    except Exception as e:
        print(f"[glossary-build-failed] {e}")

    # 1차: markdown 답변 생성
    answer_messages = _build_answer_prompt(
        user_question=user_question,
        rag_chunks=rag_chunks,
        previous_messages=previous_messages,
        style_rules=style_rules,
        glossary=glossary,
        unknown_acronyms=unknown_acronyms,
    )

    answer_json = _call_answer_json_or_fallback_markdown(
        client=client,
        messages=answer_messages,
        max_tokens=ANSWER_MAX_TOKENS,
        temperature=0.1,
    )

    final_answer = (answer_json.get("answer_markdown") or "").strip()

    if not final_answer:
        final_answer = "근거 문서를 바탕으로 답변을 생성하지 못했습니다."

    # 💡 [용어 가드] 근거·사전 어디에도 없는 약어 전개(임의 풀이)를 결정적으로 탐지.
    #    위반 시에만 1회 교정 재생성(평상시 추가 LLM 호출 0) → 그래도 남으면 안전한 것만 제거.
    violations = _find_unverified_expansions(final_answer, evidence_texts, glossary)
    if violations:
        print(f"[term-guard] unverified expansions: {[v['match'] for v in violations]}")
        guard_msg = (
            "[용어 가드 — 재작성 지시]\n"
            "직전 답변에 근거 문서와 GLOSSARY 어디에도 없는 약어 풀이(전개)가 포함되었습니다. "
            "아래 병기 표현을 모두 제거하고, 해당 약어는 근거 문서의 원문 표기 그대로만 사용해 "
            "같은 내용을 다시 작성하세요. 그 외 내용과 인용 마커는 유지하세요.\n"
            + "\n".join(f"- {v['match']}" for v in violations)
        )
        try:
            retry_json = _call_answer_json_or_fallback_markdown(
                client=client,
                messages=answer_messages
                + [{"role": "assistant", "content": final_answer},
                   {"role": "system", "content": guard_msg}],
                max_tokens=ANSWER_MAX_TOKENS,
                temperature=0.0,
            )
            retry_answer = (retry_json.get("answer_markdown") or "").strip()
            if retry_answer:
                final_answer = retry_answer
        except Exception as e:
            print(f"[term-guard-regen-failed] {e}")
        remaining = _find_unverified_expansions(final_answer, evidence_texts, glossary)
        if remaining:
            final_answer = _strip_unverified_expansions(final_answer, remaining)

    # 2차: claims + citations 생성
    citation_messages = _build_citation_prompt(
        user_question=user_question,
        final_answer=final_answer,
        rag_chunks=rag_chunks,
    )

    try:
        claims = _call_claims(client, citation_messages, CITATION_MAX_TOKENS)
    except Exception as e:
        print(f"[citation-step-failed] {e}")
        claims = []

    # 코드 레벨 검증: 존재하지 않는 인용 제거 + 지어낸 quote 차단
    claims = validate_citations(claims, rag_chunks)

    # 💡 [폴백] 2차 인용 호출이 비었지만 답변에 [n] 마커가 있으면, 마커를 근거 청크에
    # 결정적으로 매핑해 패널을 채운다(항상 화면의 [n]과 일치 → 헛빈 패널 방지).
    if not claims:
        derived = _derive_claims_from_markers(final_answer, rag_chunks)
        if derived:
            claims = derived

    # 💡 [각주 정합] 인라인 [doc] 마커를 읽는 순서 각주 [1..M]로 재번호하고 각주 목록을 만든다.
    # → 인라인 [i] ↔ 우측 패널 i번 = 그 문장. (마커가 있을 때만 적용; 없으면 기존 claims 유지)
    footnote_answer, footnotes = _build_footnotes(final_answer, claims, rag_chunks)
    if footnotes:
        final_answer = footnote_answer
        panel_claims = footnotes
    else:
        panel_claims = claims

    return {
        "answer_markdown": final_answer,
        "claims": panel_claims,
        "answer": _normalize_claims_to_answer_list(panel_claims),
        "final": final_answer,
    }

def _strip_code_fence(text: str) -> str:
    t = (text or "").strip()

    if t.startswith("```"):
        lines = t.splitlines()
        if len(lines) >= 2:
            # 첫 줄 ```json / ```markdown / ``` 제거
            lines = lines[1:]
            # 마지막 ``` 제거
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            t = "\n".join(lines).strip()

    return t


def _call_answer_json_or_fallback_markdown(
    client: OpenAI,
    messages: list[dict],
    max_tokens: int,
    temperature: float = 0.0,
) -> dict:
    completion = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=messages,
        temperature=temperature,
        stream=False,
        max_tokens=max_tokens,
    )

    try:
        print("finish_reason =", completion.choices[0].finish_reason)
    except Exception:
        pass

    text = completion.choices[0].message.content or ""
    stripped = _strip_code_fence(text)

    # 1) JSON 우선 시도
    try:
        return _safe_json_extract(stripped)
    except Exception as e:
        print(f"[answer-json-parse-failed] {e}")
        print("=== ANSWER RAW OUTPUT BEGIN ===")
        print(stripped[:5000])
        print("=== ANSWER RAW OUTPUT END ===")

    # 2) fallback: raw text 자체를 answer_markdown으로 사용
    return {
        "answer_markdown": stripped
    }

# =====================================================================
# [Phase 1] 사내 모델 Tool Calling(Function Calling) 지원 테스트 코드
# =====================================================================
def test_tool_calling(user_id: str) -> dict:
    """
    사내 모델(gpt-oss-120b)이 OpenAI 스펙의 tool_calls를 정상적으로 응답하는지 테스트합니다.
    """
    client = _make_client(user_id)

    # 1. LLM에게 알려줄 가짜 함수(도구) 명세서
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_db_top_defects",
                "description": "MySQL DB에서 특정 기간 동안 가장 많이 발생한 불량명 순위를 조회합니다.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "months": {
                            "type": "integer",
                            "description": "조회할 최근 개월 수 (예: 3)"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "조회할 상위 N개 갯수 (예: 1)"
                        }
                    },
                    "required": ["months", "limit"]
                }
            }
        }
    ]

    # 2. 도구를 사용해야만 대답할 수 있는 프롬프트 전송
    messages = [
        {"role": "system", "content": "You are an intelligent agent. Use the provided tools to answer the user's question."},
        {"role": "user", "content": "최근 3개월간 가장 많이 발생한 불량명 1위가 뭐야?"}
    ]

    print("=== [Tool Calling Test Start] ===")
    try:
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=messages,
            tools=tools,
            tool_choice="auto", # 모델이 함수 호출 여부를 스스로 결정
            temperature=0.0,
        )

        message = completion.choices[0].message
        print("Finish Reason:", completion.choices[0].finish_reason)

        # 3. 모델이 tool_calls 형태로 응답했는지 검증
        if getattr(message, 'tool_calls', None):
            print("✅ Tool Calling 지원 확인됨!")
            tool_calls_info = []
           
            for tc in message.tool_calls:
                info = {
                    "id": getattr(tc, 'id', ''),
                    "type": getattr(tc, 'type', ''),
                    "function_name": tc.function.name,
                    "function_args": tc.function.arguments
                }
                tool_calls_info.append(info)
                print(f" - 호출된 함수 이름: {tc.function.name}")
                print(f" - 전달받은 파라미터: {tc.function.arguments}")
               
            return {"status": "success", "tool_calls": tool_calls_info}
       
        else:
            print("❌ Tool Calling이 발생하지 않음 (일반 텍스트 형태로 응답함)")
            print("응답 내용:", message.content)
            return {"status": "no_tool_calls", "content": message.content}

    except Exception as e:
        print(f"❌ API 에러 발생: {e}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    # 파이썬에서 이 파일을 직접 실행하면 테스트가 수행되도록 합니다.
    # 실행법: python app/llm_client.py
    import pprint
    res = test_tool_calling(user_id="test_admin")
    print("\n[테스트 결과 요약]")
    pprint.pprint(res)