import json
import uuid
from openai import OpenAI
from app.config import (
    LLM_API_BASE_URL,
    LLM_TICKET,
    SEND_SYSTEM_NAME,
    USER_TYPE,
)

ANSWER_MAX_TOKENS = 3000
CITATION_MAX_TOKENS = 3000
REWRITE_MAX_TOKENS = 3000 #220

def _safe_json_extract(text: str) -> dict:
    t = (text or "").strip()
    i = t.find("{")
    j = t.rfind("}")
    if i == -1 or j == -1 or j <= i:
        raise ValueError(f"LLM output is not JSON: {t[:1000]}")

    candidate = t[i:j+1]
    try:
        return json.loads(candidate)
    except Exception:
        print("=== LLM RAW OUTPUT BEGIN ===")
        print(candidate[:5000])
        print("=== LLM RAW OUTPUT END ===")
        raise


def _make_client(user_id: str) -> OpenAI:
    return OpenAI(
        base_url=LLM_API_BASE_URL,
        api_key="EMPTY",
        default_headers={
            "x-dep-ticket": LLM_TICKET,
            "Send-System-Name": SEND_SYSTEM_NAME,
            "User-Id": user_id,
            "User-Type": USER_TYPE,
            "Prompt-Msg-Id": str(uuid.uuid4()),
            "Completion-Msg-Id": str(uuid.uuid4()),
        }
    )


def rewrite_query_with_history(
    user_id: str,
    user_question: str,
    previous_messages: list[dict] | None = None
) -> str:
    client = _make_client(user_id)

    system = (
        "You rewrite a follow-up user question into a standalone search query for RAG retrieval.\n"
        "Use the previous conversation only to resolve references such as 'that document', 'it', 'above', etc.\n"
        "Do NOT answer the question.\n"
        "Do NOT add unsupported facts.\n"
        "Return STRICT JSON only.\n"
    )

    payload = {
        "task": "Rewrite the user's latest question into a standalone search query.",
        "rules": [
            "Return JSON only.",
            "Preserve the user's intent exactly.",
            "Resolve ambiguous references using previous conversation when possible.",
            "Keep the rewritten query concise but specific.",
            "If the latest question is already standalone, keep it almost unchanged."
        ],
        "output_schema": {
            "standalone_query": "string"
        },
        "conversation": previous_messages or [],
        "latest_user_question": user_question
    }

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
    ]

    completion = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=messages,
        temperature=0.0,
        stream=False,
        max_tokens=REWRITE_MAX_TOKENS,
    )

    text = completion.choices[0].message.content
    parsed = _safe_json_extract(text)
    rewritten = (parsed.get("standalone_query") or "").strip()
    return rewritten or user_question


def _build_context_summary(previous_messages: list[dict] | None, max_items: int = 4) -> list[dict]:
    if not previous_messages:
        return []

    trimmed = previous_messages[-max_items:]
    out = []
    for m in trimmed:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        out.append({
            "role": role,
            "content": content[:1200]
        })
    return out


def _build_answer_prompt(
    user_question: str,
    rag_chunks: list[dict],
    previous_messages: list[dict] | None = None
) -> list[dict]:
    system = (
        "You are a helpful RAG answerer.\n"
        "You must use ONLY the provided EVIDENCE for factual claims.\n"
        "Do NOT invent facts.\n"
        "If evidence is insufficient, clearly say what is uncertain.\n"
        "Write a natural, professional Korean answer.\n"
        "Return STRICT JSON only.\n"
    )

    evidence_lines = []
    for idx, c in enumerate(rag_chunks, 1):
        evidence_lines.append({
            "no": idx,
            "doc_id": c.get("doc_id"),
            "chunk_id": c.get("chunk_id"),
            "title": c.get("title"),
            "text": (c.get("merge_title_content") or "")[:2500],
            "score": c.get("score"),
        })

    user_payload = {
        "task": "Answer the question naturally in Korean using ONLY the evidence.",
        "output_schema": {
            "answer_markdown": "Markdown formatted answer string"
        },
        "rules": [
            "Return JSON only.",
            "Do not wrap the JSON with markdown fences.",
            "No trailing commas.",
            "All strings must be valid JSON strings.",
            "Answer in Korean.",
            "Put the full answer into 'answer_markdown' as a single markdown string.",
            "The markdown must be inside a JSON string, not printed as raw markdown.",
            "If you do not return valid JSON, the response will be rejected.",
            "Example valid output: {\"answer_markdown\":\"## 제목\\n\\n- 항목1\\n- 항목2\"}",
            "Use markdown headings, bullet lists, or tables when they improve readability.",
            "If the user asks for a table, output a markdown table.",
            "If the user asks for code, output a markdown code block inside the answer_markdown string.",
            "Do not include citations in this step.",
            "If evidence is insufficient, say so clearly."
        ],
        "question": user_question,
        "evidence": evidence_lines
    }

    messages = [{"role": "system", "content": system}]
    messages.extend(_build_context_summary(previous_messages))
    messages.append({
        "role": "user",
        "content": json.dumps(user_payload, ensure_ascii=False)
    })
    return messages


