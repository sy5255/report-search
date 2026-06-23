import json
import re
import mysql.connector
from typing import List, Optional

# 💡 기존 config.py에서 미리 정의해둔 DB 환경변수 임포트
from app.config import MYSQL_HOST, MYSQL_PORT, MYSQL_DB, MYSQL_USER, MYSQL_PASS

# 만약 app.db 파일 안에 이미 DB 연결 객체를 반환하는 함수(예: get_db_connection 등)가 
# 만들어져 있다면, 아래 함수 대신 그것을 바로 import 해서 사용하셔도 무방합니다.
def get_mysql_conn():
    return mysql.connector.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        database=MYSQL_DB,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        autocommit=False,
    )

def propose_term_candidate(
    user_id: str,
    candidate_kind: str,
    candidate_type: str,
    raw_text: str,
    canonical_name: Optional[str] = None,
    target_term_id: Optional[int] = None,
    aliases: Optional[List[str]] = None
) -> bool:
    conn = get_mysql_conn()
    cur = conn.cursor()
    try:
        aliases_json = json.dumps(aliases or [], ensure_ascii=False)
        
        # 정규화: 소문자화, 특수기호 공백 처리, 다중 공백 축약
        normalized_text = raw_text.strip().lower()
        normalized_text = normalized_text.replace("-", " ").replace("_", " ").replace("/", " ")
        normalized_text = re.sub(r"\s+", " ", normalized_text).strip()
        
        sql = """
        INSERT INTO term_candidate_queue (
            candidate_kind, candidate_type, raw_text, normalized_text,
            suggested_canonical, target_term_id, proposed_aliases_json,
            proposed_by, source_stage, status, review_status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 'pending')
        """
        
        cur.execute(sql, (
            candidate_kind,         # 'new_term' or 'alias_for_existing_term'
            candidate_type,         # 'defect', 'process', 'product' 등
            raw_text.strip(),
            normalized_text,
            canonical_name,
            target_term_id,
            aliases_json,
            user_id,                # 제안한 유저 ID 기록
            "web_ui"                # 웹 UI에서 들어왔음을 명시
        ))
        conn.commit()
        return True
    except Exception as e:
        print(f"[dictionary_repo] Error proposing term: {e}")
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()

def get_all_terms() -> List[dict]:
    """DB에서 정식 등록된 용어 사전 목록을 가져오는 함수"""
    conn = get_mysql_conn()
    cur = conn.cursor(dictionary=True) 
    try:
        # 💡 프론트엔드 모달에 띄워주기 위해 priority, search_boost, expand_to_aliases 도 같이 가져옵니다!
        sql = """
            SELECT 
                td.term_id, 
                td.term_type, 
                td.canonical_name, 
                td.description,
                td.priority,
                td.search_boost,
                td.expand_to_aliases,
                GROUP_CONCAT(ta.alias_text SEPARATOR ', ') as aliases
            FROM term_dictionary td
            LEFT JOIN term_aliases ta 
              ON td.term_id = ta.term_id AND ta.status = 'active'
            WHERE td.status = 'active'
            GROUP BY td.term_id
            ORDER BY td.term_type ASC, td.canonical_name ASC
        """
        cur.execute(sql)
        rows = cur.fetchall()
        return rows
    except Exception as e:
        print(f"[dictionary_repo] 사전 데이터 조회 실패: {e}")
        return []
    finally:
        cur.close()
        conn.close()


