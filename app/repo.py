import uuid
import json
from datetime import datetime
from app.db import get_conn

def _now_dt():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def create_session(user_id: str, title: str) -> str:
    session_id = str(uuid.uuid4())
    now = _now_dt()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO chat_sessions(session_id,user_id,title,created_at,updated_at) VALUES(%s,%s,%s,%s,%s)",
            (session_id, user_id, title[:255], now, now)
        )
        conn.commit()
        return session_id
    finally:
        cur.close()
        conn.close()

def touch_session(session_id: str, user_id: str, title: str | None = None):
    now = _now_dt()
    conn = get_conn()
    cur = conn.cursor()
    try:
        if title:
            cur.execute(
                "UPDATE chat_sessions SET title=%s, updated_at=%s WHERE session_id=%s AND user_id=%s",
                (title[:255], now, session_id, user_id)
            )
        else:
            cur.execute(
                "UPDATE chat_sessions SET updated_at=%s WHERE session_id=%s AND user_id=%s",
                (now, session_id, user_id)
            )
        conn.commit()
    finally:
        cur.close()
        conn.close()

def list_sessions(user_id: str):
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT session_id, title, updated_at FROM chat_sessions "
            "WHERE user_id=%s AND archived=0 ORDER BY updated_at DESC",
            (user_id,)
        )
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()

def archive_session(session_id: str, user_id: str):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE chat_sessions SET archived=1 WHERE session_id=%s AND user_id=%s",
            (session_id, user_id)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

def get_messages(session_id: str, user_id: str):
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT msg_id, role, content, created_at FROM chat_messages "
            "WHERE session_id=%s AND user_id=%s ORDER BY created_at ASC",
            (session_id, user_id)
        )
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()

def insert_message(session_id: str, user_id: str, role: str, content: str) -> str:
    msg_id = str(uuid.uuid4())
    now = _now_dt()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO chat_messages(msg_id,session_id,user_id,role,content,created_at) VALUES(%s,%s,%s,%s,%s,%s)",
            (msg_id, session_id, user_id, role, content, now)
        )
        conn.commit()
        return msg_id
    finally:
        cur.close()
        conn.close()

def insert_turn_artifact(
    session_id: str,
    user_id: str,
    user_msg_id: str,
    assistant_msg_id: str,
    index_name: str,
    rag_response: dict | None,
    citations: dict | None,
) -> str:
    turn_id = str(uuid.uuid4())
    now = _now_dt()

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO chat_turn_artifacts(turn_id,session_id,user_id,user_msg_id,assistant_msg_id,index_name,"
            "rag_response_json,citations_json,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                turn_id,
                session_id,
                user_id,
                user_msg_id,
                assistant_msg_id,
                index_name,
                json.dumps(rag_response, ensure_ascii=False) if rag_response else None,
                json.dumps(citations, ensure_ascii=False) if citations else None,
                now
            )
        )
        conn.commit()
        return turn_id
    finally:
        cur.close()
        conn.close()

# 검색 로그 저장
def insert_search_log(
    session_id: str,
    user_id: str,
    user_msg_id: str,
    assistant_msg_id: str | None,
    index_name: str,
    original_query: str,
    rewritten_query: str | None,
    normalized_query: str | None,
    expanded_query: str | None,
    detected_terms: list | None,
    expansion_terms: dict | None,
    filters: dict | None,
    top_docs: list | None,
    retrieve_top_k: int,
) -> str:
    search_id = str(uuid.uuid4())
    now = _now_dt()

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO chat_search_logs(
                search_id, session_id, user_id, user_msg_id, assistant_msg_id, index_name,
                original_query, rewritten_query, normalized_query, expanded_query,
                detected_terms_json, expansion_terms_json, filters_json, top_docs_json,
                retrieve_top_k, created_at
            )
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                search_id,
                session_id,
                user_id,
                user_msg_id,
                assistant_msg_id,
                index_name,
                original_query,
                rewritten_query,
                normalized_query,
                expanded_query,
                json.dumps(detected_terms, ensure_ascii=False) if detected_terms is not None else None,
                json.dumps(expansion_terms, ensure_ascii=False) if expansion_terms is not None else None,
                json.dumps(filters, ensure_ascii=False) if filters is not None else None,
                json.dumps(top_docs, ensure_ascii=False) if top_docs is not None else None,
                int(retrieve_top_k or 0),
                now,
            )
        )
        conn.commit()
        return search_id
    finally:
        cur.close()
        conn.close()

