import { DEFAULT_FALLBACK_CATEGORY, type ScheduleAllocationItem } from "@/lib/api";

export function inferTopicMode(categoryName: string): string {
    const lowered = categoryName.toLowerCase();
    if (["경제", "finance", "투자", "주식", "재테크"].some((token) => lowered.includes(token))) {
        return "finance";
    }
    if (["it", "개발", "코드", "자동화", "ai", "테크"].some((token) => lowered.includes(token))) {
        return "it";
    }
    if (["육아", "아이", "부모", "가정"].some((token) => lowered.includes(token))) {
        return "parenting";
    }
    return "cafe";
}

export function normalizeAllocations(
    categories: string[],
    target: number, // Legacy (더 이상 사용 안 하지만 타입 호환성 위해 유지)
    existingAllocations: ScheduleAllocationItem[] = [],
): ScheduleAllocationItem[] {
    const normalizedCategories = categories
        .map((value) => value.trim())
        .filter((value, index, list) => value.length > 0 && list.indexOf(value) === index);
    const fallbackCategories = normalizedCategories.length > 0 ? normalizedCategories : [DEFAULT_FALLBACK_CATEGORY];

    const existingMap = new Map(existingAllocations.map((item) => [item.category, item]));
    const totalCount = existingAllocations.reduce((acc, item) => acc + (item.count || 0), 0);

    const rows: ScheduleAllocationItem[] = fallbackCategories.map((categoryName) => {
        const existing = existingMap.get(categoryName);
        let pct = existing?.percentage;
        if (pct === undefined || pct === null) {
            pct = totalCount > 0 ? ((existing?.count || 0) / totalCount) * 100 : 0;
        }
        const snappedPct = Math.round(Math.max(0, Number(pct)) / 5) * 5;

        return {
            category: categoryName,
            topic_mode: existing?.topic_mode || inferTopicMode(categoryName),
            count: existing?.count || 0,
            percentage: snappedPct,
        };
    });

    if (rows.length === 0) return [];

    let total = rows.reduce((acc, item) => acc + (item.percentage || 0), 0);

    // 합계가 0이면(초기 상태) 첫번째 항목에 몰아주거나 균등 분배 시도
    if (total === 0) {
        rows[0].percentage = 100;
        return rows;
    }

    // 100% 맞추기 로직
    if (total < 100) {
        // 남은 비율을 가장 비중이 큰 쪽에 몰아주기
        const maxIndex = rows.reduce((maxIdx, current, idx, arr) => (current.percentage || 0) > (arr[maxIdx].percentage || 0) ? idx : maxIdx, 0);
        rows[maxIndex].percentage = (rows[maxIndex].percentage || 0) + (100 - total);
        return rows;
    }

    if (total > 100) {
        // 초과 비율 빼기
        let overflow = total - 100;
        for (let index = rows.length - 1; index >= 0; index -= 1) {
            if (overflow <= 0) break;
            const currentPct = rows[index].percentage || 0;
            const deductible = Math.min(currentPct, overflow);
            // 최소 0%까지만 차감
            rows[index].percentage = currentPct - deductible;
            overflow -= deductible;
        }
    }

    // 마지막으로 소수점 및 5단위 오차 교정 (총합 100 보장)
    total = rows.reduce((acc, item) => acc + (item.percentage || 0), 0);
    if (total !== 100) {
        rows[0].percentage = (rows[0].percentage || 0) + (100 - total);
    }

    return rows;
}

export function formatKrw(value: number): string {
    return new Intl.NumberFormat("ko-KR").format(Math.max(0, Math.round(value)));
}

export function compactKeys(input: Record<string, string>): Record<string, string> {
    return Object.entries(input).reduce<Record<string, string>>((acc, [key, value]) => {
        const normalized = String(value || "").trim();
        if (normalized) {
            acc[key] = normalized;
        }
        return acc;
    }, {});
}

export function parseCommaValues(input: string): string[] {
    return String(input || "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
}
