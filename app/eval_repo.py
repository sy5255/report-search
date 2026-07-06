"""시스템 성능 평가 집계 (Cognitive Trace 대시보드용, 읽기 전용).

이미 쌓이는 텔레메트리에서 파생 — 별도 계측 없이 측정 가능한 지표만:
  - chat_messages / chat_sessions        : 사용량
  - chat_message_feedback                : 사용자 만족(👍/👎)
  - chat_turn_artifacts.rag_response_json: 인텐트 분포, verification(근거 충족도·수치 검증·근거 게이트)
  - chat_search_logs                     : 검색 0건 비율, 용어사전 히트
  - kg_* 테이블                          : Knowledge Graph 상태/커버리지

모든 쿼리는 개별 try/except — 일부 실패해도 나머지 지표는 반환한다.
"""
from datetime import datetime, timedelta

from app.db import get_conn


def _q(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall() or []


def get_eval_summary(days: int = 30) -> dict:
    since = datetime.now() - timedelta(days=days)
    out = {
        "days": days,
        "totals": {"questions": 0, "sessions": 0, "fb_up": 0, "fb_down": 0},
        "daily": [],
        "intents": [],
        "quality": {"groundedness": None, "claims_rows": 0, "numeric_ok_rate": None, "numeric_rows": 0, "gate_count": 0},
        "search": {"zero_hit_rate": None, "logs": 0, "avg_terms": None},
    }

    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        # ── 사용량 ──────────────────────────────────────────────
        try:
            r = _q(cur, "SELECT COUNT(*) c, COUNT(DISTINCT session_id) s FROM chat_messages "
                        "WHERE role='user' AND created_at>=%s", (since,))[0]
            out["totals"]["questions"] = int(r["c"] or 0)
            out["totals"]["sessions"] = int(r["s"] or 0)
        except Exception as e:
            print(f"[Eval] usage totals 실패: {e}")

        # ── 피드백 ──────────────────────────────────────────────
        try:
            for r in _q(cur, "SELECT rating, COUNT(*) c FROM chat_message_feedback "
                             "WHERE created_at>=%s GROUP BY rating", (since,)):
                if r["rating"] == "up":
                    out["totals"]["fb_up"] = int(r["c"])
                elif r["rating"] == "down":
                    out["totals"]["fb_down"] = int(r["c"])
        except Exception as e:
            print(f"[Eval] feedback totals 실패: {e}")

        # ── 일별 추이 (질문 수 / 피드백 / 근거 충족도) ───────────
        daily = {}
        try:
            for r in _q(cur, "SELECT DATE(created_at) d, COUNT(*) c FROM chat_messages "
                             "WHERE role='user' AND created_at>=%s GROUP BY DATE(created_at)", (since,)):
                daily.setdefault(str(r["d"]), {})["questions"] = int(r["c"])
        except Exception as e:
            print(f"[Eval] daily questions 실패: {e}")
        try:
            for r in _q(cur, "SELECT DATE(created_at) d, rating, COUNT(*) c FROM chat_message_feedback "
                             "WHERE created_at>=%s GROUP BY DATE(created_at), rating", (since,)):
                daily.setdefault(str(r["d"]), {})[("up" if r["rating"] == "up" else "down")] = int(r["c"])
        except Exception as e:
            print(f"[Eval] daily feedback 실패: {e}")
        try:
            for r in _q(cur, """
                SELECT DATE(created_at) d,
                       AVG(CAST(JSON_UNQUOTE(JSON_EXTRACT(rag_response_json,'$.verification.claims_supported')) AS DECIMAL(10,3))
                           / NULLIF(CAST(JSON_UNQUOTE(JSON_EXTRACT(rag_response_json,'$.verification.claims_total')) AS DECIMAL(10,3)), 0)) g
                FROM chat_turn_artifacts
                WHERE created_at>=%s
                  AND JSON_EXTRACT(rag_response_json,'$.verification.claims_total') IS NOT NULL
                GROUP BY DATE(created_at)
            """, (since,)):
                if r["g"] is not None:
                    daily.setdefault(str(r["d"]), {})["grounded"] = round(float(r["g"]), 3)
        except Exception as e:
            print(f"[Eval] daily groundedness 실패: {e}")
        out["daily"] = [
            {"d": d, "questions": v.get("questions", 0), "up": v.get("up", 0),
             "down": v.get("down", 0), "grounded": v.get("grounded")}
            for d, v in sorted(daily.items())
        ]

        # ── 인텐트 분포 ─────────────────────────────────────────
        try:
            out["intents"] = [
                {"intent": r["i"], "cnt": int(r["c"])}
                for r in _q(cur, """
                    SELECT JSON_UNQUOTE(JSON_EXTRACT(rag_response_json,'$.intent')) i, COUNT(*) c
                    FROM chat_turn_artifacts
                    WHERE created_at>=%s AND JSON_EXTRACT(rag_response_json,'$.intent') IS NOT NULL
                    GROUP BY i ORDER BY c DESC
                """, (since,))
                if r["i"]
            ]
        except Exception as e:
            print(f"[Eval] intents 실패: {e}")

        # ── 품질 (Phase 1 verification 기반) ────────────────────
        try:
            r = _q(cur, """
                SELECT
                  AVG(CAST(JSON_UNQUOTE(JSON_EXTRACT(rag_response_json,'$.verification.claims_supported')) AS DECIMAL(10,3))
                      / NULLIF(CAST(JSON_UNQUOTE(JSON_EXTRACT(rag_response_json,'$.verification.claims_total')) AS DECIMAL(10,3)), 0)) g,
                  SUM(JSON_EXTRACT(rag_response_json,'$.verification.claims_total') IS NOT NULL) rows_g,
                  AVG(CASE WHEN JSON_UNQUOTE(JSON_EXTRACT(rag_response_json,'$.verification.numeric_ok'))='true' THEN 1
                           WHEN JSON_UNQUOTE(JSON_EXTRACT(rag_response_json,'$.verification.numeric_ok'))='false' THEN 0 END) n_ok,
                  SUM(JSON_EXTRACT(rag_response_json,'$.verification.numeric_ok') IS NOT NULL) rows_n,
                  SUM(JSON_UNQUOTE(JSON_EXTRACT(rag_response_json,'$.verification.grounded'))='false') gate
                FROM chat_turn_artifacts WHERE created_at>=%s
            """, (since,))[0]
            out["quality"] = {
                "groundedness": round(float(r["g"]), 3) if r["g"] is not None else None,
                "claims_rows": int(r["rows_g"] or 0),
                "numeric_ok_rate": round(float(r["n_ok"]), 3) if r["n_ok"] is not None else None,
                "numeric_rows": int(r["rows_n"] or 0),
                "gate_count": int(r["gate"] or 0),
            }
        except Exception as e:
            print(f"[Eval] quality 실패: {e}")

        # ── 검색 품질 ───────────────────────────────────────────
        try:
            r = _q(cur, """
                SELECT COUNT(*) n,
                       AVG(JSON_LENGTH(top_docs_json)=0) zero_rate,
                       AVG(JSON_LENGTH(detected_terms_json)) avg_terms
                FROM chat_search_logs WHERE created_at>=%s
            """, (since,))[0]
            out["search"] = {
                "logs": int(r["n"] or 0),
                "zero_hit_rate": round(float(r["zero_rate"]), 3) if r["zero_rate"] is not None else None,
                "avg_terms": round(float(r["avg_terms"]), 2) if r["avg_terms"] is not None else None,
            }
        except Exception as e:
            print(f"[Eval] search 실패: {e}")
    finally:
        cur.close()
        conn.close()

    return out


def get_kg_stats() -> dict:
    out = {
        "built": None,
        "edges": {"doc_report": 0, "doc_term": 0, "report_term": 0, "term_edge": 0},
        "coverage": {"docs_linked_report_pct": None, "docs_with_terms_pct": None},
        "sources": [],
        "top_terms": [],
    }
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        try:
            rows = _q(cur, "SELECT last_built_at, docs_indexed, reports_indexed FROM kg_build_state WHERE id=1")
            if rows:
                b = rows[0]
                out["built"] = {
                    "last_built_at": str(b["last_built_at"]) if b["last_built_at"] else None,
                    "docs_indexed": int(b["docs_indexed"] or 0),
                    "reports_indexed": int(b["reports_indexed"] or 0),
                }
        except Exception as e:
            print(f"[Eval] kg build state 실패: {e}")

        for key, table in [("doc_report", "kg_doc_report"), ("doc_term", "kg_doc_term"),
                           ("report_term", "kg_report_term"), ("term_edge", "kg_term_edge")]:
            try:
                out["edges"][key] = int(_q(cur, f"SELECT COUNT(*) c FROM {table}")[0]["c"] or 0)
            except Exception as e:
                print(f"[Eval] kg count {table} 실패: {e}")

        docs_total = (out["built"] or {}).get("docs_indexed") or 0
        if docs_total:
            try:
                linked = int(_q(cur, "SELECT COUNT(DISTINCT doc_id) c FROM kg_doc_report")[0]["c"] or 0)
                out["coverage"]["docs_linked_report_pct"] = round(linked / docs_total, 3)
            except Exception:
                pass
            try:
                termed = int(_q(cur, "SELECT COUNT(DISTINCT doc_id) c FROM kg_doc_term")[0]["c"] or 0)
                out["coverage"]["docs_with_terms_pct"] = round(termed / docs_total, 3)
            except Exception:
                pass

        try:
            out["sources"] = [
                {"source": r["source"], "cnt": int(r["c"])}
                for r in _q(cur, "SELECT source, COUNT(*) c FROM kg_doc_report GROUP BY source ORDER BY c DESC")
            ]
        except Exception as e:
            print(f"[Eval] kg sources 실패: {e}")

        try:
            out["top_terms"] = [
                {"term_id": r["term_id"], "canonical_name": r["canonical_name"],
                 "term_type": r["term_type"], "docs": int(r["c"])}
                for r in _q(cur, """
                    SELECT t.term_id, td.canonical_name, td.term_type, COUNT(*) c
                    FROM kg_doc_term t JOIN term_dictionary td ON td.term_id = t.term_id
                    GROUP BY t.term_id, td.canonical_name, td.term_type
                    ORDER BY c DESC LIMIT 8
                """)
            ]
        except Exception as e:
            print(f"[Eval] kg top terms 실패: {e}")
    finally:
        cur.close()
        conn.close()

    return out
