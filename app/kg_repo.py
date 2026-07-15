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
            # top-5 ES 문서 카드와 동일한 #태그(분석담당자/발행날짜/보고서링크)를 그리도록
            # pickMailMeta가 읽는 키를 additionalField에 채운다.
            "mail_from": meta.get("mail_from") or "",
            "mail_date": meta.get("mail_date") or "",
            "report_links": meta.get("report_links") or [],
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


def get_link_samples(source: str = "", q: str = "", limit: int = 50) -> list:
    """문서↔보고서 연결 상세 (대시보드 드릴다운용).
    반환: [{doc_id, title, mail_date, report_index, source, confidence, evidence, rel_path}]
    """
    limit = max(1, min(int(limit or 50), 200))
    try:
        conn = get_conn()
        cur = conn.cursor(dictionary=True)
        sql = "SELECT doc_id, report_index, source, confidence, evidence FROM kg_doc_report"
        params = []
        if source:
            sql += " WHERE source=%s"
            params.append(source)
        sql += " ORDER BY confidence DESC"
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[KG] get_link_samples 조회 실패: {e}")
        return []

    meta_map = _doc_meta_map()
    ql = (q or "").strip().lower()
    out = []
    for r in rows:
        meta = meta_map.get(r["doc_id"]) or {}
        title = meta.get("title") or r["doc_id"]
        if ql and ql not in title.lower() and ql not in str(r["doc_id"]).lower() \
           and ql not in str(r["report_index"]).lower():
            continue
        out.append({
            "doc_id": r["doc_id"],
            "title": title,
            "mail_date": meta.get("mail_date") or "",
            "report_index": str(r["report_index"]),
            "source": r["source"],
            "confidence": float(r["confidence"] or 0),
            "evidence": r.get("evidence") or "",
            "rel_path": ((meta.get("storage") or {}).get("parsed_md_rel_path")) or "",
            "assets": meta.get("assets") or [],  # 문서 뷰어 이미지 표시용
        })
        if len(out) >= limit:
            break

    out.sort(key=lambda x: (-x["confidence"], -_parse_date_to_timestamp(x["mail_date"])))
    return out


# =====================================================================
# Phase 3: REPORT_ANALYSIS 생성 근거 조립 (KG를 답변 생성에 투입)
# =====================================================================
# 근거 텍스트 절단 길이 — tools.search_documents의 2500자 절단과 동일하게 유지
# (LLM이 보는 텍스트와 인용 검증 대상 텍스트가 일치해야 verbatim quote가 통과함)
EVIDENCE_TEXT_LIMIT = 2500

_REPORT_FACT_COLS = "report_index, Lot_ID, WF_ID, 불량명, 성분, 공정노드, 모듈, 공정명, 설비명, 분석담당자, 의뢰자명"


def get_report_rows(report_indexes: list) -> list:
    """report_index 집합의 DB 구조화 사실을 조회해 보고서 단위로 병합 반환.

    v_ai_defect_search는 같은 report_index가 여러 행(성분/불량명 분해)으로 존재할 수
    있으므로, 보고서당 1건으로 각 컬럼의 고유값을 ', '로 병합한다.
    반환: [{report_index, Lot_ID, WF_ID, 불량명, ...}]
    """
    ridx = [str(r) for r in (report_indexes or []) if str(r).strip()]
    if not ridx:
        return []
    try:
        conn = get_conn()
        cur = conn.cursor(dictionary=True)
        placeholders = ",".join(["%s"] * len(ridx))
        cur.execute(
            f"SELECT DISTINCT {_REPORT_FACT_COLS} FROM v_ai_defect_search "
            f"WHERE report_index IN ({placeholders})",
            tuple(ridx),
        )
        rows = cur.fetchall() or []
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[KG] get_report_rows 조회 실패: {e}")
        return []

    merged = {}
    order = []
    for r in rows:
        key = str(r.get("report_index") or "")
        if not key:
            continue
        if key not in merged:
            merged[key] = {"report_index": key}
            order.append(key)
        acc = merged[key]
        for col, val in r.items():
            if col == "report_index":
                continue
            v = str(val or "").strip()
            if not v:
                continue
            existing = acc.get(col)
            if not existing:
                acc[col] = v
            elif v not in existing.split(", "):
                acc[col] = existing + ", " + v
    return [merged[k] for k in order]


