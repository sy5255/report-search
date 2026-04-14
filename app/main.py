import json
from pathlib import Path
from fastapi import FastAPI, Request, Response, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
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
from app.agent import run_agent_loop
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

    # 과거 assistant 메시지에 intent와 액션 버튼 정보를 채워넣음
    for m in msgs:
        if m["role"] == "assistant":
            ast_id = m.get("msg_id")
            rag_info = artifacts_by_ast.get(ast_id, {})
            m["intent"] = rag_info.get("intent")
            m["suggested_actions"] = rag_info.get("suggested_actions", [])
            m["agent_steps"] = rag_info.get("agent_steps", [])

    return {"messages": msgs, "search_logs_by_user_msg_id": search_log_by_user_msg_id}

# =========================
# API: chat (Agent Loop 적용)
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

    # 💡 [핵심 복구 1] 프론트엔드에서 날아온 [태그]를 LLM이 건드리기 전에 가로채서 분리합니다!
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
        previous_messages = []

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

    # 5) Agent Loop 실행 및 변수 추출
    try:
        agent_result = run_agent_loop(
            user_id=user,
            user_query=retrieval_query,
            previous_messages=previous_messages,
            excluded_indexes=EXCLUDED_TOPDOC_INDEXES,
            ui_top_k=ui_top_k,
            forced_intent=forced_intent # 💡 [핵심 복구 2] 가로챈 태그를 에이전트에 다이렉트로 주입!
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

    # 6) 데이터베이스 저장 및 응답 반환
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