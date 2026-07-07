import os
import re
import time
import json
from pathlib import Path
from datetime import datetime
from email.utils import parsedate_to_datetime

from fastapi import FastAPI, Request, Response, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import VERSION
from app.auth import check_auth, validate_jwt
from app.config import (
    DEFAULT_INDEX_NAME,
    DEFAULT_TOP_K,
    INDEX_OPTIONS,
    MAIL_ROOT,
    PARSE_ROOT,
    ALLOWED_VIEW_ROOTS,
)
from app.db_schema import ensure_tables
from app import repo
# 💡 기존 일반 루프와 새로운 스트리밍 루프를 모두 가져옵니다.
from app.agent import run_agent_loop, run_agent_loop_stream
from app.llm_client import rewrite_query_with_history
from app.query_normalizer import normalize_and_expand_query
from app.dictionary_repo import (
    propose_term_candidate, 
    get_all_terms, 
    get_pending_candidates, 
    approve_candidate
)
# from app.rag_client import rag_get_archive_docs

app = FastAPI(title="RAG Search Web", version=VERSION)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
async def _kg_startup():
    """Knowledge Graph 테이블 보장 + 백그라운드 빌드 (기동 시 1회 + 24h 주기).
    실패해도 서버 기동은 막지 않는다."""
    try:
        from app.kg_builder import ensure_kg_tables, start_background_rebuild
        ensure_kg_tables()
        start_background_rebuild()
    except Exception as e:
        print(f"[KG] 초기화 스킵 (그래프 기능 비활성): {e}")

MAX_HISTORY_MESSAGES = 6  # 최근 6개 메시지 = 대략 최근 3턴
RETRIEVE_TOP_K_DEFAULT = 12
EXCLUDED_TOPDOC_INDEXES = {"rp-term-ver1"}

# 관리자 권한을 가진 User ID 화이트리스트
ADMIN_USER_IDS = ["s.park"] 

def build_sliding_window_messages(
    all_messages: list[dict],
    current_user_msg_id: str | None = None,
    max_messages: int = MAX_HISTORY_MESSAGES
) -> list[dict]:
    history = []
    for msg in all_messages or []:
        if current_user_msg_id and msg.get("msg_id") == current_user_msg_id:
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        history.append({"role": role, "content": msg.get("content", "")})
    history = history[-max_messages:]
    if history and history[0]["role"] == "assistant":
        history = history[1:]
    return history


@app.on_event("startup")
def _startup():
    ensure_tables()
    # 💡 서버 시작 시 아카이브 문서를 미리 로딩하여 첫 유저의 대기 시간 제거
    try:
        print("[Startup] 아카이브 캐시 초기화 중...")
        get_local_archive_docs()
    except Exception as e:
        print(f"[Startup] 아카이브 초기 로딩 실패: {e}")


def _require_user(request: Request) -> str:
    token = request.cookies.get("token")
    user = validate_jwt(token)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    token = request.cookies.get("token")
    user = validate_jwt(token)
    if user:
        return RedirectResponse(url="/chat", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/", response_class=HTMLResponse)
async def login(request: Request):
    form_data = await request.form()
    new_token = check_auth(form_data.get("id"), form_data.get("password"))
    if new_token:
        resp = RedirectResponse(url="/chat", status_code=302)
        resp.set_cookie(key="token", value=new_token, httponly=True)
        return resp
    return templates.TemplateResponse("login.html", {"request": request, "message": "Login failed.. Please try again"})


@app.post("/logout", response_class=HTMLResponse)
async def logout(request: Request):
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie(key="token")
    return resp

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = _require_user(request)
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user_id": user,
        "default_index": DEFAULT_INDEX_NAME,
        "index_options": INDEX_OPTIONS,
        "active_tab": "settings",
        "version": VERSION,
    })


@app.get("/admin/dictionary", response_class=HTMLResponse)
async def admin_dictionary_page(request: Request):
    user = _require_user(request)
    return templates.TemplateResponse("admin_dictionary.html", {
        "request": request,
        "user_id": user,
        "active_tab": "admin",
    })


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    user = _require_user(request)
    return templates.TemplateResponse("chat.html", {
        "request": request,
        "user_id": user,
        "default_index": DEFAULT_INDEX_NAME,
        "index_options": INDEX_OPTIONS,
        "default_top_k": DEFAULT_TOP_K,
        "active_tab": "knowledge_base" # 💡 기존 chat 라우터에도 추가
    })
    
@app.get("/archive", response_class=HTMLResponse)
async def archive_page(request: Request):
    user = _require_user(request)
    
    # 현재 접속한 유저가 관리자 리스트에 있는지 확인 (True / False)
    is_admin = user in ADMIN_USER_IDS 
    
    return templates.TemplateResponse("archive.html", {
        "request": request, 
        "active_tab": "archive",
        "user_id": user,
        "is_admin": is_admin  # 💡 Jinja2 템플릿으로 변수 전달!
    })

@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    user = _require_user(request)
    # Phase 5에서 구현할 시각화 페이지
    return templates.TemplateResponse("base.html", {
        "request": request,
        "user_id": user
    })

