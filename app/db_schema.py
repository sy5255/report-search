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