def get_pending_candidates(
    limit: int = 50, 
    offset: int = 0, 
    sort: str = "frequency",
    search: str = "",
    term_type: str = "all",
    source: str = "all"
) -> dict:
    """Admin 대기열: 서버사이드 필터링, 정렬, 그리고 대상 용어(Target Term) 조인 기능 추가"""
    conn = get_mysql_conn()
    cur = conn.cursor(dictionary=True)
    try:
        # 💡 테이블에 별칭(q, d)을 붙여 쿼리 충돌을 방지합니다.
        where_clauses = ["q.review_status = 'pending'"]
        args = []

        # 💡 [핵심 추가] 이미 정식 등록된 용어(표준어 및 동의어)는 대기열에서 제외
        # term_aliases 테이블에는 표준어(is_preferred=1)와 유의어가 모두 들어있으므로 이 테이블 하나만 검사해도 충분합니다.
        where_clauses.append("""
            NOT EXISTS (
                SELECT 1 FROM term_aliases ta 
                WHERE ta.alias_normalized = q.normalized_text 
                  AND ta.status = 'active'
            )
        """)

        if search:
            where_clauses.append("q.raw_text LIKE %s")
            args.append(f"%{search}%")
            
        if term_type and term_type != "all":
            where_clauses.append("q.candidate_type = %s")
            args.append(term_type)
            
        if source == "user":
            where_clauses.append("q.proposed_by IS NOT NULL AND q.proposed_by != '' AND q.proposed_by NOT LIKE '%build_serving%'")
        elif source == "system":
            where_clauses.append("(q.proposed_by IS NULL OR q.proposed_by = '' OR q.proposed_by LIKE '%build_serving%')")
            
        where_str = " AND ".join(where_clauses)

        # 전체 개수 카운트
        count_sql = f"SELECT COUNT(*) FROM term_candidate_queue q WHERE {where_str}"
        cur.execute(count_sql, tuple(args))
        row = cur.fetchone()
        total_count = list(row.values())[0] if row else 0

        # 정렬 기준 분기 (최신 제안순 vs 빈도순)
        if sort == "latest":
            order_by = "q.candidate_id DESC" 
        else:
            order_by = "q.detected_count DESC, q.candidate_id ASC" 

        # 💡 [핵심] LEFT JOIN을 사용해 정식 사전(term_dictionary)의 표준명(canonical_name)을 함께 가져옵니다.
        select_sql = f"""
            SELECT q.candidate_id, q.candidate_kind, q.candidate_type, q.raw_text, 
                   q.suggested_canonical, q.target_term_id, q.proposed_aliases_json, 
                   q.proposed_by, q.detected_count, q.confidence,
                   d.canonical_name AS target_canonical_name
            FROM term_candidate_queue q
            LEFT JOIN term_dictionary d ON q.target_term_id = d.term_id
            WHERE {where_str}
            ORDER BY {order_by}
            LIMIT {int(limit)} OFFSET {int(offset)}
        """
        cur.execute(select_sql, tuple(args))
        items = cur.fetchall()
        
        return {
            "total": total_count,
            "items": items if items else [],
            "limit": limit,
            "offset": offset
        }
    except Exception as e:
        print(f"🚨 [dictionary_repo] 대기열 로드 에러: {e}")
        return {"total": 0, "items": [], "limit": limit, "offset": offset}
    finally:
        cur.close()
        conn.close()

def approve_candidate(data: dict, admin_user_id: str) -> bool:
    """Admin 승인: 관리자가 수정한 값들로 큐 데이터를 덮어쓰고 상태를 approved로 변경 및 중복 큐 정리"""
    import json
    conn = get_mysql_conn()
    cur = conn.cursor()
    try:
        candidate_id = int(data.get("candidate_id"))
        
        # 💡 1. 승인할 단어의 정규화 텍스트(normalized_text)를 먼저 조회합니다.
        cur.execute("SELECT normalized_text FROM term_candidate_queue WHERE candidate_id = %s", (candidate_id,))
        row = cur.fetchone()
        norm_text = row[0] if row else None

        # 💡 2. 메인 단어 승인 처리 (기존 승인 로직 유지)
        aliases_json = json.dumps(data.get("aliases", []), ensure_ascii=False)
        
        sql_approve = """
            UPDATE term_candidate_queue
            SET review_status = 'approved',
                approved_term_type = %s,
                approved_canonical_name = %s,
                approved_priority = %s,
                approved_expand_to_aliases = %s,
                approved_search_boost = %s,
                proposed_aliases_json = %s,
                reviewed_by = %s,
                reviewed_at = CURRENT_TIMESTAMP
            WHERE candidate_id = %s
        """
        cur.execute(sql_approve, (
            data.get("approved_term_type"),
            data.get("approved_canonical_name"),
            int(data.get("approved_priority", 100)),
            int(data.get("approved_expand_to_aliases", 1)),
            float(data.get("approved_search_boost", 1.0)),
            aliases_json,
            admin_user_id,
            candidate_id
        ))
        
        # 💡 3. [고스트 트래킹 적용] 큐에 남아있는 동일한 단어(다른 카테고리 등)를 'pending'에서 'already_active'로 일괄 전환
        if norm_text:
            sql_clean_duplicates = """
                UPDATE term_candidate_queue 
                SET review_status = 'already_active', 
                    reviewed_by = 'system_auto_sync',
                    reviewed_at = CURRENT_TIMESTAMP
                WHERE normalized_text = %s 
                  AND candidate_id != %s 
                  AND review_status = 'pending'
            """
            cur.execute(sql_clean_duplicates, (norm_text, candidate_id))

        conn.commit()
        return True
    except Exception as e:
        print(f"[dictionary_repo] 승인 처리 에러: {e}")
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()

