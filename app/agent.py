import json
import re
import datetime
from app.llm_client import (
    _make_client,
    _build_citation_prompt,
    _call_json,
    _normalize_claims_to_answer_list,
    validate_citations,
    llm_answer_with_citations,
    CITATION_MAX_TOKENS
)
from app.tools import TOOLS_SCHEMA, execute_tool

# =====================================================================
# 🧠 1. 라우터 (문지기) 로직
# =====================================================================
def _call_intent_router(client, user_query: str) -> str:
    # 💡 [버튼 강제 호출 대응] 프론트엔드에서 보낸 태그를 감지하여 LLM을 거치지 않고 즉시 라우팅
    if user_query.startswith("[DB_ANALYSIS]"):
        return "DB_ANALYSIS"
    if user_query.startswith("[RAG_KNOWLEDGE]"):
        return "RAG_KNOWLEDGE"
    if user_query.startswith("[REPORT_ANALYSIS]"):
        return "REPORT_ANALYSIS"

    router_prompt = """
    당신은 반도체 불량 분석 시스템의 '의도 분류 라우터(Router)'입니다.
    사용자의 질문을 읽고 반드시 다음 5가지 인텐트 중 딱 하나만 텍스트로 반환하세요. (다른 말은 절대 금지)

    [인텐트 종류]
    1. "DB_ANALYSIS": 불량 발생 건수, 통계, 순위, 리스트 등 DB 데이터를 조회해야 하는 경우
    2. "RAG_KNOWLEDGE": 특정 불량의 발생 원리, 가이드, 해결책 등 기술 문서를 찾아야 하는 경우
    3. "HYBRID_DB_RAG": 통계 조회와 문서(원리) 검색이 모두 필요한 경우
    4. "GENERAL_CHAT": 안부 인사, 단순 대화 등 도구 검색이 필요 없는 경우
    5. "REPORT_ANALYSIS": 특정 불량 사례/보고서를 지목해 그 분석 내용·원인·조치를 심층적으로 설명해달라는 경우
       (특정 Lot, 특정 설비, 특정 시기·조건의 "사례 분석"을 원할 때. 단순 건수/순위 집계는 DB_ANALYSIS)

    [출력 예시]
    사용자: "최근 3개월 sf2 불량 순위" -> 출력: DB_ANALYSIS
    사용자: "파티클 원인이 뭐야?" -> 출력: RAG_KNOWLEDGE
    사용자: "안녕 반가워" -> 출력: GENERAL_CHAT
    사용자: "SF4에서 난 Cu 브릿지 불량 사례 상세히 분석해줘" -> 출력: REPORT_ANALYSIS
    사용자: "이 Lot에서 발생한 불량 보고서 내용 설명해줘" -> 출력: REPORT_ANALYSIS
    """
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": router_prompt},
                {"role": "user", "content": user_query}
            ],
            temperature=0.1,
            max_tokens=100
        )
        
        raw_content = response.choices[0].message.content
        if not raw_content:
            print("[Router] 모델이 빈 응답을 반환하여 기본값(HYBRID)으로 폴백합니다.")
            return "HYBRID_DB_RAG"
            
        intent = raw_content.strip().upper()
        
        if "REPORT_ANALYSIS" in intent: return "REPORT_ANALYSIS"
        elif "DB_ANALYSIS" in intent: return "DB_ANALYSIS"
        elif "RAG_KNOWLEDGE" in intent: return "RAG_KNOWLEDGE"
        elif "GENERAL_CHAT" in intent: return "GENERAL_CHAT"
        elif "HYBRID_DB_RAG" in intent: return "HYBRID_DB_RAG"
        
        return "HYBRID_DB_RAG"
        
    except Exception as e:
        print(f"[Router API Error] {e}")
        return "HYBRID_DB_RAG"

