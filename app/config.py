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
DEFAULT_INDEX_NAME = "rp-ifa-ver2-full"
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