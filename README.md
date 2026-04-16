# 불량 분석 RAG 시스템 (Report Search)

불량 분석 업무를 효율화하기 위해 구축된 **Hybrid RAG(Retrieval-Augmented Generation) & DB 통계 조회 기반 에이전트 시스템**입니다. 사내 기술 보고서를 검색하는 RAG 기반 `문서 검색` 기능과, MySQL DB의 정량적 데이터를 쿼리하는 `DB 통계 분석` 기능을 단일 채팅 인터페이스에서 지능적으로 라우팅하여 제공합니다.

---

## 1. 시스템 개요 (System Overview)

*   **목표**: 방대한 원인/해결책 관련 사내 문서와, 정형화된 공정/불량 데이터 통계를 하나의 시스템에서 사용자 대화형식(Chatbot)으로 조회.
*   **기술 스택**:
    *   **Backend**: Python, FastAPI
    *   **Agent/LLM**: `gpt-oss-120b` (Tools API 지원), OpenAI Python SDK
    *   **Search**: ElasticSearch (RAG 용, RRF 지원)
    *   **Database**: MySQL (세션 히스토리, 검색 로그, 도구 조회용 DB)
    *   **Frontend**: Jinja2 Templates, Vanilla JS/CSS

---

## 2. 파이프라인 및 아키텍처 흐름 (Architecture Flow)

사용자가 채팅 인터페이스에서 질문을 입력한 후 답변을 받을 때까지, 시스템은 다음의 파이프라인을 거칩니다:

1.  **사용자 입력 및 히스토리 구성 (`/api/chat`)**: 
    * DB에서 해당 세션의 가장 최근 메시지를 가져와 슬라이딩 윈도우 방식으로 문맥(Context)을 구성합니다 (최근 6개 = 3턴 유지).
2.  **질문 재작성 (Query Rewriting)**: 
    * 대화 맥락을 기반으로 후속 질문(Follow-up)인 경우 이를 단독 검색이 가능한 포맷(Standalone Query)으로 치환합니다.
3.  **검색어 정규화 및 확장 (Normalization & Expansion)**: 
    * 사내 용어 사전(Term Dictionary)을 기반으로 사용자의 질문 속 약어를 표준어(Canonical Name)로 치환합니다. 검색율을 높이기 위해 동의어를 조건별로 확장합니다.
4.  **의도 분류 및 에이전트 라우팅 (Intent Routing)**: 
    * 사용 메시지 텍스트를 LLM 라우터에 통과시켜 4가지 모드(DB, RAG, Hybrid, General) 중 하나로 분류합니다. (명시적인 유저 클릭 태그 `[DB_ANALYSIS]` 등은 우선 처리)
5.  **LLM 도구 호출 (Tool Calling)**: 
    * 에이전트가 인텐트에 맞는 도구(`query_database`, `search_documents`)를 호출하여 외부 지점(DB / ElasticSearch)에 접근해 데이터를 수집합니다.
6.  **마크다운 응답 및 근거 생성 (Answer & Citations Generation)**: 
    * 수집된 데이터를 바탕으로 Markdown 포맷의 답변을 1차 생성하고, 그 후 답변에서 팩트(Claim)를 뽑아내서 검색된 문서 Chunk ID와 일치시키는 2차 Citation 추출 파이프라인을 돌립니다.
7.  **데이터 보존 (Persistence & Logging)**: 
    * Rewrite, Normalize된 질의와 툴 사용 과정(agent_steps), Citation 결과를 모두 턴 단위 아티팩트로 DB에 영속화 한 뒤 사용자에게 응답을 반환합니다.

---

## 3. 핵심 구현 로직 상세 (Core Implementation Logic)

시스템의 인공지능 기반 처리를 담당하는 핵심 모듈들의 로직은 다음과 같습니다.

### 3.1. 검색어 정규화 및 단어 확장 (`query_normalizer.py`)
이 모듈은 단순한 키워드 매칭을 넘어서, 사내 전문 용어를 표준어(Canonical Name)로 수렴시키고, RAG 검색 시 누락이 없도록 동의어(Alias)를 확장합니다.

*   **Longest-match & Non-overlap 검출 로직**:
    *   토큰 경계 정규식 매칭을 통해 여러 동의어 복합군이 겹칠 경우, 가장 긴(Alias Length) 길이를 가졌거나, 우선순위가 높은(`priority`) 단어를 최우선으로 채택하고 겹치는 구간의 후보는 제거합니다.
*   **분야별 팽창(Expansion) 제한 정책 (Policy)**:
    *   **Aggressive (`chemistry`, `node`, `product`)**: 표준 단어뿐 아니라 Preferred/Other 묶음 안의 다양한 닉네임과 변형 약어를 최대 5개까지 과감하게 검색어에 추가합니다.
    *   **Conservative (`process`)**: 일반 용어와 겹칠 위험이 있으므로 동의어 병합을 최대 2개로 제한합니다.
    *   **Minimal (`owner`, `defect`)**: 확장을 거의 하지 않고, 표준어를 우선 검색합니다.
*   최종적으로 "원래 쿼리 -> 정규화된 쿼리 -> 확장된 쿼리"의 진행 로그가 수집되어, 검색 디버깅 및 사용자 추적(Search Logs)에 사용됩니다.

### 3.2. 의도 분류 및 에이전트 라우팅 (`agent.py`)
에이전트가 무분별하게 모든 도구를 사용하는 할루시네이션(비생산적 자원 낭비)을 막기 위한 관문입니다.

