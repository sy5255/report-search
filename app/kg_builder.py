"""Knowledge Graph 빌더.

DB(v_ai_defect_search) · 문서(아카이브) · 용어사전(term_dictionary)을
결정적 규칙(LLM 미사용)으로 연결해 MySQL 조인 테이블 4개를 생성한다.

엣지:
  E1 kg_doc_report  : 문서↔보고서  (본문 Lot(#WF) 사전 매칭 주력 + EDM 링크 ID 토큰 보조)
                      ※ report_index는 문서 어디에도 없음(경로의 export_숫자는 메일 내보내기 ID로 DB와 무관)
  E2 kg_doc_term    : 문서↔용어    (기존 detect_terms_in_query를 본문에 실행)
  E3 kg_report_term : 보고서↔용어  (DB 컬럼값 ↔ canonical/alias 매칭)
  E4 kg_term_edge   : 용어↔용어    (E2/E3 self-join 동시출현, materialized)

실행: 서버 기동 시 백그라운드 + 24h 주기 (main.py) / 수동: python -m app.kg_builder --force
"""
import os
import re
import sys
import json
import time
import threading
from datetime import datetime, timedelta

from app.db import get_conn
from app.archive_loader import get_local_archive_docs, PROCESSED_JSON_PATH
from app.query_normalizer import (
    load_term_dictionary,
    build_term_entries,
    detect_terms_in_query,
    normalize_alias_text,
)

# E1: 본문 토큰화 (Lot 사전 매칭용) — 영숫자 시작, 4자 이상 원시 토큰
DOC_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{3,}")
LOT_MIN_NORM_LEN = 5       # 정규화 후 이 길이 미만인 Lot은 오탐 위험으로 사전에서 제외
LOT_WF_WINDOW = 80         # Lot 출현 위치 주변에서 WF_ID를 찾는 범위(문자)
MAX_REPORTS_PER_LOT = 10   # 하나의 Lot이 과다한 report에 물리면 공용/더미 Lot으로 보고 스킵
# EDM URL의 진짜 문서 식별자(fileid): .../verLink/<18자리>/<버전> 에서 verLink 뒤 숫자만 캡처.
# 끝의 /버전, 쿼리 파라미터, 짧은 숫자, viewer/<짧은코드> 형태는 제외됨.
EDM_FILEID_RE = re.compile(r"verLink/(\d{10,})")
MAX_REPORTS_PER_EDM_FILEID = 20  # safety cap (18자리 정확 매칭이라 사실상 거의 안 걸림)

# E2 본문 텍스트 매칭을 허용하는 용어 유형.
# owner(사람 이름)는 일반 명사와 충돌하는 오탐이 많아 본문 매칭에서 제외하고
# mail_from 필드 exact 매칭으로만 연결한다.
TEXT_MATCH_TERM_TYPES = {"defect", "chemistry", "node", "process", "product", "equipment", "analysis", "acronym"}

# 문서 1건당 본문 매칭 최대 길이 (성능 상한)
DOC_TEXT_MATCH_LIMIT = 100_000

# E3: DB 컬럼 → (term_type, 매칭 방식, confidence)
REPORT_COLUMN_TERM_MAP = [
    ("불량명",     "defect",    "contains",  0.7),
    ("성분",       "chemistry", "csv_exact", 1.0),
    ("공정노드",   "node",      "exact",     1.0),
    ("모듈",       "process",   "exact",     1.0),
    ("공정명",     "process",   "contains",  0.7),
    ("설비명",     "equipment", "contains",  0.7),
    ("분석담당자", "owner",     "exact",     1.0),
    ("의뢰자명",   "owner",     "exact",     1.0),
]

REBUILD_INTERVAL_SEC = 86400  # 24h
_BUILD_LOCK = threading.Lock()


