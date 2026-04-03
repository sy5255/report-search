import json
import requests
from app.config import RAG_BASE, PASS_KEY, RAG_KEY, DEFAULT_PERMISSION_GROUPS

def rag_retrieve_rrf(index_name: str, query_text: str, top_k: int = 5, filters: dict | None = None):
    url = f"{RAG_BASE}/retrieve-rrf"
    headers = {
        "Content-Type": "application/json",
        "x-dep-ticket": PASS_KEY,
        "api-key": RAG_KEY,
    }

    fields = {
        "index_name": index_name,
        "permission_groups": DEFAULT_PERMISSION_GROUPS,
        "query_text": query_text,
        "num_result_doc": top_k,
        "fields_exclude": ["v_merge_title_content"],
    }

    if filters:
        fields["filter"] = filters

    resp = requests.post(url, headers=headers, data=json.dumps(fields), timeout=30)
    resp.raise_for_status()
    return resp.json()