def soft_delete_term(term_id: int) -> bool:
    """용어를 Soft Delete (status = 'inactive'로 변경)"""
    conn = get_mysql_conn()
    cur = conn.cursor()
    try:
        # 1. 마스터 테이블 비활성화
        cur.execute("UPDATE term_dictionary SET status = 'inactive' WHERE term_id = %s", (term_id,))
        # 2. 하위 Alias들도 모두 비활성화
        cur.execute("UPDATE term_aliases SET status = 'inactive' WHERE term_id = %s", (term_id,))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"[dictionary_repo] Soft Delete 에러: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def update_term_details(term_id: int, payload: dict) -> bool:
    """용어 정보 수정 (에러 완벽 방어 및 어드민 파라미터 추가)"""
    conn = get_mysql_conn()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM term_dictionary WHERE term_id = %s", (term_id,))
        row = cur.fetchone()
        if not row:
            return False
        
        # 💡 dictionary.get()을 써서 파이썬 KeyError를 원천 차단합니다.
        term_type = payload.get("term_type", row.get("term_type"))
        canonical_name = payload.get("canonical_name", row.get("canonical_name"))
        display_name = payload.get("display_name", row.get("display_name", canonical_name))
        description = payload.get("description", row.get("description", ""))
        
        # 💡 새롭게 추가된 어드민 전용 파라미터들
        priority = int(payload.get("priority", row.get("priority", 100)))
        search_boost = float(payload.get("search_boost", row.get("search_boost", 1.0)))
        expand_to_aliases = int(payload.get("expand_to_aliases", row.get("expand_to_aliases", 1)))
        
        sql = """
            UPDATE term_dictionary 
            SET term_type = %s,
                canonical_name = %s,
                display_name = %s,
                description = %s,
                priority = %s,
                search_boost = %s,
                expand_to_aliases = %s
            WHERE term_id = %s
        """
        cur.execute(sql, (term_type, canonical_name, display_name, description, priority, search_boost, expand_to_aliases, term_id))
        
        # 유의어 업데이트 로직
        aliases = payload.get("aliases")
        if aliases is not None:
            cur.execute("UPDATE term_aliases SET status = 'inactive' WHERE term_id = %s", (term_id,))
            for alt in aliases:
                alt_clean = alt.strip()
                if not alt_clean: continue
                import re
                alt_norm = re.sub(r"\s+", " ", alt_clean.lower().replace("-", " ").replace("_", " ").replace("/", " ")).strip()
                
                # 중복 에러(Integrity Error) 방지를 위한 UPSERT 논리
                cur.execute("SELECT alias_id FROM term_aliases WHERE term_id = %s AND alias_normalized = %s", (term_id, alt_norm))
                alias_row = cur.fetchone()
                
                if alias_row:
                    cur.execute("UPDATE term_aliases SET status = 'active', alias_text = %s WHERE alias_id = %s", (alt_clean, alias_row['alias_id']))
                else:
                    cur.execute("""
                        INSERT INTO term_aliases (term_id, alias_text, alias_normalized, match_type, status)
                        VALUES (%s, %s, %s, 'contains', 'active')
                    """, (term_id, alt_clean, alt_norm))
                    
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        # 💡 만약 또 에러가 난다면, 파이썬 터미널에 범인이 적나라하게 찍힙니다!
        print(f"🚨 [dictionary_repo] 쿼리 에러 상세 발생: {type(e).__name__} - {e}")
        return False
    finally:
        cur.close()
        conn.close() 