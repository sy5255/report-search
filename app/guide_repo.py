"""분석 프로세스 가이드 문서 (guide.md) 읽기/쓰기.

'분석 프로세스 안내'(PROCESS_GUIDE) 에이전트의 답변 근거이자, 관리자 편집 대상.
파일 IO만 담당 — DB 불필요.
"""
from pathlib import Path

GUIDE_PATH = Path(__file__).resolve().parent.parent / "guide.md"

_DEFAULT_GUIDE = (
    "# 불량 분석 의뢰 안내\n\n"
    "아직 가이드 내용이 작성되지 않았습니다. 관리자 페이지(/admin/guide)에서 작성해 주세요.\n"
)


def load_guide() -> str:
    """guide.md 전체 텍스트. 파일이 없거나 읽기 실패 시 기본 안내 문자열."""
    try:
        if GUIDE_PATH.exists():
            text = GUIDE_PATH.read_text(encoding="utf-8")
            return text if text.strip() else _DEFAULT_GUIDE
    except Exception as e:
        print(f"[GUIDE] load 실패: {e}")
    return _DEFAULT_GUIDE


def save_guide(text: str) -> bool:
    """guide.md 저장. 성공 시 True."""
    try:
        GUIDE_PATH.write_text(str(text or ""), encoding="utf-8")
        return True
    except Exception as e:
        print(f"[GUIDE] save 실패: {e}")
        return False