# =====================================================================
# 🧠 2. 전문가 프롬프트 팩토리
# =====================================================================
def _get_specialist_prompt(intent: str, current_date: str) -> str:
    # 📌 공통 페르소나 (Agent 명칭 적용 및 💡 속마음 출력 규칙 추가)
    base_persona = f"""
    [페르소나 및 기본 역할]
    - 당신은 사내 불량 분석을 수행하는 '{intent.replace('_', ' ')} Agent'입니다.
    - 오늘 날짜는 {current_date} 입니다.
    - ⭐️ 모든 답변은 반드시 100% 한국어로만 작성하세요!
    - ⭐️ 도구를 호출하기 전, 반드시 당신이 지금 무엇을 하는지 'Thought: [생각]' 형태로 1줄 먼저 출력하세요.
      Thought는 화면에 그대로 표시되므로, 전문 용어(쿼리, 인텐트, 라우팅, RAG 등) 없이 누구나 이해할 수 있는
      쉽고 친근한 한국어 문장으로 쓰세요. (좋은 예: "최근 3개월 불량 발생 건수를 집계하고 있어요")
      절대 Thought 영역에 SQL이나 코드, 영문 시스템 용어를 노출하지 마세요.
    """
    
    # 📌 DB 전문가 스키마 (SQL 강제 접기 포함)
    db_schema = """
    [데이터베이스 스키마 정보: v_ai_defect_search]
    - report_index, Lot_ID, WF_ID, 분석완료일시, 불량명, 성분, 공정노드, 모듈, 라인, 공정명, 설비명, 의뢰자명, 분석담당자, 보고서링크
    
    [DB 전문가 엄격 규칙]
    1. 정적 데이터(완벽 일치 '=' 사용): 공정노드('NPW', 'SF2', 'SF3', 'SF4', 'SF5', 'SF7'), 모듈('BEOL', 'FIN', 'MOL', 'NS', 'PC', 'RMG', 'RPG', 'SD')
    2. 동적 데이터(Fuzzy Search, 'LIKE' 강제): '불량명', '설비명', '의뢰자명' 등은 종류가 너무 많으므로 무조건 `LIKE '%키워드%'` 로 넓게 검색하세요.
    3. 복합 명사 주의: "Depo PC"처럼 모듈명(PC)이 섞여도 무조건 `모듈='PC'`로 하지 말고, `WHERE 불량명 LIKE '%Depo PC%'` 조건을 우선 고려.
    
    4. ⭐️ 화학 성분('성분' 컬럼) 검색 규칙 (절대 엄수): 성분 데이터는 여러 원소가 콤마로 결합되어 있으며 공백과 대소문자가 불규칙합니다. 단일 원소(예: C) 검색 시 오탐지(예: Cr, Cu 매칭)를 방지하기 위해 절대로 `LIKE`를 쓰지 말고, 반드시 아래 공식을 사용하세요.
       - 공식: `WHERE FIND_IN_SET(UPPER('검색원소'), UPPER(REPLACE(성분, ' ', ''))) > 0`
       
    5. ⭐️ 쿼리 확장(OR 블록) 번역 규칙 (절대 엄수): 유저의 질문에 `("A" OR "B")` 형태의 동의어 묶음이 포함되어 있을 수 있습니다. 이 경우 절대 `LIKE '%(A OR B)%'` 형태로 문법에 맞지 않는 쿼리를 작성하지 마세요. 반드시 괄호를 풀고 `(컬럼명 LIKE '%A%' OR 컬럼명 LIKE '%B%')` 형태로 올바른 SQL 구문으로 번역하여 작성해야 합니다.
    
    6. ⭐️ 목록 조회 시 중복 제거 규칙 (절대 엄수): 통계가 아닌 불량 내역(목록)을 조회하여 표로 그릴 때, 동일한 report_index, Lot_ID, WF_ID가 여러 줄 중복 출력되면 절대 안 됩니다!
       - SQL 쿼리 끝에 반드시 `GROUP BY report_index, Lot_ID, WF_ID`를 추가하여 행을 하나로 묶으세요.
       - 이때 데이터 유실을 막기 위해 SELECT 절에서는 `MAX()` 대신 반드시 `GROUP_CONCAT(DISTINCT 불량명 SEPARATOR ', ') AS 불량명`, `GROUP_CONCAT(DISTINCT 성분 SEPARATOR ', ') AS 성분`으로 작성하고, 나머지 단순 컬럼들은 `MAX(분석담당자)` 처럼 집계 함수를 씌우세요.
       
    7. 자가 교정: 결과가 0건이면 엄격한 조건을 버리고 불량명 `LIKE` 위주로 쿼리를 재작성하여 다시 도구 호출.
    8. 출력 포맷: DB 통계/목록 결과는 무조건 **마크다운 표(Table)**로만 출력.
    9. ⭐️ SQL 노출 (필수): 당신이 실행한 최종 SQL 쿼리는 반드시 답변 맨 마지막에 아래 형식을 지켜서 넣으세요.
       ⚠️ 주의: 유저가 클릭하기 전에는 숨겨져 있어야 하므로 <details open>이 아닌 반드시 <details>를 사용하세요.
       
       <details>
       <summary>💡 실행된 최종 SQL 쿼리 보기</summary>
       \n```sql\n(여기에 SQL 작성)\n```\n
       </details>
    """

    # 📌 RAG 전문가 규칙
    rag_rules = """
    [RAG 전문가 엄격 규칙]
    1. 문서를 요약할 때는 **절대 마크다운 표(| 기호)를 사용하지 마세요.**
    2. 대신 아래의 '인용구(>) 기반 카드형 포맷'을 엄격하게 사용하세요. 인용구 (>)기호는 문장 하나하나가 끝날때마다 남발하지 말고, 문단 소제목이 끝날때만 사용하세요
       ### 💡 [주제] 분석 내용
       > **📌 주요 내용**
       #### (상세 내용 작성)
    """

    if intent == "DB_ANALYSIS":
        return base_persona + db_schema + "\n- 당신은 'DB 통계 Agent'로서 오직 DB 통계를 내는 것에만 집중하세요. (도구: query_database 위주)"

    elif intent == "REPORT_ANALYSIS":
        report_analysis_rule = """
        [⭐️ 보고서 심층분석 규칙 - 절대 엄수]
        - 당신의 임무는 사용자가 지목한 불량 사례(보고서)를 찾아내는 것까지입니다.
          최종 서술형 답변은 시스템이 확보한 근거 문서를 바탕으로 별도 단계에서 생성됩니다.
        - 반드시 `query_database` 도구로 질문 조건(불량명/Lot/설비/기간 등)에 맞는 보고서를 조회하세요.
          이때 SELECT 절에 반드시 report_index 컬럼을 포함해야 합니다. (이 값이 후속 근거 연결의 열쇠입니다)
        - 심층분석 대상은 소수 정예가 좋습니다. `GROUP BY report_index, Lot_ID, WF_ID` + `LIMIT 5` 이내로
          가장 관련성 높은 보고서만 추리세요.
        - 결과가 0건이면 조건을 완화(불량명 LIKE 위주)하여 다시 조회하세요.
        - 조회가 끝나면 추가 서술 없이 간단히 "관련 보고서를 찾았습니다"라고만 답하세요.
        """
        return base_persona + db_schema + report_analysis_rule + "\n- 당신은 '보고서 심층분석 Agent'로서 분석 대상 보고서를 정확히 찾는 데 집중하세요. (도구: query_database 전용)"

    elif intent == "RAG_KNOWLEDGE":
        rag_action_rule = """
        [⭐️ 도구 호출 필수 규칙 - 절대 엄수]
        - 당신은 현재 기술 문서를 검색해야 하는 RAG 모드입니다.
        - 절대로 당신의 자체적인 사전 지식(기억)만으로 답변을 지어내지 마세요!
        - 최종 답변을 작성하기 전에 반드시 `search_documents` 도구를 먼저 호출하여 사내 문서를 확보해야 합니다.
        """
        return base_persona + rag_rules + rag_action_rule + "\n- 당신은 '문서 검색 Agent'로서 기술 문서를 검색하고 원리를 설명하는 것에 집중하세요. (도구: search_documents 위주)"
        
    elif intent == "HYBRID_DB_RAG":
        hybrid_specific = """
        [하이브리드 전용 출력 포맷 - 충돌 해결]
        - DB에서 가져온 통계/목록 데이터는 **마크다운 표(Table)**로 보여주세요.
        - 문서(RAG)에서 가져온 가이드나 원리 내용은 절대 표를 쓰지 말고 **인용구(>) 기반 카드형 포맷**으로 이어서 보여주세요.
        """
        hybrid_action_rule = """
        [⭐️ 도구 호출 필수 규칙 - 절대 엄수]
        - 당신은 현재 통계와 원리를 모두 분석해야 하는 하이브리드 모드입니다.
        - 반드시 1) `query_database` 도구를 호출하여 수치/통계 데이터를 확보하고, 2) `search_documents` 도구를 호출하여 원리와 해결책을 확보하세요.
        - 두 도구의 결과를 모두 확인하기 전에는 절대로 최종 답변을 생성하지 마세요! 부족한 도구가 있다면 반드시 마저 호출하세요.
        """
        return base_persona + db_schema + rag_rules + hybrid_specific + hybrid_action_rule + "\n- 당신은 '통합 분석 Agent'로서 모든 도구를 사용하세요."
        
    else: # GENERAL_CHAT
        general_chat_rule = """
        [GENERAL CHAT 전문가 엄격 규칙]
        1. 도구를 사용하지 말고 다정하고 전문적으로 답변하세요.
        2. ⭐️ 정체성 강제 규칙 (절대 엄수): 사용자가 "너는 누구야?", "뭐하는 애야?", "역할이 뭐야?" 등 정체성을 물어보면, 무조건 첫 문장에 **"저는 사내 불량 분석 업무를 돕기 위해 개발된 대화형 에이전트입니다"**라는 핵심 소속과 목적을 명확하게 밝히세요. 
        3. 일반적인 범용 AI 어시스턴트라는 식의 모호한 답변이나 기본 학습된 자기소개는 절대 출력하지 마세요.
        """
        return base_persona + general_chat_rule

