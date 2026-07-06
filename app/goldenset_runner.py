"""골든셋 오프라인 평가 러너.

대표 질문 + 기대 정답(goldenset.json)으로 검색 recall · 인텐트 라우팅 정확도 · 용어 감지율을 측정하고
결과를 eval_goldenset_runs 테이블에 남긴다. 실서버에서만 동작(ES/DB 필요).

실행: python -m app.goldenset_runner [--user <id>] [--k 10] [--no-intent]
"""
import os
import sys
import json
import uuid
from pathlib import Path
from datetime import datetime

from app.db import get_conn
from app.config import DEFAULT_INDEX_NAME
from app.query_normalizer import normalize_and_expand_query
from app.rag_client import rag_retrieve_rrf

GOLDENSET_PATH = Path(__file__).resolve().parent.parent / "goldenset.json"


# =====================================================================
# 순수 채점 로직 (LLM/DB 없이 단위 테스트 가능)
# =====================================================================
def score_item(item: dict, retrieved_ids: list, detected_canonicals: list,
               router_intent: str | None) -> dict:
    """한 문항의 채점 결과. 각 축은 기대값이 있을 때만 채점(scored 플래그).
    retrieved_ids: 검색 결과 doc_id 순위 리스트(0-index=1위).
    """
    res = {
        "id": item.get("id"),
        "question": item.get("question"),
        "scored_retrieval": False, "hit1": None, "hit5": None, "hit10": None, "found_rank": None,
        "scored_intent": False, "intent_ok": None, "expected_intent": item.get("expected_intent"),
        "router_intent": router_intent,
        "scored_terms": False, "term_rate": None,
        "detected": detected_canonicals,
    }

    expected_docs = [str(x) for x in (item.get("expected_doc_ids") or []) if str(x).strip()]
    if expected_docs:
        res["scored_retrieval"] = True
        exp = set(expected_docs)
        rank = None
        for i, rid in enumerate(retrieved_ids):
            if str(rid) in exp:
                rank = i + 1
                break
        res["found_rank"] = rank
        res["hit1"] = bool(rank and rank <= 1)
        res["hit5"] = bool(rank and rank <= 5)
        res["hit10"] = bool(rank and rank <= 10)

    exp_intent = (item.get("expected_intent") or "").strip()
    if exp_intent and router_intent is not None:
        res["scored_intent"] = True
        res["intent_ok"] = (router_intent == exp_intent)

    expected_terms = [str(x).strip() for x in (item.get("expected_terms") or []) if str(x).strip()]
    if expected_terms:
        res["scored_terms"] = True
        det = set(detected_canonicals or [])
        found = sum(1 for t in expected_terms if t in det)
        res["term_rate"] = round(found / len(expected_terms), 3)

    return res


def aggregate(item_results: list) -> dict:
    def _rate(flagkey, okkey):
        rows = [r for r in item_results if r.get(flagkey)]
        if not rows:
            return None, 0
        vals = [1 if r.get(okkey) else 0 for r in rows]
        return round(sum(vals) / len(vals), 3), len(rows)

    hit1, n_ret = _rate("scored_retrieval", "hit1")
    hit5, _ = _rate("scored_retrieval", "hit5")
    hit10, _ = _rate("scored_retrieval", "hit10")

    ret_rows = [r for r in item_results if r.get("scored_retrieval")]
    mrr = None
    if ret_rows:
        rr = [(1.0 / r["found_rank"]) if r.get("found_rank") else 0.0 for r in ret_rows]
        mrr = round(sum(rr) / len(rr), 3)

    intent_acc, n_int = _rate("scored_intent", "intent_ok")

    term_rows = [r for r in item_results if r.get("scored_terms")]
    term_rate = None
    if term_rows:
        term_rate = round(sum(r["term_rate"] for r in term_rows) / len(term_rows), 3)

    return {
        "total": len(item_results),
        "hit_at_1": hit1, "hit_at_5": hit5, "hit_at_10": hit10, "mrr": mrr,
        "intent_accuracy": intent_acc, "term_detect_rate": term_rate,
        "scored_retrieval": n_ret, "scored_intent": n_int, "scored_terms": len(term_rows),
    }


# =====================================================================
# 실행 (DB/ES 필요)
# =====================================================================
def load_goldenset() -> list:
    if not GOLDENSET_PATH.exists():
        print(f"[Goldenset] {GOLDENSET_PATH} 파일이 없습니다.")
        return []
    try:
        data = json.load(open(GOLDENSET_PATH, encoding="utf-8"))
    except Exception as e:
        print(f"[Goldenset] JSON 파싱 실패: {e}")
        return []
    return [it for it in (data.get("items") or []) if it.get("enabled", True) and it.get("question")]