@app.get("/trace", response_class=HTMLResponse)
async def trace_page(request: Request):
    user = _require_user(request)
    return templates.TemplateResponse("trace.html", {
        "request": request,
        "user_id": user,
        "active_tab": "trace",
        "is_admin": user in ADMIN_USER_IDS,
    })


# =========================
# API: sessions
# =========================
@app.get("/api/sessions")
async def api_sessions(request: Request):
    user = _require_user(request)
    return {"sessions": repo.list_sessions(user)}


@app.post("/api/sessions")
async def api_create_session(request: Request):
    user = _require_user(request)
    body = await request.json()
    title = (body.get("title") or "New Chat").strip()
    sid = repo.create_session(user, title)
    return {"session_id": sid}

@app.get("/api/sessions/{session_id}")
async def api_get_session_messages(session_id: str, request: Request):
    user = _require_user(request)
    msgs = repo.get_messages(session_id, user)
    search_logs = repo.list_search_logs_for_session(session_id, user)

    artifacts_by_ast = {}
    try:
        artifacts_by_ast = repo.get_artifacts_for_session(session_id, user)
    except Exception as e:
        print(f"Artifacts 로드 실패: {e}")

    search_log_by_user_msg_id = {}
    for log in search_logs or []:
        user_msg_id = log.get("user_msg_id")
        if not user_msg_id:
            continue
        search_log_by_user_msg_id[user_msg_id] = log

    feedback_map = {}
    try:
        feedback_map = repo.get_feedback_map(session_id, user)
    except Exception as e:
        print(f"Feedback 로드 실패: {e}")

    for m in msgs:
        if m["role"] == "assistant":
            ast_id = m.get("msg_id")
            rag_info = artifacts_by_ast.get(ast_id, {})
            m["intent"] = rag_info.get("intent")
            m["suggested_actions"] = rag_info.get("suggested_actions", [])
            m["agent_steps"] = rag_info.get("agent_steps", [])
            m["related_docs"] = rag_info.get("related_docs", [])
            m["feedback"] = feedback_map.get(ast_id)

    return {"messages": msgs, "search_logs_by_user_msg_id": search_log_by_user_msg_id}

