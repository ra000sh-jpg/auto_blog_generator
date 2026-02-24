"""네이버 블로그 발행기에 사용되는 상수 및 CSS 셀렉터 모음."""

BLOG_WRITE_URL = "https://blog.naver.com/{blog_id}/postwrite"

RETRYABLE_ERRORS = frozenset({
    "ELEMENT_NOT_FOUND",
    "NETWORK_TIMEOUT",
    "RATE_LIMITED",
    "PUBLISH_FAILED",
    "UNKNOWN",
})

AI_IMAGE_PREFIXES = (
    "together_",
    "fal_",
    "openai_",
    "dashscope_",
    "pollinations_",
    "huggingface_",
)

THUMBNAIL_PLACEMENT_MODES = frozenset({"cover", "body_top"})
AI_TOGGLE_MODES = frozenset({"off", "metadata", "force"})

TITLE_SELECTORS = [
    ".se-section-documentTitle .se-text-paragraph",
    ".se-title-text",
    "[data-component='documentTitle'] [contenteditable]",
    ".se-section-title .se-text-paragraph",
]

BODY_SELECTORS = [
    ".se-section-text .se-text-paragraph",
    ".se-content .se-text-paragraph",
    ".se-component-content .se-text-paragraph",
    ".se-main-container [contenteditable='true']:not([data-se-doc-title])",
]

# 작성 중인 글 복구 팝업 취소 버튼
DRAFT_CANCEL_SELECTORS = [
    "[role='dialog'] button:has-text('취소')",
    "[class*='dialog'] button:has-text('취소')",
    "[class*='modal'] button:has-text('취소')",
    "[class*='popup'] button:has-text('취소')",
    "[class*='layer'] button:has-text('취소')",
    "[role='dialog'] [role='button']:has-text('취소')",
    "[class*='dialog'] [role='button']:has-text('취소')",
    "button:has-text('취소')",
    "[role='button']:has-text('취소')",
    "text=취소",
]

PUBLISH_BTN_1_SELECTORS = [
    ".se-header button:has-text('발행')",
    "button.publish_btn__m9KHH",
    "button[class*='publish_btn']",
    "button:has-text('발행')",
]

PUBLISH_BTN_2_SELECTORS = [
    ".layer_result button.confirm_btn__WEaBq",
    ".layer_result button:has-text('발행')",
    "button[class*='confirm_btn']",
    "button.confirm_btn",
]