*   **LLM 라우터 (문지기)**:
    - `"최근 발생 건수는?"` -> `DB_ANALYSIS` (정량 도구 필요)
    - `"에러 발생 원인이 뭐야?"` -> `RAG_KNOWLEDGE` (기술 문서 필요)
    - 이 외 통합형(`HYBRID_DB_RAG`), 일상 대화형(`GENERAL_CHAT`) 등 4가지 인텐트로 질문을 강제 분류합니다.
*   **강제 인텐트 우회 (UI Action Tags)**:
    - 프론트엔드 버튼(Action Chips)을 통해 `[DB_ANALYSIS]`와 같은 태그가 문두에 삽입되어 들어올 경우, LLM 라우팅을 거치지 않고 (LLM 호출 비용 및 대기 시간 Zero) 즉시 해당 인텐트로 직행합니다.
*   **물리적 무기 압수 (Tools Filtering)**:
    - 라우터가 인텐트를 결정하면, 해당 인텐트에 불필요한 도구는 모델의 `tools` 목록 객체에서 아이에 삭제해 버리기 때문에 도구를 문맥 밖으로 남용하는 사태를 코드 단위에서 방지합니다.

### 3.3. 프롬프트 페르소나 및 도구 스키마 (`agent.py`, `tools.py`)
라우팅 된 도구를 바탕으로, 에이전트의 구체적인 행동 단계를 유도하는 매우 엄격한 Rule과 System Prompt가 할당됩니다.

*   **`query_database` 도구(DB Agent)의 특수 로직**:
    *   **집계(Aggregation) 강제 (매우 중요)**: 모델이 수만 건의 불량 명세를 `SELECT *` 로 전부 렉시콘 파싱하는 것을 방지하기 위해, 데이터 반환 행 수가 `50`을 초과할 시 시스템이 임의로 Error를 돌려보내며 프롬프트를 주입합니다: `"개별 데이터를 전부 가져오지 말고 내부에 COUNT(), GROUP_CONCAT(), GROUP BY를 적용해 재호출해라"`.
    *   **출력 강제**: DB의 내용은 항상 **마크다운 표(Table)**로 반환하도록 지시되며, 응답 맨 밑에 유저가 참고할 수 있도록 SQL 원문을 포함한 `<details>` 토글 태그를 생성하게 만듭니다.
*   **`search_documents` 도구(RAG Agent) 특수 로직**:
    *   절대 모델의 사전 지식으로만 답변을 방지하는 규칙 삽입.
    *   **카드형 포맷**: 마크다운 표 생성을 금지하고 `> 📌 주요 내용` 같은 Quotation(인용구) UI 카드 형태로 내용을 요약하도록 제어합니다.

### 3.4. 응답 생성 및 인용 매핑 메커니즘 (`llm_client.py`)
Tool Calling이 끝나고 최종 답변을 도출할 때는 높은 사실 정확도를 보장하기 위해 2단계 검증을 가집니다.

1.  **구조화된 답변 추출 (Answer Generate Mode)**: 
    * Markdown 코드를 통째로 JSON 내의 `answer_markdown` Key 값에 주입하도록 하여 프로그램 상에서 파싱이 쉽도록 통제합니다 (실패 시 순수 Text Fallback).
2.  **역방향 Citation 매핑 (Post-Citation Mode)**: 
    * 모델이 생산한 텍스트 답변이 실제로 신뢰할 수 있는지 검토하기 위해 만들어진 답변만을 읽어들인 다음 **단순 팩트 명제(Claim)**를 최대 3개 뽑아냅니다.
    * 해당 팩트들이 도구에서 가져온 `rag_chunks` 소스의 어떤 부분(`doc_id`, `chunk_id`)에 해당하는지 역 매핑하는 LLM 함수를 추가 동작시켜, 답변 상단의 "참고 문서 카드"를 정확히 렌더링하도록 JSON을 리턴합니다.

### 3.5. 영속성 및 상태 보존(Database) (`repo.py`)
- 단순한 대화 메시지 로그를 넘어 턴과 세션, 아티팩트를 세세하게 저장합니다:
    *   **`chat_sessions` / `chat_messages`**: 기본 채팅 트래킹
    *   **`chat_search_logs`**: 텍스트 분석에 특화되어 오리지널 쿼리가 Rewrite, Normalization 된 후 도출된 동의어군(`expanded_query`)과 필터(DB조건), RRF 랭킹 등의 로직 실행 내역 전체 저장. (검색 품질 추적용)
    *   **`chat_turn_artifacts`**: 도구 호출 과정에서 발생한 추론 일지(`agent_steps`) 및 프론트엔드가 사용할 Intent 기반 UI 추천 버튼(`suggested_actions`)을 턴 ID와 함께 보장합니다.

---

## 4. 프론트엔드 연동과 확장 (Frontend State & UI Chips)

*   템플릿 (`template/chat.html`) 환경에서 FastAPI가 렌더하는 시점과 JavaScript fetch 호출을 분리하여 빠른 렌더링 보장.
*   **액션 칩 생성 방식**:
    - 백엔드는 상태(State)에 따라 `suggested_actions` 배열을 반환합니다.
    - 예를 들어 유저가 통계 조회(`DB_ANALYSIS`)를 진행했다면, 백엔드는 다음과 같은 Action UI Object 목록을 클라이언트로 내려줍니다:
      `[{"label": "📖 문서 검색 Agent 호출하기", "action": "[RAG_KNOWLEDGE]", "disabled": false}]`
    - 이를 통해 유저는 시스템의 하이브리드 기능을 적극적으로 탐색하며 심층 분석을 유도받을 수 있습니다.