# =========================
# API: chat_stream (실시간 스트리밍 전용 - 💡 신규 추가)
# =========================
@app.post("/api/chat_stream")
async def api_chat_stream(request: Request):
    user = _require_user(request)
    body = await request.json()

    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        session_id = repo.create_session(user, "New Chat")

    user_text = (body.get("user_text") or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="user_text required")

    forced_intent = None
    if user_text.startswith("[DB_ANALYSIS]"):
        forced_intent = "DB_ANALYSIS"
        user_text = user_text.replace("[DB_ANALYSIS]", "").strip()
    elif user_text.startswith("[RAG_KNOWLEDGE]"):
        forced_intent = "RAG_KNOWLEDGE"
        user_text = user_text.replace("[RAG_KNOWLEDGE]", "").strip()
    elif user_text.startswith("[REPORT_ANALYSIS]"):
        forced_intent = "REPORT_ANALYSIS"
        user_text = user_text.replace("[REPORT_ANALYSIS]", "").strip()

    index_names = body.get("index_names")
    if isinstance(index_names, list):
        index_names = [str(x).strip() for x in index_names if str(x).strip()]
    elif isinstance(body.get("index_name"), str):
        index_names = [body.get("index_name").strip()]
    else:
        index_names = [DEFAULT_INDEX_NAME]

    index_name_for_db = ",".join(index_names)
    ui_top_k = int(body.get("top_k") or DEFAULT_TOP_K)
    retrieve_top_k = max(ui_top_k, RETRIEVE_TOP_K_DEFAULT)
    filters = body.get("filters")

    # 1) 현재 user message 저장
    user_msg_id = repo.insert_message(session_id, user, "user", user_text)

    # 2) 이전 대화 로드 + 슬라이딩 윈도우
    previous_messages = []
    try:
        all_messages = repo.get_messages(session_id, user)
        previous_messages = build_sliding_window_messages(
            all_messages=all_messages,
            current_user_msg_id=user_msg_id,
            max_messages=MAX_HISTORY_MESSAGES,
        )
    except Exception as e:
        print(f"Failed to load previous messages: {e}")

    # 💡 스트리밍 제너레이터 함수
    async def generate_response():
        yield json.dumps({"type": "step", "message": "🔄 질문을 이해하고 검색에 맞게 다듬고 있어요..."}, ensure_ascii=False) + "\n"

        rewritten_query = user_text
        try:
            rewritten_query = rewrite_query_with_history(
                user_id=user,
                user_question=user_text,
                previous_messages=previous_messages,
            )
        except Exception as e:
            print(f"Query rewrite failed: {e}")

        query_norm = {
            "original_query": rewritten_query,
            "normalized_query": rewritten_query,
            "expanded_query": rewritten_query,
            "detected_terms": [],
            "expansion_terms": {},
        }
        try:
            scope_candidates = ["all", "inline_fa_report"]
            query_norm = normalize_and_expand_query(
                query_text=rewritten_query,
                scope_candidates=scope_candidates,
            )
        except Exception as e:
            print(f"Query normalization failed: {e}")

        # 💡 [핵심] 동적 미니 용어 사전 생성 및 프롬프트 주입
        detected_terms = query_norm.get("detected_terms") or []
        unique_terms = {}
        
        for dt in detected_terms:
            c_name = dt.get("canonical_name")
            desc = dt.get("description")
            t_type = dt.get("term_type", "unknown")
            if c_name and desc and c_name not in unique_terms:
                unique_terms[c_name] = f"* {c_name} ({t_type}): {desc}"

        # 💡 프론트엔드 UI에 띄워줄 강제 주입용 로그 문자열 준비
        glossary_step_log = None
        if unique_terms:
            glossary_text = "\n".join(unique_terms.values())
            system_prompt = (
                "당신은 사내 지식 도우미입니다. 사용자의 질문과 관련된 사내 전문 용어의 뜻을 아래 사전을 통해 파악하고, "
                "이를 바탕으로 검색된 문서의 문맥을 깊이 있게 이해하여 답변을 작성하세요.\n\n"
                f"[사내 용어 사전 (Mini-Glossary)]\n{glossary_text}"
            )
            previous_messages.insert(0, {"role": "system", "content": system_prompt})
            
            term_keys = ", ".join(unique_terms.keys())
            glossary_step_log = f"📚 사내 용어 사전에서 관련 용어의 뜻을 확인했어요 ({term_keys})"

        retrieval_query = (query_norm.get("expanded_query") or rewritten_query).strip() or rewritten_query

        final_data = None
        is_first_agent_chunk = True

        for chunk in run_agent_loop_stream(
            user_id=user,
            user_query=retrieval_query,
            previous_messages=previous_messages,
            excluded_indexes=EXCLUDED_TOPDOC_INDEXES,
            ui_top_k=ui_top_k,
            forced_intent=forced_intent,
            index_names=index_names
        ):
            # 💡 [핵심 해킹 로직] 에이전트 스트림 청크를 가로채어 배열 맨 앞에 로그를 끼워 넣습니다.
            if glossary_step_log:
                try:
                    chunk_dict = json.loads(chunk.strip())
                    # 실시간 스텝 배열 업데이트 시
                    if "steps" in chunk_dict and isinstance(chunk_dict["steps"], list):
                        if glossary_step_log not in chunk_dict["steps"]:
                            chunk_dict["steps"].insert(0, glossary_step_log)
                        chunk = json.dumps(chunk_dict, ensure_ascii=False) + "\n"
                    # 최종 완료 데이터 업데이트 시
                    elif chunk_dict.get("type") == "final" and "steps" in chunk_dict.get("data", {}):
                        if glossary_step_log not in chunk_dict["data"]["steps"]:
                            chunk_dict["data"]["steps"].insert(0, glossary_step_log)
                        chunk = json.dumps(chunk_dict, ensure_ascii=False) + "\n"
                except Exception:
                    pass

            yield chunk
            
            # 💡 만약 배열이 아니라 단일 메시지 방식의 UI라면, 첫 청크 직후에 다시 한번 확실하게 쏴줍니다.
            if is_first_agent_chunk and glossary_step_log:
                yield json.dumps({"type": "step", "message": glossary_step_log}, ensure_ascii=False) + "\n"
                is_first_agent_chunk = False
            
            try:
                chunk_dict = json.loads(chunk.strip())
                if chunk_dict.get("type") == "final":
                    final_data = chunk_dict.get("data")
            except Exception:
                pass

        # 6) 루프 종료 후 DB 저장 및 최종 결과 렌더링을 위한 데이터 반환
        if final_data:
            final_answer = final_data.get("final_answer", "응답을 생성하지 못했습니다.")
            assistant_msg_id = repo.insert_message(session_id, user, "assistant", final_answer)

            rag_resp_for_store = {
                "agent_used": True,
                "original_query": user_text,
                "rewritten_query": rewritten_query,
                "normalized_query": query_norm.get("normalized_query"),
                "expanded_query": query_norm.get("expanded_query"),
                "detected_terms": query_norm.get("detected_terms") or [],
                "expansion_terms": query_norm.get("expansion_terms") or {},
                "top_docs": final_data.get("top_docs", []),
                "related_docs": final_data.get("related_docs", []),
                "intent" : final_data.get("intent"),
                "suggested_actions" : final_data.get("suggested_actions", []),
                "agent_steps" : final_data.get("steps", []),
                # 평가 대시보드(Cognitive Trace) 시계열 집계용
                "verification": final_data.get("verification", {})
            }
            
            repo.insert_turn_artifact(
                session_id=session_id,
                user_id=user,
                user_msg_id=user_msg_id,
                assistant_msg_id=assistant_msg_id,
                index_name=index_name_for_db,
                rag_response=rag_resp_for_store,
                citations=final_data.get("citations", {"answer": [], "final": final_answer, "claims": []})
            )

            try:
                repo.insert_search_log(
                    session_id=session_id,
                    user_id=user,
                    user_msg_id=user_msg_id,
                    assistant_msg_id=assistant_msg_id,
                    index_name=index_name_for_db,
                    original_query=user_text,
                    rewritten_query=rewritten_query,
                    normalized_query=query_norm.get("normalized_query"),
                    expanded_query=query_norm.get("expanded_query"),
                    detected_terms=query_norm.get("detected_terms") or [],
                    expansion_terms=query_norm.get("expansion_terms") or {},
                    filters=filters,
                    top_docs=final_data.get("top_docs", []),
                    retrieve_top_k=retrieve_top_k,
                )
            except Exception as e:
                print(f"search log insert failed: {e}")

            try:
                if user_text:
                    repo.touch_session(session_id, user, title=user_text[:60])
            except Exception:
                repo.touch_session(session_id, user)

            # 프론트엔드 자바스크립트가 처리할 수 있는 형태의 종합 최종 데이터
            final_res_payload = {
                "session_id": session_id,
                "assistant_text": final_answer,
                "assistant_msg_id": assistant_msg_id,
                "citations": final_data.get("citations", {}),
                "top_docs": final_data.get("top_docs", []),
                "rewritten_query": rewritten_query,
                "normalized_query": query_norm.get("normalized_query"),
                "expanded_query": query_norm.get("expanded_query"),
                "detected_terms": query_norm.get("detected_terms") or [],
                "expansion_terms": query_norm.get("expansion_terms") or {},
                "intent": final_data.get("intent"),
                "suggested_actions": final_data.get("suggested_actions", []),
                "agent_steps": final_data.get("steps", []),
                "verification": final_data.get("verification", {}),
                "related_docs": final_data.get("related_docs", [])
            }
            yield json.dumps({"type": "final", "data": final_res_payload}, ensure_ascii=False) + "\n"

    # StreamingResponse로 감싸서 반환 (ndjson 방식)
    return StreamingResponse(generate_response(), media_type="application/x-ndjson")