def get_latest_artifact(session_id: str, user_id: str):
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT rag_response_json, citations_json, index_name, created_at "
            "FROM chat_turn_artifacts WHERE session_id=%s AND user_id=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id, user_id)
        )
        row = cur.fetchone()
        if not row:
            return None

        rag = row.get("rag_response_json")
        cits = row.get("citations_json")

        if isinstance(rag, str):
            try: rag = json.loads(rag)
            except: pass
        if isinstance(cits, str):
            try: cits = json.loads(cits)
            except: pass

        return {
            "rag_response": rag,
            "citations": cits,
            "index_name": row.get("index_name"),
            "created_at": str(row.get("created_at")),
        }
    finally:
        cur.close()
        conn.close()

def get_artifact_by_assistant_msg(session_id: str, user_id: str, assistant_msg_id: str):
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT rag_response_json, citations_json, index_name, created_at "
            "FROM chat_turn_artifacts "
            "WHERE session_id=%s AND user_id=%s AND assistant_msg_id=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id, user_id, assistant_msg_id)
        )
        row = cur.fetchone()
        if not row:
            return None

        rag = row.get("rag_response_json")
        cits = row.get("citations_json")
        if isinstance(rag, str):
            try: rag = json.loads(rag)
            except: pass
        if isinstance(cits, str):
            try: cits = json.loads(cits)
            except: pass

        return {
            "rag_response": rag,
            "citations": cits,
            "index_name": row.get("index_name"),
            "created_at": str(row.get("created_at")),
        }
    finally:
        cur.close()
        conn.close()

def get_search_log_by_user_msg(session_id: str, user_id: str, user_msg_id: str):
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT
                search_id,
                session_id,
                user_id,
                user_msg_id,
                assistant_msg_id,
                index_name,
                original_query,
                rewritten_query,
                normalized_query,
                expanded_query,
                detected_terms_json,
                expansion_terms_json,
                filters_json,
                top_docs_json,
                retrieve_top_k,
                created_at
            FROM chat_search_logs
            WHERE session_id=%s AND user_id=%s AND user_msg_id=%s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (session_id, user_id, user_msg_id)
        )
        row = cur.fetchone()
        if not row:
            return None

        for k in ("detected_terms_json", "expansion_terms_json", "filters_json", "top_docs_json"):
            v = row.get(k)
            if isinstance(v, str):
                try:
                    row[k] = json.loads(v)
                except Exception:
                    pass

        return {
            "search_id": row.get("search_id"),
            "session_id": row.get("session_id"),
            "user_id": row.get("user_id"),
            "user_msg_id": row.get("user_msg_id"),
            "assistant_msg_id": row.get("assistant_msg_id"),
            "index_name": row.get("index_name"),
            "original_query": row.get("original_query"),
            "rewritten_query": row.get("rewritten_query"),
            "normalized_query": row.get("normalized_query"),
            "expanded_query": row.get("expanded_query"),
            "detected_terms": row.get("detected_terms_json"),
            "expansion_terms": row.get("expansion_terms_json"),
            "filters": row.get("filters_json"),
            "top_docs": row.get("top_docs_json"),
            "retrieve_top_k": row.get("retrieve_top_k"),
            "created_at": str(row.get("created_at")),
        }
    finally:
        cur.close()
        conn.close()


def list_search_logs_for_session(session_id: str, user_id: str):
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT
                search_id,
                session_id,
                user_id,
                user_msg_id,
                assistant_msg_id,
                index_name,
                original_query,
                rewritten_query,
                normalized_query,
                expanded_query,
                detected_terms_json,
                expansion_terms_json,
                filters_json,
                top_docs_json,
                retrieve_top_k,
                created_at
            FROM chat_search_logs
            WHERE session_id=%s AND user_id=%s
            ORDER BY created_at ASC
            """,
            (session_id, user_id)
        )
        rows = cur.fetchall() or []

        out = []
        for row in rows:
            for k in ("detected_terms_json", "expansion_terms_json", "filters_json", "top_docs_json"):
                v = row.get(k)
                if isinstance(v, str):
                    try:
                        row[k] = json.loads(v)
                    except Exception:
                        pass

            out.append({
                "search_id": row.get("search_id"),
                "session_id": row.get("session_id"),
                "user_id": row.get("user_id"),
                "user_msg_id": row.get("user_msg_id"),
                "assistant_msg_id": row.get("assistant_msg_id"),
                "index_name": row.get("index_name"),
                "original_query": row.get("original_query"),
                "rewritten_query": row.get("rewritten_query"),
                "normalized_query": row.get("normalized_query"),
                "expanded_query": row.get("expanded_query"),
                "detected_terms": row.get("detected_terms_json"),
                "expansion_terms": row.get("expansion_terms_json"),
                "filters": row.get("filters_json"),
                "top_docs": row.get("top_docs_json"),
                "retrieve_top_k": row.get("retrieve_top_k"),
                "created_at": str(row.get("created_at")),
            })

        return out
    finally:
        cur.close()
        conn.close()