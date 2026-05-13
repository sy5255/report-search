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
from app.dictionary_repo import propose_term_candidate, get_all_terms

app = FastAPI(title="RAG Search Web", version=VERSION)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

MAX_HISTORY_MESSAGES = 6  # 최근 6개 메시지 = 대략 최근 3턴
RETRIEVE_TOP_K_DEFAULT = 12
EXCLUDED_TOPDOC_INDEXES = {"rp-term-ver1"}


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
    return templates.TemplateResponse("archive.html", {
        "request": request, 
        "active_tab": "archive", # 프론트엔드에서 밑줄을 그리기 위한 변수
        "user_id": user
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
    # Phase 5에서 구현할 인지 추적 페이지
    return templates.TemplateResponse("base.html", {
        "request": request,
        "user_id": user})


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

    for m in msgs:
        if m["role"] == "assistant":
            ast_id = m.get("msg_id")
            rag_info = artifacts_by_ast.get(ast_id, {})
            m["intent"] = rag_info.get("intent")
            m["suggested_actions"] = rag_info.get("suggested_actions", [])
            m["agent_steps"] = rag_info.get("agent_steps", [])

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
        # 첫 번째 터미널 메시지 스트리밍
        yield json.dumps({"type": "step", "message": "🔄 질의어 문맥 분석 및 정규화 진행 중..."}, ensure_ascii=False) + "\n"

        # 3) rewrite
        rewritten_query = user_text
        try:
            rewritten_query = rewrite_query_with_history(
                user_id=user,
                user_question=user_text,
                previous_messages=previous_messages,
            )
        except Exception as e:
            print(f"Query rewrite failed: {e}")

        # 4) normalization + expansion
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

        # 5) Agent Loop 실행 및 실시간 데이터 중계
        final_data = None
        for chunk in run_agent_loop_stream(
            user_id=user,
            user_query=retrieval_query,
            previous_messages=previous_messages,
            excluded_indexes=EXCLUDED_TOPDOC_INDEXES,
            ui_top_k=ui_top_k,
            forced_intent=forced_intent
        ):
            # 에이전트 단계별 로그 텍스트를 바로 프론트엔드로 쏩니다
            yield chunk
            
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
                "intent" : final_data.get("intent"),
                "suggested_actions" : final_data.get("suggested_actions", []),
                "agent_steps" : final_data.get("steps", [])
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
                "agent_steps": final_data.get("steps", [])
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
            forced_intent=forced_intent 
        )
        final_answer = agent_result.get("final_answer", "응답을 생성하지 못했습니다.")
        citations_json = agent_result.get("citations", {"answer": [], "final": final_answer, "claims": []})
        top_docs_ui = agent_result.get("top_docs", [])
       
        intent = agent_result.get("intent")
        suggested_actions = agent_result.get("suggested_actions", [])
        agent_steps = agent_result.get("steps", [])

    except Exception as e:
        print(f"Agent error: {e}")
        final_answer = "에이전트 처리 중 오류가 발생했습니다."
        citations_json = {"answer": [], "final": final_answer, "claims": []}
        top_docs_ui = []
       
        intent = "GENERAL_CHAT"
        suggested_actions = []
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
# 속도 최적화를 위해 파일을 한 번만 읽어 메모리에 저장하는 전역 캐시
_ARCHIVE_CACHE = []
# _CACHE_LOADED = False
_LAST_PROCESSED_MTIME = 0.0  # processed.json의 마지막 수정 시간 저장
_LAST_CHECK_TIME = 0.0 
# 24시간(86,400초) 간격으로 체크하도록 수정
CACHE_CHECK_INTERVAL = 86400 

# processed.json 파일 경로 설정
PROCESSED_JSON_PATH = PARSE_ROOT / "_state" / "processed.json"

# 허용된 작성자 화이트리스트 (이름만 작성)
ALLOWED_AUTHORS = ["성지아 <j.na@s.com>", "김지수 <s.go@s.com>", "고미연 <y.ko@s.com>", "김영인 <i.kim@s.com>", "진연수 <s.jin@s.com>", "유미래 <g.y@s.com>", "신현빈 <s.shin@s.com>", "서세린 <s.se@s.com>", "오슬미 <s.y@s.com>", "이자린 <k.lee@s.com>", "김장미 <m.kim@s.com>", "김소희 <j.kim@s.com>", "이나연 <h.oh@s.com>", "윤희서 <k.y@s.com>", "미인지 <s.mg@s.com>"]

# 1. 날짜 문자열을 진짜 시간(Timestamp) 숫자로 변환하는 강력한 함수
def _parse_date_to_timestamp(date_str):
    if not date_str:
        return 0.0 # 날짜가 아예 없으면 맨 뒤로 보냄
    
    # 1) 이메일 표준 형식 시도 (예: Fri, 01 Aug 2025 12:34:56 +0900)
    try:
        dt = parsedate_to_datetime(date_str)
        if dt is not None:
            return dt.timestamp()
    except Exception:
        pass
        
    # 2) 정규식을 이용해 강제로 연/월/일 추출 (예: 2026-04-10, 2026. 4. 10, 2026년 4월 등)
    match = re.search(r'(\d{4})[-./년\s]+(\d{1,2})[-./월\s]+(\d{1,2})', date_str)
    if match:
        try:
            y, m, d = map(int, match.groups())
            return datetime(y, m, d).timestamp()
        except Exception:
            pass
            
    return 0.0 # 파싱에 완전히 실패하면 맨 뒤로

# 💡 2. 로컬 문서 검색 로직
def get_local_archive_docs():
    global _ARCHIVE_CACHE, _LAST_PROCESSED_MTIME, _LAST_CHECK_TIME
    
    now = time.time()

    # 마지막 체크 후 24시간이 지나지 않았고 캐시가 있다면 즉시 반환
    if now - _LAST_CHECK_TIME < CACHE_CHECK_INTERVAL and _ARCHIVE_CACHE:
        # 💡 이 조건문 덕분에 서버는 24시간 동안 파일 시스템을 건드리지 않고 
        # 메모리(RAM)에 있는 데이터를 0.0001초 만에 반환합니다.
        return _ARCHIVE_CACHE
    
    if not PROCESSED_JSON_PATH.exists():
        print(f"[Archive] {PROCESSED_JSON_PATH} 파일을 찾을 수 없습니다.")
        return []

    _LAST_CHECK_TIME = now
    current_mtime = os.path.getmtime(PROCESSED_JSON_PATH)

    if _LAST_PROCESSED_MTIME == current_mtime and _ARCHIVE_CACHE:
        return _ARCHIVE_CACHE

    print(f"[Archive] 24시간 경과: 주기적 캐시 갱신 시작... (mtime: {current_mtime})")
    
    try:
        with open(PROCESSED_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        items = data.get("items", {})
        print(f"[Archive Debug] JSON에서 읽어온 전체 아이템 수: {len(items)}")
        
        category_max_versions = {}
        for rel_path in items.keys():
            parts = rel_path.split('/')
            if len(parts) >= 2:
                category = parts[0]
                version_str = parts[1]
                match = re.search(r'ver(\d+)', version_str)
                if match:
                    v_num = int(match.group(1))
                    if v_num > category_max_versions.get(category, -1):
                        category_max_versions[category] = v_num

        print(f"[Archive Debug] 탐지된 카테고리별 최신 버전: {category_max_versions}")

        docs = []
        
        # 💡 스킵 사유를 기록할 카운터
        skip_reasons = {
            "status_not_done": 0,
            "not_latest_version": 0,
            "md_file_not_found": 0,
            "no_mail_meta_tag": 0,
            "parse_error": 0
        }
        
        first_missing_path = None # 에러가 난 첫 번째 경로를 기억하기 위함

        for rel_path, info in items.items():
            if info.get("status") != "DONE":
                skip_reasons["status_not_done"] += 1
                continue
            
            parts = rel_path.split('/')
            category = parts[0]
            version_str = parts[1]
            match = re.search(r'ver(\d+)', version_str)
            
            if not match or int(match.group(1)) != category_max_versions.get(category):
                skip_reasons["not_latest_version"] += 1
                continue

            # 경로 계산
            safe_rel_dir = Path(rel_path).parent
            safe_out_dir = PARSE_ROOT / safe_rel_dir
            md_files = list(safe_out_dir.rglob("*.md"))
            
            if not md_files:
                skip_reasons["md_file_not_found"] += 1
                if not first_missing_path:
                    first_missing_path = safe_out_dir # 경로가 어떻게 꼬였는지 터미널에 출력하기 위해 저장
                continue
            
            filepath = md_files[0]
            
            try:
                content = filepath.read_text(encoding="utf-8", errors="ignore")
                
                # 메타데이터가 없는 파일 거르기
                if "[MAIL_META]" not in content:
                    skip_reasons["no_mail_meta_tag"] += 1
                    continue
                
                # 💡 여기서 변수들을 모두 '빈 바구니'로 초기화해야 합니다! (이 부분이 지워져서 났던 에러입니다)
                title = filepath.stem
                mail_from = ""
                mail_date = ""
                report_links = [] 
                
                # 1. 작성자(From) 파싱
                from_match = re.search(r'From\s*:\s*(.*?)(?=\s*(?:Date|To|Cc|Bcc|Subject|\[)|\n|$)', content, re.IGNORECASE)
                if from_match: 
                    # 꺾쇠 유지, 따옴표만 제거
                    mail_from = from_match.group(1).strip().replace('"', '').replace("'", "")
                
                # 💡 2. 화이트리스트 검사 (허용된 사람 아니면 여기서 바로 스킵!)
                if mail_from not in ALLOWED_AUTHORS:
                    skip_reasons["not_allowed_author"] = skip_reasons.get("not_allowed_author", 0) + 1
                    continue
                
                # 3. 날짜, 제목, EDM 링크 파싱
                date_match = re.search(r'Date\s*:\s*(.*?)(?=\s*(?:From|To|Cc|Bcc|Subject|\[)|\n|$)', content, re.IGNORECASE)
                if date_match: mail_date = date_match.group(1).strip()
                
                subject_match = re.search(r'Subject\s*:\s*(.*?)(?=\s*(?:From|Date|To|Cc|Bcc|\[)|\n|$)', content, re.IGNORECASE)
                if subject_match: title = subject_match.group(1).strip()
                
                edm_match = re.search(r'EDM\s*링크\s*:\s*(http[^\s\n]+)', content, re.IGNORECASE)
                if edm_match: report_links.append(edm_match.group(1).strip())
                
                # 4. 이미지 에셋 탐색 로직
                rel_dir = filepath.parent.relative_to(PARSE_ROOT)
                target_parts = []
                for part in rel_dir.parts:
                    if part.startswith("export_"): break
                    target_parts.append(part)
                
                attachments_dir = MAIL_ROOT.joinpath(*target_parts) / "attachments"
                assets = []
                if attachments_dir.exists() and attachments_dir.is_dir():
                    for ext in ["*.png", "*.jpg", "*.jpeg", "*.webp", "*.PNG", "*.JPG"]:
                        for img_path in attachments_dir.glob(ext):
                            assets.append({
                                "path": str(img_path.relative_to(MAIL_ROOT)).replace("\\", "/"),
                                "file_name": img_path.name
                            })

                # 5. 모든 데이터를 묶어서 카드 1개 완성!
                docs.append({
                    "doc_id": filepath.name,
                    "title": title,
                    "mail_from": mail_from,
                    "mail_date": mail_date,
                    "report_links": report_links,
                    "storage": {"parsed_md_rel_path": str(filepath.relative_to(PARSE_ROOT)).replace("\\", "/")},
                    "assets": assets,
                    "raw_content": content,
                    "version_tag": version_str.upper()
                })
                
            except Exception as e:
                skip_reasons["parse_error"] += 1
                if skip_reasons["parse_error"] == 1:
                    print(f"\n🚨 [디버그] 치명적 에러 원인 발견: {type(e).__name__} - {e}\n")

        docs.sort(key=lambda x: _parse_date_to_timestamp(x["mail_date"]), reverse=True)
        
        _ARCHIVE_CACHE = docs
        _LAST_PROCESSED_MTIME = current_mtime
        
        # 💡 리포트 최종 출력
        print("-" * 50)
        print(f"[Archive Report] 필터링 및 로딩 결과")
        print(f"  - 성공적으로 로드된 문서: {len(docs)}개")
        print(f"  - [Skip] 상태가 DONE이 아님: {skip_reasons['status_not_done']}개")
        print(f"  - [Skip] 구버전 폴더(최신 아님): {skip_reasons['not_latest_version']}개")
        print(f"  - [Skip] MD 파일 경로 못 찾음: {skip_reasons['md_file_not_found']}개")
        print(f"  - [Skip] 문서 내 [MAIL_META] 없음: {skip_reasons['no_mail_meta_tag']}개")
        print(f"  - [Skip] 읽기 에러 등: {skip_reasons['parse_error']}개")
        if first_missing_path:
            print(f"\n⚠️ 주의: MD 파일을 찾지 못한 첫 번째 경로를 확인해보세요!")
            print(f"서버가 찾으려 한 경로: {first_missing_path}")
        print("-" * 50)
        
    except Exception as e:
        print(f"[Archive] processed.json 읽기 실패: {e}")
        
    return _ARCHIVE_CACHE



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