# =========================
# API: chat (기존 동기화 방식 호환용 유지)
# =========================
@app.post("/api/chat")
async def api_chat(request: Request):
    user = _require_user(request)
    body = await request.json()

    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        session_id = repo.create_session(user, "New Chat")

    user_text = (body.get("user_text") or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="user_text required")

    forced_intent = None
    if user_text.startswith("[DB_ANALYSIS]"):
        forced_intent = "DB_ANALYSIS"
        user_text = user_text.replace("[DB_ANALYSIS]", "").strip()
    elif user_text.startswith("[RAG_KNOWLEDGE]"):
        forced_intent = "RAG_KNOWLEDGE"
        user_text = user_text.replace("[RAG_KNOWLEDGE]", "").strip()
    elif user_text.startswith("[REPORT_ANALYSIS]"):
        forced_intent = "REPORT_ANALYSIS"
        user_text = user_text.replace("[REPORT_ANALYSIS]", "").strip()

    index_names = body.get("index_names")
    if isinstance(index_names, list):
        index_names = [str(x).strip() for x in index_names if str(x).strip()]
    elif isinstance(body.get("index_name"), str):
        index_names = [body.get("index_name").strip()]
    else:
        index_names = [DEFAULT_INDEX_NAME]

    index_name_for_db = ",".join(index_names)
    ui_top_k = int(body.get("top_k") or DEFAULT_TOP_K)
    retrieve_top_k = max(ui_top_k, RETRIEVE_TOP_K_DEFAULT)
    filters = body.get("filters")

    user_msg_id = repo.insert_message(session_id, user, "user", user_text)

    previous_messages = []
    try:
        all_messages = repo.get_messages(session_id, user)
        previous_messages = build_sliding_window_messages(
            all_messages=all_messages,
            current_user_msg_id=user_msg_id,
            max_messages=MAX_HISTORY_MESSAGES,
        )
    except Exception as e:
        print(f"Failed to load previous messages: {e}")

    rewritten_query = user_text
    try:
        rewritten_query = rewrite_query_with_history(
            user_id=user,
            user_question=user_text,
            previous_messages=previous_messages,
        )
    except Exception as e:
        print(f"Query rewrite failed: {e}")

    query_norm = {
        "original_query": rewritten_query,
        "normalized_query": rewritten_query,
        "expanded_query": rewritten_query,
        "detected_terms": [],
        "expansion_terms": {},
    }
    try:
        scope_candidates = ["all", "inline_fa_report"]
        query_norm = normalize_and_expand_query(
            query_text=rewritten_query,
            scope_candidates=scope_candidates,
        )
    except Exception as e:
        print(f"Query normalization failed: {e}")

    retrieval_query = (query_norm.get("expanded_query") or rewritten_query).strip() or rewritten_query

    try:
        agent_result = run_agent_loop(
            user_id=user,
            user_query=retrieval_query,
            previous_messages=previous_messages,
            excluded_indexes=EXCLUDED_TOPDOC_INDEXES,
            ui_top_k=ui_top_k,
            forced_intent=forced_intent,
            index_names=index_names
        )
        final_answer = agent_result.get("final_answer", "응답을 생성하지 못했습니다.")
        citations_json = agent_result.get("citations", {"answer": [], "final": final_answer, "claims": []})
        top_docs_ui = agent_result.get("top_docs", [])
        related_docs = agent_result.get("related_docs", [])

        intent = agent_result.get("intent")
        suggested_actions = agent_result.get("suggested_actions", [])
        
        # 💡 [신규 추가] agent_steps 배열의 맨 앞에 로그 강제 주입
        agent_steps = agent_result.get("steps", [])
        if unique_terms:
            term_keys = ", ".join(unique_terms.keys())
            agent_steps.insert(0, f"📚 사내 용어 사전 지식 적용 완료 ({term_keys})")

    except Exception as e:
        print(f"Agent error: {e}")
        final_answer = "에이전트 처리 중 오류가 발생했습니다."
        citations_json = {"answer": [], "final": final_answer, "claims": []}
        top_docs_ui = []
       
        intent = "GENERAL_CHAT"
        suggested_actions = []
        related_docs = []
        agent_steps = [f"❌ 시스템 에러 발생: {str(e)}"]

    assistant_msg_id = repo.insert_message(session_id, user, "assistant", final_answer)

    rag_resp_for_store = {
        "agent_used": True,
        "original_query": user_text,
        "rewritten_query": rewritten_query,
        "normalized_query": query_norm.get("normalized_query"),
        "expanded_query": query_norm.get("expanded_query"),
        "detected_terms": query_norm.get("detected_terms") or [],
        "expansion_terms": query_norm.get("expansion_terms") or {},
        "top_docs": top_docs_ui,
        "related_docs": related_docs,
        "intent" : intent,
        "suggested_actions" : suggested_actions,
        "agent_steps" : agent_steps
    }
    repo.insert_turn_artifact(
        session_id=session_id,
        user_id=user,
        user_msg_id=user_msg_id,
        assistant_msg_id=assistant_msg_id,
        index_name=index_name_for_db,
        rag_response=rag_resp_for_store,
        citations=citations_json
    )

    try:
        repo.insert_search_log(
            session_id=session_id,
            user_id=user,
            user_msg_id=user_msg_id,
            assistant_msg_id=assistant_msg_id,
            index_name=index_name_for_db,
            original_query=user_text,
            rewritten_query=rewritten_query,
            normalized_query=query_norm.get("normalized_query"),
            expanded_query=query_norm.get("expanded_query"),
            detected_terms=query_norm.get("detected_terms") or [],
            expansion_terms=query_norm.get("expansion_terms") or {},
            filters=filters,
            top_docs=top_docs_ui,
            retrieve_top_k=retrieve_top_k,
        )
    except Exception as e:
        print(f"search log insert failed: {e}")

    try:
        if user_text:
            repo.touch_session(session_id, user, title=user_text[:60])
    except Exception:
        repo.touch_session(session_id, user)

    return {
        "session_id": session_id,
        "assistant_text": final_answer,
        "assistant_msg_id": assistant_msg_id,
        "citations": citations_json,
        "top_docs": top_docs_ui,
        "rewritten_query": rewritten_query,
        "normalized_query": query_norm.get("normalized_query"),
        "expanded_query": query_norm.get("expanded_query"),
        "detected_terms": query_norm.get("detected_terms") or [],
        "expansion_terms": query_norm.get("expansion_terms") or {},
        "intent": intent,
        "suggested_actions": suggested_actions,
        "agent_steps": agent_steps,
    }

