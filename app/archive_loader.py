"""로컬 아카이브 문서 로더.

main.py에서 분리한 모듈 — FastAPI 앱 생성 부작용 없이
kg_builder(배치)와 웹 서버가 동일한 문서 소스를 공유하기 위함.
"""
import os
import re
import json
import time
from pathlib import Path
from datetime import datetime
from email.utils import parsedate_to_datetime

from app.config import MAIL_ROOT, PARSE_ROOT

# 속도 최적화를 위해 파일을 한 번만 읽어 메모리에 저장하는 전역 캐시
_ARCHIVE_CACHE = []
_LAST_PROCESSED_MTIME = 0.0  # processed.json의 마지막 수정 시간 저장
_LAST_CHECK_TIME = 0.0
# 24시간(86,400초) 간격으로 체크하도록 수정
CACHE_CHECK_INTERVAL = 86400

# processed.json 파일 경로 설정
PROCESSED_JSON_PATH = PARSE_ROOT / "_state" / "processed.json"

# 허용된 작성자 화이트리스트 (이름만 작성)
ALLOWED_AUTHORS = ["성지아 <j.na@s.com>", "김지수 <s.go@s.com>", "고미연 <y.ko@s.com>", "김영인 <i.kim@s.com>", "진연수 <s.jin@s.com>", "유미래 <g.y@s.com>", "신현빈 <s.shin@s.com>", "서세린 <s.se@s.com>", "오슬미 <s.y@s.com>", "이자린 <k.lee@s.com>", "김장미 <m.kim@s.com>", "김소희 <j.kim@s.com>", "이나연 <h.oh@s.com>", "윤희서 <k.y@s.com>", "미인지 <s.mg@s.com>"]

# 1. 날짜 문자열을 진짜 시간(Timestamp) 숫자로 변환하는 강력한 함수
def _parse_date_to_timestamp(date_str):
    if not date_str:
        return 0.0 # 날짜가 아예 없으면 맨 뒤로 보냄

    # 1) 이메일 표준 형식 시도 (예: Fri, 01 Aug 2025 12:34:56 +0900)
    try:
        dt = parsedate_to_datetime(date_str)
        if dt is not None:
            return dt.timestamp()
    except Exception:
        pass

    # 2) 정규식을 이용해 강제로 연/월/일 추출 (예: 2026-04-10, 2026. 4. 10, 2026년 4월 등)
    match = re.search(r'(\d{4})[-./년\s]+(\d{1,2})[-./월\s]+(\d{1,2})', date_str)
    if match:
        try:
            y, m, d = map(int, match.groups())
            return datetime(y, m, d).timestamp()
        except Exception:
            pass

    return 0.0 # 파싱에 완전히 실패하면 맨 뒤로

