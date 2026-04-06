import json
import datetime  # ⭐️ 날짜 처리를 위해 추가됨
from app.llm_client import (
    _make_client,
    _build_citation_prompt,
    _call_json,
    _normalize_claims_to_answer_list,
    CITATION_MAX_TOKENS
)
from app.tools import TOOLS_SCHEMA, execute_tool

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

def run_agent_loop(user_id: str, user_query: str, previous_messages: list, excluded_indexes: set, ui_top_k: int) -> dict:
    client = _make_client(user_id)
    
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")
    
    # ========================================================
    # ⭐️ 허용된 값(Enum)과 나노 변환 규칙이 추가된 데이터 사전
    # ========================================================
    db_schema_info = """
    [데이터베이스 스키마 정보]
    - 테이블명: v_ai_defect_search (AI 분석 전용 뷰 테이블)
    - 주요 컬럼:
      * report_index (분석 리포트 고유번호)
      * 분석완료일시 (TEXT 타입, 예: '2026-01-09 14:45:50')
      * 불량명 (분석된 불량의 이름)
      * 성분 (예: 'Co,C', 'Fe', 'Al, F' 등 콤마로 연결된 화학 원소. NULL 존재함)
      
      * 💡공정노드 (Node): 반도체 공정 세대를 나타냅니다.
        - [허용된 값]: 'NPW', 'SF2', 'SF3', 'SF4', 'SF5', 'SF7'
        - [동의어 변환 절대 규칙]: 사용자가 "N나노" 또는 "Nnm" (예: 2나노, 3nm)라고 질문하면, 데이터베이스에 저장된 값인 'SF' + N (예: 'SF2', 'SF3')으로 치환하여 SQL 조건(`WHERE 공정노드 = 'SF2'`)을 작성하세요.
      
      * 💡모듈 (Module): 특정 공정 단계를 의미합니다. 공정노드와 절대 헷갈리지 마세요.
        - [허용된 값]: 'BEOL', 'FIN', 'MOL', 'NS', 'PC', 'RMG', 'RPG', 'SD'
      
      * 의뢰자명 (분석을 의뢰한 담당자 이름. 예: '홍길동')
      * 분석담당자 (해당 분석을 실제로 진행한 담당자 이름)
      * 보고서링크 (분석 결과 보고서를 볼 수 있는 EDM URL 링크)
      * 제품명, 라인, 공정명, 설비명, 의뢰부서, 분석_제목 등

    [SQL 작성 엄격 규칙]
    1. 기간 조건: '최근 N개월'은 오늘 날짜를 기준으로 `분석완료일시 >= DATE_SUB(NOW(), INTERVAL N MONTH)` 를 사용하세요.
    2. 건수 집계: 발생 건수를 셀 때는 COUNT(*)가 아니라 반드시 `COUNT(DISTINCT report_index)`를 사용하세요.
    3. 성분(원소) 필터링: 성분 컬럼을 조회할 때는 반드시 `성분 IS NOT NULL AND 성분 != '' AND 성분 NOT IN ('없음', '하부 단차', '확인 안됨', '확인안됨', '-', 'Abnormal MBC', 'Abnormal')` 조건을 걸어 쓰레기값을 제외하세요.
    4. 💡 성분 개별 집계(가장 중요한 규칙): 사용자가 '가장 많이 발생한 성분(원소)'을 통계 내달라고 하면, 콤마(,)로 연결된 문자열을 쪼개야 하므로 무조건 아래의 재귀 CTE(Recursive CTE) 템플릿을 응용해서 작성하세요!

    -- [성분 쪼개기 템플릿]
    WITH RECURSIVE SplitElements AS (
      SELECT report_index,
             TRIM(SUBSTRING_INDEX(성분, ',', 1)) AS element,
             SUBSTRING(성분, LENGTH(SUBSTRING_INDEX(성분, ',', 1)) + 2) AS remainder
      FROM v_ai_defect_search
      WHERE 성분 IS NOT NULL AND 성분 != '' AND 성분 NOT IN ('없음', '-', '확인 안됨') -- (여기에 분석완료일시 등 추가 조건 삽입)
      UNION ALL
      SELECT report_index,
             TRIM(SUBSTRING_INDEX(remainder, ',', 1)),
             IF(INSTR(remainder, ',') > 0, SUBSTRING(remainder, LENGTH(SUBSTRING_INDEX(remainder, ',', 1)) + 2), NULL)
      FROM SplitElements
      WHERE remainder IS NOT NULL AND remainder != ''
    )
    SELECT element AS '성분', COUNT(DISTINCT report_index) AS cnt
    FROM SplitElements
    GROUP BY element ORDER BY cnt DESC LIMIT 5;
    """
    
    system_prompt = f"""
    [페르소나 및 기본 역할 - 절대 엄수]
    - 당신의 정체는 사내 불량 데이터 분석과 기술 문서 검색을 돕는 '사내 불량 분석 AI 에이전트'입니다.
    - 사용자가 "넌 누구야?", "자신을 소개해봐" 등의 질문을 하면, 스스로를 '불량 데이터 통계와 원인 분석을 돕기 위해 개발된 사내 전문 에이전트'라고 친절하고 당당하게 소개하세요. 절대 자신을 일반적인 오픈소스 모델이나 단순한 AI라고 칭하지 마세요.
    - ⭐️ 모든 답변은 반드시 100% 한국어로만 작성하세요! (단, SQL 쿼리문이나 영어 고유명사는 예외) ⭐️
    
    [현재 컨텍스트]
    - 오늘 날짜는 {current_date} 입니다.
    {db_schema_info}
    
    [도구 선택의 4가지 절대 분기 규칙]
    사용자의 질문을 분석하여 다음 4가지 케이스 중 하나를 선택해 행동하세요.
    1. 일반 대화 (도구 사용 안 함): "안녕", "고마워", "넌 누구야?" 같은 단순 인사/대화일 경우 도구를 절대 호출하지 말고 당신의 페르소나에 맞춰 자연스럽게 대답하세요.
    2. DB 통계 (query_database 사용): "N개월간 몇 건?", "가장 많이 발생한 불량은?", "순위는?" 등 숫자를 집계해야 하는 질문은 무조건 `query_database` 도구만 사용하세요.
    3. RAG 지식 (search_documents 사용): "이 불량의 원리가 뭐야?", "해결책이 뭐야?" 등 개념, 원리, 방법론을 묻는 질문은 DB에 없으므로 무조건 `search_documents` 도구만 사용하세요.
    4. 하이브리드: "1위 불량이 뭔지 찾고(DB), 그 불량의 원리를 알려줘(RAG)" 처럼 섞여 있다면 두 도구를 순차적으로 모두 사용하세요.
    
    [출력 및 포맷팅 엄격 규칙]
    1. 🚫 RAG 답변 시 표(Table) 생성 원천 차단: `search_documents`로 찾은 문서를 요약할 때 마크다운 표(| 기호)를 사용하면 프론트엔드 시스템이 심각하게 고장납니다. 절대 표를 그리지 말고 소제목(###)과 불릿 리스트(-)로만 구조화하여 설명하세요. 
    2. ✅ 표 생성의 유일한 예외: 단, 사용자의 질문에 "표로 만들어줘", "표 형태로"라는 단어가 명시적으로 포함되어 있을 때만 예외적으로 표를 생성하세요. 이때 표 안에서는 절대 줄바꿈(\\n)을 하지 말고 필요시 `<br>` 태그만 사용하세요.
    3. 마크다운 표 병합 금지 (DB 통계 시): 숫자를 표로 보여줄 때, 중복된 값이라도 생략하거나 빈칸으로 두지 말고 모든 행에 값을 명시적으로 꽉 채워 넣으세요.
    4. 거짓말 금지 (Hallucination 방지): 도구가 반환한 실제 데이터만 사용하여 답변하세요. 쿼리 결과가 없거나 실패했다면 임의로 데이터를 지어내지 말고 "데이터를 찾을 수 없습니다"라고 정직하게 말하세요.
    5. DB 조회 결과가 너무 많다는 에러를 받으면, 데이터를 통째로 가져오지 말고 SQL 안에서 COUNT, GROUP BY로 집계되도록 쿼리를 수정하세요.
    6. 사용자가 특정 불량에 대해 물어보면 '분석담당자'와 '보고서링크'를 함께 제공하세요.
    7. 당신이 실행한 SQL 쿼리가 있다면 최종 답변 맨 밑에 ```sql ... ``` 마크다운 블록으로 반드시 보여주세요.
    """

    messages = [{"role": "system", "content": system_prompt}]
    if previous_messages:
        messages.extend(previous_messages[-4:])
    messages.append({"role": "user", "content": user_query})

    MAX_STEPS = 7
    all_collected_hits = [] 

    for step in range(MAX_STEPS):
        print(f"\n[Agent Step {step+1}] LLM 판단 중...")
        
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            temperature=0.0
        )
        
        ai_message = response.choices[0].message
        messages.append(ai_message)
        
        if getattr(ai_message, 'tool_calls', None):
            for tool_call in ai_message.tool_calls:
                func_name = tool_call.function.name
                
                try:
                    args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as e:
                    print(f"  ❌ LLM JSON 파싱 에러 (무시하고 재시도): {e}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": "Error: JSON 파라미터 파싱에 실패했습니다. 형식이나 따옴표를 다시 확인하고 호출하세요."
                    })
                    continue

                print(f"  👉 실행 도구: {func_name} | 파라미터: {args}")
                
                tool_result_str, hits = execute_tool(func_name, args)
                if hits:
                    all_collected_hits.extend(hits)
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result_str
                })
            continue

        else:
            print("  ✅ 최종 답변 생성 완료")
            final_answer = ai_message.content or ""
            
            citations_json = {"answer": [], "final": final_answer, "claims": []}
            top_docs_ui = []

            if all_collected_hits:
                hits_visible = _dedupe_and_filter_hits(all_collected_hits, excluded_indexes)
                top_docs_ui = [_to_ui_doc_from_hit(h) for h in hits_visible[:ui_top_k]]
                rag_chunks = [_to_ui_doc_from_hit(h) for h in hits_visible[:6]]
                
                try:
                    citation_messages = _build_citation_prompt(
                        user_question=user_query,
                        final_answer=final_answer,
                        rag_chunks=rag_chunks,
                    )
                    citation_res = _call_json(client, citation_messages, CITATION_MAX_TOKENS, 0.0)
                    claims = citation_res.get("claims") or []
                    
                    citations_json = {
                        "answer": _normalize_claims_to_answer_list(claims),
                        "final": final_answer,
                        "claims": claims
                    }
                except Exception as e:
                    print(f"  ❌ 인용구 생성 실패: {e}")

            return {
                "final_answer": final_answer,
                "citations": citations_json,
                "top_docs": top_docs_ui
            }
            
    return {
        "final_answer": "에이전트 처리 단계를 초과했습니다.",
        "citations": {"answer": [], "final": "에이전트 처리 단계를 초과했습니다.", "claims": []},
        "top_docs": []
    }