# =========================
# API: Digital Archive (Local Exact Match) - 💡 신규 추가
# =========================
# 로더 본체는 app/archive_loader.py로 이동 (kg_builder와 공유)
from app.archive_loader import (
    get_local_archive_docs,
    _parse_date_to_timestamp,
    CACHE_CHECK_INTERVAL,
)

# 로그인 후 표시할 공지사항 파일 (프로젝트 루트, 관리자가 직접 편집)
ANNOUNCEMENTS_PATH = Path(__file__).resolve().parent.parent / "announcements.json"



@app.get("/api/archive/documents")
async def api_get_archive_documents(
    request: Request,
    q: str = Query("", description="검색 키워드"),
    author: str = Query("", description="담당자 다중 필터 (콤마로 구분)"), # 💡 all 대신 빈 문자열 기본값
    start_date: str = Query("", description="시작일"),
    end_date: str = Query("", description="종료일"),
    skip: int = Query(0),
    limit: int = Query(20),
    sort: str = Query("desc")
):
    user = _require_user(request)
    all_docs = get_local_archive_docs()
    
    filtered = []
    q_lower = q.lower().strip()
    
    # 💡 콤마로 구분된 담당자 문자열을 리스트로 변환
    author_list = [a.strip() for a in author.split(",")] if author else []
    
    start_ts = _parse_date_to_timestamp(start_date) if start_date else 0
    end_ts = _parse_date_to_timestamp(end_date) + 86399 if end_date else float('inf')
    
    for doc in all_docs:
        if q_lower and (q_lower not in doc["title"].lower() and q_lower not in doc["raw_content"].lower()):
            continue
            
        # 💡 담당자 다중 필터 로직 적용
        if author_list and doc.get("mail_from") not in author_list:
            continue
            
        doc_ts = _parse_date_to_timestamp(doc.get("mail_date", ""))
        if not (start_ts <= doc_ts <= end_ts):
            continue
            
        filtered.append(doc)
                
    filtered.sort(key=lambda x: _parse_date_to_timestamp(x.get("mail_date", "")), reverse=(sort == "desc"))
    paginated = filtered[skip : skip + limit]
    
    res_docs = [dict(d, raw_content=None) for d in paginated]
    return {
        "total_fetched": len(filtered),
        "documents": res_docs,
        "has_more": len(filtered) > (skip + limit)
    }
    
