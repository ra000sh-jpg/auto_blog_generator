"""이미지 생성 단독 테스트 스크립트."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.images import DashScopeImageClient


async def main() -> None:
    parser = argparse.ArgumentParser(description="DashScope 이미지 생성 테스트")
    parser.add_argument("--prompt", default="A beautiful sunset over mountains")
    parser.add_argument("--style", default=", oil painting style, Van Gogh")
    args = parser.parse_args()

    client = DashScopeImageClient()
    try:
        result = await client.generate(
            prompt=args.prompt,
            style_suffix=args.style,
        )
        if result.success:
            print(f"Success! Image saved to: {result.local_path}")
        else:
            print(f"Failed: {result.error_message}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
