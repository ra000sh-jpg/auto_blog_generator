import asyncio
from modules.automation.job_store import JobStore
from modules.llm.llm_router import LLMRouter

store = JobStore(db_path="data/automation.db")
router = LLMRouter(job_store=store)
print(router.get_saved_settings()["text_api_keys"])