# =====================================================================
# 🧠 3. 문맥 맞춤형 액션 칩 (UI 버튼) 생성기
# =====================================================================
def _get_suggested_actions(intent: str, db_used: bool = False) -> list:
    if intent == "DB_ANALYSIS":
        actions = [
            {"label": "📖 관련 사내 문서도 찾아보기", "action": "[RAG_KNOWLEDGE]"},
            {"label": "🧩 이 보고서 심층분석", "action": "[REPORT_ANALYSIS]"},
        ]
        # DB 쿼리가 실행되었을 때만 '재검색' 기능 활성화
        if db_used:
            actions.append({"label": "⚠️ 조건을 넓혀서 다시 검색", "action": "retry"})
        else:
            actions.append({"label": "⚠️ 조건을 넓혀서 다시 검색", "action": "none", "disabled": True})
        return actions

    elif intent == "RAG_KNOWLEDGE":
        return [
            {"label": "📊 DB 통계로도 확인해보기", "action": "[DB_ANALYSIS]"}
        ]
    elif intent == "REPORT_ANALYSIS":
        return [
            {"label": "📊 DB 통계로도 확인해보기", "action": "[DB_ANALYSIS]"},
            {"label": "📖 관련 사내 문서도 찾아보기", "action": "[RAG_KNOWLEDGE]"},
        ]
    elif intent == "HYBRID_DB_RAG":
        return []
    else:
        return []

# =====================================================================
# 🧠 3.5. 검증 유틸 (숫자 에코 체크 · 근거 게이트 템플릿)
# =====================================================================
_NUM_RE = re.compile(r"\d[\d,\.]*")

def _extract_numbers(text: str) -> set:
    """2자리 이상 숫자만 추출 (콤마 제거, 노이즈 축소)."""
    out = set()
    for m in _NUM_RE.finditer(text or ""):
        s = m.group(0).replace(",", "").rstrip(".")
        digits = re.sub(r"\D", "", s)
        if len(digits) >= 2:
            out.add(s)
    return out