def get_report_terms(report_indexes: list) -> list:
    """report_index 집합과 연결된 용어 정의 조회 (kg_report_term ⋈ term_dictionary).

    반환: [{report_index, term_id, canonical_name, term_type, description, src_cols}]
    """
    ridx = [str(r) for r in (report_indexes or []) if str(r).strip()]
    if not ridx:
        return []
    try:
        conn = get_conn()
        cur = conn.cursor(dictionary=True)
        placeholders = ",".join(["%s"] * len(ridx))
        cur.execute(
            f"""
            SELECT rt.report_index, rt.term_id,
                   td.canonical_name, td.term_type, td.description,
                   GROUP_CONCAT(DISTINCT rt.src_col) AS src_cols,
                   MAX(rt.confidence) AS confidence
            FROM kg_report_term rt
            JOIN term_dictionary td ON td.term_id = rt.term_id
            WHERE rt.report_index IN ({placeholders})
            GROUP BY rt.report_index, rt.term_id, td.canonical_name, td.term_type, td.description
            ORDER BY confidence DESC
            """,
            tuple(ridx),
        )
        rows = cur.fetchall() or []
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[KG] get_report_terms 조회 실패: {e}")
        return []

    return [{
        "report_index": str(r["report_index"]),
        "term_id": r["term_id"],
        "canonical_name": r.get("canonical_name") or "",
        "term_type": r.get("term_type") or "",
        "description": r.get("description") or "",
        "src_cols": r.get("src_cols") or "",
    } for r in rows]


def get_report_evidence_chunks(report_indexes: list, limit: int = 8) -> list:
    """KG로 연결된 원본 문서를 **실제 본문을 채운 인용 가능 chunk**로 반환.

    get_docs_for_reports와 달리 merge_title_content에 아카이브 원문(raw_content,
    EVIDENCE_TEXT_LIMIT 절단)을 채워 llm_answer_with_citations → validate_citations의
    verbatim quote 검증을 통과할 수 있게 한다. provenance(kg_source/confidence/evidence)를
    함께 태그해 UI에서 연결 근거를 추적할 수 있다.
    """
    ridx = [str(r) for r in (report_indexes or []) if str(r).strip()]
    if not ridx:
        return []
    try:
        conn = get_conn()
        cur = conn.cursor(dictionary=True)
        placeholders = ",".join(["%s"] * len(ridx))
        cur.execute(
            f"SELECT doc_id, report_index, source, MAX(confidence) AS confidence, "
            f"MAX(evidence) AS evidence "
            f"FROM kg_doc_report WHERE report_index IN ({placeholders}) "
            f"GROUP BY doc_id, report_index, source "
            f"ORDER BY confidence DESC",
            tuple(ridx),
        )
        rows = cur.fetchall() or []
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[KG] get_report_evidence_chunks 조회 실패: {e}")
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
        title = meta.get("title") or doc_id
        body = (meta.get("raw_content") or "").strip()
        text = f"{title}\n{body}"[:EVIDENCE_TEXT_LIMIT]
        chunk = _to_ui_doc(meta, report_index=str(r["report_index"]))
        chunk["chunk_id"] = doc_id  # 안정적 인용 키 (아카이브 문서는 청크 분할 없음)
        chunk["merge_title_content"] = text
        chunk["kg_source"] = r["source"]
        chunk["kg_confidence"] = float(r["confidence"] or 0)
        chunk["kg_evidence"] = r.get("evidence") or ""
        out.append(chunk)
        if len(out) >= limit:
            break
    return out


def _format_report_fact_text(row: dict) -> str:
    """DB 구조화 사실을 인용 가능한 평문 텍스트로 포맷 (verbatim quote 대상)."""
    lines = [f"[보고서 {row.get('report_index')} DB 분석 기록]"]
    for col in ("Lot_ID", "WF_ID", "불량명", "성분", "공정노드", "모듈", "공정명",
                "설비명", "분석담당자", "의뢰자명"):
        v = str(row.get(col) or "").strip()
        if v:
            lines.append(f"{col}: {v}")
    return "\n".join(lines)


def build_report_analysis_context(report_indexes: list, max_reports: int = 5,
                                  doc_chunk_limit: int = 8) -> dict:
    """REPORT_ANALYSIS 생성 근거 통합 조립 (결정적, LLM 미사용).

    반환:
      chunks     : rag_chunks 형태 근거 목록 — (a) 보고서별 DB 사실 chunk
                   (doc_id=f"db:{ridx}", kg_source="db") + (b) KG 연결문서 chunk(실제 본문).
      glossary   : 연결 용어 정의 텍스트 (system 프롬프트 주입용, chunk 아님)
      db_rows    : 병합된 보고서 DB 행 (numeric echo·UI용)
      linked_report_indexes : 연결문서가 1건 이상 확보된 report_index 집합 (ES 보완 판단용)
    """
    ridx = [str(r) for r in (report_indexes or []) if str(r).strip()][:max_reports]
    empty = {"chunks": [], "glossary": "", "db_rows": [], "linked_report_indexes": set()}
    if not ridx:
        return empty

    db_rows = get_report_rows(ridx)
    doc_chunks = get_report_evidence_chunks(ridx, limit=doc_chunk_limit)
    terms = get_report_terms(ridx)

    chunks = []
    for row in db_rows:
        r = row["report_index"]
        chunks.append({
            "doc_id": f"db:{r}",
            "chunk_id": "db",
            "title": f"보고서 {r} DB 분석 기록",
            "merge_title_content": _format_report_fact_text(row),
            "score": None,
            "additionalField": {},
            "_index": "kg-report-db",
            "kg_source": "db",
            "report_index": r,
        })
    chunks.extend(doc_chunks)

    glossary_lines = []
    seen_terms = set()
    for t in terms:
        name = t["canonical_name"]
        if not name or name in seen_terms or not t["description"]:
            continue
        seen_terms.add(name)
        glossary_lines.append(f"* {name} ({t['term_type']}): {t['description']}")

    return {
        "chunks": chunks,
        "glossary": "\n".join(glossary_lines),
        "db_rows": db_rows,
        "linked_report_indexes": {c.get("report_index") for c in doc_chunks if c.get("report_index")},
    }


