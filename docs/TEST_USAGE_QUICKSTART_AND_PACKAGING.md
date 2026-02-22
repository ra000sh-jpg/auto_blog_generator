# Test Usage Quickstart and Packaging Guide

## 1. 목표
- 정규 배포 전, 2인(본인 + 배우자) 테스트 사용을 빠르게 시작하기 위한 최소 실행 가이드입니다.
- 터미널 명령만 따라하면 동일 환경으로 재현되도록 구성했습니다.

## 2. 사전 준비
- macOS
- Python 3.10+
- Node.js 20+ and npm
- Git
- Chrome 또는 Chromium

## 3. 설치 절차 (새 노트북)
```bash
git clone https://github.com/ra000sh-jpg/auto_blog_generator.git
cd auto_blog_generator

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

python3 -m playwright install chromium

cd frontend
npm install
cd ..
```

## 4. 환경변수 준비
1. 루트에 `.env` 파일 생성
2. 최소 권장 키:
   - `NAVER_BLOG_ID`
   - `QWEN_API_KEY` 또는 `DEEPSEEK_API_KEY` (둘 중 하나 이상)
   - 선택: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - 선택: 이미지 키 (`PEXELS_API_KEY`, `TOGETHER_API_KEY`, `HF_TOKEN` 등)

## 5. 네이버 세션 1회 등록
```bash
python scripts/naver_login.py
```
- 브라우저에서 로그인 완료 후 터미널에서 Enter
- 세션 파일 생성 확인: `data/sessions/naver/state.json`

## 6. 실행 방법
### 대시보드 + API 동시 실행
```bash
bash scripts/start_dev.sh
```
- Frontend: `http://localhost:3000`
- Backend docs: `http://localhost:8000/docs`

### 실발행 단건 테스트
```bash
python scripts/publish_once.py \
  --title "테스트 발행" \
  --keywords "테스트,자동화,블로그" \
  --use-llm \
  --headful
```

## 7. 패키징 (전달용 압축)
- 민감정보/로그/세션/캐시를 제외한 전달용 압축 생성:
```bash
bash scripts/package_test_bundle.sh
```
- 생성 위치:
  - `dist/auto_blog_generator_test_bundle_<timestamp>.tar.gz`

## 8. 문제 발생 시
1. 로그 확인
```bash
bash scripts/status.sh
bash scripts/logs_worker.sh
```
2. 스크린샷/증거 확인
  - `data/screenshots/`
3. 빠른 헬스체크
```bash
python scripts/healthcheck.py
```

## 9. 테스트 완료 판정
- 아래 조건 충족 시 "테스트 사용 준비 완료"
1. 대시보드 접속 성공
2. 실발행 1회 성공
3. 스케줄러 정상 기동 및 로그 출력 확인
4. API 키/세션이 Git에 포함되지 않음 확인
