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
    """DB에서 정식 등록된 용어 사전 목록을 가져오는 함수 (웹 UI 렌더링용)"""
    conn = get_mysql_conn()
    cur = conn.cursor(dictionary=True) 
    try:
        # 용어 정보와 해당 용어에 속한 활성 유의어(alias)들을 콤마로 묶어서 한 번에 가져옵니다.
        sql = """
            SELECT 
                td.term_id, 
                td.term_type, 
                td.canonical_name, 
                td.description,
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