def _numeric_echo_check(final_answer: str, db_tool_results: list, allowed_extra: set) -> dict:
    """DB 답변 속 숫자가 실제 도구 결과에 존재하는지 결정적으로 검사.
    SQL <details> 블록과 코드펜스는 제외. 사용자 질문 속 숫자는 허용."""
    ans = re.sub(r"<details>.*?</details>", " ", final_answer or "", flags=re.S | re.I)
    ans = re.sub(r"```.*?```", " ", ans, flags=re.S)
    ans_nums = _extract_numbers(ans)
    src_nums = set(allowed_extra or set())
    for t in db_tool_results or []:
        src_nums |= _extract_numbers(t)
    unmatched = sorted(n for n in ans_nums if n not in src_nums)
    return {"numeric_ok": not unmatched, "unmatched": unmatched[:10]}


# 스트리밍 진행 멘트용 인텐트 한글 라벨 (유저에게 노출되는 문구)
INTENT_LABELS = {
    "DB_ANALYSIS": "DB 통계 분석",
    "RAG_KNOWLEDGE": "사내 문서 검색",
    "HYBRID_DB_RAG": "통계 + 문서 통합 분석",
    "GENERAL_CHAT": "일반 대화",
    "REPORT_ANALYSIS": "보고서 심층분석",
}

def _intent_label(intent: str) -> str:
    return INTENT_LABELS.get(intent, intent)


_REPORT_INDEX_JSON_RE = re.compile(r'"report_index"\s*:\s*"?(\d+)')

def _extract_report_indexes(tool_result_str: str, cap: int = 20) -> set:
    """DB 도구 결과(JSON 문자열)에서 report_index 값 수집 (KG 관련 문서 조인용)."""
    out = set()
    try:
        rows = json.loads(tool_result_str)
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict) and r.get("report_index") not in (None, ""):
                    out.add(str(r["report_index"]))
                    if len(out) >= cap:
                        return out
            return out
    except Exception:
        pass
    for m in _REPORT_INDEX_JSON_RE.finditer(tool_result_str or ""):
        out.add(m.group(1))
        if len(out) >= cap:
            break
    return out


NO_EVIDENCE_ANSWER = (
    "### 🔍 사내 문서에서 근거를 찾지 못했습니다\n\n"
    "> **📌 안내**\n"
    "질문과 관련된 사내 기술 문서를 검색했지만, 신뢰할 수 있는 근거 문서를 확보하지 못했습니다.\n"
    "추측으로 답변을 지어내지 않도록 답변 생성을 중단했습니다.\n\n"
    "#### 이렇게 해보세요\n"
    "- 핵심 키워드를 바꾸거나 줄여서 다시 질문해 주세요.\n"
    "- 특정 공정/불량명이라면 표준 용어(약어 대신 정식 명칭)로 시도해 주세요.\n"
    "- 발생 건수·순위 같은 통계 질문이라면 아래 버튼으로 DB 통계 조회를 이용해 보세요."
)

RAG_SYNTHESIS_STYLE_RULES = (
    "모든 답변은 100% 한국어로 작성하세요. "
    "문서 요약 시 마크다운 표(| 기호)를 사용하지 말고, 아래 인용구(>) 기반 카드형 포맷을 사용하세요:\n"
    "### 💡 [주제] 분석 내용\n"
    "> **📌 주요 내용**\n"
    "#### (상세 내용 작성)"
)

REPORT_ANALYSIS_STYLE_RULES = (
    "모든 답변은 100% 한국어로 작성하세요. "
    "당신은 특정 불량 사례(보고서)를 심층 분석하는 케이스 리포트를 작성합니다.\n"
    "- 근거로 제공된 [보고서 DB 분석 기록]과 연결 문서 내용만 사용하고, 근거에 없는 원인·조치는 절대 추정하지 마세요.\n"
    "- 보고서별 핵심 사실(Lot/설비/공정/불량명/성분)은 간단한 마크다운 표로 정리해도 좋습니다.\n"
    "- 발생 현상 → 분석 내용 → 원인/조치 순서로 서술하되, 문서에서 확인된 내용에는 아래 카드형 포맷을 사용하세요:\n"
    "### 💡 [사례] 분석 내용\n"
    "> **📌 주요 내용**\n"
    "#### (상세 내용 작성)\n"
    "- 여러 보고서가 있으면 공통점과 차이점을 마지막에 짧게 정리하세요."
)

# =====================================================================
# 🧠 4. 메인 에이전트 루프 (실시간 스트리밍 및 시행착오 데이터 분리 전송)
# =====================================================================
def _dedupe_and_filter_hits(hits: list[dict], excluded_indexes: set) -> list[dict]:
    best = {}
    for h in hits or []:
        idx = (h.get("_index") or "").strip()
        if idx in excluded_indexes:
            continue
        src = h.get("_source") or {}
        doc_id = src.get("doc_id")
        if not doc_id:
            continue
            
        prev = best.get(doc_id)
        if prev is None or (h.get("_score") or 0) > (prev.get("_score") or 0):
            best[doc_id] = h

    out = list(best.values())
    out.sort(key=lambda x: (-(x.get("_score") or 0), (x.get("_rank") or 10**9)))
    return out