def _build_citation_prompt(
    user_question: str,
    final_answer: str,
    rag_chunks: list[dict],
) -> list[dict]:
    system = (
        "You are a strict citation mapper.\n"
        "Use ONLY the provided EVIDENCE.\n"
        "Do NOT rewrite the answer.\n"
        "Do NOT invent claims.\n"
        "Return STRICT JSON only.\n"
    )

    evidence_lines = []
    for idx, c in enumerate(rag_chunks, 1):
        evidence_lines.append({
            "no": idx,
            "doc_id": c.get("doc_id"),
            "chunk_id": c.get("chunk_id"),
            "title": c.get("title"),
            "text": (c.get("merge_title_content") or "")[:2500],
            "score": c.get("score"),
        })

    user_payload = {
        "task": (
            "Extract at most 3 main factual claims from the answer and attach citations "
            "from the evidence."
        ),
        "output_schema": {
            "claims": [
                {
                    "claim": "Short factual claim in Korean",
                    "citations": [
                        {
                            "doc_id": "string",
                            "chunk_id": "string",
                            "score": "number or null"
                        }
                    ]
                }
            ]
        },
        "rules": [
            "Return JSON only.",
            "No markdown fences.",
            "No trailing commas.",
            "All strings must be valid JSON strings.",
            "Return at most 3 claims.",
            "Each claim must be short and concise.",
            "Each claim should have 1 to 2 citations.",
            "Use only doc_id and chunk_id that exist in evidence.",
            "If the answer contains uncertainty, only cite the supported factual parts."
        ],
        "question": user_question,
        "answer": final_answer,
        "evidence": evidence_lines
    }

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}
    ]


def _normalize_claims_to_answer_list(claims: list[dict] | None) -> list[dict]:
    out = []
    for c in claims or []:
        claim = (c.get("claim") or "").strip()
        cites = []
        for x in (c.get("citations") or []):
            cites.append({
                "doc_id": x.get("doc_id"),
                "chunk_id": x.get("chunk_id"),
                "quote": "",
                "score": x.get("score"),
            })
        if claim:
            out.append({
                "sentence": claim,
                "citations": cites
            })
    return out


def _call_json(client: OpenAI, messages: list[dict], max_tokens: int, temperature: float = 0.0) -> dict:
    completion = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=messages,
        temperature=temperature,
        stream=False,
        max_tokens=max_tokens,
    )

    try:
        print("finish_reason =", completion.choices[0].finish_reason)
    except Exception:
        pass

    text = completion.choices[0].message.content
    return _safe_json_extract(text)


def llm_answer_with_citations(
    user_id: str,
    user_question: str,
    rag_chunks: list[dict],
    previous_messages: list[dict] | None = None
) -> dict:
    client = _make_client(user_id)

    # 1차: markdown 답변 생성
    answer_messages = _build_answer_prompt(
        user_question=user_question,
        rag_chunks=rag_chunks,
        previous_messages=previous_messages,
    )

    answer_json = _call_answer_json_or_fallback_markdown(
        client=client,
        messages=answer_messages,
        max_tokens=ANSWER_MAX_TOKENS,
        temperature=0.1,
    )

    final_answer = (answer_json.get("answer_markdown") or "").strip()

    if not final_answer:
        final_answer = "근거 문서를 바탕으로 답변을 생성하지 못했습니다."

    # 2차: claims + citations 생성
    citation_messages = _build_citation_prompt(
        user_question=user_question,
        final_answer=final_answer,
        rag_chunks=rag_chunks,
    )

    try:
        citation_json = _call_json(
            client=client,
            messages=citation_messages,
            max_tokens=CITATION_MAX_TOKENS,
            temperature=0.0,
        )
        claims = citation_json.get("claims") or []
    except Exception as e:
        print(f"[citation-step-failed] {e}")
        claims = []

    return {
        "answer_markdown": final_answer,
        "claims": claims,
        "answer": _normalize_claims_to_answer_list(claims),
        "final": final_answer,
    }

