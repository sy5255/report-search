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