def ensure_eval_tables():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS eval_goldenset_runs (
            run_id VARCHAR(36) PRIMARY KEY,
            created_at DATETIME NOT NULL,
            total INT NOT NULL DEFAULT 0,
            hit_at_1 FLOAT NULL, hit_at_5 FLOAT NULL, hit_at_10 FLOAT NULL, mrr FLOAT NULL,
            intent_accuracy FLOAT NULL, term_detect_rate FLOAT NULL,
            scored_retrieval INT NOT NULL DEFAULT 0,
            scored_intent INT NOT NULL DEFAULT 0,
            scored_terms INT NOT NULL DEFAULT 0,
            summary_json JSON NULL,
            INDEX idx_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        conn.commit()
    finally:
        cur.close()
        conn.close()


def _retrieved_doc_ids(query: str, k: int) -> tuple:
    """(doc_id 순위 리스트, 상위 제목 리스트)"""
    try:
        resp = rag_retrieve_rrf(DEFAULT_INDEX_NAME, query, top_k=k)
        hits = (resp.get("hits", {}) or {}).get("hits", []) or []
    except Exception as e:
        print(f"[Goldenset] 검색 실패: {e}")
        return [], []
    ids, titles = [], []
    for h in hits:
        src = h.get("_source", {}) or {}
        ids.append(src.get("doc_id"))
        titles.append(src.get("title"))
    return ids, titles


def run(user_id: str = "goldenset_eval", k: int = 10, do_intent: bool = True) -> dict:
    ensure_eval_tables()
    items = load_goldenset()
    if not items:
        print("[Goldenset] 평가할 문항이 없습니다. goldenset.json에 enabled=true 문항을 추가하세요.")
        return {}

    client = None
    if do_intent and any((it.get("expected_intent") or "").strip() for it in items):
        try:
            from app.llm_client import _make_client
            client = _make_client(user_id)
        except Exception as e:
            print(f"[Goldenset] LLM 클라이언트 생성 실패(인텐트 평가 스킵): {e}")

    item_results = []
    for it in items:
        q = it["question"]
        try:
            norm = normalize_and_expand_query(query_text=q, scope_candidates=["all", "inline_fa_report"])
        except Exception as e:
            print(f"[Goldenset] 정규화 실패 q={q[:30]}: {e}")
            norm = {"expanded_query": q, "detected_terms": []}
        detected = [d.get("canonical_name") for d in (norm.get("detected_terms") or []) if d.get("canonical_name")]
        retrieval_query = (norm.get("expanded_query") or q).strip() or q

        retrieved_ids, top_titles = ([], [])
        if it.get("expected_doc_ids"):
            retrieved_ids, top_titles = _retrieved_doc_ids(retrieval_query, k)

        router_intent = None
        if client is not None and (it.get("expected_intent") or "").strip():
            try:
                from app.agent import _call_intent_router
                router_intent = _call_intent_router(client, q)
            except Exception as e:
                print(f"[Goldenset] 라우팅 실패 q={q[:30]}: {e}")

        r = score_item(it, retrieved_ids, detected, router_intent)
        r["top_titles"] = top_titles[:5]
        r["retrieval_query"] = retrieval_query
        item_results.append(r)

    summary = aggregate(item_results)
    run_id = str(uuid.uuid4())
    now = datetime.now()

    # 영속화
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO eval_goldenset_runs
            (run_id, created_at, total, hit_at_1, hit_at_5, hit_at_10, mrr,
             intent_accuracy, term_detect_rate, scored_retrieval, scored_intent, scored_terms, summary_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (run_id, now, summary["total"], summary["hit_at_1"], summary["hit_at_5"],
              summary["hit_at_10"], summary["mrr"], summary["intent_accuracy"], summary["term_detect_rate"],
              summary["scored_retrieval"], summary["scored_intent"], summary["scored_terms"],
              json.dumps({"summary": summary, "items": item_results}, ensure_ascii=False)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[Goldenset] 결과 저장 실패(콘솔 리포트만 출력): {e}")

    _print_report(summary, item_results)
    return {"run_id": run_id, "summary": summary}


def _fmt(v):
    return f"{v*100:.1f}%" if isinstance(v, (int, float)) else "-"


def _print_report(summary, items):
    print("=" * 62)
    print("[Goldenset] 오프라인 평가 결과")
    print("=" * 62)
    print(f"문항 수            : {summary['total']}")
    print(f"검색 hit@1/5/10    : {_fmt(summary['hit_at_1'])} / {_fmt(summary['hit_at_5'])} / {_fmt(summary['hit_at_10'])}"
          f"  (채점 {summary['scored_retrieval']}문항)")
    print(f"검색 MRR           : {summary['mrr'] if summary['mrr'] is not None else '-'}")
    print(f"인텐트 정확도      : {_fmt(summary['intent_accuracy'])}  (채점 {summary['scored_intent']}문항)")
    print(f"용어 감지율        : {_fmt(summary['term_detect_rate'])}  (채점 {summary['scored_terms']}문항)")
    print("-" * 62)
    print("문항별 (검색 실패/인텐트 오분류 위주):")
    for r in items:
        flags = []
        if r["scored_retrieval"]:
            flags.append(f"검색 {'rank'+str(r['found_rank']) if r['found_rank'] else 'MISS'}")
        if r["scored_intent"]:
            flags.append(f"인텐트 {'O' if r['intent_ok'] else 'X('+str(r['router_intent'])+')'}")
        if r["scored_terms"]:
            flags.append(f"용어 {int((r['term_rate'] or 0)*100)}%")
        bad = (r["scored_retrieval"] and not r["hit5"]) or (r["scored_intent"] and not r["intent_ok"])
        mark = "⚠️" if bad else "  "
        print(f"  {mark} {(r['question'] or '')[:40]:<40} | {' · '.join(flags)}")
    print("=" * 62)


if __name__ == "__main__":
    args = sys.argv[1:]
    user = "goldenset_eval"
    k = 10
    do_intent = "--no-intent" not in args
    if "--user" in args:
        try: user = args[args.index("--user") + 1]
        except Exception: pass
    if "--k" in args:
        try: k = max(1, min(50, int(args[args.index("--k") + 1])))
        except Exception: pass
    run(user_id=user, k=k, do_intent=do_intent)
