import json
from typing import Any, Tuple
from app.db import get_conn
from app.rag_client import rag_retrieve_rrf
from app.config import DEFAULT_INDEX_NAME

# LLM에게 제공할 도구 명세서
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": "MySQL DB를 조회하여 통계나 분석 결과를 가져옵니다. '최근 3개월 분석 개수', '불량명 순위', '가장 많이 나온 화학 원소' 등에 사용하세요. 반드시 SELECT 문만 사용해야 합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_query": {
                        "type": "string",
                        "description": "실행할 정확한 MySQL SELECT 쿼리문"
                    }
                },
                "required": ["sql_query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "사내 이메일 및 보고서 문서를 RAG로 검색합니다. 특정 문서의 내용 요약, 비교, 원인 파악이 필요할 때 사용하세요.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "문서 검색에 사용할 키워드 또는 문장"
                    },
                    "intent": {
                        "type": "string",
                        "enum": ["요약형", "비교분석형", "일반검색"],
                        "description": "검색 결과로 얻고 싶은 답변의 유형"
                    }
                },
                "required": ["query", "intent"]
            }
        }
    }
]

def execute_tool(func_name: str, args: dict) -> Tuple[str, list]:
    """
    반환값: (LLM에게 전달할 결과 문자열, 프론트엔드/인용구를 위해 수집된 원본 문서 목록)
    """
    try:
        if func_name == "query_database":
            sql = args.get("sql_query", "")
            if not sql.strip().upper().startswith("SELECT"):
                return "Error: 데이터 보호를 위해 SELECT 쿼리만 허용됩니다.", []
           
            conn = get_conn()
            cur = conn.cursor(dictionary=True)
            cur.execute(sql)
            rows = cur.fetchall()
            cur.close()
            conn.close()

            # [핵심 방어 로직 수정] LIMIT이 아니라 "집계(COUNT)"를 강제함!
            if len(rows) > 50:
                return f"Error: 쿼리 결과가 {len(rows)}건으로 너무 많아 모델의 한도를 초과했습니다. 개별 데이터를 전부 가져오지 말고, 반드시 SQL 내부에서 COUNT(), SUM(), GROUP BY 등을 사용하여 계산이 완료된 '통계 결과'만 조회하도록 쿼리를 수정하세요.", []

            return json.dumps(rows, ensure_ascii=False), []

        elif func_name == "search_documents":
            search_query = args.get("query", "")
            intent = args.get("intent", "일반검색")
           
            # 검색 실행
            rag_result = rag_retrieve_rrf(
                index_name=DEFAULT_INDEX_NAME, # 필요 시 기본 인덱스명 조정
                query_text=search_query,
                top_k=8
            )
            hits = rag_result.get("hits", {}).get("hits", [])
           
            extracted_docs = []
            for hit in hits:
                src = hit.get("_source", {})
                content = src.get("merge_title_content", "")[:1500]
                extracted_docs.append(f"[Title: {src.get('title')}] {content}")
               
            result_str = f"(검색 의도: {intent}, 쿼리: {search_query})\n\n" + "\n---\n".join(extracted_docs)
           
            # 결과 문자열과 원본 hits를 함께 반환
            return result_str, hits

        else:
            return f"Error: {func_name} 도구를 찾을 수 없습니다.", []
           
    except Exception as e:
        return f"Error executing {func_name}: {str(e)}", []