# 💡 2. 로컬 문서 검색 로직
def get_local_archive_docs():
    global _ARCHIVE_CACHE, _LAST_PROCESSED_MTIME, _LAST_CHECK_TIME

    now = time.time()

    # 마지막 체크 후 24시간이 지나지 않았고 캐시가 있다면 즉시 반환
    if now - _LAST_CHECK_TIME < CACHE_CHECK_INTERVAL and _ARCHIVE_CACHE:
        # 💡 이 조건문 덕분에 서버는 24시간 동안 파일 시스템을 건드리지 않고
        # 메모리(RAM)에 있는 데이터를 0.0001초 만에 반환합니다.
        return _ARCHIVE_CACHE

    if not PROCESSED_JSON_PATH.exists():
        print(f"[Archive] {PROCESSED_JSON_PATH} 파일을 찾을 수 없습니다.")
        return []

    _LAST_CHECK_TIME = now
    current_mtime = os.path.getmtime(PROCESSED_JSON_PATH)

    if _LAST_PROCESSED_MTIME == current_mtime and _ARCHIVE_CACHE:
        return _ARCHIVE_CACHE

    print(f"[Archive] 24시간 경과: 주기적 캐시 갱신 시작... (mtime: {current_mtime})")

    try:
        with open(PROCESSED_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        items = data.get("items", {})
        print(f"[Archive Debug] JSON에서 읽어온 전체 아이템 수: {len(items)}")

        category_max_versions = {}
        for rel_path in items.keys():
            parts = rel_path.split('/')
            if len(parts) >= 2:
                category = parts[0]
                version_str = parts[1]
                match = re.search(r'ver(\d+)', version_str)
                if match:
                    v_num = int(match.group(1))
                    if v_num > category_max_versions.get(category, -1):
                        category_max_versions[category] = v_num

        print(f"[Archive Debug] 탐지된 카테고리별 최신 버전: {category_max_versions}")

        docs = []

        # 💡 스킵 사유를 기록할 카운터
        skip_reasons = {
            "status_not_done": 0,
            "not_latest_version": 0,
            "md_file_not_found": 0,
            "no_mail_meta_tag": 0,
            "parse_error": 0
        }

        first_missing_path = None # 에러가 난 첫 번째 경로를 기억하기 위함

        for rel_path, info in items.items():
            if info.get("status") != "DONE":
                skip_reasons["status_not_done"] += 1
                continue

            parts = rel_path.split('/')
            category = parts[0]
            version_str = parts[1]
            match = re.search(r'ver(\d+)', version_str)

            if not match or int(match.group(1)) != category_max_versions.get(category):
                skip_reasons["not_latest_version"] += 1
                continue

            # 경로 계산
            safe_rel_dir = Path(rel_path).parent
            safe_out_dir = PARSE_ROOT / safe_rel_dir
            md_files = list(safe_out_dir.rglob("*.md"))

            if not md_files:
                skip_reasons["md_file_not_found"] += 1
                if not first_missing_path:
                    first_missing_path = safe_out_dir # 경로가 어떻게 꼬였는지 터미널에 출력하기 위해 저장
                continue

            filepath = md_files[0]

            try:
                content = filepath.read_text(encoding="utf-8", errors="ignore")

                # 메타데이터가 없는 파일 거르기
                if "[MAIL_META]" not in content:
                    skip_reasons["no_mail_meta_tag"] += 1
                    continue

                # 💡 여기서 변수들을 모두 '빈 바구니'로 초기화해야 합니다! (이 부분이 지워져서 났던 에러입니다)
                title = filepath.stem
                mail_from = ""
                mail_date = ""
                report_links = []

                # 1. 작성자(From) 파싱
                from_match = re.search(r'From\s*:\s*(.*?)(?=\s*(?:Date|To|Cc|Bcc|Subject|\[)|\n|$)', content, re.IGNORECASE)
                if from_match:
                    # 꺾쇠 유지, 따옴표만 제거
                    mail_from = from_match.group(1).strip().replace('"', '').replace("'", "")

                # 💡 2. 화이트리스트 검사 (허용된 사람 아니면 여기서 바로 스킵!)
                if mail_from not in ALLOWED_AUTHORS:
                    skip_reasons["not_allowed_author"] = skip_reasons.get("not_allowed_author", 0) + 1
                    continue

                # 3. 날짜, 제목, EDM 링크 파싱
                date_match = re.search(r'Date\s*:\s*(.*?)(?=\s*(?:From|To|Cc|Bcc|Subject|\[)|\n|$)', content, re.IGNORECASE)
                if date_match: mail_date = date_match.group(1).strip()

                subject_match = re.search(r'Subject\s*:\s*(.*?)(?=\s*(?:From|Date|To|Cc|Bcc|\[)|\n|$)', content, re.IGNORECASE)
                if subject_match: title = subject_match.group(1).strip()

                edm_match = re.search(r'EDM\s*링크\s*:\s*(http[^\s\n]+)', content, re.IGNORECASE)
                if edm_match: report_links.append(edm_match.group(1).strip())

                # 4. 이미지 에셋 탐색 로직
                rel_dir = filepath.parent.relative_to(PARSE_ROOT)
                target_parts = []
                for part in rel_dir.parts:
                    if part.startswith("export_"): break
                    target_parts.append(part)

                attachments_dir = MAIL_ROOT.joinpath(*target_parts) / "attachments"
                assets = []
                if attachments_dir.exists() and attachments_dir.is_dir():
                    for ext in ["*.png", "*.jpg", "*.jpeg", "*.webp", "*.PNG", "*.JPG"]:
                        for img_path in attachments_dir.glob(ext):
                            assets.append({
                                "path": str(img_path.relative_to(MAIL_ROOT)).replace("\\", "/"),
                                "file_name": img_path.name
                            })

                # 5. 모든 데이터를 묶어서 카드 1개 완성!
                docs.append({
                    "doc_id": filepath.name,
                    "title": title,
                    "mail_from": mail_from,
                    "mail_date": mail_date,
                    "report_links": report_links,
                    "storage": {"parsed_md_rel_path": str(filepath.relative_to(PARSE_ROOT)).replace("\\", "/")},
                    "assets": assets,
                    "raw_content": content,
                    "version_tag": version_str.upper()
                })

            except Exception as e:
                skip_reasons["parse_error"] += 1
                if skip_reasons["parse_error"] == 1:
                    print(f"\n🚨 [디버그] 치명적 에러 원인 발견: {type(e).__name__} - {e}\n")

        docs.sort(key=lambda x: _parse_date_to_timestamp(x["mail_date"]), reverse=True)

        _ARCHIVE_CACHE = docs
        _LAST_PROCESSED_MTIME = current_mtime

        # 💡 리포트 최종 출력
        print("-" * 50)
        print(f"[Archive Report] 필터링 및 로딩 결과")
        print(f"  - 성공적으로 로드된 문서: {len(docs)}개")
        print(f"  - [Skip] 상태가 DONE이 아님: {skip_reasons['status_not_done']}개")
        print(f"  - [Skip] 구버전 폴더(최신 아님): {skip_reasons['not_latest_version']}개")
        print(f"  - [Skip] MD 파일 경로 못 찾음: {skip_reasons['md_file_not_found']}개")
        print(f"  - [Skip] 문서 내 [MAIL_META] 없음: {skip_reasons['no_mail_meta_tag']}개")
        print(f"  - [Skip] 읽기 에러 등: {skip_reasons['parse_error']}개")
        if first_missing_path:
            print(f"\n⚠️ 주의: MD 파일을 찾지 못한 첫 번째 경로를 확인해보세요!")
            print(f"서버가 찾으려 한 경로: {first_missing_path}")
        print("-" * 50)

    except Exception as e:
        print(f"[Archive] processed.json 읽기 실패: {e}")

    return _ARCHIVE_CACHE
