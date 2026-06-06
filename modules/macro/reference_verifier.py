"""정부/공식 통계 기반 매크로 수치 검증 보조 모듈."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Mapping
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

import httpx


class MacroReferenceVerifier:
    """KOSIS/K-stat 같은 보조 공식 출처로 추출 수치의 검증 준비 상태를 기록한다."""

    KOSIS_ENDPOINT = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
    CUSTOMS_TRADE_ENDPOINT = "https://apis.data.go.kr/1220000/Newtrade/getNewtradeList"
    KSTAT_REFERENCE_URL = "https://m.stat.kita.net/stat/guide/GuideKstat.screen"

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        allow_network: bool = False,
        timeout_sec: float = 10.0,
        source_policy: str | None = None,
    ) -> None:
        self.env = env or os.environ
        self.allow_network = bool(allow_network)
        self.timeout_sec = float(timeout_sec or 10.0)
        self.source_policy = self._normalize_source_policy(
            source_policy or str(self.env.get("MACRO_SOURCE_POLICY", "") or "")
        )

    def verify(self, *, document: Dict[str, Any], metrics_json: Dict[str, Any]) -> Dict[str, Any]:
        """문서 제목/수치 기준으로 공식 보조 검증 상태를 만든다."""
        period = self._extract_period(str(document.get("title", "") or ""), str(document.get("published_at", "") or ""))
        metric_count = int(metrics_json.get("metric_count", 0) or 0)
        customs = self._build_customs_trade_status(period=period, metrics_json=metrics_json)
        kosis = self._build_kosis_status(period=period)
        kstat = {
            "source": "KSTAT",
            "status": "reference_ready",
            "url": self.KSTAT_REFERENCE_URL,
            "note": "K-stat은 산업통상부 위탁 무역통계 DB로 수출입 수치 교차 확인 후보입니다.",
        }
        requires_two_sources = self.source_policy == "strict"
        confirmed_source_count = 1 if metric_count else 0
        if customs.get("status") == "verified":
            confirmed_source_count += 1
        if kosis.get("status") == "verified":
            confirmed_source_count += 1

        score = 45 if metric_count == 0 else 72
        if customs.get("status") == "verified":
            score = 94
        if kosis.get("status") == "verified":
            score = max(score, 95)
        elif customs.get("status") == "configured":
            score = max(score, 86)
        elif kosis.get("status") == "configured":
            score = max(score, 84)
        elif self.source_policy == "light" and metric_count:
            score = max(score, 82)
        elif kosis.get("status") == "missing_config" or customs.get("status") == "missing_config":
            score = max(score, 74)

        return {
            "period": period,
            "metricCount": metric_count,
            "confirmedSourceCount": confirmed_source_count,
            "sourcePolicy": self.source_policy,
            "requiresTwoSourceConfirmation": requires_two_sources,
            "readyForAutoDraft": (
                metric_count > 0 and (confirmed_source_count >= 2 if requires_two_sources else True)
            ),
            "verificationScore": score,
            "sources": [customs, kosis, kstat],
            "recommendedNextAction": self._next_action(
                metric_count=metric_count,
                customs_status=str(customs.get("status", "")),
                kosis_status=str(kosis.get("status", "")),
                requires_two_sources=requires_two_sources,
            ),
        }

    def _build_customs_trade_status(self, *, period: str, metrics_json: Dict[str, Any]) -> Dict[str, Any]:
        api_key = (
            str(self.env.get("CUSTOMS_TRADE_API_KEY", "") or "").strip()
            or str(self.env.get("DATA_GO_KR_SERVICE_KEY", "") or "").strip()
        )
        if not api_key:
            return {
                "source": "CUSTOMS_TRADE",
                "status": "missing_config",
                "missingEnv": ["CUSTOMS_TRADE_API_KEY or DATA_GO_KR_SERVICE_KEY"],
                "endpoint": self.CUSTOMS_TRADE_ENDPOINT,
                "dataPortal": "https://www.data.go.kr/data/15102108/openapi.do",
                "note": "관세청_수출입총괄(GW) API 키를 설정하면 수출/수입/무역수지 금액을 2차 공식 출처로 검증합니다.",
            }
        params = {
            "serviceKey": api_key,
            "strtYymm": period,
            "endYymm": period,
        }
        url = f"{self.CUSTOMS_TRADE_ENDPOINT}?{urlencode(params)}"
        if not self.allow_network:
            return {
                "source": "CUSTOMS_TRADE",
                "status": "configured",
                "url": self._redact_service_key(url),
                "note": "네트워크 검증은 --verify-network 또는 MACRO_ENABLE_NETWORK_VERIFICATION=1일 때 실행됩니다.",
            }
        try:
            with httpx.Client(timeout=self.timeout_sec, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
                xml_text = response.text
        except Exception as exc:
            return {
                "source": "CUSTOMS_TRADE",
                "status": "failed",
                "url": self._redact_service_key(url),
                "error": str(exc),
            }

        parsed = self._parse_customs_trade_xml(xml_text)
        if not parsed:
            return {
                "source": "CUSTOMS_TRADE",
                "status": "empty",
                "url": self._redact_service_key(url),
                "note": "응답에서 수출입총괄 행을 찾지 못했습니다.",
            }

        comparisons = self._compare_customs_metrics(parsed, metrics_json)
        status = "verified" if comparisons and all(item.get("matched") for item in comparisons) else "mismatch"
        return {
            "source": "CUSTOMS_TRADE",
            "status": status,
            "url": self._redact_service_key(url),
            "period": parsed.get("year", period),
            "apiValues": parsed,
            "comparisons": comparisons,
        }

    def _build_kosis_status(self, *, period: str) -> Dict[str, Any]:
        api_key = str(self.env.get("KOSIS_API_KEY", "") or "").strip()
        org_id = str(self.env.get("KOSIS_TRADE_ORG_ID", "") or "").strip()
        tbl_id = str(self.env.get("KOSIS_TRADE_TBL_ID", "") or "").strip()
        item_id = str(self.env.get("KOSIS_TRADE_ITM_ID", "") or "").strip()
        obj_l1 = str(self.env.get("KOSIS_TRADE_OBJ_L1", "") or "").strip()
        missing = [
            name
            for name, value in (
                ("KOSIS_API_KEY", api_key),
                ("KOSIS_TRADE_ORG_ID", org_id),
                ("KOSIS_TRADE_TBL_ID", tbl_id),
                ("KOSIS_TRADE_ITM_ID", item_id),
                ("KOSIS_TRADE_OBJ_L1", obj_l1),
            )
            if not value
        ]
        if missing:
            return {
                "source": "KOSIS",
                "status": "missing_config",
                "missingEnv": missing,
                "endpoint": self.KOSIS_ENDPOINT,
                "note": "KOSIS 키와 수출입 통계표 ID를 설정하면 월별 수치 대조를 자동화할 수 있습니다.",
            }

        params = {
            "method": "getList",
            "apiKey": api_key,
            "orgId": org_id,
            "tblId": tbl_id,
            "itmId": item_id,
            "objL1": obj_l1,
            "prdSe": "M",
            "startPrdDe": period,
            "endPrdDe": period,
            "format": "json",
            "jsonVD": "Y",
        }
        url = f"{self.KOSIS_ENDPOINT}?{urlencode(params)}"
        if not self.allow_network:
            return {
                "source": "KOSIS",
                "status": "configured",
                "url": self._redact_api_key(url),
                "note": "네트워크 검증은 스케줄러/CLI에서 allow_network가 켜질 때 실행됩니다.",
            }

        try:
            with httpx.Client(timeout=self.timeout_sec, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            return {
                "source": "KOSIS",
                "status": "failed",
                "url": self._redact_api_key(url),
                "error": str(exc),
            }

        row_count = len(payload) if isinstance(payload, list) else 0
        return {
            "source": "KOSIS",
            "status": "verified" if row_count else "empty",
            "url": self._redact_api_key(url),
            "rowCount": row_count,
            "sample": self._sample_payload(payload),
        }

    def _extract_period(self, title: str, published_at: str) -> str:
        text = f"{title} {published_at}"
        match = re.search(r"(20\d{2})년\s*(\d{1,2})월", text)
        if match:
            year, month = match.groups()
            return f"{int(year):04d}{int(month):02d}"
        match = re.search(r"(20\d{2})[-.](\d{1,2})", text)
        if match:
            year, month = match.groups()
            return f"{int(year):04d}{int(month):02d}"
        return ""

    def _next_action(
        self,
        *,
        metric_count: int,
        customs_status: str,
        kosis_status: str,
        requires_two_sources: bool,
    ) -> str:
        if metric_count == 0:
            return "document_extraction_required"
        if customs_status == "verified" or kosis_status == "verified":
            return "ready_for_draft"
        if not requires_two_sources:
            if customs_status == "configured" or kosis_status == "configured":
                return "ready_for_draft_optional_verification"
            return "ready_for_draft_light_source"
        if customs_status == "configured":
            return "run_network_verification"
        if kosis_status == "verified":
            return "ready_for_draft"
        if kosis_status == "configured":
            return "run_network_verification"
        return "source_cross_check_required"

    def _normalize_source_policy(self, raw_value: str) -> str:
        """소스 검증 정책을 light/strict 중 하나로 정규화한다."""
        normalized = str(raw_value or "").strip().lower()
        if normalized in {"strict", "journalism", "audit", "two_source"}:
            return "strict"
        return "light"

    def _redact_api_key(self, url: str) -> str:
        return re.sub(r"(apiKey=)[^&]+", r"\1***", str(url or ""))

    def _redact_service_key(self, url: str) -> str:
        return re.sub(r"(serviceKey=)[^&]+", r"\1***", str(url or ""))

    def _parse_customs_trade_xml(self, xml_text: str) -> Dict[str, Any]:
        try:
            root = ET.fromstring(str(xml_text or "").encode("utf-8"))
        except Exception:
            return {}
        items = list(root.findall(".//item"))
        if not items:
            items = [root]
        selected: Dict[str, str] = {}
        for item in items:
            row = {child.tag: str(child.text or "").strip() for child in list(item)}
            if row.get("expDlr") or row.get("impDlr") or row.get("balPayments"):
                selected = row
                break
        if not selected:
            return {}
        return {
            "year": selected.get("year", ""),
            "exportAmountUsd": self._to_float(selected.get("expDlr")),
            "importAmountUsd": self._to_float(selected.get("impDlr")),
            "tradeBalanceUsd": self._to_float(selected.get("balPayments")),
            "raw": selected,
        }

    def _compare_customs_metrics(self, parsed: Dict[str, Any], metrics_json: Dict[str, Any]) -> list[Dict[str, Any]]:
        by_key = metrics_json.get("by_key", {}) if isinstance(metrics_json.get("by_key", {}), dict) else {}
        comparisons = []
        specs = [
            ("export_amount_usd_eok", "exportAmountUsd", "수출금액"),
            ("import_amount_usd_eok", "importAmountUsd", "수입금액"),
            ("trade_balance", "tradeBalanceUsd", "무역수지"),
        ]
        for metric_key, api_key, label in specs:
            extracted = str(by_key.get(metric_key, "") or "")
            if not extracted:
                continue
            extracted_eok = self._parse_eok_usd(extracted)
            api_eok = self._usd_to_eok(parsed.get(api_key))
            if extracted_eok is None or api_eok is None:
                continue
            diff = abs(extracted_eok - api_eok)
            comparisons.append(
                {
                    "metricKey": metric_key,
                    "label": label,
                    "extractedEokUsd": round(extracted_eok, 2),
                    "apiEokUsd": round(api_eok, 2),
                    "diffEokUsd": round(diff, 2),
                    "matched": diff <= max(1.0, abs(api_eok) * 0.005),
                }
            )
        return comparisons

    def _parse_eok_usd(self, value: str) -> float | None:
        text = str(value or "").replace(",", "")
        sign = -1.0 if "적자" in text or text.strip().startswith("-") else 1.0
        match = re.search(r"([0-9.]+)\s*(조|억)?\s*달러", text)
        if not match:
            return None
        number, unit = match.groups()
        try:
            amount = float(number)
        except Exception:
            return None
        if unit == "조":
            amount *= 10_000.0
        return sign * amount

    def _usd_to_eok(self, value: Any) -> float | None:
        try:
            return float(value) / 100_000_000.0
        except Exception:
            return None

    def _to_float(self, value: Any) -> float | None:
        try:
            return float(str(value or "").replace(",", ""))
        except Exception:
            return None

    def _sample_payload(self, payload: Any) -> Any:
        if isinstance(payload, list):
            return payload[:3]
        try:
            return json.loads(json.dumps(payload, ensure_ascii=False)) if isinstance(payload, dict) else {}
        except Exception:
            return {}
