__init__.py 
VERSION = "0.2.0"

config.py
from pathlib import Path
import os

# ===== Shared workspace roots =====
MAIL_ROOT = Path("/config/work/sharedworkspace/mail_archive")
PARSE_ROOT = Path("/config/work/sharedworkspace/parsing_archive")

# ===== Auth/JWT =====
JWT_KEY = "abcd"  # 기존과 동일

# ===== MySQL (Cloud DB) =====
MYSQL_HOST = "10.111.111.111" 
MYSQL_PORT = 1234 
MYSQL_DB   = "db" 
MYSQL_USER = "dbuser"
MYSQL_PASS = "123!" 

# ===== RAG API =====
RAG_BASE = "http://ap/elastic/v2"
PASS_KEY = "credw=="  
RAG_KEY  = "rag-cnQ"  

DEFAULT_PERMISSION_GROUPS = ["rag-public"]

# ===== LLM (gpt-oss-120b) =====
LLM_API_BASE_URL = "http://api/gpt-oss-120b/v1" 
LLM_TICKET = "c:TICKET-eQ=="  
SEND_SYSTEM_NAME = "AutoMeasure"
USER_ID = "s.park"
USER_TYPE = "AD_ID"

# ===== UI / Search defaults =====
DEFAULT_INDEX_NAME = "rp-ifa1-ver1-full"
DEFAULT_TOP_K = 8

# (선택) 인덱스 옵션: MVP는 고정 리스트로 제공
INDEX_OPTIONS = [
    "rp-ifa-ver1-full",
    "rp-ifa-ver1-lite",
    "rp-ifa-ver1-raw",
    "rp-ifa1-ver1-full",
    "rp-ifa1-ver1-raw",
    "rp-term-ver1"
    # "rp-other-temp-ver1-full",
    # "rp-other-temp-ver1-lite",
    # "rp-other-temp-ver1-raw",
]

# ===== Security: path traversal guard root allow list =====
ALLOWED_VIEW_ROOTS = {
    "MAIL_ROOT": MAIL_ROOT,
    "PARSE_ROOT": PARSE_ROOT,
}

db_schema.py
from app.db import get_conn

def ensure_tables():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            session_id VARCHAR(36) PRIMARY KEY,
            user_id VARCHAR(128) NOT NULL,
            title VARCHAR(255) NOT NULL,
            archived TINYINT(1) NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            INDEX idx_user_id (user_id),
            INDEX idx_updated_at (updated_at),
            INDEX idx_archived (archived)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        try:
            cur.execute("ALTER TABLE chat_sessions ADD COLUMN archived TINYINT(1) NOT NULL DEFAULT 0;")
        except Exception:
            pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            msg_id VARCHAR(36) PRIMARY KEY,
            session_id VARCHAR(36) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            role ENUM('user','assistant') NOT NULL,
            content LONGTEXT NOT NULL,
            created_at DATETIME NOT NULL,
            INDEX idx_session_id (session_id),
            INDEX idx_user_id (user_id),
            INDEX idx_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_turn_artifacts (
            turn_id VARCHAR(36) PRIMARY KEY,
            session_id VARCHAR(36) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            user_msg_id VARCHAR(36) NOT NULL,
            assistant_msg_id VARCHAR(36) NOT NULL,
            index_name VARCHAR(255) NOT NULL,
            rag_response_json JSON NULL,
            citations_json JSON NULL,
            created_at DATETIME NOT NULL,
            INDEX idx_session_id (session_id),
            INDEX idx_user_id (user_id),
            INDEX idx_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        # ✅ 검색 품질 검증용 로그 테이블
        cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_search_logs (
            search_id VARCHAR(36) PRIMARY KEY,
            session_id VARCHAR(36) NOT NULL,
            user_id VARCHAR(128) NOT NULL,
            user_msg_id VARCHAR(36) NOT NULL,
            assistant_msg_id VARCHAR(36) NULL,
            index_name VARCHAR(255) NOT NULL,

            original_query LONGTEXT NOT NULL,
            rewritten_query LONGTEXT NULL,
            normalized_query LONGTEXT NULL,
            expanded_query LONGTEXT NULL,

            detected_terms_json JSON NULL,
            expansion_terms_json JSON NULL,
            filters_json JSON NULL,
            top_docs_json JSON NULL,

            retrieve_top_k INT NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,

            INDEX idx_session_id (session_id),
            INDEX idx_user_id (user_id),
            INDEX idx_user_msg_id (user_msg_id),
            INDEX idx_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        conn.commit()
    finally:
        cur.close()
        conn.close()


