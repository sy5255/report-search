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
from app.rag_client import rag_retrieve_rrf
from app.llm_client import llm_answer_with_citations, rewrite_query_with_history
from app.query_normalizer import normalize_and_expand_query

app = FastAPI(title="RAG Search Web", version=VERSION)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

MAX_HISTORY_MESSAGES = 6  # 최근 6개 메시지 = 대략 최근 3턴
RETRIEVE_TOP_K_DEFAULT = 12
ANSWER_EVIDENCE_DOCS = 6
EXCLUDED_TOPDOC_INDEXES = {"rp-term-ver1"}


def build_sliding_window_messages(
    all_messages: list[dict],
    current_user_msg_id: str | None = None,
    max_messages: int = MAX_HISTORY_MESSAGES
) -> list[dict]:
    """
    DB 전체 메시지 중 최근 N개만 남긴다.
    current_user_msg_id가 주어지면 해당 메시지는 제외한다.
    """
    history = []

    for msg in all_messages or []:
        if current_user_msg_id and msg.get("msg_id") == current_user_msg_id:
            continue

        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue

        history.append({
            "role": role,
            "content": msg.get("content", "")
        })

    history = history[-max_messages:]

    # assistant로 시작하면 문맥이 어색하니 제거
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


def dedupe_hits_by_doc_id_keep_best(hits: list[dict]) -> list[dict]:
    """
    같은 doc_id가 여러 번 나오면:
    - _score가 가장 높은 chunk(hit) 1개만 남김
    - 결과는 score desc로 정렬 (동점이면 rank asc)
    """
    best = {}
    for h in hits or []:
        src = h.get("_source") or {}
        doc_id = src.get("doc_id")
        if not doc_id:
            continue

        prev = best.get(doc_id)
        if prev is None:
            best[doc_id] = h
            continue

        if (h.get("_score") or 0) > (prev.get("_score") or 0):
            best[doc_id] = h

    out = list(best.values())
    out.sort(key=lambda x: (-(x.get("_score") or 0), (x.get("_rank") or 10**9)))
    return out


def filter_hits_by_excluded_indexes(
    hits: list[dict],
    excluded_indexes: set[str] | None = None
) -> list[dict]:
    excluded = excluded_indexes or set()
    out = []

    for h in hits or []:
        idx = (h.get("_index") or "").strip()
        if idx in excluded:
            continue
        out.append(h)

    return out


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

    search_log_by_user_msg_id = {}
    for log in search_logs or []:
        user_msg_id = log.get("user_msg_id")
        if not user_msg_id:
            continue
        search_log_by_user_msg_id[user_msg_id] = log

    return {
        "messages": msgs,
        "search_logs_by_user_msg_id": search_log_by_user_msg_id
    }


# =========================
# API: chat (RAG -> LLM Phase2 JSON citations)
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
        print(f"Query rewrite failed, fallback to original query: {e}")
        rewritten_query = user_text

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
        print(f"Query normalization failed, fallback to rewritten query: {e}")

    retrieval_query = (query_norm.get("expanded_query") or rewritten_query).strip() or rewritten_query

    # 5) retrieve
    rag_resp = None
    hits = []

    try:
        rag_resp = rag_retrieve_rrf(
            index_name=index_names,
            query_text=retrieval_query,
            top_k=retrieve_top_k,
            filters=filters
        )
        hits = (((rag_resp or {}).get("hits") or {}).get("hits") or [])
    except Exception:
        merged_hits = []
        for idx in index_names:
            try:
                r = rag_retrieve_rrf(
                    index_name=idx,
                    query_text=retrieval_query,
                    top_k=retrieve_top_k,
                    filters=filters
                )
                hs = (((r or {}).get("hits") or {}).get("hits") or [])
                merged_hits.extend(hs)
            except Exception:
                continue

        hits_dedup = dedupe_hits_by_doc_id_keep_best(merged_hits)
        hits = hits_dedup[:retrieve_top_k]
        rag_resp = {"hits": {"hits": hits}}

    # ✅ TopDocs / Sentence Citations / answer 근거에서 제외할 인덱스 제거
    hits_dedup = dedupe_hits_by_doc_id_keep_best(hits)
    hits_visible = filter_hits_by_excluded_indexes(
        hits_dedup,
        excluded_indexes=EXCLUDED_TOPDOC_INDEXES,
    )

    # ✅ answer / citations도 filtered hits 기준 사용
    answer_hits = hits_visible[:ANSWER_EVIDENCE_DOCS]

    rag_chunks = []
    for h in answer_hits:
        src = h.get("_source") or {}
        rag_chunks.append({
            "doc_id": src.get("doc_id"),
            "chunk_id": src.get("chunk_id") or h.get("_id"),
            "title": src.get("title"),
            "merge_title_content": src.get("merge_title_content") or "",
            "score": h.get("_score"),
            "additionalField": src.get("additionalField") or {},
            "_index": h.get("_index"),
            "_rank": h.get("_rank"),
        })

    # 6) answer
    try:
        citations_json = llm_answer_with_citations(
            user_id=user,
            user_question=user_text,
            rag_chunks=rag_chunks,
            previous_messages=previous_messages,
        )
    except Exception as e:
        print(f"LLM error: {e}")
        citations_json = {
            "answer": [{
                "sentence": "LLM 호출/파싱에 실패했습니다. (프롬프트/JSON 파싱/게이트웨이 응답을 확인해주세요.)",
                "citations": []
            }],
            "final": "LLM 호출/파싱에 실패했습니다."
        }

    final_answer = (citations_json.get("final") or "").strip()
    if not final_answer:
        parts = []
        for a in (citations_json.get("answer") or []):
            s = (a.get("sentence") or "").strip()
            if s:
                parts.append(s)
        final_answer = "\n".join(parts).strip()

    # 7) assistant message 저장
    assistant_msg_id = repo.insert_message(session_id, user, "assistant", final_answer)

    rag_resp_for_store = dict(rag_resp or {})
    rag_resp_for_store["original_query"] = user_text
    rag_resp_for_store["rewritten_query"] = rewritten_query
    rag_resp_for_store["normalized_query"] = query_norm.get("normalized_query")
    rag_resp_for_store["expanded_query"] = query_norm.get("expanded_query")
    rag_resp_for_store["detected_terms"] = query_norm.get("detected_terms") or []
    rag_resp_for_store["expansion_terms"] = query_norm.get("expansion_terms") or {}
    rag_resp_for_store["top_docs"] = [_to_ui_doc_from_hit(h) for h in hits_visible]

    # 8) artifact 저장
    repo.insert_turn_artifact(
        session_id=session_id,
        user_id=user,
        user_msg_id=user_msg_id,
        assistant_msg_id=assistant_msg_id,
        index_name=index_name_for_db,
        rag_response=rag_resp_for_store,
        citations=citations_json
    )

    # 9) search log 저장
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
            top_docs=[_to_ui_doc_from_hit(h) for h in hits_visible],
            retrieve_top_k=retrieve_top_k,
        )
    except Exception as e:
        print(f"search log insert failed: {e}")

    # 10) 세션 제목 갱신
    try:
        if user_text:
            repo.touch_session(session_id, user, title=user_text[:60])
    except Exception:
        repo.touch_session(session_id, user)

    top_docs_ui = [_to_ui_doc_from_hit(h) for h in hits_visible[:ui_top_k]]

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
    # rel is PARSE_ROOT relative path (we stored parsed_md_rel_path relative to PARSE_ROOT)
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