"""Knowledge Graph 조회 (읽기 전용).

kg_builder가 생성한 조인 테이블을 사용해 연관 문서/보고서/용어를 조회한다.
모든 함수는 그래프가 비어있거나 테이블이 없으면 빈 결과를 반환한다 (기존 플로우 무회귀).
"""
from app.db import get_conn
from app.archive_loader import get_local_archive_docs, _parse_date_to_timestamp


def _doc_meta_map() -> dict:
    return {d["doc_id"]: d for d in (get_local_archive_docs() or [])}


def _to_ui_doc(meta: dict, report_index: str | None = None) -> dict:
    """기존 openDocModal / Top Documents 패널이 그대로 열 수 있는 shape."""
    return {
        "doc_id": meta["doc_id"],
        "chunk_id": None,
        "title": meta.get("title") or meta["doc_id"],
        "mail_date": meta.get("mail_date") or "",
        "mail_from": meta.get("mail_from") or "",
        "report_index": report_index,
        "score": None,
        "merge_title_content": "",
        "additionalField": {
            "storage": meta.get("storage") or {},
            "assets": meta.get("assets") or [],
        },
        "_index": "kg-related",
    }


def get_docs_for_reports(report_indexes: list, limit: int = 5) -> list:
    """report_index 목록과 연결된 문서를 최신순으로 반환 (UI doc shape)."""
    ridx = [str(r) for r in (report_indexes or []) if str(r).strip()]
    if not ridx:
        return []

    try:
        conn = get_conn()
        cur = conn.cursor(dictionary=True)
        placeholders = ",".join(["%s"] * len(ridx))
        cur.execute(
            f"SELECT doc_id, report_index, MAX(confidence) AS confidence "
            f"FROM kg_doc_report WHERE report_index IN ({placeholders}) "
            f"GROUP BY doc_id, report_index",
            tuple(ridx),
        )
        rows = cur.fetchall() or []
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[KG] get_docs_for_reports 조회 실패: {e}")
        return []

    meta_map = _doc_meta_map()
    out = []
    seen = set()
    for r in rows:
        doc_id = r["doc_id"]
        if doc_id in seen:
            continue
        meta = meta_map.get(doc_id)
        if not meta:
            continue
        seen.add(doc_id)
        out.append(_to_ui_doc(meta, report_index=str(r["report_index"])))

    out.sort(key=lambda d: _parse_date_to_timestamp(d.get("mail_date") or ""), reverse=True)
    return out[:limit]


def get_related(report_index: str = "", doc_id: str = "") -> dict:
    """단건 기준 연관 항목: report_index → 문서들 / doc_id → 보고서들과 그 형제 문서들."""
    result = {"report_indexes": [], "docs": []}

    try:
        conn = get_conn()
        cur = conn.cursor(dictionary=True)

        if report_index:
            result["report_indexes"] = [str(report_index)]
        elif doc_id:
            cur.execute("SELECT DISTINCT report_index FROM kg_doc_report WHERE doc_id=%s", (doc_id,))
            result["report_indexes"] = [str(r["report_index"]) for r in (cur.fetchall() or [])]

        cur.close()
        conn.close()
    except Exception as e:
        print(f"[KG] get_related 조회 실패: {e}")
        return result

    if result["report_indexes"]:
        docs = get_docs_for_reports(result["report_indexes"], limit=20)
        if doc_id:
            docs = [d for d in docs if d["doc_id"] != doc_id]
        result["docs"] = docs
    return result


def get_term_overview(term_id: int, top_n: int = 10) -> dict:
    """용어 허브용: 관련 문서/보고서 수 + 상위 문서 + 동시출현 용어 (Phase 3 UI 대비 API)."""
    out = {"term_id": term_id, "docs_count": 0, "reports_count": 0, "top_docs": [], "co_terms": []}
    try:
        conn = get_conn()
        cur = conn.cursor(dictionary=True)

        cur.execute("SELECT COUNT(*) AS c FROM kg_doc_term WHERE term_id=%s", (term_id,))
        out["docs_count"] = int((cur.fetchone() or {}).get("c") or 0)

        cur.execute("SELECT COUNT(DISTINCT report_index) AS c FROM kg_report_term WHERE term_id=%s", (term_id,))
        out["reports_count"] = int((cur.fetchone() or {}).get("c") or 0)

        cur.execute(
            "SELECT doc_id, freq FROM kg_doc_term WHERE term_id=%s ORDER BY freq DESC LIMIT %s",
            (term_id, top_n),
        )
        top_doc_rows = cur.fetchall() or []

        cur.execute("""
            SELECT
                CASE WHEN term_a=%s THEN term_b ELSE term_a END AS other_term,
                co_doc_count, co_report_count
            FROM kg_term_edge
            WHERE term_a=%s OR term_b=%s
            ORDER BY (co_doc_count + co_report_count) DESC
            LIMIT %s
        """, (term_id, term_id, term_id, top_n))
        co_rows = cur.fetchall() or []

        # 동시출현 용어 이름 붙이기
        other_ids = [r["other_term"] for r in co_rows]
        names = {}
        if other_ids:
            placeholders = ",".join(["%s"] * len(other_ids))
            cur.execute(
                f"SELECT term_id, canonical_name, term_type FROM term_dictionary WHERE term_id IN ({placeholders})",
                tuple(other_ids),
            )
            names = {r["term_id"]: r for r in (cur.fetchall() or [])}

        cur.close()
        conn.close()
    except Exception as e:
        print(f"[KG] get_term_overview 조회 실패: {e}")
        return out

    meta_map = _doc_meta_map()
    for r in top_doc_rows:
        meta = meta_map.get(r["doc_id"])
        if meta:
            d = _to_ui_doc(meta)
            d["freq"] = int(r.get("freq") or 0)
            out["top_docs"].append(d)

    for r in co_rows:
        info = names.get(r["other_term"]) or {}
        out["co_terms"].append({
            "term_id": r["other_term"],
            "canonical_name": info.get("canonical_name"),
            "term_type": info.get("term_type"),
            "co_doc_count": int(r.get("co_doc_count") or 0),
            "co_report_count": int(r.get("co_report_count") or 0),
        })
    return out
