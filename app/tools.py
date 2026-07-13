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
# NOTE: draw_chart 툴은 제거됨 — 차트는 프론트가 답변의 마크다운 표를 파싱해
# 온디맨드('차트로 보기' 버튼)로 그린다(LLM 0회, 에이전트 루프 추가 비용 없음).
# agent.py의 스펙 수집/렌더 경로는 과거 저장 차트 하위호환용으로 유지.

def execute_tool(func_name: str, args: dict, index_names: list | None = None) -> Tuple[str, list]:
    """
    반환값: (LLM에게 전달할 결과 문자열, 프론트엔드/인용구를 위해 수집된 원본 문서 목록)
    index_names: 검색할 인덱스 목록(UI 선택 반영). None/빈 값이면 DEFAULT_INDEX_NAME 사용.
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

            # UI에서 선택한 인덱스 목록 반영 (외부 RAG API의 콤마 구분 지원이 불확실하므로
            # 인덱스별로 개별 검색 후 병합 — 대부분 1개라 추가 비용 미미)
            targets = [x for x in (index_names or []) if str(x).strip()] or [DEFAULT_INDEX_NAME]
            hits = []
            seen_ids = set()
            for idx_name in targets:
                try:
                    rag_result = rag_retrieve_rrf(index_name=idx_name, query_text=search_query, top_k=8)
                    for h in rag_result.get("hits", {}).get("hits", []):
                        key = ((h.get("_source") or {}).get("doc_id"), h.get("_id"))
                        if key in seen_ids:
                            continue
                        seen_ids.add(key)
                        hits.append(h)
                except Exception as e:
                    print(f"[search_documents] 인덱스 '{idx_name}' 검색 실패(스킵): {e}")
            # 점수순 정렬 후 총 8건 캡 (단일 인덱스일 때 기존 동작과 동일)
            hits.sort(key=lambda h: -(h.get("_score") or 0))
            hits = hits[:8]

            extracted_docs = []
            for hit in hits:
                src = hit.get("_source", {})
                # citation 검증 evidence(2500자)와 동일 길이로 절단 — LLM이 본 것과 검증 대상 불일치 방지
                content = src.get("merge_title_content", "")[:2500]
                extracted_docs.append(f"[Title: {src.get('title')}] {content}")

            result_str = f"(검색 의도: {intent}, 쿼리: {search_query})\n\n" + "\n---\n".join(extracted_docs)

            # 결과 문자열과 원본 hits를 함께 반환
            return result_str, hits

        else:
            return f"Error: {func_name} 도구를 찾을 수 없습니다.", []
           
    except Exception as e:
        return f"Error executing {func_name}: {str(e)}", []