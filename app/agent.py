import json
import datetime
from app.llm_client import (
    _make_client,
    _build_citation_prompt,
    _call_json,
    _normalize_claims_to_answer_list,
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

    router_prompt = """
    당신은 반도체 불량 분석 시스템의 '의도 분류 라우터(Router)'입니다.
    사용자의 질문을 읽고 반드시 다음 4가지 인텐트 중 딱 하나만 텍스트로 반환하세요. (다른 말은 절대 금지)
    
    [인텐트 종류]
    1. "DB_ANALYSIS": 불량 발생 건수, 통계, 순위, 리스트 등 DB 데이터를 조회해야 하는 경우
    2. "RAG_KNOWLEDGE": 특정 불량의 발생 원리, 가이드, 해결책 등 기술 문서를 찾아야 하는 경우
    3. "HYBRID_DB_RAG": 통계 조회와 문서(원리) 검색이 모두 필요한 경우
    4. "GENERAL_CHAT": 안부 인사, 단순 대화 등 도구 검색이 필요 없는 경우
    
    [출력 예시]
    사용자: "최근 3개월 sf2 불량 순위" -> 출력: DB_ANALYSIS
    사용자: "파티클 원인이 뭐야?" -> 출력: RAG_KNOWLEDGE
    사용자: "안녕 반가워" -> 출력: GENERAL_CHAT
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
        
        if "DB_ANALYSIS" in intent: return "DB_ANALYSIS"
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
    - ⭐️ 도구를 호출하기 전, 반드시 당신이 왜 이 도구를 쓰는지 비즈니스 언어로 1줄 요약하여 'Thought: [생각]' 형태로 메시지 본문에 먼저 출력하세요. (절대 Thought 영역에 SQL이나 코드를 노출하지 마세요)
    """
    
    # 📌 DB 전문가 스키마 (SQL 강제 접기 포함)
    db_schema = """
    [데이터베이스 스키마 정보: v_ai_defect_search]
    - report_index, Lot_ID, WF_ID, 분석완료일시, 불량명, 성분, 공정노드, 모듈, 라인, 공정명, 설비명, 의뢰자명, 분석담당자, 보고서링크
    
    [DB 전문가 엄격 규칙]
    1. 정적 데이터(완벽 일치 '=' 사용): 공정노드('NPW', 'SF2', 'SF3', 'SF4', 'SF5', 'SF7'), 모듈('BEOL', 'FIN', 'MOL', 'NS', 'PC', 'RMG', 'RPG', 'SD')
    2. 동적 데이터(Fuzzy Search, 'LIKE' 강제): '불량명', '설비명', '의뢰자명' 등은 종류가 너무 많으므로 무조건 `LIKE '%키워드%'` 로 넓게 검색하세요.
    3. 복합 명사 주의: "Depo PC"처럼 모듈명(PC)이 섞여도 무조건 `모듈='PC'`로 하지 말고, `WHERE 불량명 LIKE '%Depo PC%'` 조건을 우선 고려.
    4. ⭐️ 목록 조회 시 중복 제거 규칙 (절대 엄수): 통계가 아닌 불량 내역(목록)을 조회하여 표로 그릴 때, 동일한 report_index, Lot_ID, WF_ID가 여러 줄 중복 출력되면 절대 안 됩니다!
       - SQL 쿼리 끝에 반드시 `GROUP BY report_index, Lot_ID, WF_ID`를 추가하여 행을 하나로 묶으세요.
       - 이때 데이터 유실을 막기 위해 SELECT 절에서는 `MAX()` 대신 반드시 `GROUP_CONCAT(DISTINCT 불량명 SEPARATOR ', ') AS 불량명`, `GROUP_CONCAT(DISTINCT 성분 SEPARATOR ', ') AS 성분`으로 작성하고, 나머지 단순 컬럼들은 `MAX(분석담당자)` 처럼 집계 함수를 씌우세요.
    5. 자가 교정: 결과가 0건이면 엄격한 조건을 버리고 불량명 `LIKE` 위주로 쿼리를 재작성하여 다시 도구 호출.
    6. 출력 포맷: DB 통계/목록 결과는 무조건 **마크다운 표(Table)**로만 출력.
    7. ⭐️ SQL 노출 (필수): 당신이 실행한 최종 SQL 쿼리는 반드시 답변 맨 마지막에 아래 형식을 지켜서 넣으세요.
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
        return base_persona + "\n- 도구를 사용하지 말고 다정하고 전문적으로 답변하세요."

# =====================================================================
# 🧠 3. 문맥 맞춤형 액션 칩 (UI 버튼) 생성기
# =====================================================================
def _get_suggested_actions(intent: str, db_used: bool = False) -> list:
    if intent == "DB_ANALYSIS":
        actions = [
            {"label": "📖 문서 검색 Agent 호출하기", "action": "[RAG_KNOWLEDGE]"}
        ]
        # DB 쿼리가 실행되었을 때만 '재검색' 기능 활성화
        if db_used:
            actions.append({"label": "⚠️ 조건을 넓혀서 다시 검색", "action": "retry"})
        else:
            actions.append({"label": "⚠️ 조건을 넓혀서 다시 검색", "action": "none", "disabled": True})
        return actions

    elif intent == "RAG_KNOWLEDGE":
        return [
            {"label": "📊 DB 통계 Agent 호출하기", "action": "[DB_ANALYSIS]"}
        ]
    elif intent == "HYBRID_DB_RAG":
        return []
    else:
        return []

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
        yield yield_step(f"🎯 유저 요청으로 Agent 강제 전환: {intent}")
    else:
        yield yield_step("🔍 질문 의도 파악 및 라우팅 진행 중...")
        intent = _call_intent_router(client, user_query)
        yield yield_step(f"🎯 판단된 인텐트: {intent}")

    system_prompt = _get_specialist_prompt(intent, current_date)
    
    messages = [{"role": "system", "content": system_prompt}]
    if previous_messages:
        messages.extend(previous_messages[-4:])
    messages.append({"role": "user", "content": user_query})

    # [2. 무기 압수 (Tools 필터링)]
    available_tools = TOOLS_SCHEMA
    if intent == "DB_ANALYSIS":
        available_tools = [t for t in TOOLS_SCHEMA if t["function"]["name"] == "query_database"]
    elif intent == "RAG_KNOWLEDGE":
        available_tools = [t for t in TOOLS_SCHEMA if t["function"]["name"] == "search_documents"]

    MAX_STEPS = 10
    all_collected_hits = []
    used_db = False 

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
                    if func_name == "query_database": parsed_thought = f"📊 DB 통계 데이터를 조회하고 있습니다..."
                    elif func_name == "search_documents": parsed_thought = f"📖 사내 기술 문서를 검색하고 있습니다..."

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
                
                # 💡 결과 0건 시 시행착오(자가교정) 피드백도 기록
                if func_name == "query_database" and ("[]" in tool_result_str or "0건" in tool_result_str):
                    tool_result_str += "\n[시스템 알림]: 검색 결과가 0건입니다. 방금 넣은 모듈, 라인 등의 조건을 지우고 불량명 LIKE 검색 위주로 쿼리를 재작성하여 다시 도구를 호출해 보세요."
                    yield yield_step(thought="⚠️ 검색 결과가 0건입니다. 조건을 넓혀 재검색을 시도합니다.")
                
                if hits:
                    all_collected_hits.extend(hits)
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_result_str})
            continue
        
        else:
            yield yield_step("✅ 분석 완료! 답변 생성 및 인용구(Citation) 매핑 중...")
            
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
                    citations_json = {"answer": _normalize_claims_to_answer_list(claims), "final": final_answer, "claims": claims}
                except Exception as e:
                    print(f"  ❌ 인용구 생성 실패: {e}")

            # 💡 [4. 최종 완료 로그 및 데이터 반환]
            final_result = {
                "intent": intent,
                "final_answer": final_answer,
                "suggested_actions": _get_suggested_actions(intent, db_used=used_db),
                "citations": citations_json,
                "top_docs": top_docs_ui,
                "steps": execution_steps
            }
            yield json.dumps({"type": "final", "data": final_result}, ensure_ascii=False) + "\n"
            return
            
    # 에이전트 루프 초과 시
    yield yield_step("❌ 에이전트 처리 단계를 초과하여 종료되었습니다.")
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