# 담당자 목록 추출 API 추가
@app.get("/api/archive/filters")
async def api_get_archive_filters(request: Request):
    user = _require_user(request)
    all_docs = get_local_archive_docs()
    
    # 중복 제거된 담당자(mail_from) 목록 추출 (가나다순 정렬)
    authors = sorted(list(set(doc.get("mail_from", "") for doc in all_docs if doc.get("mail_from"))))
    return {"authors": authors}

# =========================
# Viewers: md / eml / asset
# =========================
def _safe_join(root: Path, rel_path: str) -> Path:
    rp = (rel_path or "").lstrip("/").replace("\\", "/")
    p = (root / rp).resolve()
    root_r = root.resolve()
    if not str(p).startswith(str(root_r)):
        raise HTTPException(status_code=400, detail="Invalid path")
    return p


@app.get("/api/view/md")
async def view_md(request: Request, rel: str):
    _ = _require_user(request)
    p = _safe_join(PARSE_ROOT, rel)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return PlainTextResponse(p.read_text(encoding="utf-8", errors="ignore"), media_type="text/plain; charset=utf-8")


@app.get("/api/view/asset")
async def view_asset(request: Request, rel: str):
    _ = _require_user(request)
    p = _safe_join(MAIL_ROOT, rel)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    data = p.read_bytes()
    ext = p.suffix.lower()
    if ext in (".png",):
        mt = "image/png"
    elif ext in (".jpg", ".jpeg"):
        mt = "image/jpeg"
    elif ext in (".webp",):
        mt = "image/webp"
    else:
        mt = "application/octet-stream"
    return Response(content=data, media_type=mt)


@app.get("/api/search-log/by-user-msg/{user_msg_id}")
async def api_search_log_by_user_msg(user_msg_id: str, request: Request, session_id: str = Query(...)):
    user = _require_user(request)
    log = repo.get_search_log_by_user_msg(session_id=session_id, user_id=user, user_msg_id=user_msg_id)
    return {"search_log": log}


@app.post("/api/sessions/{session_id}/archive")
async def api_archive_session(session_id: str, request: Request):
    user = _require_user(request)
    repo.archive_session(session_id, user)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/pin")
async def api_pin_session(session_id: str, request: Request):
    user = _require_user(request)
    body = await request.json()
    pinned = bool(body.get("pinned"))
    ok = repo.set_session_pin(session_id, user, pinned)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True, "pinned": pinned}


@app.post("/api/sessions/{session_id}/folder")
async def api_folder_session(session_id: str, request: Request):
    user = _require_user(request)
    body = await request.json()
    folder = body.get("folder")
    ok = repo.set_session_folder(session_id, user, folder)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True, "folder": folder}


@app.patch("/api/sessions/{session_id}")
async def api_patch_session(session_id: str, request: Request):
    user = _require_user(request)
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    ok = repo.update_session_title(session_id, user, title)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True, "title": title[:255]}


@app.post("/api/feedback")
async def api_post_feedback(request: Request):
    user = _require_user(request)
    body = await request.json()
    assistant_msg_id = (body.get("assistant_msg_id") or "").strip()
    rating = (body.get("rating") or "").strip()
    comment = body.get("comment")
    session_id = body.get("session_id")
    if not assistant_msg_id or rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="assistant_msg_id and rating(up|down) required")
    result = repo.upsert_feedback(
        assistant_msg_id=assistant_msg_id,
        user_id=user,
        rating=rating,
        comment=comment,
        session_id=session_id,
    )
    return {"ok": True, **result}


