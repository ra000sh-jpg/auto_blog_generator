/**
 * 한글 UI 라벨 중앙 관리
 *
 * 규칙:
 * - 번역 대상: 버튼/라벨/안내문/에러문구
 * - 비번역 대상: API 경로, enum 값(queued, retry_wait 등), provider id, topic key
 * - job.status 원문은 그대로 유지하고 UI 표기만 이 맵으로 변환
 */

/** 작업 상태 한글 표기 매핑 */
export const STATUS_LABEL: Record<string, string> = {
  queued: "대기 중",
  running: "생성 중",
  publishing: "발행 중",
  ready_to_publish: "발행 대기",
  completed: "완료",
  retry_wait: "재시도 대기",
  failed: "실패",
  cancelled: "취소됨",
};

/** 대기열 통계 키 한글 표기 매핑 */
export const QUEUE_STAT_LABEL: Record<string, string> = {
  queued: "대기",
  running: "생성 중",
  publishing: "발행 중",
  ready_to_publish: "발행 대기",
  completed: "완료",
  retry_wait: "재시도 대기",
  failed: "실패",
  cancelled: "취소됨",
};

/** 네비게이션 라벨 */
export const NAV_LABEL = {
  dashboard: "대시보드",
  jobs: "작업 목록",
  settings: "설정",
} as const;

/** 작업 테이블 헤더 */
export const JOB_TABLE_HEADER = {
  title: "제목",
  status: "상태",
  topicPersona: "토픽 / 페르소나",
  keywords: "키워드",
  scheduled: "예약 시각",
  action: "관리",
} as const;

/** 작업 상세 모달 필드 라벨 */
export const JOB_DETAIL_LABEL = {
  jobId: "작업 ID",
  title: "제목",
  status: "상태",
  platform: "플랫폼",
  persona: "페르소나",
  topic: "토픽",
  category: "카테고리",
  finalContent: "생성된 본문",
} as const;

/** VLM 시각 평가 라벨 */
export const VLM_EVAL_LABEL = {
  totalScore: "시각 품질 점수",
  layout: "레이아웃",
  readability: "가독성",
  imageQuality: "이미지 품질",
  visualConsistency: "시각 일관성",
  overallImpression: "전체 인상",
} as const;

/** 새 작업 폼 필드 라벨 */
export const JOB_FORM_LABEL = {
  title: "제목",
  seedKeywords: "시드 키워드",
  topicMode: "토픽 모드",
  personaId: "페르소나",
  scheduledAt: "예약 시각",
  keywordsOverride: "키워드 지정",
  categoryOverride: "카테고리 지정",
} as const;
