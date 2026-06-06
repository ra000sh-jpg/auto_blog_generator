"""정부 보고서/보도자료 본문 파서."""

from __future__ import annotations

from io import BytesIO
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict
import zipfile
import xml.etree.ElementTree as ET
import os

try:
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - optional dependency
    BeautifulSoup = None


class MacroDocumentParser:
    """HTML/PDF 문서를 블로그 분석용 텍스트로 변환한다."""

    def parse_html(self, html: str) -> Dict[str, Any]:
        """HTML에서 제목과 본문 텍스트를 추출한다."""
        raw_html = str(html or "")
        if not raw_html.strip():
            return {"title": "", "text": "", "tables": [], "parser": "empty"}

        if BeautifulSoup is None:
            text = self.normalize_text(re.sub(r"<[^>]+>", " ", raw_html))
            return {"title": "", "text": text, "tables": [], "parser": "regex"}

        soup = BeautifulSoup(raw_html, "html.parser")
        for selector in ("script", "style", "nav", "footer", "header", "aside", "form"):
            for node in soup.select(selector):
                node.decompose()

        title = ""
        title_node = soup.find(["h1", "h2", "title"])
        if title_node:
            title = self.normalize_text(title_node.get_text(" ", strip=True))

        tables = []
        for table in soup.find_all("table"):
            table_text = self.normalize_text(table.get_text(" ", strip=True))
            if table_text:
                tables.append(table_text)

        target = soup.find("article") or soup.find("main") or soup.body or soup
        blocks = [
            self.normalize_text(node.get_text(" ", strip=True))
            for node in target.find_all(["p", "li", "td", "th", "div"])
        ]
        text = "\n".join(item for item in blocks if item)
        if not text:
            text = self.normalize_text(target.get_text(" ", strip=True))
        return {
            "title": title,
            "text": self.normalize_text(text),
            "tables": tables[:20],
            "parser": "beautifulsoup",
        }

    def parse_pdf_bytes(self, data: bytes) -> Dict[str, Any]:
        """PDF bytes에서 텍스트와 표를 가능한 여러 엔진으로 추출한다."""
        if not data:
            return {"title": "", "text": "", "tables": [], "parser": "empty_pdf"}
        attempts = []

        pymupdf_result = self._parse_pdf_with_pymupdf(data)
        attempts.append(self._attempt_summary(pymupdf_result))
        if pymupdf_result.get("text"):
            pymupdf_result["attempts"] = attempts
            return pymupdf_result

        pypdf_result = self._parse_pdf_with_pypdf(data)
        attempts.append(self._attempt_summary(pypdf_result))
        if pypdf_result.get("text"):
            pypdf_result["attempts"] = attempts
            return pypdf_result

        pdfplumber_result = self._parse_pdf_with_pdfplumber(data)
        attempts.append(self._attempt_summary(pdfplumber_result))
        if pdfplumber_result.get("text") or pdfplumber_result.get("tables"):
            pdfplumber_result["attempts"] = attempts
            return pdfplumber_result

        return {
            "title": "",
            "text": "",
            "tables": [],
            "parser": "pdf_failed",
            "attempts": attempts,
            "error": "; ".join(str(item.get("error", "")) for item in attempts if item.get("error")),
        }

    def parse_hwpx_bytes(self, data: bytes) -> Dict[str, Any]:
        """HWPX bytes에서 ZIP/XML 구조를 이용해 텍스트를 추출한다."""
        if not data:
            return {"title": "", "text": "", "tables": [], "parser": "empty_hwpx"}
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                names = archive.namelist()
                xml_names = [
                    name
                    for name in names
                    if name.lower().endswith(".xml")
                    and (
                        name.startswith("Contents/")
                        or name.startswith("Preview/")
                        or name.endswith("header.xml")
                    )
                ]
                chunks = []
                tables = []
                for name in sorted(xml_names):
                    try:
                        xml_data = archive.read(name)
                    except Exception:
                        continue
                    parsed = self._extract_xml_text(xml_data)
                    if parsed:
                        chunks.append(parsed)
                    if "/section" in name.lower() or "section" in name.lower():
                        table_text = self._extract_hwpx_table_like_text(xml_data)
                        if table_text:
                            tables.append(table_text)
                return {
                    "title": "",
                    "text": self.normalize_text("\n".join(chunks)),
                    "tables": tables[:20],
                    "parser": "hwpx_zip_xml",
                }
        except Exception as exc:
            return {
                "title": "",
                "text": "",
                "tables": [],
                "parser": "hwpx_failed",
                "error": str(exc),
            }

    def parse_hwp_bytes(self, data: bytes) -> Dict[str, Any]:
        """HWP bytes를 선택적 외부 파서로 텍스트화한다."""
        if not data:
            return {"title": "", "text": "", "tables": [], "parser": "empty_hwp"}
        attempts = []
        for command_name, args in self._hwp_parser_commands():
            if not shutil.which(args[0]):
                attempts.append({"parser": command_name, "status": "missing"})
                continue
            result = self._parse_with_external_command(data, suffix=".hwp", args=args, parser_name=command_name)
            attempts.append(self._attempt_summary(result))
            if result.get("text"):
                result["attempts"] = attempts
                return result
        return {
            "title": "",
            "text": "",
            "tables": [],
            "parser": "unsupported_hwp",
            "attempts": attempts,
            "error": "No local HWP parser command is available.",
        }

    def _hwp_parser_commands(self) -> list[tuple[str, list[str]]]:
        commands = [
            ("kordoc", ["kordoc"]),
            ("unhwp", ["unhwp", "text"]),
            ("hwp5txt", ["hwp5txt"]),
        ]
        if os.environ.get("MACRO_ENABLE_NPX_KORDOC", "").strip().lower() in {"1", "true", "yes"}:
            commands.insert(0, ("npx-kordoc", ["npx", "-y", "kordoc"]))
        return commands

    def _parse_pdf_with_pymupdf(self, data: bytes) -> Dict[str, Any]:
        try:
            import pymupdf  # type: ignore[import-untyped]
        except Exception:
            try:
                import fitz as pymupdf  # type: ignore[import-untyped]
            except Exception:
                return {"title": "", "text": "", "tables": [], "parser": "pymupdf", "error": "pymupdf is not installed"}
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
            chunks = []
            for page_index in range(min(30, len(doc))):
                page = doc[page_index]
                chunks.append(page.get_text("text") or "")
            text = self.normalize_text("\n".join(chunks))
            return {"title": "", "text": text, "tables": [], "parser": "pymupdf"}
        except Exception as exc:
            return {"title": "", "text": "", "tables": [], "parser": "pymupdf", "error": str(exc)}

    def _parse_pdf_with_pypdf(self, data: bytes) -> Dict[str, Any]:
        try:
            from pypdf import PdfReader  # type: ignore[import-untyped]
        except Exception:
            return {
                "title": "",
                "text": "",
                "tables": [],
                "parser": "unsupported_pdf",
                "error": "pypdf is not installed",
            }

        try:
            reader = PdfReader(BytesIO(data))
            chunks = []
            for page in reader.pages[:30]:
                chunks.append(page.extract_text() or "")
            return {
                "title": "",
                "text": self.normalize_text("\n".join(chunks)),
                "tables": [],
                "parser": "pypdf",
            }
        except Exception as exc:
            return {
                "title": "",
                "text": "",
                "tables": [],
                "parser": "pypdf_failed",
                "error": str(exc),
            }

    def _parse_pdf_with_pdfplumber(self, data: bytes) -> Dict[str, Any]:
        try:
            import pdfplumber  # type: ignore[import-untyped]
        except Exception:
            return {"title": "", "text": "", "tables": [], "parser": "pdfplumber", "error": "pdfplumber is not installed"}
        try:
            chunks = []
            tables = []
            with pdfplumber.open(BytesIO(data)) as pdf:
                for page in pdf.pages[:30]:
                    chunks.append(page.extract_text() or "")
                    for table in page.extract_tables() or []:
                        rows = [" | ".join(str(cell or "").strip() for cell in row) for row in table if row]
                        table_text = "\n".join(row for row in rows if row.strip())
                        if table_text:
                            tables.append(table_text)
            return {
                "title": "",
                "text": self.normalize_text("\n".join(chunks)),
                "tables": tables[:20],
                "parser": "pdfplumber",
            }
        except Exception as exc:
            return {"title": "", "text": "", "tables": [], "parser": "pdfplumber", "error": str(exc)}

    def _parse_with_external_command(
        self,
        data: bytes,
        *,
        suffix: str,
        args: list[str],
        parser_name: str,
    ) -> Dict[str, Any]:
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix) as tmp_file:
                tmp_file.write(data)
                tmp_file.flush()
                completed = subprocess.run(
                    [*args, tmp_file.name],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            text = self.normalize_text(completed.stdout)
            error = self.normalize_text(completed.stderr)
            return {
                "title": "",
                "text": text,
                "tables": [],
                "parser": parser_name,
                "error": "" if completed.returncode == 0 else error,
            }
        except Exception as exc:
            return {"title": "", "text": "", "tables": [], "parser": parser_name, "error": str(exc)}

    def _extract_xml_text(self, xml_data: bytes) -> str:
        try:
            root = ET.fromstring(xml_data)
        except Exception:
            return ""
        chunks = []
        for node in root.iter():
            if node.text and node.text.strip():
                chunks.append(node.text.strip())
        return self.normalize_text("\n".join(chunks))

    def _extract_hwpx_table_like_text(self, xml_data: bytes) -> str:
        try:
            root = ET.fromstring(xml_data)
        except Exception:
            return ""
        rows = []
        for node in root.iter():
            tag = node.tag.split("}", 1)[-1].lower()
            if tag not in {"tr", "row"}:
                continue
            cells = []
            for child in node.iter():
                child_tag = child.tag.split("}", 1)[-1].lower()
                if child_tag in {"tc", "cell"}:
                    cell_text = self.normalize_text(" ".join(text.strip() for text in child.itertext() if text.strip()))
                    if cell_text:
                        cells.append(cell_text)
            if cells:
                rows.append(" | ".join(cells))
        return self.normalize_text("\n".join(rows))

    def _attempt_summary(self, parsed: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "parser": str(parsed.get("parser", "")),
            "text_length": len(str(parsed.get("text", "") or "")),
            "table_count": len(parsed.get("tables", []) or []),
            "error": str(parsed.get("error", "") or ""),
            "status": "parsed" if parsed.get("text") or parsed.get("tables") else "failed",
        }

    def normalize_text(self, value: str) -> str:
        """공백과 줄바꿈을 분석 가능한 형태로 정돈한다."""
        text = str(value or "").replace("\xa0", " ")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()