def _to_ui_doc_from_hit(h: dict) -> dict:
    src = h.get("_source") or {}
    return {
        "doc_id": src.get("doc_id"),
        "chunk_id": src.get("chunk_id") or h.get("_id"),
        "title": src.get("title"),
        "merge_title_content": src.get("merge_title_content") or "",
        "score": h.get("_score"),
        "additionalField": src.get("additionalField") or {},
        "_index": h.get("_index"),
        "_rank": h.get("_rank"),
    }

def _rag_fallback_chunks(user_query: str, excluded_indexes: set, top_k: int = 8, limit: int = 12) -> list:
    """서버측 결정적 재검색 안전망.

    RAG 답변인데 에이전트 루프가 근거를 하나도 못 모았을 때(LLM이 search_documents를 건너뛰었거나
    일시적 0건) 헛되이 '근거 없음'으로 실패하지 않도록, 서버가 직접 user_query로 검색한다.
    근거는 여전히 실제 검색된 문서로만 채워지므로 안티-할루시네이션 원칙은 유지된다.
    반환: rag_chunks 형태 리스트(빈 결과·예외 시 []).
    """
    from app.rag_client import rag_retrieve_rrf
    from app.config import DEFAULT_INDEX_NAME
    try:
        rag_result = rag_retrieve_rrf(index_name=DEFAULT_INDEX_NAME, query_text=user_query, top_k=top_k)
        hits = rag_result.get("hits", {}).get("hits", [])
    except Exception as e:
        print(f"[RAG] 서버측 재검색 폴백 실패: {e}")
        return []
    return [_to_ui_doc_from_hit(h) for h in _dedupe_and_filter_hits(hits, excluded_indexes)[:limit]]


def _report_es_fallback_chunks(ctx: dict, excluded_indexes: set, per_report_k: int = 3, total_limit: int = 4) -> list:
    """[REPORT_ANALYSIS ES 보완] KG 연결문서가 없는 보고서만 불량명 기반 시맨틱 검색으로 근거 보완.

    ctx는 kg_repo.build_report_analysis_context의 반환값. KG chunk와 doc_id 중복은 제거하고
    kg_source="search" 태그를 붙여 provenance를 구분한다.
    """
    from app.rag_client import rag_retrieve_rrf
    from app.config import DEFAULT_INDEX_NAME

    linked = ctx.get("linked_report_indexes") or set()
    kg_doc_ids = {c.get("doc_id") for c in (ctx.get("chunks") or [])}
    out = []
    for row in ctx.get("db_rows") or []:
        if len(out) >= total_limit:
            break
        ridx = row.get("report_index")
        if ridx in linked:
            continue
        defect = str(row.get("불량명") or "").strip()
        if not defect:
            continue
        query = " ".join(x for x in (defect, str(row.get("공정명") or "").strip()) if x)
        try:
            rag_result = rag_retrieve_rrf(index_name=DEFAULT_INDEX_NAME, query_text=query, top_k=per_report_k)
            hits = rag_result.get("hits", {}).get("hits", [])
        except Exception as e:
            print(f"[KG] ES 보완 검색 실패 (report {ridx}): {e}")
            continue
        for h in _dedupe_and_filter_hits(hits, excluded_indexes):
            chunk = _to_ui_doc_from_hit(h)
            if not chunk.get("doc_id") or chunk["doc_id"] in kg_doc_ids:
                continue
            chunk["kg_source"] = "search"
            chunk["report_index"] = ridx
            kg_doc_ids.add(chunk["doc_id"])
            out.append(chunk)
            if len(out) >= total_limit:
                break
    return out


