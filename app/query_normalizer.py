#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
from typing import Any, Dict, List, Tuple

from app.db import get_conn

TERM_CACHE_LIMIT = 5000

WORDLIKE_TYPES = {"chemistry", "process", "product", "defect", "node", "owner"}

# 확장 강도 정책
# - aggressive: canonical + aliases 적극 확장
# - conservative: canonical 위주, alias는 소수만
# - minimal: canonical만 유지하거나 거의 확장 안 함
TERM_TYPE_EXPANSION_POLICY = {
    "chemistry": "aggressive",
    "product": "aggressive",
    "node": "aggressive",
    "owner": "minimal",
    "process": "conservative",
    "defect": "minimal",
}

PROCESS_ALIAS_EXPANSION_LIMIT = 2   # process는 최대 2개까지만 확장(canonical 포함)
DEFAULT_ALIAS_EXPANSION_LIMIT = 5   # aggressive 타입도 너무 길어지지 않게 제한


def normalize_alias_text(s: str) -> str:
    s = str(s or "")
    s = s.replace("\u00A0", " ")
    s = s.lower()
    s = s.replace("-", " ").replace("_", " ").replace("/", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _token_boundary_pattern(alias_text: str) -> re.Pattern:
    """
    alias_text가 짧은 영문 약어여도 최대한 안전하게 찾기 위한 패턴.
    예:
      DHF -> 경계 기반
      foreign material -> foreign\\s+material
      김지혜 -> escape 기반
    """
    alias_text = str(alias_text or "").strip()
    escaped = re.escape(alias_text)
    escaped = escaped.replace(r"\ ", r"\s+")
    return re.compile(rf"(?i)(?<![A-Za-z0-9가-힣]){escaped}(?![A-Za-z0-9가-힣])")

def load_term_dictionary(scope_candidates: List[str] | None = None) -> List[Dict[str, Any]]:
    """
    term_dictionary + term_aliases를 join해서 검색용 사전 row 로드
    scope는 all + 현재 category 정도를 허용
    """
    scopes = ["all"]
    for s in (scope_candidates or []):
        s = str(s or "").strip()
        if s and s not in scopes:
            scopes.append(s)

    placeholders = ",".join(["%s"] * len(scopes))

    sql = f"""
    SELECT
        td.term_id,
        td.term_type,
        td.canonical_name,
        td.display_name,
        td.scope,
        td.status,
        td.is_verified,
        td.priority,
        td.expand_to_aliases,
        td.search_boost,

        ta.alias_id,
        ta.alias_text,
        ta.alias_normalized,
        ta.match_type,
        ta.language_code,
        ta.is_preferred,
        ta.status AS alias_status
    FROM term_dictionary td
    LEFT JOIN term_aliases ta
      ON td.term_id = ta.term_id
     AND ta.status = 'active'
    WHERE td.status = 'active'
      AND td.scope IN ({placeholders})
    ORDER BY
        td.priority ASC,
        td.term_type ASC,
        td.term_id ASC,
        ta.is_preferred DESC,
        ta.alias_id ASC
    LIMIT {TERM_CACHE_LIMIT}
    """

    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(sql, tuple(scopes))
        rows = cur.fetchall()
        return rows or []
    finally:
        cur.close()
        conn.close()


def build_term_entries(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_term: Dict[int, Dict[str, Any]] = {}

    for r in rows:
        term_id = int(r["term_id"])
        entry = by_term.get(term_id)
        if entry is None:
            entry = {
                "term_id": term_id,
                "term_type": r["term_type"],
                "canonical_name": r["canonical_name"],
                "display_name": r.get("display_name") or r["canonical_name"],
                "scope": r.get("scope") or "all",
                "priority": int(r.get("priority") or 100),
                "expand_to_aliases": int(r.get("expand_to_aliases") or 0),
                "search_boost": float(r.get("search_boost") or 1.0),
                "is_verified": int(r.get("is_verified") or 0),
                "aliases": [],
            }
            by_term[term_id] = entry

        alias_text = r.get("alias_text")
        if alias_text:
            entry["aliases"].append({
                "alias_id": r.get("alias_id"),
                "alias_text": alias_text,
                "alias_normalized": r.get("alias_normalized") or normalize_alias_text(alias_text),
                "match_type": r.get("match_type") or "contains",
                "language_code": r.get("language_code"),
                "is_preferred": int(r.get("is_preferred") or 0),
            })

    out = []
    for e in by_term.values():
        alias_texts = [a["alias_text"] for a in e["aliases"] if a.get("alias_text")]
        if e["canonical_name"] not in alias_texts:
            e["aliases"].insert(0, {
                "alias_id": None,
                "alias_text": e["canonical_name"],
                "alias_normalized": normalize_alias_text(e["canonical_name"]),
                "match_type": "contains",
                "language_code": None,
                "is_preferred": 1,
            })

        # alias 길이 긴 순 + preferred 우선 정렬
        e["aliases"].sort(
            key=lambda a: (
                -len(str(a.get("alias_text") or "")),
                -int(a.get("is_preferred") or 0),
                str(a.get("alias_text") or "").lower(),
            )
        )
        out.append(e)

    out.sort(key=lambda x: (x["priority"], x["term_type"], x["canonical_name"]))
    return out


def _collect_all_term_matches(query: str, term_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    각 term/alias에 대해 가능한 매치를 모두 수집
    이후 longest-match + non-overlap 정책으로 최종 선별
    """
    text = str(query or "")
    all_matches: List[Dict[str, Any]] = []

    for term in term_entries:
        for alias in term["aliases"]:
            alias_text = str(alias.get("alias_text") or "").strip()
            if not alias_text:
                continue

            pat = _token_boundary_pattern(alias_text)
            for m in pat.finditer(text):
                matched_text = m.group(0)
                match_source = "canonical" if normalize_alias_text(alias_text) == normalize_alias_text(term["canonical_name"]) else "alias"

                all_matches.append({
                    "term_id": term["term_id"],
                    "term_type": term["term_type"],
                    "canonical_name": term["canonical_name"],
                    "display_name": term["display_name"],
                    "matched_text": matched_text,
                    "matched_span": [m.start(), m.end()],
                    "alias_text": alias_text,
                    "priority": term["priority"],
                    "expand_to_aliases": term["expand_to_aliases"],
                    "search_boost": term["search_boost"],
                    "is_verified": term["is_verified"],
                    "match_source": match_source,
                    "match_length": len(alias_text),
                    "alias_is_preferred": int(alias.get("is_preferred") or 0),
                })

    return all_matches


def _select_non_overlapping_best_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    규칙:
    1) 긴 alias 우선
    2) 같은 길이면 priority 낮은(term priority 높음) 것 우선
    3) preferred alias 우선
    4) 시작 위치 빠른 것 우선

    그리고 선택된 span과 겹치는 후보는 제거
    """
    if not matches:
        return []

    matches_sorted = sorted(
        matches,
        key=lambda x: (
            -int(x["match_length"]),
            int(x["priority"]),
            -int(x.get("alias_is_preferred", 0)),
            int(x["matched_span"][0]),
            str(x["canonical_name"]).lower(),
        )
    )

    selected: List[Dict[str, Any]] = []

    for cand in matches_sorted:
        s, e = cand["matched_span"]
        overlapped = False
        for prev in selected:
            ps, pe = prev["matched_span"]
            if not (e <= ps or s >= pe):
                overlapped = True
                break
        if overlapped:
            continue
        selected.append(cand)

    selected.sort(key=lambda x: (x["matched_span"][0], x["priority"], -x["match_length"]))
    return selected


def detect_terms_in_query(query: str, term_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    all_matches = _collect_all_term_matches(query, term_entries)
    selected = _select_non_overlapping_best_matches(all_matches)

    seen_keys = set()
    out = []

    for d in selected:
        key = (
            d["term_id"],
            d["term_type"],
            d["canonical_name"],
            tuple(d["matched_span"]),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append(d)

    return out


def apply_canonical_rewrite(query: str, detected_terms: List[Dict[str, Any]]) -> str:
    """
    query 안에서 잡힌 alias를 canonical_name으로 치환
    detected_terms는 이미 non-overlap 보장 상태라고 가정
    """
    if not detected_terms:
        return query

    spans = [(d["matched_span"][0], d["matched_span"][1], d["canonical_name"]) for d in detected_terms]
    spans.sort(key=lambda x: x[0])

    out = []
    last = 0
    for s, e, repl in spans:
        out.append(query[last:s])
        out.append(repl)
        last = e
    out.append(query[last:])
    return "".join(out).strip()


def _get_expansion_policy(term_type: str) -> str:
    return TERM_TYPE_EXPANSION_POLICY.get(term_type, "conservative")


def _dedupe_keep_order(items: List[str]) -> List[str]:
    out = []
    seen = set()
    for x in items:
        s = str(x or "").strip()
        if not s:
            continue
        nx = normalize_alias_text(s)
        if nx in seen:
            continue
        seen.add(nx)
        out.append(s)
    return out


def build_expansion_terms(detected_terms: List[Dict[str, Any]], term_entries: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    by_term_id = {t["term_id"]: t for t in term_entries}
    out: Dict[str, List[str]] = {}

    for d in detected_terms:
        if int(d.get("expand_to_aliases") or 0) != 1:
            continue

        term = by_term_id.get(d["term_id"])
        if not term:
            continue

        term_type = term["term_type"]
        policy = _get_expansion_policy(term_type)

        aliases = term.get("aliases") or []
        preferred_aliases = []
        other_aliases = []

        for a in aliases:
            alias_text = str(a.get("alias_text") or "").strip()
            if not alias_text:
                continue
            if int(a.get("is_preferred") or 0) == 1:
                preferred_aliases.append(alias_text)
            else:
                other_aliases.append(alias_text)

        candidates: List[str] = [term["canonical_name"]]

        if policy == "aggressive":
            candidates.extend(preferred_aliases)
            candidates.extend(other_aliases)
            candidates = _dedupe_keep_order(candidates)[:DEFAULT_ALIAS_EXPANSION_LIMIT]

        elif policy == "conservative":
            candidates.extend(preferred_aliases[:1])
            # process는 matched_text가 canonical이 아니면 그 matched alias도 유지해주는 편이 좋음
            matched_text = str(d.get("matched_text") or "").strip()
            if matched_text:
                candidates.append(matched_text)
            candidates = _dedupe_keep_order(candidates)[:PROCESS_ALIAS_EXPANSION_LIMIT]

        elif policy == "minimal":
            # canonical만 우선. 단, 사용자가 친 표현이 canonical과 다르면 matched_text 1개는 허용
            matched_text = str(d.get("matched_text") or "").strip()
            if matched_text and normalize_alias_text(matched_text) != normalize_alias_text(term["canonical_name"]):
                candidates.append(matched_text)
            candidates = _dedupe_keep_order(candidates)[:2]

        else:
            candidates = _dedupe_keep_order(candidates)[:2]

        vals = out.setdefault(term_type, [])
        for c in candidates:
            if c not in vals:
                vals.append(c)

    return out


def build_expanded_query(
    original_query: str,
    normalized_query: str,
    expansion_terms: Dict[str, List[str]]
) -> str:
    """
    RAG API가 structured boolean query를 받지 않으니,
    일단 평평한 텍스트 확장으로 보냄.
    """
    pieces = []
    if normalized_query:
        pieces.append(normalized_query)

    seen_norm_phrases = set()
    if normalized_query:
        seen_norm_phrases.add(normalize_alias_text(normalized_query))

    for _term_type, terms in expansion_terms.items():
        for t in terms:
            st = str(t).strip()
            if not st:
                continue

            norm = normalize_alias_text(st)
            if not norm:
                continue

            # normalized query 전체 문자열과 완전히 같으면 skip
            if norm in seen_norm_phrases:
                continue

            # 이미 pieces 안에 같은 phrase가 있으면 skip
            duplicated = False
            for p in pieces:
                if normalize_alias_text(p) == norm:
                    duplicated = True
                    break
            if duplicated:
                continue

            pieces.append(st)
            seen_norm_phrases.add(norm)

    merged = " ".join(pieces)
    merged = re.sub(r"\s+", " ", merged).strip()
    return merged or original_query


def normalize_and_expand_query(
    query_text: str,
    scope_candidates: List[str] | None = None,
) -> Dict[str, Any]:
    rows = load_term_dictionary(scope_candidates=scope_candidates)
    term_entries = build_term_entries(rows)

    detected_terms = detect_terms_in_query(query_text, term_entries)
    normalized_query = apply_canonical_rewrite(query_text, detected_terms)
    expansion_terms = build_expansion_terms(detected_terms, term_entries)
    expanded_query = build_expanded_query(
        original_query=query_text,
        normalized_query=normalized_query,
        expansion_terms=expansion_terms,
    )

    return {
        "original_query": query_text,
        "normalized_query": normalized_query,
        "expanded_query": expanded_query,
        "detected_terms": [
            {
                "term_id": d["term_id"],
                "term_type": d["term_type"],
                "canonical_name": d["canonical_name"],
                "matched_text": d["matched_text"],
                "alias_text": d["alias_text"],
                "matched_span": d["matched_span"],
                "priority": d["priority"],
                "expand_to_aliases": d["expand_to_aliases"],
                "search_boost": d["search_boost"],
                "is_verified": d["is_verified"],
                "match_source": d.get("match_source"),
                "match_length": d.get("match_length"),
                "expansion_policy": _get_expansion_policy(d["term_type"]),
            }
            for d in detected_terms
        ],
        "expansion_terms": expansion_terms,
    }