@app.get("/api/announcements/active")
async def api_active_announcements(request: Request):
    """로그인 후 표시할 활성 공지 목록.
    announcements.json(프로젝트 루트)을 관리자가 편집한다.
    - 전역 enabled=false 이면 전체 숨김.
    - 각 item은 enabled=true 이고 오늘이 [start_date, end_date] 범위여야 노출.
    - important=true 이면 프론트에서 '일주일간 보지 않기'를 무시하고 강제 표시.
    """
    _require_user(request)
    try:
        if not ANNOUNCEMENTS_PATH.exists():
            return {"items": []}
        with open(ANNOUNCEMENTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[Announcements] 읽기 실패: {e}")
        return {"items": []}

    if not data.get("enabled", True):
        return {"items": []}

    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for it in (data.get("items") or []):
        if not it.get("enabled", True):
            continue
        start = (it.get("start_date") or "").strip()
        end = (it.get("end_date") or "").strip()
        if start and today < start:
            continue
        if end and today > end:
            continue
        out.append({
            "id": str(it.get("id") or ""),
            "title": it.get("title") or "",
            "body": it.get("body") or "",
            "important": bool(it.get("important", False)),
            "start_date": start,
            "end_date": end,
        })
    return {"items": out}


# =========================
# API: 성능 평가 대시보드 (Cognitive Trace)
# =========================
@app.get("/api/eval/summary")
async def api_eval_summary(request: Request, days: int = Query(30)):
    _require_user(request)
    from app.eval_repo import get_eval_summary
    return get_eval_summary(days=min(max(days, 1), 180))


@app.get("/api/kg/stats")
async def api_kg_stats(request: Request):
    _require_user(request)
    from app.eval_repo import get_kg_stats
    return get_kg_stats()


@app.get("/api/eval/goldenset")
async def api_eval_goldenset(request: Request):
    _require_user(request)
    from app.eval_repo import get_goldenset_latest
    return get_goldenset_latest()


@app.get("/api/eval/goldenset/runs")
async def api_eval_goldenset_runs(request: Request, limit: int = Query(50)):
    _require_user(request)
    from app.eval_repo import list_goldenset_runs
    return list_goldenset_runs(limit=limit)


@app.get("/api/eval/goldenset/runs/{run_id}")
async def api_eval_goldenset_run(run_id: str, request: Request):
    _require_user(request)
    from app.eval_repo import get_goldenset_run
    data = get_goldenset_run(run_id)
    if not data:
        raise HTTPException(status_code=404, detail="run not found")
    return data


@app.delete("/api/eval/goldenset/runs/{run_id}")
async def api_delete_goldenset_run(run_id: str, request: Request):
    user = _require_user(request)
    if user not in ADMIN_USER_IDS:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    from app.eval_repo import delete_goldenset_run
    ok = delete_goldenset_run(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail="run not found")
    return {"ok": True}


@app.get("/api/eval/feedback-cases")
async def api_eval_feedback_cases(request: Request, limit: int = Query(50)):
    """👎 피드백 실패 사례 목록 (타 유저 질문이 노출되므로 관리자 전용)."""
    user = _require_user(request)
    if user not in ADMIN_USER_IDS:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    from app.eval_repo import get_feedback_cases
    return {"cases": get_feedback_cases(limit=limit)}


@app.post("/api/eval/goldenset/candidates")
async def api_add_goldenset_candidate(request: Request):
    """실패 사례를 골든셋 후보 문항으로 승격 (관리자 전용).

    enabled=false로 추가되므로 평가 실행(CLI 전용)에는 곧바로 포함되지 않는다.
    관리자가 goldenset.json에서 정답 doc_ids를 채운 뒤 enabled=true로 활성화하는 흐름.
    """
    import hashlib
    user = _require_user(request)
    if user not in ADMIN_USER_IDS:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    body = await request.json()
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question required")

    from app.goldenset_runner import GOLDENSET_PATH
    data = {"items": []}
    try:
        if GOLDENSET_PATH.exists():
            data = json.load(open(GOLDENSET_PATH, encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"goldenset.json 읽기 실패: {e}")
    items = data.setdefault("items", [])

    if any((it.get("question") or "").strip() == question for it in items):
        return {"ok": False, "duplicated": True, "message": "이미 같은 질문의 문항이 있습니다."}

    note = (body.get("note") or "").strip()
    item = {
        "id": f"fb-{hashlib.sha1(question.encode('utf-8')).hexdigest()[:8]}",
        "question": question,
        "expected_intent": (body.get("expected_intent") or "").strip(),
        "expected_doc_ids": [],
        "expected_terms": [],
        "enabled": False,
        "notes": ("👎 피드백에서 승격" + (f": {note}" if note else "")
                  + " — 정답 doc_ids를 채우고 enabled=true로 바꾸면 평가에 포함됩니다."),
    }
    items.append(item)
    try:
        with open(GOLDENSET_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"goldenset.json 저장 실패: {e}")
    return {"ok": True, "item": item}


# =========================
# API: Knowledge Graph 연관 조회
# =========================
@app.get("/api/kg/related")
async def api_kg_related(request: Request, report_index: str = Query(""), doc_id: str = Query("")):
    """report_index 또는 doc_id 기준으로 연결된 문서/보고서를 반환."""
    _require_user(request)
    if not report_index and not doc_id:
        raise HTTPException(status_code=400, detail="report_index or doc_id required")
    from app.kg_repo import get_related
    return get_related(report_index=report_index.strip(), doc_id=doc_id.strip())


@app.get("/api/kg/term/{term_id}")
async def api_kg_term(term_id: int, request: Request):
    """용어 기준 연관 문서/보고서 수 + 상위 문서 + 동시출현 용어."""
    _require_user(request)
    from app.kg_repo import get_term_overview
    return get_term_overview(term_id)


@app.get("/api/kg/links")
async def api_kg_links(request: Request, source: str = Query(""), q: str = Query(""), limit: int = Query(50)):
    """문서↔보고서 연결 상세 (매칭 근거 evidence 포함) — 대시보드 드릴다운용."""
    _require_user(request)
    from app.kg_repo import get_link_samples
    return {"links": get_link_samples(source=source.strip(), q=q.strip(), limit=limit)}


@app.get("/api/sessions/{session_id}/latest-artifact")
async def api_latest_artifact(session_id: str, request: Request):
    user = _require_user(request)
    art = repo.get_latest_artifact(session_id, user)
    return {"artifact": art}


@app.get("/api/artifacts/by-assistant/{assistant_msg_id}")
async def api_artifact_by_assistant(assistant_msg_id: str, request: Request, session_id: str = Query(...)):
    user = _require_user(request)
    art = repo.get_artifact_by_assistant_msg(session_id=session_id, user_id=user, assistant_msg_id=assistant_msg_id)
    return {"artifact": art}

@app.post("/api/dictionary/propose")
async def api_propose_term(request: Request):
    # 기존에 구현해두신 인증 로직 활용 (JWT 토큰 등에서 user id 추출)
    user_id = _require_user(request) 
    
    body = await request.json()
    
    candidate_kind = body.get("kind", "new_term")
    candidate_type = body.get("type", "defect")
    raw_text = body.get("raw_text")
    canonical_name = body.get("canonical")
    target_term_id = body.get("target_id")
    aliases = body.get("aliases", [])
    
    if not raw_text:
        raise HTTPException(status_code=400, detail="raw_text is required")
        
    success = propose_term_candidate(
        user_id=user_id,
        candidate_kind=candidate_kind,
        candidate_type=candidate_type,
        raw_text=raw_text,
        canonical_name=canonical_name,
        target_term_id=target_term_id,
        aliases=aliases
    )
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to submit proposal to DB")
        
    return {"status": "success", "message": "Term proposed successfully. Pending approval."}

# API: Term Dictionary 목록 조회
@app.get("/api/dictionary/terms")
async def api_get_dictionary_terms(request: Request):
    user = _require_user(request)

    terms = get_all_terms()
    return {"terms": terms}

@app.get("/api/dictionary/pending")
async def api_get_pending_candidates(
    request: Request,
    limit: int = Query(50),
    offset: int = Query(0),
    sort: str = Query("frequency"),
    search: str = Query(""),      # 💡 검색어
    type: str = Query("all"),     # 💡 카테고리
    source: str = Query("all")    # 💡 출처(user/system)
):
    """대기열(Pending Queue) 로딩 (페이지네이션 및 정렬 정책 적용)"""
    user = _require_user(request)
    
    if user not in ADMIN_USER_IDS:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
        
    from app.dictionary_repo import get_pending_candidates
    result_dict = get_pending_candidates(
        limit=limit, offset=offset, sort=sort, search=search, term_type=type, source=source
    )
    return result_dict

@app.post("/api/dictionary/approve")
async def api_approve_term(request: Request):
    user = _require_user(request)
    if user not in ADMIN_USER_IDS:
        raise HTTPException(status_code=403)
        
    body = await request.json()
    success = approve_candidate(body, user)
    
    if not success:
        raise HTTPException(status_code=500, detail="승인 처리 중 DB 에러가 발생했습니다.")
    return {"status": "success"}

# =========================
# API: Admin Dictionary (Pending, Approve, Update, Delete)
# =========================
# @app.get("/api/dictionary/pending")
# async def api_get_pending_candidates(
#     request: Request,
#     limit: int = Query(100),
#     offset: int = Query(0)
# ):
#     """대기열(Pending Queue) 로딩 (페이지네이션 적용됨)"""
#     user = _require_user(request)
    
#     # 관리자만 대기열을 볼 수 있도록 권한 체크
#     if user not in ADMIN_USER_IDS:
#         raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
        
#     # dictionary_repo에서 {"total": int, "items": list} 형태로 반환
#     result_dict = get_pending_candidates(limit=limit, offset=offset)
#     return result_dict



@app.delete("/api/dictionary/terms/{term_id}")
async def api_delete_term(term_id: int, request: Request):
    """정식 용어 사전 Soft Delete (비활성화)"""
    user = _require_user(request)
    
    # 💡 1. 관리자 권한(Admin) 체크 로직 추가
    if user not in ADMIN_USER_IDS:
        raise HTTPException(status_code=403, detail="용어 삭제는 관리자 권한이 필요합니다.")
    
    # 2. 삭제(Soft Delete) 로직 실행 (앞서 dictionary_repo.py에 추가한 함수 호출)
    from app.dictionary_repo import soft_delete_term
    success = soft_delete_term(term_id)
    if not success:
        raise HTTPException(status_code=500, detail="용어 삭제에 실패했습니다.")
        
    return {"status": "success", "message": "용어가 삭제(비활성화) 되었습니다. 5분 내에 검색 엔진에 반영됩니다."}


@app.put("/api/dictionary/terms/{term_id}")
async def api_update_term(term_id: int, request: Request):
    """정식 용어 사전 정보 수정"""
    user = _require_user(request)
    
    # 💡 1. 관리자 권한(Admin) 체크 로직 추가
    if user not in ADMIN_USER_IDS:
        raise HTTPException(status_code=403, detail="용어 수정은 관리자 권한이 필요합니다.")
    
    payload = await request.json()
    
    # 2. 수정 로직 실행 (앞서 dictionary_repo.py에 추가한 함수 호출)
    from app.dictionary_repo import update_term_details
    success = update_term_details(term_id, payload)
    if not success:
        raise HTTPException(status_code=500, detail="용어 수정에 실패했습니다.")
        
    return {"status": "success", "message": "용어가 수정되었습니다. 5분 내에 검색 엔진에 반영됩니다."}