def run_agent_loop_stream(user_id: str, user_query: str, previous_messages: list, excluded_indexes: set, ui_top_k: int, forced_intent: str = None):
    client = _make_client(user_id)
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")
    
    execution_steps = []

    # 💡 [핵심 추가] 텍스트(Thought)와 데이터(Technical Detail)를 객체로 담아 전송
    def yield_step(thought: str, tech_detail: str = None):
        step_obj = {"thought": thought}
        if tech_detail:
            step_obj["technical_detail"] = tech_detail
        execution_steps.append(step_obj)
        return json.dumps({"type": "step", "data": step_obj}, ensure_ascii=False) + "\n"

    # [1. 강제 인텐트 적용 및 로그 바구니 생성]
    if forced_intent:
        intent = forced_intent.replace("[", "").replace("]","").strip()
        yield yield_step(f"🎯 요청하신 대로 '{_intent_label(intent)}' 방식으로 진행할게요")
    else:
        yield yield_step("🔍 질문을 읽고 어떤 방식으로 답변할지 정하고 있어요...")
        intent = _call_intent_router(client, user_query)
        yield yield_step(f"🎯 '{_intent_label(intent)}' 방식으로 답변을 준비할게요")

    system_prompt = _get_specialist_prompt(intent, current_date)
    
    messages = [{"role": "system", "content": system_prompt}]
    if previous_messages:
        messages.extend(previous_messages[-4:])
    messages.append({"role": "user", "content": user_query})

    # [2. 무기 압수 (Tools 필터링)]
    available_tools = TOOLS_SCHEMA
    if intent in ("DB_ANALYSIS", "REPORT_ANALYSIS"):
        available_tools = [t for t in TOOLS_SCHEMA if t["function"]["name"] == "query_database"]
    elif intent == "RAG_KNOWLEDGE":
        available_tools = [t for t in TOOLS_SCHEMA if t["function"]["name"] == "search_documents"]

    MAX_STEPS = 10
    all_collected_hits = []
    used_db = False
    db_tool_results = []
    collected_report_indexes = set()

    for step in range(MAX_STEPS):
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=messages,
            tools=available_tools,
            tool_choice="auto",
            temperature=0.0
        )
        
        ai_message = response.choices[0].message
        messages.append(ai_message)
        
        # 💡 LLM의 'Thought: ' 부분을 추출하여 정제된 속마음 가져오기
        thought_text = ai_message.content.strip() if ai_message.content else ""
        parsed_thought = ""
        if "Thought:" in thought_text:
            parsed_thought = thought_text.split("Thought:")[1].split("\n")[0].strip()

        if getattr(ai_message, 'tool_calls', None):
            for tool_call in ai_message.tool_calls:
                func_name = tool_call.function.name

                if func_name == "query_database":
                    used_db = True
                
                # LLM이 Thought를 안 뱉었을 때의 기본값 설정
                if not parsed_thought:
                    if func_name == "query_database": parsed_thought = "📊 DB에서 통계 데이터를 찾아보고 있어요..."
                    elif func_name == "search_documents": parsed_thought = "📖 사내 기술 문서를 찾아보고 있어요..."

                # 💡 파라미터 파싱 및 SQL 추출 (Technical Detail 구성)
                try:
                    args = json.loads(tool_call.function.arguments)
                    if "query" in args or "sql" in args:
                        sql_str = args.get("query") or args.get("sql")
                        tech_detail = f"[Tool] {func_name}\n[SQL Query]\n{sql_str}"
                    else:
                        tech_detail = f"[Tool] {func_name}\n[Args]\n{json.dumps(args, ensure_ascii=False, indent=2)}"
                except Exception:
                    args = {}
                    tech_detail = f"[Tool] {func_name}\n(파라미터 파싱 실패)"

                # 프론트엔드로 Thought와 Tech_detail 발사!
                yield yield_step(thought=parsed_thought, tech_detail=tech_detail)

                tool_result_str, hits = execute_tool(func_name, args)

                # 숫자 에코 체크용으로 DB 도구 결과 원문 보관 + KG 조인용 report_index 수집
                if func_name == "query_database":
                    db_tool_results.append(tool_result_str)
                    collected_report_indexes.update(_extract_report_indexes(tool_result_str))

                # 💡 결과 0건 시 시행착오(자가교정) 피드백도 기록
                if func_name == "query_database" and ("[]" in tool_result_str or "0건" in tool_result_str):
                    tool_result_str += "\n[시스템 알림]: 검색 결과가 0건입니다. 방금 넣은 모듈, 라인 등의 조건을 지우고 불량명 LIKE 검색 위주로 쿼리를 재작성하여 다시 도구를 호출해 보세요."
                    yield yield_step(thought="⚠️ 조건에 맞는 결과가 없어요. 검색 범위를 넓혀서 다시 찾아볼게요.")
                
                if hits:
                    all_collected_hits.extend(hits)
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_result_str})
            continue
        
        else:
            final_answer = ai_message.content or ""

            citations_json = {"answer": [], "final": final_answer, "claims": []}
            top_docs_ui = []
            rag_chunks = []
            if all_collected_hits:
                hits_visible = _dedupe_and_filter_hits(all_collected_hits, excluded_indexes)
                top_docs_ui = [_to_ui_doc_from_hit(h) for h in hits_visible[:ui_top_k]]
                rag_chunks = [_to_ui_doc_from_hit(h) for h in hits_visible[:12]]

            # 💡 [Phase 3: REPORT_ANALYSIS] KG로 근거를 조립해 생성에 투입 (전용 경로)
            if intent == "REPORT_ANALYSIS":
                yield yield_step("🧩 찾은 보고서를 지식그래프와 연결해 근거를 모으고 있어요...")
                ctx = {"chunks": [], "glossary": "", "db_rows": [], "linked_report_indexes": set()}
                if collected_report_indexes:
                    try:
                        from app.kg_repo import build_report_analysis_context
                        ctx = build_report_analysis_context(sorted(collected_report_indexes))
                    except Exception as e:
                        print(f"[KG] 보고서 분석 컨텍스트 조립 실패: {e}")

                es_chunks = []
                try:
                    es_chunks = _report_es_fallback_chunks(ctx, excluded_indexes)
                except Exception as e:
                    print(f"[KG] ES 보완 검색 스킵: {e}")
                if es_chunks:
                    yield yield_step(f"🔎 연결 문서가 부족한 보고서는 문서 검색으로 {len(es_chunks)}건을 보완했어요")

                all_chunks = (ctx.get("chunks") or []) + es_chunks

                # 앵커(보고서)를 못 찾았으면 곧바로 실패하지 말고 일반 문서검색으로 우아하게 강등
                degraded = False
                if not all_chunks:
                    yield yield_step("🔎 특정 보고서를 특정하지 못해, 관련 사내 문서를 폭넓게 검색할게요...")
                    all_chunks = _rag_fallback_chunks(user_query, excluded_indexes)
                    degraded = True

                # 근거 게이트: 폴백 후에도 근거가 하나도 없을 때만 자유 생성을 차단하고 정직 응답
                if not all_chunks:
                    yield yield_step("⚠️ 분석할 보고서 근거를 확보하지 못했어요. 추측으로 답하지 않고 안내 메시지를 드릴게요.")
                    final_answer = NO_EVIDENCE_ANSWER
                    final_result = {
                        "intent": intent,
                        "final_answer": final_answer,
                        "suggested_actions": _get_suggested_actions(intent, db_used=used_db),
                        "citations": {"answer": [], "final": final_answer, "claims": []},
                        "top_docs": [],
                        "steps": execution_steps,
                        "verification": {"grounded": False},
                    }
                    yield json.dumps({"type": "final", "data": final_result}, ensure_ascii=False) + "\n"
                    return

                # 연결 용어 정의를 미니 용어사전으로 주입 (main.py의 detected_terms 주입 패턴과 동일)
                prev_for_synth = list(previous_messages or [])
                if ctx.get("glossary"):
                    prev_for_synth.insert(0, {
                        "role": "system",
                        "content": (
                            "아래는 분석 대상 보고서와 연결된 사내 전문 용어 정의입니다. "
                            "문맥 이해에 활용하세요.\n\n[사내 용어 사전]\n" + ctx["glossary"]
                        ),
                    })

                kg_doc_count = sum(1 for c in all_chunks if c.get("kg_source") not in (None, "db", "search"))
                yield yield_step(
                    f"✅ 근거 {len(all_chunks)}건(DB 기록 {len(ctx.get('db_rows') or [])}·연결 문서 {kg_doc_count}·검색 보완 {len(es_chunks)})을 확보했어요! "
                    "근거 내용만 바탕으로 심층분석을 정리하고 있어요..."
                )
                try:
                    synth = llm_answer_with_citations(
                        user_id=user_id,
                        user_question=user_query,
                        rag_chunks=all_chunks,
                        previous_messages=prev_for_synth,
                        style_rules=(RAG_SYNTHESIS_STYLE_RULES if degraded else REPORT_ANALYSIS_STYLE_RULES),
                        client=client,
                    )
                    if (synth.get("answer_markdown") or "").strip():
                        final_answer = synth["answer_markdown"]
                    citations_json = {
                        "answer": synth.get("answer") or [],
                        "final": final_answer,
                        "claims": synth.get("claims") or [],
                    }
                except Exception as e:
                    print(f"  ❌ 보고서 심층분석 합성 실패, 루프 답변으로 폴백: {e}")

                # 근거 패널용 문서: DB 사실 chunk는 제외하고 실제 문서(KG 연결 + 검색 보완)만 노출
                doc_chunks = [c for c in all_chunks if c.get("kg_source") != "db"]
                top_docs_ui = doc_chunks
                related_docs = [c for c in doc_chunks if c.get("kg_source") != "search"]

                # 검증: 답변 속 숫자가 근거(DB 기록·문서 본문·질문)에 존재하는지 결정적 검사
                allowed_nums = _extract_numbers(user_query)
                for c in all_chunks:
                    allowed_nums |= _extract_numbers(c.get("merge_title_content") or "")
                verification = {"grounded": True}
                verification.update(_numeric_echo_check(
                    final_answer=final_answer,
                    db_tool_results=db_tool_results,
                    allowed_extra=allowed_nums,
                ))
                claim_list = citations_json.get("answer") or []
                if claim_list:
                    verification["claims_supported"] = sum(1 for c in claim_list if c.get("support") == "supported")
                    verification["claims_total"] = len(claim_list)

                final_result = {
                    "intent": intent,
                    "final_answer": final_answer,
                    "suggested_actions": _get_suggested_actions(intent, db_used=used_db),
                    "citations": citations_json,
                    "top_docs": top_docs_ui,
                    "related_docs": related_docs,
                    "steps": execution_steps,
                    "verification": verification,
                }
                yield json.dumps({"type": "final", "data": final_result}, ensure_ascii=False) + "\n"
                return

            # 💡 [근거 게이트] RAG 모드인데 확보된 근거 문서가 없으면 자유 생성을 차단하고 정직 응답
            if intent == "RAG_KNOWLEDGE" and not rag_chunks:
                # LLM이 검색 도구를 건너뛰었거나 일시적 0건일 수 있으므로, 서버가 직접 재검색해 헛실패 방지
                yield yield_step("🔎 근거가 비어 있어 사내 문서를 한 번 더 검색하고 있어요...")
                fb_chunks = _rag_fallback_chunks(user_query, excluded_indexes)
                if fb_chunks:
                    rag_chunks = fb_chunks
                    top_docs_ui = fb_chunks[:ui_top_k]

            if intent == "RAG_KNOWLEDGE" and not rag_chunks:
                yield yield_step("⚠️ 참고할 사내 문서를 찾지 못했어요. 추측으로 답하지 않고 안내 메시지를 드릴게요.")
                final_answer = NO_EVIDENCE_ANSWER
                final_result = {
                    "intent": intent,
                    "final_answer": final_answer,
                    "suggested_actions": _get_suggested_actions(intent, db_used=used_db),
                    "citations": {"answer": [], "final": final_answer, "claims": []},
                    "top_docs": [],
                    "steps": execution_steps,
                    "verification": {"grounded": False},
                }
                yield json.dumps({"type": "final", "data": final_result}, ensure_ascii=False) + "\n"
                return

            if intent == "RAG_KNOWLEDGE" and rag_chunks:
                # 💡 [생성 분리] 최종 답변을 evidence-only 합성으로 교체 (근거 없는 서술 차단)
                yield yield_step("✅ 참고 문서를 찾았어요! 문서 내용만 바탕으로 답변을 정리하고 있어요...")
                try:
                    synth = llm_answer_with_citations(
                        user_id=user_id,
                        user_question=user_query,
                        rag_chunks=rag_chunks,
                        previous_messages=previous_messages,
                        style_rules=RAG_SYNTHESIS_STYLE_RULES,
                        client=client,
                    )
                    if (synth.get("answer_markdown") or "").strip():
                        final_answer = synth["answer_markdown"]
                    citations_json = {
                        "answer": synth.get("answer") or [],
                        "final": final_answer,
                        "claims": synth.get("claims") or [],
                    }
                except Exception as e:
                    print(f"  ❌ 근거 기반 합성 실패, 루프 답변으로 폴백: {e}")

            elif rag_chunks:
                # 기존 경로 (HYBRID 등): 사후 인용구 매핑 + 코드 검증
                yield yield_step("✅ 분석이 끝났어요! 답변에 참고 문서를 연결하고 있어요...")
                try:
                    citation_messages = _build_citation_prompt(
                        user_question=user_query,
                        final_answer=final_answer,
                        rag_chunks=rag_chunks,
                    )
                    citation_res = _call_json(client, citation_messages, CITATION_MAX_TOKENS, 0.0)
                    claims = validate_citations(citation_res.get("claims") or [], rag_chunks)
                    citations_json = {"answer": _normalize_claims_to_answer_list(claims), "final": final_answer, "claims": claims}
                except Exception as e:
                    print(f"  ❌ 인용구 생성 실패: {e}")
            else:
                yield yield_step("✅ 분석이 끝났어요! 답변을 정리했어요.")

            # 💡 [KG 자동 조인] DB 결과의 report_index로 연결된 원본 보고서 문서 첨부
            related_docs = []
            if collected_report_indexes:
                try:
                    from app.kg_repo import get_docs_for_reports
                    related_docs = get_docs_for_reports(sorted(collected_report_indexes), limit=5)
                except Exception as e:
                    print(f"[KG] 관련 문서 조회 스킵: {e}")
                if related_docs:
                    yield yield_step(f"🔗 이 통계와 연결된 원본 보고서 문서 {len(related_docs)}건을 찾았어요")
                    existing_doc_ids = {d.get("doc_id") for d in top_docs_ui}
                    for d in related_docs:
                        if d["doc_id"] not in existing_doc_ids:
                            top_docs_ui.append(d)

            # 💡 [검증 요약] 숫자 에코 체크(DB) + claim 지원율
            verification = {"grounded": True}
            if used_db and db_tool_results:
                verification.update(_numeric_echo_check(
                    final_answer=final_answer,
                    db_tool_results=db_tool_results,
                    allowed_extra=_extract_numbers(user_query),
                ))
            claim_list = citations_json.get("answer") or []
            if claim_list:
                verification["claims_supported"] = sum(1 for c in claim_list if c.get("support") == "supported")
                verification["claims_total"] = len(claim_list)

            # 💡 [4. 최종 완료 로그 및 데이터 반환]
            final_result = {
                "intent": intent,
                "final_answer": final_answer,
                "suggested_actions": _get_suggested_actions(intent, db_used=used_db),
                "citations": citations_json,
                "top_docs": top_docs_ui,
                "related_docs": related_docs,
                "steps": execution_steps,
                "verification": verification,
            }
            yield json.dumps({"type": "final", "data": final_result}, ensure_ascii=False) + "\n"
            return
            
    # 에이전트 루프 초과 시
    yield yield_step("❌ 분석 과정이 너무 길어져서 여기서 멈췄어요. 질문을 조금 더 구체적으로 해주시면 도움이 돼요.")
    timeout_result = {
        "intent": intent,
        "final_answer": "에이전트 처리 단계를 초과했습니다.",
        "suggested_actions": [],
        "citations": {"answer": [], "final": "에이전트 처리 단계를 초과했습니다.", "claims": []},
        "top_docs": [],
        "steps": execution_steps
    }
    yield json.dumps({"type": "final", "data": timeout_result}, ensure_ascii=False) + "\n"
    return

