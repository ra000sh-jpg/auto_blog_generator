---
title: "Telegram Onboarding UX Improvement Blueprint"
status: "planning"
---

## 텔레그램 연동 UX 개선 청사진 (Idea 1 + Codex 권장안 A)

## 1. 개요

현재 텔레그램 봇 토큰과 Chat ID를 얻어 입력하는 과정이 사용자 친화적이지 않아 이탈이 발생할 수 있습니다.
온보딩의 마지막 단계(Step 5)와 설정(Settings) 페이지의 텔레그램 연동 UI를 3-Step 직관적 가이드 방식으로 전면 개편하고, 어려운 **Chat ID 획득 과정을 자동화**합니다.
*본 설계는 기존 Webhook 충돌 방지 및 보안을 고려하여 작성되었습니다.*

---

## 2. 프론트엔드 UI 개선 (3-Step 가이드)

### Step 1: 봇 만들기 (BotFather)

- **UI 요소:**
  - 텔레그램 호환성을 위한 이중 버튼: `tg://resolve?domain=BotFather` (앱) 및 `https://t.me/BotFather` (웹)
  - 스마트폰 스캔용 **QR 코드** (`qrcode.react` 라이브러리로 클라이언트 렌더링 - 런타임 안정성 및 외부 의존도 제거)
- **텍스트 가이드:**
  - 카메라로 QR을 스캔하거나 버튼을 눌러 **BotFather** 대화창을 여세요.
  - `/newbot` 입력 후 봇 이름과 아이디(`_bot` 끝)를 순서대로 입력하세요.

### Step 2: 토큰 입력 및 1/2차 검증

- **UI 요소:**
  - 봇 토큰 입력란 (기존에 연동된 경우 마스킹된 토큰은 placeholder로만 노출, value는 빈 값).
  - [토큰 확인] 버튼 및 완료 체크마크.
- **동작 로직:**
  1. **프론트 1차 방어:** 정규식(`^[0-9]+:[a-zA-Z0-9_-]+$`) 검사 수행.
  2. **서버 2차 검증:** `/api/telegram/verify_token` (가칭) 호출로 Telegram `getMe` 검증 및 `bot_username` 반환.

### Step 3: 웹훅 인증코드로 Chat ID 자동 획득 (강력 권장안 A)

- **UI 요소:**
  - 내 봇 링크 및 QR 코드 제공.
  - 복사 가능한 **전용 인증 명령어** 노출 (예: `/start autoblog_1a2b3c`)
  - [연동 확인 및 완료] 버튼.
- **텍스트 가이드:**
  - 우측의 내 봇을 열고, 아래의 **`/start autoblog_...`** 명령어를 복사하여 전송하세요.
  - 메시지를 보낸 후 [연동 확인 및 완료]를 눌러주세요.
- **동작 로직:**
  - 단일 채팅(Private)에서의 인증 메시지만 허용하며, 그룹/타인 오탐을 방지하기 위해 **1회용 인증코드 (TTL 5분)** 사용.

---

## 3. 백엔드 로직 설계 (Webhook 우선 + 폴백)

기존 시스템이 텔레그램 Webhook을 사용 중이므로, `getUpdates` 단독 사용 시 충돌 리스크가 큽니다. 따라서 Webhook으로 인증코드를 가로채는 방식을 기본으로 합니다.

### 인증 플로우

1. **토큰 검증 API (`POST /api/telegram/verify_token`):**
   - 텔레그램 `getMe` 호출.
   - 유효하면 6자리 고유 인증코드(`auth_code`) 생성 후 임시 캐시(Redis/메모리)에 5분간 저장 및 프론트에 반환.
2. **Webhook 메시지 수신 (`POST /webhook/telegram`):**
   - 사용자가 전송한 메시지가 `/start autoblog_<auth_code>` 형태인지 검사.
   - Private 챗인지 확인 후, 캐시의 인증코드와 일치하면 해당 `chat_id`를 임시로 승인(Authenticated) 상태로 저장.
3. **최종 연동 완료 API (`POST /api/telegram/verify`):**
   - 프론트엔드가 호출하면 백엔드가 해당 인증코드가 승인되었는지 확인.
   - 성공 시 기존 `telegram_chat_id`를 덮어쓰고 시스템 세팅에 안전하게 마스킹 형태로 저장.
   - `getUpdates`는 Webhook 설정 전이거나 유실 시에만 대비하는 폴백 수단으로 후순위 배치.

### 추가 보안 요구사항 (P0)

- 비밀값(토큰/시크릿)은 서버 로그 및 응답 API에서 완전 마스킹 처리할 것.
- 기존 연동자가 재연동 시 [토큰 덮어쓰기] 확인 모달 등 보호 장치 제공.
