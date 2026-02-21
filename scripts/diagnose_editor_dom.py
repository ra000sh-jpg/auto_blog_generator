"""네이버 스마트에디터 DOM 진단 스크립트."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass


def _print_section(title: str, data: Any) -> None:
    print()
    print(f"[{title}]")
    print(json.dumps(data, ensure_ascii=False, indent=2))


async def _extract_dom(page) -> Dict[str, Any]:
    script = """
    () => {
      const pickAttrs = (el, keys) => {
        const out = {};
        for (const k of keys) {
          const v = el.getAttribute(k);
          if (v !== null && v !== "") out[k] = v;
        }
        return out;
      };

      const fileInputs = Array.from(document.querySelectorAll("input[type='file']")).map((el) => ({
        accept: el.getAttribute("accept") || "",
        name: el.getAttribute("name") || "",
        class: el.getAttribute("class") || "",
        id: el.getAttribute("id") || "",
        hidden: getComputedStyle(el).display === "none" || el.hidden,
      }));

      const toolbarEls = Array.from(document.querySelectorAll("[class*='se-toolbar']")).map((el) => {
        const attr = {};
        for (const { name, value } of Array.from(el.attributes)) {
          if (name.startsWith("data-")) attr[name] = value;
        }
        return {
          text: (el.textContent || "").trim().slice(0, 200),
          class: el.getAttribute("class") || "",
          aria_label: el.getAttribute("aria-label") || "",
          data: attr,
        };
      });

      const imageKeywords = ["이미지", "사진", "image", "photo", "cover", "커버", "썸네일", "thumbnail"];
      const ariaButtons = Array.from(document.querySelectorAll("button[aria-label]"))
        .map((el) => {
          const label = el.getAttribute("aria-label") || "";
          return {
            aria_label: label,
            class: el.getAttribute("class") || "",
            id: el.getAttribute("id") || "",
            text: (el.textContent || "").trim().slice(0, 120),
          };
        })
        .filter((item) => imageKeywords.some((kw) => item.aria_label.toLowerCase().includes(kw.toLowerCase())));

      const coverEls = Array.from(document.querySelectorAll("[class*='cover'], [class*='thumbnail']")).map((el) => ({
        tag: el.tagName.toLowerCase(),
        class: el.getAttribute("class") || "",
        id: el.getAttribute("id") || "",
        aria_label: el.getAttribute("aria-label") || "",
        text: (el.textContent || "").trim().slice(0, 120),
      }));

      const imageButtons = Array.from(document.querySelectorAll("button[class*='image'], a[class*='image']"))
        .slice(0, 5)
        .map((el) => ({
          tag: el.tagName.toLowerCase(),
          class: el.getAttribute("class") || "",
          id: el.getAttribute("id") || "",
          aria_label: el.getAttribute("aria-label") || "",
          text: (el.textContent || "").trim().slice(0, 120),
          href: el.getAttribute("href") || "",
          data: pickAttrs(el, ["data-name", "data-action", "data-command"]),
        }));

      return {
        file_inputs: {
          count: fileInputs.length,
          items: fileInputs,
        },
        se_toolbars: {
          count: toolbarEls.length,
          items: toolbarEls,
        },
        image_related_aria_buttons: {
          count: ariaButtons.length,
          items: ariaButtons,
        },
        cover_or_thumbnail_elements: {
          count: coverEls.length,
          items: coverEls,
        },
        image_class_buttons_top5: {
          count: imageButtons.length,
          items: imageButtons,
        },
      };
    }
    """
    return await page.evaluate(script)


async def main() -> None:
    blog_id = os.getenv("NAVER_BLOG_ID", "").strip()
    if not blog_id:
        raise RuntimeError("NAVER_BLOG_ID 환경변수가 필요합니다.")

    state_path = Path("data/sessions/naver/state.json")
    if not state_path.exists():
        raise RuntimeError(f"세션 파일이 없습니다: {state_path}")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("playwright 설치 필요: pip3 install playwright && python3 -m playwright install chromium") from exc

    write_url = f"https://blog.naver.com/{blog_id}/postwrite"
    analysis: Dict[str, Any] = {}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(state_path))
        page = await context.new_page()
        await page.goto(write_url, wait_until="networkidle", timeout=60_000)
        await asyncio.sleep(5.0)
        analysis = await _extract_dom(page)
        await context.close()
        await browser.close()

    _print_section("1) input[type='file']", analysis.get("file_inputs", {}))
    _print_section("2) [class*='se-toolbar']", analysis.get("se_toolbars", {}))
    _print_section("3) button[aria-label] 이미지 관련", analysis.get("image_related_aria_buttons", {}))
    _print_section("4) [class*='cover'], [class*='thumbnail']", analysis.get("cover_or_thumbnail_elements", {}))
    _print_section("5) [class*='image'] button/a top5", analysis.get("image_class_buttons_top5", {}))

    out_path = Path("data/editor_dom_analysis.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"저장 완료: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
