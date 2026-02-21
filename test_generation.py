import asyncio
import os
import uuid
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path("/Users/naseunghwan/Desktop/auto_blog_generator/.env"))

from modules.config import load_config
from modules.llm import get_generator, llm_generate_fn
from modules.automation.job_store import Job
from modules.automation.time_utils import now_utc

async def main():
    app_config = load_config()
    get_generator(app_config.llm)
    
    job = Job(
        job_id=str(uuid.uuid4()),
        title="[테스트] 에코프로와 금리인하 분석",
        seed_keywords=["에코프로", "금리인하"],
        platform="naver",
        persona_id="P4",
        scheduled_at=now_utc(),
        status="running",
        category="경제·비즈니스",
        tags=[]
    )
    
    # RAG 뉴스 수집 및 LLM 생성
    result = await llm_generate_fn(job)
    content = result.get("final_content", "")
    
    os.makedirs("data/drafts", exist_ok=True)
    with open("data/drafts/test_economy_post.md", "w") as f:
        f.write(content)
    
    print("SUCCESS: data/drafts/test_economy_post.md")

if __name__ == "__main__":
    asyncio.run(main())