# =====================================================================
# DDL
# =====================================================================
def ensure_kg_tables():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS kg_doc_report (
            doc_id VARCHAR(255) NOT NULL,
            report_index VARCHAR(64) NOT NULL,
            source ENUM('lot_wf','lot','edm_token') NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            evidence VARCHAR(255) NULL,
            PRIMARY KEY (doc_id, report_index),
            INDEX idx_report (report_index)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        try:
            cur.execute("ALTER TABLE kg_doc_report ADD COLUMN evidence VARCHAR(255) NULL;")
        except Exception:
            pass
        cur.execute("""
        CREATE TABLE IF NOT EXISTS kg_doc_term (
            doc_id VARCHAR(255) NOT NULL,
            term_id INT NOT NULL,
            freq INT NOT NULL DEFAULT 1,
            sample_alias VARCHAR(255) NULL,
            PRIMARY KEY (doc_id, term_id),
            INDEX idx_term (term_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS kg_report_term (
            report_index VARCHAR(64) NOT NULL,
            term_id INT NOT NULL,
            src_col VARCHAR(32) NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            PRIMARY KEY (report_index, term_id, src_col),
            INDEX idx_term (term_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS kg_term_edge (
            term_a INT NOT NULL,
            term_b INT NOT NULL,
            co_doc_count INT NOT NULL DEFAULT 0,
            co_report_count INT NOT NULL DEFAULT 0,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (term_a, term_b)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS kg_build_state (
            id TINYINT PRIMARY KEY,
            last_built_at DATETIME NULL,
            docs_indexed INT NOT NULL DEFAULT 0,
            reports_indexed INT NOT NULL DEFAULT 0,
            processed_mtime DOUBLE NOT NULL DEFAULT 0
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        conn.commit()
    finally:
        cur.close()
        conn.close()


# =====================================================================
# E1: 문서 ↔ 보고서 (본문 Lot 사전 매칭 주력 + EDM 링크 ID 토큰 보조)
# =====================================================================
def _norm_token(s: str) -> str:
    """토큰 정규화: 영숫자만 남기고 대문자화 (Lot#WF, Lot #WF, 줄바꿈 등 표기 변형 흡수)."""
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def build_lot_map(report_rows: list) -> dict:
    """{정규화된 Lot_ID: [(WF_ID 원문, report_index), ...]}"""
    lot_map = {}
    for r in report_rows:
        ridx = str(r.get("report_index") or "").strip()
        if not ridx:
            continue
        lot = _norm_token(r.get("Lot_ID"))
        if len(lot) < LOT_MIN_NORM_LEN:
            continue
        wf = str(r.get("WF_ID") or "").strip()
        lot_map.setdefault(lot, []).append((wf, ridx))
    return lot_map


def build_doc_report_edges_by_lot(docs: list, lot_map: dict) -> list:
    """R1(주력): 문서 본문 토큰과 DB Lot_ID 사전의 교집합으로 연결.

    - 본문 표기가 Lot#WF / Lot #WF / 줄바꿈 분리 등 제각각이므로 정규식으로 형식을
      추측하지 않고, 실제 Lot_ID 값 집합과 정규화 토큰을 대조한다.
    - Lot 출현 위치 ±LOT_WF_WINDOW 원문에서 해당 Lot의 WF_ID가 발견되면 (Lot,WF)로
      정밀화(source='lot_wf', 0.95), 아니면 그 Lot의 모든 report와 연결(source='lot', 0.8).
    - evidence에 매칭 근거(어떤 Lot/WF로 연결됐는지)를 기록해 웹에서 정합 추적 가능하게 한다.
    반환: [(doc_id, report_index, source, confidence, evidence)]
    """
    best = {}

    def put(doc_id, ridx, source, conf, evidence):
        key = (doc_id, str(ridx))
        prev = best.get(key)
        if prev is None or conf > prev[1]:
            best[key] = (source, conf, str(evidence or "")[:255])

    lot_keys = set(lot_map.keys())

    for d in docs:
        doc_id = d.get("doc_id")
        if not doc_id:
            continue
        text = f"{d.get('title') or ''}\n{(d.get('raw_content') or '')[:DOC_TEXT_MATCH_LIMIT]}"

        # 본문 토큰의 정규화값 → 원문 위치 목록
        token_positions = {}
        for m in DOC_TOKEN_RE.finditer(text):
            norm = _norm_token(m.group(0))
            if len(norm) >= LOT_MIN_NORM_LEN:
                token_positions.setdefault(norm, []).append((m.start(), m.end()))

        for lot in (set(token_positions.keys()) & lot_keys):
            entries = lot_map[lot]
            report_ids = {ridx for (_, ridx) in entries}
            if len(report_ids) > MAX_REPORTS_PER_LOT:
                continue  # 공용/더미 Lot 오탐 가드

            # WF 정밀화: Lot 출현 위치 주변 원문에서 WF_ID 경계 매칭
            wf_hits = {}  # report_index -> wf
            for wf, ridx in entries:
                if not wf:
                    continue
                pat = re.compile(rf"(?<![0-9A-Za-z]){re.escape(wf)}(?![0-9A-Za-z])", re.IGNORECASE)
                for (s, e) in token_positions[lot]:
                    window = text[max(0, s - LOT_WF_WINDOW): e + LOT_WF_WINDOW]
                    if pat.search(window):
                        wf_hits[ridx] = wf
                        break

            if wf_hits:
                for ridx, wf in wf_hits.items():
                    put(doc_id, ridx, "lot_wf", 0.95, f"Lot {lot} + WF {wf}")
            else:
                for ridx in report_ids:
                    put(doc_id, ridx, "lot", 0.8, f"Lot {lot}")

    return [(doc_id, ridx, src, conf, ev) for (doc_id, ridx), (src, conf, ev) in best.items()]


def build_edm_fileid_map(report_rows: list) -> dict:
    """{보고서링크의 verLink fileid: set(report_index)} — 과다 매핑 fileid는 제외(safety)."""
    fid_map = {}
    for r in report_rows:
        ridx = str(r.get("report_index") or "").strip()
        link = str(r.get("보고서링크") or "")
        if not ridx or not link:
            continue
        for fid in EDM_FILEID_RE.findall(link):
            fid_map.setdefault(fid, set()).add(ridx)
    return {f: rs for f, rs in fid_map.items() if len(rs) <= MAX_REPORTS_PER_EDM_FILEID}


def build_doc_report_edges_by_edm(docs: list, edm_fileid_map: dict, already_linked: set) -> list:
    """R2(보조): 문서에 가끔 DB와 동일한 edm2/verLink 링크가 들어오는 경우, 그 안의 fileid(18자리)를
    DB 보고서링크의 fileid와 정확 매칭해 연결한다. 짧은 코드형(viewer/xxxx)은 fileid가 없어 제외.
    R1(Lot)이 이미 연결한 쌍에는 적용하지 않는다.
    반환: [(doc_id, report_index, 'edm_token', 0.85, evidence)]
    """
    seen = set()
    out = []
    for d in docs:
        doc_id = d.get("doc_id")
        if not doc_id:
            continue
        for link in (d.get("report_links") or []):
            for fid in EDM_FILEID_RE.findall(str(link or "")):
                for ridx in edm_fileid_map.get(fid, ()):
                    key = (doc_id, ridx)
                    if key in already_linked or key in seen:
                        continue
                    seen.add(key)
                    out.append((doc_id, ridx, "edm_token", 0.85, f"EDM fileid {fid}"))
    return out


# =====================================================================
# E2: 문서 ↔ 용어
# =====================================================================
def build_doc_term_edges(docs: list, term_entries: list) -> list:
    """반환: [(doc_id, term_id, freq, sample_alias)]"""
    text_entries = [t for t in term_entries if t.get("term_type") in TEXT_MATCH_TERM_TYPES]

    # owner: mail_from exact 매칭용 lookup (정규화 alias → term_id)
    owner_lookup = {}
    for t in term_entries:
        if t.get("term_type") != "owner":
            continue
        for a in (t.get("aliases") or []):
            key = normalize_alias_text(a.get("alias_text") or "")
            if key:
                owner_lookup[key] = t["term_id"]

    # ⚠️ doc_id는 md 파일명이라 서로 다른 폴더의 문서가 같은 doc_id를 가질 수 있다
    # (같은 제목의 메일 등). PK(doc_id, term_id) 충돌을 막기 위해 corpus 전체에서
    # (doc_id, term_id) 단위로 병합 집계한다 (freq 합산, sample은 최초값 유지).
    agg = {}  # (doc_id, term_id) -> [freq, sample_alias]
    for d in docs:
        doc_id = d.get("doc_id")
        if not doc_id:
            continue
        text = f"{d.get('title') or ''}\n{(d.get('raw_content') or '')[:DOC_TEXT_MATCH_LIMIT]}"

        counts = {}   # term_id -> [freq, sample_alias]
        try:
            for m in detect_terms_in_query(text, text_entries):
                tid = m["term_id"]
                if tid in counts:
                    counts[tid][0] += 1
                else:
                    counts[tid] = [1, (m.get("matched_text") or "")[:255]]
        except Exception as e:
            print(f"[KG] 용어 매칭 실패 doc={doc_id}: {e}")

        # owner 매칭: mail_from은 "이름 <메일>" 전체 문자열이라 사전 alias("이름")와
        # exact 불일치했음 → 전체/이름부/이메일부 3가지 키로 시도
        mf = str(d.get("mail_from") or "")
        name_part = mf.split("<")[0].strip()
        email_part = mf[mf.find("<") + 1: mf.find(">")].strip() if ("<" in mf and ">" in mf) else ""
        owner_tid = (owner_lookup.get(normalize_alias_text(mf))
                     or owner_lookup.get(normalize_alias_text(name_part))
                     or owner_lookup.get(normalize_alias_text(email_part)))
        if owner_tid and owner_tid not in counts:
            counts[owner_tid] = [1, mf[:255]]

        for tid, (freq, sample) in counts.items():
            key = (doc_id, tid)
            if key in agg:
                agg[key][0] += freq
            else:
                agg[key] = [freq, sample]

    return [(doc_id, tid, freq, sample) for (doc_id, tid), (freq, sample) in agg.items()]


# =====================================================================
# E3: 보고서 ↔ 용어
# =====================================================================
def fetch_report_rows() -> list:
    cols = "report_index, Lot_ID, WF_ID, 불량명, 성분, 공정노드, 모듈, 공정명, 설비명, 분석담당자, 의뢰자명, 보고서링크"
    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(f"SELECT DISTINCT {cols} FROM v_ai_defect_search")
        return cur.fetchall() or []
    finally:
        cur.close()
        conn.close()


def _build_term_lookups(term_entries: list) -> dict:
    """유형별 매칭용 lookup 구성.
    exact: {norm_alias: term_id} / contains: [(norm_alias, term_id)] (길이 2 이상만)
    """
    lookups = {}
    for t in term_entries:
        ttype = t.get("term_type")
        lk = lookups.setdefault(ttype, {"exact": {}, "contains": []})
        for a in (t.get("aliases") or []):
            norm = normalize_alias_text(a.get("alias_text") or "")
            if not norm:
                continue
            lk["exact"].setdefault(norm, t["term_id"])
            if len(norm) >= 2:
                lk["contains"].append((norm, t["term_id"]))
    return lookups


def build_report_term_edges(rows: list, term_entries: list) -> list:
    """반환: [(report_index, term_id, src_col, confidence)]"""
    lookups = _build_term_lookups(term_entries)
    seen = set()
    out = []

    def put(ridx, tid, col, conf):
        key = (str(ridx), tid, col)
        if key in seen:
            return
        seen.add(key)
        out.append((str(ridx), tid, col, conf))

    for row in rows:
        ridx = row.get("report_index")
        if ridx in (None, ""):
            continue
        for col, ttype, mode, conf in REPORT_COLUMN_TERM_MAP:
            val = str(row.get(col) or "").strip()
            if not val:
                continue
            lk = lookups.get(ttype)
            if not lk:
                continue

            if mode == "exact":
                tid = lk["exact"].get(normalize_alias_text(val))
                if tid:
                    put(ridx, tid, col, conf)
            elif mode == "csv_exact":
                for part in val.split(","):
                    tid = lk["exact"].get(normalize_alias_text(part))
                    if tid:
                        put(ridx, tid, col, conf)
            elif mode == "contains":
                nval = normalize_alias_text(val)
                for norm_alias, tid in lk["contains"]:
                    if norm_alias in nval:
                        put(ridx, tid, col, conf)
    return out


# =====================================================================
# 빌드 파이프라인
# =====================================================================
def _batch_insert(cur, sql: str, rows: list, batch: int = 1000):
    for i in range(0, len(rows), batch):
        cur.executemany(sql, rows[i:i + batch])


def build_graph(force: bool = False) -> dict:
    started = time.time()
    ensure_kg_tables()

    # 소스 로드
    docs = get_local_archive_docs() or []
    term_rows = load_term_dictionary(scope_candidates=["all", "inline_fa_report"])
    term_entries = build_term_entries(term_rows)

    report_rows = []
    try:
        report_rows = fetch_report_rows()
    except Exception as e:
        print(f"[KG] v_ai_defect_search 조회 실패 (E1/E3 스킵 — 문서↔용어 엣지만 빌드): {e}")

    # 엣지 계산
    # E1: Lot 사전 매칭(주력) → EDM 링크 ID 토큰(보조, 미연결 쌍만)
    lot_map = build_lot_map(report_rows)
    e1_lot = build_doc_report_edges_by_lot(docs, lot_map)
    already_linked = {(doc_id, ridx) for (doc_id, ridx, _, _, _) in e1_lot}
    edm_fileid_map = build_edm_fileid_map(report_rows)
    e1 = e1_lot + build_doc_report_edges_by_edm(docs, edm_fileid_map, already_linked)

    e2 = build_doc_term_edges(docs, term_entries)
    e3 = build_report_term_edges(report_rows, term_entries)

    # 저장 (전체 재빌드: DELETE 후 배치 insert)
    conn = get_conn()
    cur = conn.cursor()
    try:
        # INSERT IGNORE: doc_id(파일명) 중복 등 예기치 못한 PK 충돌이 있어도 빌드 전체가 죽지 않게
        cur.execute("DELETE FROM kg_doc_report")
        _batch_insert(cur, "INSERT IGNORE INTO kg_doc_report (doc_id, report_index, source, confidence, evidence) VALUES (%s,%s,%s,%s,%s)", e1)

        cur.execute("DELETE FROM kg_doc_term")
        _batch_insert(cur, "INSERT IGNORE INTO kg_doc_term (doc_id, term_id, freq, sample_alias) VALUES (%s,%s,%s,%s)", e2)

        cur.execute("DELETE FROM kg_report_term")
        _batch_insert(cur, "INSERT IGNORE INTO kg_report_term (report_index, term_id, src_col, confidence) VALUES (%s,%s,%s,%s)", e3)

        # E4: 동시출현 materialize
        cur.execute("DELETE FROM kg_term_edge")
        cur.execute("""
            INSERT INTO kg_term_edge (term_a, term_b, co_doc_count, co_report_count, updated_at)
            SELECT a.term_id, b.term_id, COUNT(*), 0, NOW()
            FROM kg_doc_term a
            JOIN kg_doc_term b ON a.doc_id = b.doc_id AND a.term_id < b.term_id
            GROUP BY a.term_id, b.term_id
        """)
        cur.execute("""
            INSERT INTO kg_term_edge (term_a, term_b, co_doc_count, co_report_count, updated_at)
            SELECT a.term_id, b.term_id, 0, COUNT(DISTINCT a.report_index), NOW()
            FROM kg_report_term a
            JOIN kg_report_term b ON a.report_index = b.report_index AND a.term_id < b.term_id
            GROUP BY a.term_id, b.term_id
            ON DUPLICATE KEY UPDATE
                co_report_count = VALUES(co_report_count),
                updated_at = NOW()
        """)

        # 빌드 상태 갱신
        mtime = 0.0
        try:
            if PROCESSED_JSON_PATH.exists():
                mtime = os.path.getmtime(PROCESSED_JSON_PATH)
        except Exception:
            pass
        cur.execute("""
            INSERT INTO kg_build_state (id, last_built_at, docs_indexed, reports_indexed, processed_mtime)
            VALUES (1, NOW(), %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                last_built_at = NOW(),
                docs_indexed = VALUES(docs_indexed),
                reports_indexed = VALUES(reports_indexed),
                processed_mtime = VALUES(processed_mtime)
        """, (len(docs), len(report_rows), mtime))

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    summary = {
        "docs": len(docs),
        "report_rows": len(report_rows),
        "doc_report_edges": len(e1),
        "doc_term_edges": len(e2),
        "report_term_edges": len(e3),
        "elapsed_sec": round(time.time() - started, 1),
    }
    print("-" * 50)
    print("[KG Build] 완료")
    for k, v in summary.items():
        print(f"  - {k}: {v}")
    print("-" * 50)
    return summary


def maybe_rebuild() -> bool:
    """24h 경과 또는 processed.json 변경 시에만 빌드. 동시 실행 방지."""
    if not _BUILD_LOCK.acquire(blocking=False):
        return False
    try:
        state = None
        try:
            conn = get_conn()
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT last_built_at, processed_mtime FROM kg_build_state WHERE id=1")
            state = cur.fetchone()
            cur.close()
            conn.close()
        except Exception:
            pass

        current_mtime = 0.0
        try:
            if PROCESSED_JSON_PATH.exists():
                current_mtime = os.path.getmtime(PROCESSED_JSON_PATH)
        except Exception:
            pass

        if state and state.get("last_built_at"):
            fresh = datetime.now() - state["last_built_at"] < timedelta(seconds=REBUILD_INTERVAL_SEC)
            same_src = float(state.get("processed_mtime") or 0) == current_mtime
            if fresh and same_src:
                return False

        build_graph()
        return True
    except Exception as e:
        print(f"[KG] 자동 빌드 실패 (기존 그래프 유지): {e}")
        return False
    finally:
        _BUILD_LOCK.release()


# =====================================================================
# 상태 점검 리포트 (--report): 빌드 없이 현재 그래프 상태 + 샘플 정합 재검증
# =====================================================================
def _recheck_pair(doc: dict, report_index: str, source: str,
                  lots_by_report: dict, edm_fileids_by_report: dict) -> bool:
    """샘플 연결쌍의 근거가 문서에 실제 존재하는지 재검증 (precision 스팟 체크)."""
    text = f"{doc.get('title') or ''}\n{(doc.get('raw_content') or '')[:DOC_TEXT_MATCH_LIMIT]}"

    if source in ("lot", "lot_wf"):
        lots = lots_by_report.get(str(report_index)) or []
        if not lots:
            return False
        tokens = set()
        for m in DOC_TOKEN_RE.finditer(text):
            norm = _norm_token(m.group(0))
            if len(norm) >= LOT_MIN_NORM_LEN:
                tokens.add(norm)
        return any(_norm_token(l) in tokens for l in lots)

    if source == "edm_token":
        doc_fids = set()
        for link in (doc.get("report_links") or []):
            doc_fids.update(EDM_FILEID_RE.findall(str(link or "")))
        return bool(doc_fids & (edm_fileids_by_report.get(str(report_index)) or set()))

    return False


def print_report(sample_n: int = 10):
    from app.eval_repo import get_kg_stats

    stats = get_kg_stats()
    built = stats.get("built")

    print("=" * 60)
    print("[KG Report] Knowledge Graph 상태 점검")
    print("=" * 60)

    if not built or not built.get("last_built_at"):
        print("아직 빌드된 그래프가 없습니다. 먼저 실행하세요: python -m app.kg_builder --force")
        return

    print(f"마지막 빌드      : {built['last_built_at']}")
    print(f"색인 문서        : {built['docs_indexed']:,}개 / 색인 DB 행: {built['reports_indexed']:,}개")
    e = stats["edges"]
    print(f"엣지             : 문서↔보고서 {e['doc_report']:,} · 문서↔용어 {e['doc_term']:,} · "
          f"보고서↔용어 {e['report_term']:,} · 용어동시출현 {e['term_edge']:,}")
    c = stats["coverage"]
    fmt_pct = lambda v: f"{v*100:.1f}%" if v is not None else "-"
    print(f"커버리지         : 문서→보고서 {fmt_pct(c['docs_linked_report_pct'])} · "
          f"문서→용어 {fmt_pct(c['docs_with_terms_pct'])}")

    conn = get_conn()
    cur = conn.cursor(dictionary=True)
    try:
        print("-" * 60)
        print("연결 소스 분포 (신뢰도: lot_wf > lot > edm_token):")
        cur.execute("SELECT source, COUNT(*) c, ROUND(AVG(confidence),2) conf FROM kg_doc_report GROUP BY source ORDER BY c DESC")
        for r in cur.fetchall() or []:
            print(f"  - {r['source']:<10}: {int(r['c']):>7,}건 (avg conf {r['conf']})")

        print("-" * 60)
        print("과연결 상위 문서 (한 문서가 지나치게 많은 report에 물리면 오탐 의심):")
        cur.execute("SELECT doc_id, COUNT(*) c FROM kg_doc_report GROUP BY doc_id ORDER BY c DESC LIMIT 10")
        for r in cur.fetchall() or []:
            print(f"  - {int(r['c']):>4}건  {r['doc_id'][:70]}")

        # ── 무작위 샘플 정합 재검증 ──
        print("-" * 60)
        print(f"무작위 연결 샘플 {sample_n}쌍 정합 재검증:")
        cur.execute(f"SELECT doc_id, report_index, source, confidence, evidence FROM kg_doc_report ORDER BY RAND() LIMIT {int(sample_n)}")
        samples = cur.fetchall() or []
        if not samples:
            print("  (연결 엣지가 없습니다)")
            return

        ridxs = sorted({str(s["report_index"]) for s in samples})
        placeholders = ",".join(["%s"] * len(ridxs))
        lots_by_report, edm_fileids_by_report = {}, {}
        try:
            cur.execute(f"SELECT DISTINCT report_index, Lot_ID, 보고서링크 FROM v_ai_defect_search "
                        f"WHERE report_index IN ({placeholders})", tuple(ridxs))
            for r in cur.fetchall() or []:
                key = str(r["report_index"])
                if r.get("Lot_ID"):
                    lots_by_report.setdefault(key, []).append(str(r["Lot_ID"]))
                for fid in EDM_FILEID_RE.findall(str(r.get("보고서링크") or "")):
                    edm_fileids_by_report.setdefault(key, set()).add(fid)
        except Exception as ex:
            print(f"  (DB 뷰 조회 실패로 재검증 불가: {ex})")
            return

        docs_map = {d["doc_id"]: d for d in (get_local_archive_docs() or [])}
        ok = 0
        for s in samples:
            doc = docs_map.get(s["doc_id"])
            if doc is None:
                verdict = "?"
                note = "(문서 캐시에 없음)"
            else:
                good = _recheck_pair(doc, str(s["report_index"]), s["source"], lots_by_report, edm_fileids_by_report)
                verdict = "O" if good else "X"
                ok += 1 if good else 0
                title = (doc.get("title") or s["doc_id"])[:40]
                note = f"{title}"
            ev = s.get("evidence") or ""
            print(f"  [{verdict}] report {s['report_index']:<8} {s['source']:<9} conf {s['confidence']:.2f}  {note}"
                  + (f"  [근거: {ev}]" if ev else ""))

        print("-" * 60)
        print(f"샘플 정합률: {ok}/{len(samples)}  (X가 2개 이상이면 MAX_REPORTS_PER_LOT/LOT_MIN_NORM_LEN 튜닝 권장)")
    finally:
        cur.close()
        conn.close()


def start_background_rebuild():
    """서버 기동 시 호출: 즉시 1회 시도 후 1시간 간격으로 24h 신선도 검사."""
    def loop():
        while True:
            maybe_rebuild()
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True, name="kg-rebuild").start()


if __name__ == "__main__":
    if "--report" in sys.argv:
        n = 10
        if "--sample" in sys.argv:
            try:
                n = max(1, min(100, int(sys.argv[sys.argv.index("--sample") + 1])))
            except Exception:
                pass
        print_report(sample_n=n)
    elif "--force" in sys.argv:
        build_graph(force=True)
    else:
        if not maybe_rebuild():
            print("[KG] 최신 상태입니다. 강제 재빌드는 --force, 상태 점검은 --report 옵션을 사용하세요.")
