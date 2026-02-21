import logging

import sys
from pathlib import Path

# 프로젝트 루트 경로 추가
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.append(str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO)

def main():
    from modules.collectors.naver_datalab import NaverDataLabCollector

    print("=== Testing Naver DataLab Collector ===")

    collector = NaverDataLabCollector()

    # 1. 디지털/가전 키워드 5개 가져오기
    try:
        print("\n[Test 1] Fetching Digital/Appliance keywords...")
        keywords = collector.fetch_trending_keywords("디지털/가전", count=5)
        if keywords:
            print("✅ Success! Collected keywords:")
            for i, kw in enumerate(keywords, 1):
                print(f"  {i}. {kw}")
        else:
            print("❌ Failed or no keywords returned.")
    except Exception as e:
        print(f"❌ Error: {e}")

    # 2. 전체 카테고리 TOP 3 가져오기
    try:
        print("\n[Test 2] Fetching Top 3 keywords for all categories...")
        all_trends = collector.fetch_all_categories(top_n=3)
        if all_trends:
            print("✅ Success! Sample results:")
            count = 0
            for cat, kws in all_trends.items():
                if count >= 3:
                    # 3개 카테고리만 출력한다.
                    break
                print(f"  [{cat}]: {kws}")
                count += 1
        else:
            print("❌ Failed to fetch all categories.")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    main()
