"""
단일 포스트 업로드 스크립트

사용법:
    python scripts/upload_custom_post.py --file post.html [--blog-id myblog]

지정된 HTML/Markdown 파일의 제목과 본문 텍스트, 이미지를 추출하여
네이버 블로그에 실발행합니다.
"""

import argparse
import asyncio
import os
import sys
import re
from pathlib import Path

# 프로젝트 루트 경로 추가 (절대 경로 보장)
current_dir = Path(__file__).parent.resolve()
project_root = current_dir.parent
sys.path.insert(0, str(project_root))

# .env 파일 로드 (선택 사항)
try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

from modules.uploaders.playwright_publisher import PlaywrightPublisher
from modules.exceptions import PublishError, SessionExpiredError

def parse_args():
    parser = argparse.ArgumentParser(description="Custom 블로그 포스트 업로드 스크립트")
    parser.add_argument("--file", "-f", required=True, help="업로드할 HTML 또는 Markdown 파일 경로 (.html, .md, .txt)")
    parser.add_argument("--blog-id", default=None, help="네이버 블로그 ID (입력 안하면 NAVER_BLOG_ID 환경변수 사용)")
    parser.add_argument("--tags", default="", help="쉼표 구분 태그 목록")
    parser.add_argument("--category", default=None, help="카테고리명")
    parser.add_argument("--thumbnail", default=None, help="썸네일 이미지 경로 (없으면 추출된 첫 번째 이미지 사용)")
    return parser.parse_args()

def parse_html(file_content: str, base_dir: Path):
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(file_content, "html.parser")

        # 1. 제목 추출 (h1)
        title = ""
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
            h1.extract()  # 파싱 후 본문에서 제외

        # 2. 이미지 목록 추출
        images = []
        for img in soup.find_all("img"):
            src = img.get("src")
            if src:
                # 로컬 파일 경로 보정
                img_path = Path(src)
                if not img_path.is_absolute():
                    img_path = base_dir / img_path

                if img_path.exists():
                    images.append(str(img_path.resolve()))
            img.extract()  # 텍스트 추출을 위해 img 태그 제거 (PlaywrightPublisher가 images 배열로 별도 삽입)

        # 3. 본문 텍스트 추출
        # 줄바꿈 태그 변환
        for br in soup.find_all("br"):
            br.replace_with("\n")

        # 문단 간격 유지를 위해 separator 추가
        content = soup.get_text(separator="\n\n", strip=True)

        return title, content, images
    except ImportError:
        print("💡 BeautifulSoup4가 설치되어 있지 않아 정규식 모드로 파싱합니다.")
        return parse_markdown(file_content, base_dir)

def parse_markdown(file_content: str, base_dir: Path):
    title = ""
    lines = file_content.splitlines()

    # 1. 제목: # 제목이나 가장 첫 줄
    for i, line in enumerate(lines[:10]):
        line_s = line.strip()
        if line_s.startswith("# ") or line_s.startswith("<h1>"):
            title = line_s.replace("# ", "").replace("<h1>", "").replace("</h1>", "").strip()
            lines[i] = ""
            break

    if not title:
        for i, line in enumerate(lines):
            line_s = line.strip()
            if line_s:
                title = line_s
                lines[i] = ""
                break

    # 2. 이미지 추출 (마크다운 ![alt](src) 또는 <img src="...">)
    images = []
    img_pattern = re.compile(r'!\[.*?\]\((.*?)\)|<img.*?src=["\'](.*?)["\'].*?>', re.IGNORECASE)

    content_lines = []
    for line in lines:
        # 이미지 태그 검색
        match = img_pattern.search(line)
        if match:
            src = match.group(1) or match.group(2)
            if src:
                img_path = Path(src)
                if not img_path.is_absolute():
                    img_path = base_dir / img_path
                if img_path.exists():
                    images.append(str(img_path.resolve()))
            # 본문에선 텍스트만 남김
            line = img_pattern.sub("", line)

        if line.strip():
            # 기본 HTML 태그 제거
            line = re.sub(r'<[^>]+>', '', line)
            content_lines.append(line.strip())

    content = "\n\n".join(content_lines)
    return title, content, images

async def main():
    args = parse_args()

    # 블로그 ID 얻기
    blog_id = args.blog_id or os.getenv("NAVER_BLOG_ID")
    if not blog_id:
        print("❌ 타겟 블로그 ID가 없습니다.")
        print("💡 실행 방법: python scripts/upload_custom_post.py --file post.html --blog-id [본인_블로그_ID]")
        sys.exit(1)

    # 타겟 파일 확인
    file_path = Path(args.file)
    if not file_path.exists() or not file_path.is_file():
        print(f"❌ 업로드할 파일을 찾을 수 없습니다: {file_path}")
        sys.exit(1)

    file_content = file_path.read_text(encoding="utf-8")

    # 모드에 따른 파싱 (HTML vs Markdown)
    if file_path.suffix.lower() in [".html", ".htm"]:
        title, content, images = parse_html(file_content, file_path.parent)
    else:
        title, content, images = parse_markdown(file_content, file_path.parent)

    if not title:
        title = "제목 없음"

    # 태그 처리
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []

    # 썸네일 처리
    thumbnail = args.thumbnail
    if not thumbnail and images:
        thumbnail = images[0]

    print(f"==================================================")
    print(f"  커스텀 포스트 자동 발행 준비")
    print(f"==================================================")
    print(f"  블로그 ID : {blog_id}")
    print(f"  제목      : {title}")
    print(f"  내용 길이 : {len(content):,} 자")
    print(f"  첨부 이미지: {len(images)} 개")
    if tags:
        print(f"  태그      : {', '.join(tags)}")
    print(f"==================================================\n")

    # Publisher 초기화
    # 기존 data/sessions/naver/state.json 사용 (수동 로그인 불필요)
    publisher = PlaywrightPublisher(blog_id=blog_id)

    try:
        print("🚀 네이버 블로그에 발행을 시작합니다. (Playwright가 실행 중입니다...)")

        # 내부 로직 안에서 content, images 등을 알아서 배치
        result = await publisher.publish(
            title=title,
            content=content,
            thumbnail=thumbnail,
            images=images,
            tags=tags,
            category=args.category
        )

        if result.success:
            print(f"\n🎉 [발행 성공] 포스트가 정상적으로 네이버 블로그에 업로드 되었습니다!")
            print(f"🔗 확인 링크: {result.url}\n")
        else:
            print(f"\n❌ [발행 실패] 오류가 발생했습니다.")
            print(f" - 시스템 에러 코드: {result.error_code}")
            print(f" - 에러 메시지: {result.error_message}\n")

    except SessionExpiredError as e:
        print(f"\n🚫 [세션 만료] 로그인 세션 시간이 초과되었거나 끊어졌습니다.")
        print(f"상세 정보: {e}")
        print("해결 방법: 터미널에서 `python scripts/naver_login.py`를 실행하여 새로운 세션을 저장하세요.\n")
    except PublishError as e:
        print(f"\n🚫 [오류 발생] 발행 과정 중 서버/인증 문제 또는 UI 변경 이슈가 발생했습니다.")
        print(f"에러 코드: {e.error_code}")
        print(f"상세 원인: {e}")
        if e.error_code == "CAPTCHA_REQUIRED":
            print("🚨 해결 방법: 캡차가 감지되었습니다. 직접 크롬 창을 띄워 로그인하고 캡차를 해제해야 합니다.\n")
    except Exception as e:
        print(f"\n🚨 [알 수 없는 오류] 시스템 예외가 발생했습니다:\n{e}\n")

if __name__ == "__main__":
    asyncio.run(main())
