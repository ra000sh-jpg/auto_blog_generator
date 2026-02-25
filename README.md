# Auto Blog Generator

## 설치
```bash
curl -fsSL https://raw.githubusercontent.com/ra000sh-jpg/auto_blog_generator/main/scripts/install.sh | bash
```

## 업데이트
```bash
auto-blog update
```

또는 대시보드 우상단 `업데이트 확인` 버튼을 클릭하세요.

## 명령어
- `auto-blog start`
- `auto-blog stop`
- `auto-blog restart`
- `auto-blog status`
- `auto-blog logs`
- `auto-blog update`

## 초기 설정
프로젝트 루트의 `.env` 파일을 열어 아래 섹션 값을 채우세요.

- `LLM API Keys`
  - `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `TOGETHER_API_KEY`, `DASHSCOPE_API_KEY`
  - LLM 제공자 API 키와 Claude 모델 관련 기본값을 설정합니다.

- `Image API Keys`
  - `PEXELS_API_KEY`, `FAL_KEY`, `HF_TOKEN`
  - 이미지 생성/수집 관련 API 키와 이미지 동작 옵션을 설정합니다.

- `Naver Blog Credentials`
  - `NAVER_BLOG_ID`, `PLAYWRIGHT_HEADLESS`, `THUMBNAIL_PLACEMENT_MODE`
  - 네이버 발행 계정/브라우저 실행 모드/썸네일 배치를 설정합니다.

- `Server Settings`
  - `AUTOBLOG_API_TOKEN`, `AUTOBLOG_DB_PATH`, `LOG_LEVEL`, `LOG_FORMAT`, `DRY_RUN`
  - API 인증, DB 경로, 로그 포맷, 드라이런 모드를 설정합니다.

- `Scheduler Settings`
  - `SCHEDULER_DISABLED`
  - 스케줄러 자동 실행 여부를 설정합니다.

- `Telegram Settings`
  - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_WEBHOOK_SECRET`
  - 텔레그램 알림/웹훅 연동값을 설정합니다.