# =====================================================================
# 🧠 5. 기존 비스트리밍(동기) 방식 호환용 Wrapper 함수 (💡 삭제하면 안 됨!)
# =====================================================================
def run_agent_loop(user_id: str, user_query: str, previous_messages: list, excluded_indexes: set, ui_top_k: int, forced_intent: str = None) -> dict:
    """
    기존 /api/chat 엔드포인트에서 호출할 수 있도록
    스트리밍 제너레이터를 돌려서 마지막 최종 결과 데이터만 추출해 반환합니다.
    """
    final_data = None
    
    for chunk in run_agent_loop_stream(user_id, user_query, previous_messages, excluded_indexes, ui_top_k, forced_intent):
        try:
            chunk_dict = json.loads(chunk.strip())
            if chunk_dict.get("type") == "final":
                final_data = chunk_dict.get("data")
        except Exception:
            pass
            
    if final_data:
        return final_data
        
    return {
        "intent": "GENERAL_CHAT",
        "final_answer": "에이전트 처리 중 응답을 생성하지 못했습니다.",
        "suggested_actions": [],
        "citations": {"answer": [], "final": "응답 실패", "claims": []},
        "top_docs": [],
        "steps": [{"thought": "❌ 시스템 에러 발생: 최종 응답을 추출하지 못했습니다."}]
    }