def get_term_network(term_id: int, top_n: int = 16) -> dict:
    """그래프 탐색기(V2)용 2-hop 네트워크: 중심+이웃 노드 + 이웃끼리 엣지.

    반환:
      center: {term_id, canonical_name, term_type, docs_count, reports_count}
      nodes : [{term_id, canonical_name, term_type, weight(co_doc+co_report), docs_count, reports_count}]
              (중심 이웃 상위 top_n, weight 내림차순)
      edges : [{a, b, weight}]  — 중심↔이웃 + 이웃↔이웃(kg_term_edge에 존재하는 쌍만)
    """
    ov = get_term_overview(term_id, top_n=top_n)
    center = {
        "term_id": term_id,
        "canonical_name": None,
        "term_type": None,
        "docs_count": ov.get("docs_count", 0),
        "reports_count": ov.get("reports_count", 0),
    }
    co = ov.get("co_terms") or []
    if not co:
        return {"center": center, "nodes": [], "edges": []}

    nodes = []
    id_set = set()
    for c in co:
        tid = c.get("term_id")
        if tid is None or tid in id_set:
            continue
        id_set.add(tid)
        nodes.append({
            "term_id": tid,
            "canonical_name": c.get("canonical_name") or f"#{tid}",
            "term_type": c.get("term_type") or "",
            "weight": int(c.get("co_doc_count") or 0) + int(c.get("co_report_count") or 0),
            "docs_count": int(c.get("co_doc_count") or 0),
            "reports_count": int(c.get("co_report_count") or 0),
        })

    edges = [{"a": term_id, "b": c["term_id"],
              "weight": int(c.get("co_doc_count") or 0) + int(c.get("co_report_count") or 0)}
             for c in co if c.get("term_id") in id_set]

    # 이웃↔이웃 엣지 (2-hop): kg_term_edge에서 두 끝점이 모두 이웃 집합에 있는 쌍
    neigh = [n["term_id"] for n in nodes]
    if len(neigh) >= 2:
        try:
            conn = get_conn()
            cur = conn.cursor(dictionary=True)
            ph = ",".join(["%s"] * len(neigh))
            cur.execute(
                f"""SELECT term_a, term_b, co_doc_count, co_report_count
                    FROM kg_term_edge
                    WHERE term_a IN ({ph}) AND term_b IN ({ph})""",
                tuple(neigh) + tuple(neigh),
            )
            for r in (cur.fetchall() or []):
                a, b = r["term_a"], r["term_b"]
                if a == b:
                    continue
                edges.append({"a": a, "b": b,
                              "weight": int(r.get("co_doc_count") or 0) + int(r.get("co_report_count") or 0)})
            cur.close()
            conn.close()
        except Exception as e:
            print(f"[KG] get_term_network 이웃 엣지 조회 실패(무시): {e}")

    return {"center": center, "nodes": nodes, "edges": edges}


def get_term_overview(term_id: int, top_n: int = 10) -> dict:
    """용어 허브용: 관련 문서/보고서 수 + 상위 문서 + 동시출현 용어 (Phase 3 UI 대비 API)."""
    out = {"term_id": term_id, "docs_count": 0, "reports_count": 0, "top_docs": [], "co_terms": [], "top_reports": []}
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

        # 연결 보고서 목록 (연결 문서가 0건일 때 사이드패널 fallback 용)
        report_rows = []
        rep_meta = {}
        try:
            cur.execute("""
                SELECT report_index, GROUP_CONCAT(DISTINCT src_col) AS cols, MAX(confidence) AS conf
                FROM kg_report_term WHERE term_id=%s
                GROUP BY report_index ORDER BY conf DESC LIMIT %s
            """, (term_id, top_n))
            report_rows = cur.fetchall() or []
            if report_rows:
                ridxs = [str(r["report_index"]) for r in report_rows]
                ph = ",".join(["%s"] * len(ridxs))
                cur.execute(
                    f"SELECT DISTINCT report_index, 불량명, 분석완료일시 FROM v_ai_defect_search "
                    f"WHERE report_index IN ({ph})", tuple(ridxs))
                for m in (cur.fetchall() or []):
                    rep_meta.setdefault(str(m["report_index"]), m)
        except Exception as e:
            print(f"[KG] top_reports 보강 실패(무시): {e}")

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

    for r in report_rows:
        ridx = str(r["report_index"])
        m = rep_meta.get(ridx) or {}
        out["top_reports"].append({
            "report_index": ridx,
            "defect": m.get("불량명") or "",
            "date": str(m.get("분석완료일시") or ""),
            "src_cols": r.get("cols") or "",
        })
    return out