def _strip_code_fence(text: str) -> str:
    t = (text or "").strip()

    if t.startswith("```"):
        lines = t.splitlines()
        if len(lines) >= 2:
            # 첫 줄 ```json / ```markdown / ``` 제거
            lines = lines[1:]
            # 마지막 ``` 제거
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            t = "\n".join(lines).strip()

    return t


def _call_answer_json_or_fallback_markdown(
    client: OpenAI,
    messages: list[dict],
    max_tokens: int,
    temperature: float = 0.0,
) -> dict:
    completion = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=messages,
        temperature=temperature,
        stream=False,
        max_tokens=max_tokens,
    )

    try:
        print("finish_reason =", completion.choices[0].finish_reason)
    except Exception:
        pass

    text = completion.choices[0].message.content or ""
    stripped = _strip_code_fence(text)

    # 1) JSON 우선 시도
    try:
        return _safe_json_extract(stripped)
    except Exception as e:
        print(f"[answer-json-parse-failed] {e}")
        print("=== ANSWER RAW OUTPUT BEGIN ===")
        print(stripped[:5000])
        print("=== ANSWER RAW OUTPUT END ===")

    # 2) fallback: raw text 자체를 answer_markdown으로 사용
    return {
        "answer_markdown": stripped
    }

# =====================================================================
# [Phase 1] 사내 모델 Tool Calling(Function Calling) 지원 테스트 코드
# =====================================================================
def test_tool_calling(user_id: str) -> dict:
    """
    사내 모델(gpt-oss-120b)이 OpenAI 스펙의 tool_calls를 정상적으로 응답하는지 테스트합니다.
    """
    client = _make_client(user_id)

    # 1. LLM에게 알려줄 가짜 함수(도구) 명세서
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_db_top_defects",
                "description": "MySQL DB에서 특정 기간 동안 가장 많이 발생한 불량명 순위를 조회합니다.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "months": {
                            "type": "integer",
                            "description": "조회할 최근 개월 수 (예: 3)"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "조회할 상위 N개 갯수 (예: 1)"
                        }
                    },
                    "required": ["months", "limit"]
                }
            }
        }
    ]

    # 2. 도구를 사용해야만 대답할 수 있는 프롬프트 전송
    messages = [
        {"role": "system", "content": "You are an intelligent agent. Use the provided tools to answer the user's question."},
        {"role": "user", "content": "최근 3개월간 가장 많이 발생한 불량명 1위가 뭐야?"}
    ]

    print("=== [Tool Calling Test Start] ===")
    try:
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=messages,
            tools=tools,
            tool_choice="auto", # 모델이 함수 호출 여부를 스스로 결정
            temperature=0.0,
        )

        message = completion.choices[0].message
        print("Finish Reason:", completion.choices[0].finish_reason)

        # 3. 모델이 tool_calls 형태로 응답했는지 검증
        if getattr(message, 'tool_calls', None):
            print("✅ Tool Calling 지원 확인됨!")
            tool_calls_info = []
           
            for tc in message.tool_calls:
                info = {
                    "id": getattr(tc, 'id', ''),
                    "type": getattr(tc, 'type', ''),
                    "function_name": tc.function.name,
                    "function_args": tc.function.arguments
                }
                tool_calls_info.append(info)
                print(f" - 호출된 함수 이름: {tc.function.name}")
                print(f" - 전달받은 파라미터: {tc.function.arguments}")
               
            return {"status": "success", "tool_calls": tool_calls_info}
       
        else:
            print("❌ Tool Calling이 발생하지 않음 (일반 텍스트 형태로 응답함)")
            print("응답 내용:", message.content)
            return {"status": "no_tool_calls", "content": message.content}

    except Exception as e:
        print(f"❌ API 에러 발생: {e}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    # 파이썬에서 이 파일을 직접 실행하면 테스트가 수행되도록 합니다.
    # 실행법: python app/llm_client.py
    import pprint
    res = test_tool_calling(user_id="test_admin")
    print("\n[테스트 결과 요약]")
    pprint.pprint(res)