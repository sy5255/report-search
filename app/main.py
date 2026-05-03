import os 
import re  
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
        "active_tab": "archive" # 프론트엔드에서 밑줄을 그리기 위한 변수
    })

@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    user = _require_user(request)
    # Phase 5에서 구현할 시각화 페이지
    return templates.TemplateResponse("base.html", {"request": request})

@app.get("/trace", response_class=HTMLResponse)
async def trace_page(request: Request):
    user = _require_user(request)
    # Phase 5에서 구현할 인지 추적 페이지
    return templates.TemplateResponse("base.html", {"request": request})


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
_CACHE_LOADED = False

# 💡 1. 날짜 문자열을 진짜 시간(Timestamp) 숫자로 변환하는 강력한 함수
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
    global _ARCHIVE_CACHE, _CACHE_LOADED
    if _CACHE_LOADED:
        return _ARCHIVE_CACHE
        
    docs = []
    
    for filepath in PARSE_ROOT.rglob("*.md"):
        try:
            content = filepath.read_text(encoding="utf-8", errors="ignore")
            
            # 중간 산출물 무시 로직
            if "[MAIL_META]" not in content:
                continue
            
            title = filepath.stem
            mail_from = ""
            mail_date = ""
            report_links = []
            
            # 💡 3. 메타데이터 파싱 로직 강화 (정규식 기반: 한 줄 / 여러 줄 완벽 방어)
            # From: 뒤부터 다음 예약어(Date, Subject 등)나 대괄호([), 줄바꿈이 오기 전까지 추출
            from_match = re.search(r'From\s*:\s*(.*?)(?=\s*(?:Date|To|Cc|Bcc|Subject|\[)|\n|$)', content, re.IGNORECASE)
            if from_match:
                mail_from = from_match.group(1).strip()
                
            # Date: 뒤부터 다음 예약어나 대괄호([), 줄바꿈이 오기 전까지 추출
            date_match = re.search(r'Date\s*:\s*(.*?)(?=\s*(?:From|To|Cc|Bcc|Subject|\[)|\n|$)', content, re.IGNORECASE)
            if date_match:
                mail_date = date_match.group(1).strip()
                
            # Subject: 가 있을 경우 타이틀 덮어쓰기
            subject_match = re.search(r'Subject\s*:\s*(.*?)(?=\s*(?:From|Date|To|Cc|Bcc|\[)|\n|$)', content, re.IGNORECASE)
            if subject_match:
                title = subject_match.group(1).strip()
                
            # EDM 링크: 뒤의 http 주소 추출
            edm_match = re.search(r'EDM\s*링크\s*:\s*(http[^\s\n]+)', content, re.IGNORECASE)
            if edm_match:
                report_links.append(edm_match.group(1).strip())
                            
            # 프론트엔드 경로
            rel_md_path = str(filepath.relative_to(PARSE_ROOT)).replace("\\", "/")
            
            # 1. PARSE_ROOT 기준 상대 경로 추출
            rel_dir = filepath.parent.relative_to(PARSE_ROOT)
            # attachments_dir = MAIL_ROOT / rel_dir / "attachments"

            # print(f"debug 마크다운 파일: {filepath}")
            # print(f"debug 예상 이미지 폴더: {attachments_dir}")
            # print(f"debug 폴더 존재 여부: {attachments_dir.exists()}")

            # 2. 'export_' 로 시작하는 폴더를 찾으면 그 앞까지만 경로로 사용 (재귀 탐색 X)
            target_parts = []
            for part in rel_dir.parts:
                if part.startswith("export_"):
                    break
                target_parts.append(part)

            # 3. MAIL_ROOT와 잘라낸 경로를 결합하여 정확한 이미지 폴더 경로 생성
            attachments_dir = MAIL_ROOT.joinpath(*target_parts) / "attachments"
            
            assets = []
            if attachments_dir.exists() and attachments_dir.is_dir():
                for ext in ["*.png", "*.jpg", "*.jpeg", "*.webp", "*.PNG", "*.JPG"]:
                    for img_path in attachments_dir.glob(ext):
                        rel_img_path = str(img_path.relative_to(MAIL_ROOT)).replace("\\", "/")
                        assets.append({"path": rel_img_path, "file_name": img_path.name})
                        
            # 카드 객체 추가
            docs.append({
                "doc_id": filepath.name,
                "title": title,
                "mail_from": mail_from,
                "mail_date": mail_date,
                "report_links": report_links,
                "storage": {"parsed_md_rel_path": rel_md_path},
                "assets": assets,
                "raw_content": content 
            })
        except Exception as e:
            print(f"[Archive Error] Failed to load {filepath}: {e}")
            
    # 💡 4. 가장 중요한 정렬 로직! (알파벳 정렬이 아닌 실제 시간 기준 내림차순 정렬)
    docs.sort(key=lambda x: _parse_date_to_timestamp(x["mail_date"]), reverse=True)
    
    _ARCHIVE_CACHE = docs
    _CACHE_LOADED = True
    return _ARCHIVE_CACHE

@app.get("/api/archive/documents")
async def api_get_archive_documents(
    request: Request,
    q: str = Query("", description="검색 키워드"),
    skip: int = Query(0, description="건너뛸 문서 수"),
    limit: int = Query(20, description="가져올 문서 수"),
    sort: str = Query("desc", description="정렬 순서 (desc/asc)")
):
    user = _require_user(request)
    all_docs = get_local_archive_docs()
    
    filtered = []
    q_lower = q.lower().strip()
    
    # 1. Exact Match 키워드 검색
    for doc in all_docs:
        if not q_lower:
            filtered.append(doc) 
        else:
            if q_lower in doc["title"].lower() or q_lower in doc["raw_content"].lower():
                filtered.append(doc)
                
    # 💡 2. 여기가 문제였습니다! (알파벳 정렬 -> 타임스탬프 정렬로 완벽 교체)
    filtered.sort(
        key=lambda x: _parse_date_to_timestamp(x.get("mail_date", "")),
        reverse=(sort == "desc")
    )
    
    # 3. 페이지네이션
    paginated = filtered[skip : skip + limit]
    
    # 4. 프론트엔드 전송 (무거운 raw_content는 제외)
    res_docs = []
    for d in paginated:
        r = dict(d)
        r.pop("raw_content", None)
        res_docs.append(r)
        
    return {
        "total_fetched": len(filtered),
        "documents": res_docs,
        "has_more": len(filtered) > (skip + limit)
    }

# =========================
# Viewers: md / eml / asset
# =========================
def _safe_join(root: Path, rel_path: str) -> Path:
    rp = (rel_path or "").lstrip("/").replace("\\", "/")
    p = (root / rp).resolve()
    root_r = root.resolve()
    if not str(p).startswith(str(root_r)):
        raise HTTPE