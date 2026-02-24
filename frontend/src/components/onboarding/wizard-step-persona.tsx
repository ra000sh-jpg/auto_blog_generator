"use client";

import { useMemo, useState } from "react";
import {
    savePersonaLab,
    type OnboardingStatusResponse,
    type PersonaQuestionBankResponse,
    type PersonaQuestionItem,
} from "@/lib/api";
import { parseCommaValues } from "@/lib/utils/formatters";

// --- Helper Functions from onboarding-wizard.tsx ---

const QUESTIONNAIRE_DIMENSIONS = [
    "structure",
    "evidence",
    "distance",
    "criticism",
    "density",
] as const;

type QuestionnaireScores = {
    structure: number;
    evidence: number;
    distance: number;
    criticism: number;
    density: number;
};

type QuestionnairePreview = {
    scores: QuestionnaireScores;
    answeredCount: number;
    requiredCount: number;
    completionRatio: number;
};

type PersonaSummaryCard = {
    title: string;
    subtitle: string;
    tags: string[];
};

type RadarGeometry = {
    dataPoints: string;
    basePoints: string;
    axes: Array<{ x1: number; y1: number; x2: number; y2: number; labelX: number; labelY: number; label: string }>;
};

function calculateQuestionnairePreview(
    questions: PersonaQuestionItem[],
    answers: Record<string, string>,
    requiredCount: number,
): QuestionnairePreview {
    const baseScores: QuestionnaireScores = {
        structure: 50,
        evidence: 50,
        distance: 50,
        criticism: 50,
        density: 50,
    };

    if (questions.length === 0) {
        return {
            scores: baseScores,
            answeredCount: 0,
            requiredCount: Math.max(1, requiredCount || 5),
            completionRatio: 0,
        };
    }

    const resolvedAnswers = new Map<string, string>();
    for (const [questionId, optionId] of Object.entries(answers)) {
        if (!questionId || !optionId) continue;
        resolvedAnswers.set(questionId, optionId);
    }

    const idealCaps: Record<string, number> = {};
    const weightedSums: Record<string, number> = {};
    const weightedCaps: Record<string, number> = {};
    for (const dimension of QUESTIONNAIRE_DIMENSIONS) {
        idealCaps[dimension] = 0;
        weightedSums[dimension] = 0;
        weightedCaps[dimension] = 0;
    }

    let answeredCount = 0;
    for (const question of questions) {
        const safeWeight = Math.max(1, Number(question.weight || 1));
        for (const dimension of QUESTIONNAIRE_DIMENSIONS) {
            const hasEffect = question.options.some(
                (option) => Number(option.effects?.[dimension] || 0) !== 0,
            );
            if (hasEffect) {
                idealCaps[dimension] += 2 * safeWeight;
            }
        }

        const selectedOptionId = resolvedAnswers.get(question.question_id);
        if (!selectedOptionId) continue;
        const selectedOption = question.options.find((option) => option.option_id === selectedOptionId);
        if (!selectedOption) continue;
        answeredCount += 1;

        for (const dimension of QUESTIONNAIRE_DIMENSIONS) {
            const effect = Number(selectedOption.effects?.[dimension] || 0);
            if (effect === 0) continue;
            weightedSums[dimension] += effect * safeWeight;
            weightedCaps[dimension] += 2 * safeWeight;
        }
    }

    const nextScores: QuestionnaireScores = { ...baseScores };
    for (const dimension of QUESTIONNAIRE_DIMENSIONS) {
        const cap = weightedCaps[dimension];
        if (cap <= 0) {
            nextScores[dimension] = 50;
            continue;
        }
        const normalized = weightedSums[dimension] / cap;
        const rawScore = Math.round(50 + normalized * 35);
        nextScores[dimension] = Math.max(0, Math.min(100, rawScore));
    }

    const safeRequiredCount = Math.max(1, Math.min(questions.length, requiredCount || 5));
    return {
        scores: nextScores,
        answeredCount,
        requiredCount: safeRequiredCount,
        completionRatio: Number((answeredCount / questions.length).toFixed(3)),
    };
}

function derivePersonaSummary(scores: QuestionnaireScores): PersonaSummaryCard {
    const { structure, evidence, distance, criticism, density } = scores;
    const isHigh = (value: number) => value >= 65;
    const isLow = (value: number) => value <= 35;

    if (isHigh(structure) && isHigh(evidence) && isHigh(criticism)) {
        return {
            title: "냉철한 팩트폭격기",
            subtitle: "데이터 중심으로 논점을 날카롭게 정리하는 분석형 페르소나",
            tags: ["#두괄식", "#근거중심", "#직설형"],
        };
    }
    if (isHigh(structure) && isHigh(evidence) && isHigh(distance)) {
        return {
            title: "분석적 전문가",
            subtitle: "권위 있는 톤으로 근거와 논리를 촘촘히 전달하는 타입",
            tags: ["#전문가톤", "#체계적", "#객관성"],
        };
    }
    if (isLow(distance) && isLow(criticism) && isLow(density)) {
        return {
            title: "친근한 생활 코치",
            subtitle: "부담 없이 읽히는 말투로 실전 팁을 전달하는 공감형 페르소나",
            tags: ["#친근한톤", "#부드러운피드백", "#가독성"],
        };
    }
    if (isHigh(density) && isHigh(structure)) {
        return {
            title: "치밀한 아카이버",
            subtitle: "정보량과 구조를 동시에 챙기는 리서치형 페르소나",
            tags: ["#정보밀도", "#체계정리", "#실무형"],
        };
    }
    return {
        title: "균형 잡힌 실전 가이드",
        subtitle: "상황에 맞게 톤과 강도를 조절하는 하이브리드 페르소나",
        tags: ["#균형형", "#실전중심", "#유연한스타일"],
    };
}

function buildRadarGeometry(scores: QuestionnaireScores): RadarGeometry {
    const axes = [
        { key: "structure", label: "구조" },
        { key: "evidence", label: "근거" },
        { key: "distance", label: "거리" },
        { key: "criticism", label: "비판" },
        { key: "density", label: "밀도" },
    ] as const;

    const center = 90;
    const radius = 68;
    const step = (Math.PI * 2) / axes.length;
    const startAngle = -Math.PI / 2;

    const dataPoints: string[] = [];
    const basePoints: string[] = [];
    const lines: RadarGeometry["axes"] = [];

    axes.forEach((axis, index) => {
        const angle = startAngle + step * index;
        const maxX = center + radius * Math.cos(angle);
        const maxY = center + radius * Math.sin(angle);
        const value = Math.max(0, Math.min(100, Number(scores[axis.key])));
        const ratio = value / 100;
        const px = center + radius * ratio * Math.cos(angle);
        const py = center + radius * ratio * Math.sin(angle);

        dataPoints.push(`${px.toFixed(2)},${py.toFixed(2)}`);
        basePoints.push(`${maxX.toFixed(2)},${maxY.toFixed(2)}`);
        lines.push({
            x1: center,
            y1: center,
            x2: maxX,
            y2: maxY,
            labelX: center + (radius + 18) * Math.cos(angle),
            labelY: center + (radius + 18) * Math.sin(angle),
            label: axis.label,
        });
    });

    return {
        dataPoints: dataPoints.join(" "),
        basePoints: basePoints.join(" "),
        axes: lines,
    };
}

// --- End Helper Functions ---

type WizardStepPersonaProps = {
    initialOnboardingStatus: OnboardingStatusResponse;
    questionBank: PersonaQuestionBankResponse | null;
    onNext: () => void;
    onPrev: () => void;
};

export default function WizardStepPersona({
    initialOnboardingStatus,
    questionBank,
    onNext,
    onPrev,
}: WizardStepPersonaProps) {
    const vp = initialOnboardingStatus.voice_profile;
    const _meta = vp?.questionnaire_meta as Record<string, unknown> | undefined;
    const initialAnswers: Record<string, string> = {};
    if (_meta?.resolved_answers && Array.isArray(_meta.resolved_answers)) {
        for (const item of _meta.resolved_answers) {
            if (!item || typeof item !== "object") continue;
            const payload = item as Record<string, unknown>;
            const qId = String(payload.question_id || "").trim();
            const oId = String(payload.option_id || "").trim();
            if (qId && oId) {
                initialAnswers[qId] = oId;
            }
        }
    }

    const savedMbti = ((vp?.mbti as string) || "").trim().toUpperCase();

    const [personaId] = useState(initialOnboardingStatus.persona_id || "P1");
    const [identity, setIdentity] = useState((vp?.identity as string) || "");
    const [toneHint, setToneHint] = useState((vp?.tone_hint as string) || "");
    const [interestsText, setInterestsText] = useState((initialOnboardingStatus.interests || []).join(", "));
    const [questionAnswers, setQuestionAnswers] = useState<Record<string, string>>(initialAnswers);
    const [mbtiEnabled, setMbtiEnabled] = useState(Boolean(vp?.mbti_enabled && savedMbti));
    const [mbti, setMbti] = useState(savedMbti);
    const [mbtiConfidence, setMbtiConfidence] = useState(Math.max(0, Math.min(100, Number(vp?.mbti_confidence ?? 70))));
    const [ageGroup, setAgeGroup] = useState((vp?.age_group as string) || "30대");
    const [gender, setGender] = useState((vp?.gender as string) || "남성");

    const [saving, setSaving] = useState(false);
    const [stepMessage, setStepMessage] = useState("");

    const mbtiWeightPercent = useMemo(() => {
        if (!mbtiEnabled) return 0;
        return Math.round(10 + (mbtiConfidence / 100) * 10);
    }, [mbtiEnabled, mbtiConfidence]);

    const questionnairePreview = useMemo(
        () =>
            calculateQuestionnairePreview(
                questionBank?.questions || [],
                questionAnswers,
                questionBank?.required_count || 5,
            ),
        [questionBank, questionAnswers],
    );
    const personaSummary = useMemo(
        () => derivePersonaSummary(questionnairePreview.scores),
        [questionnairePreview.scores],
    );
    const radarGeometry = useMemo(
        () => buildRadarGeometry(questionnairePreview.scores),
        [questionnairePreview.scores],
    );

    function handleQuestionSelect(questionId: string, optionId: string) {
        setQuestionAnswers((previous) => ({
            ...previous,
            [questionId]: optionId,
        }));
    }

    async function handleSavePersonaStep() {
        setSaving(true);
        setStepMessage("");
        try {
            const resolvedMbti = (mbti || "").trim().toUpperCase();
            if (mbtiEnabled && !resolvedMbti) {
                setStepMessage("MBTI 보정을 사용하려면 MBTI를 선택해주세요.");
                setSaving(false);
                return;
            }
            if (questionBank && questionnairePreview.answeredCount < questionnairePreview.requiredCount) {
                setStepMessage(`상황형 질문을 최소 ${questionnairePreview.requiredCount}개 이상 선택해주세요.`);
                setSaving(false);
                return;
            }

            const questionnaireAnswers = Object.entries(questionAnswers).map(([qId, oId]) => ({
                question_id: qId,
                option_id: oId,
            }));
            await savePersonaLab({
                persona_id: personaId,
                identity,
                target_audience: "일반 대중",
                tone_hint: toneHint,
                interests: parseCommaValues(interestsText),
                mbti: mbtiEnabled ? resolvedMbti : "",
                mbti_enabled: mbtiEnabled,
                mbti_confidence: mbtiEnabled ? mbtiConfidence : 0,
                questionnaire_version: questionBank?.version || "v1",
                questionnaire_answers: questionnaireAnswers,
                age_group: ageGroup,
                gender,
                structure_score: questionnairePreview.scores.structure,
                evidence_score: questionnairePreview.scores.evidence,
                distance_score: questionnairePreview.scores.distance,
                criticism_score: questionnairePreview.scores.criticism,
                density_score: questionnairePreview.scores.density,
                style_strength: 40,
            });
            onNext();
        } catch (error) {
            setStepMessage(error instanceof Error ? error.message : "저장 실패");
        } finally {
            setSaving(false);
        }
    }

    return (
        <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4">
            <h2 className="text-xl font-bold">2단계. 나만의 AI 페르소나 설계</h2>
            <p className="text-sm text-slate-600">블로그를 대신 작성해줄 AI의 직업, 성격, 성향을 세밀하게 설정합니다.</p>

            <div className="rounded-2xl border border-indigo-100 bg-indigo-50/40 p-5 space-y-4">
                <div className="flex items-center justify-between gap-4">
                    <div>
                        <h3 className="font-semibold text-slate-900">상황형 질문지 (Persona Lab)</h3>
                        <p className="text-xs text-slate-600 mt-1">
                            취향이 아닌 행동 패턴을 기반으로 5차원 글쓰기 성향을 계산합니다.
                        </p>
                    </div>
                    <div className="text-right">
                        <p className="text-xs text-slate-500">진행률</p>
                        <p className="text-sm font-bold text-indigo-700">
                            {questionnairePreview.answeredCount}/{questionBank?.questions?.length || 0}
                        </p>
                    </div>
                </div>

                <div className="h-2 w-full overflow-hidden rounded-full bg-indigo-100">
                    <div
                        className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-blue-500 transition-all"
                        style={{ width: `${Math.round(questionnairePreview.completionRatio * 100)}%` }}
                    />
                </div>

                {questionBank && questionBank.questions.length > 0 ? (
                    <div className="space-y-3">
                        {questionBank.questions.map((question) => (
                            <div key={question.question_id} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
                                <p className="text-xs font-semibold uppercase tracking-wide text-indigo-700">
                                    {question.title}
                                </p>
                                <p className="mt-1 text-sm font-medium text-slate-800">{question.scenario}</p>
                                <div className="mt-3 grid gap-2">
                                    {question.options.map((option) => {
                                        const selected = questionAnswers[question.question_id] === option.option_id;
                                        return (
                                            <button
                                                key={option.option_id}
                                                type="button"
                                                onClick={() => handleQuestionSelect(question.question_id, option.option_id)}
                                                className={`rounded-lg border px-3 py-2 text-left transition-all ${selected
                                                    ? "border-indigo-500 bg-indigo-50 text-indigo-900 shadow-sm"
                                                    : "border-slate-200 bg-white text-slate-700 hover:border-indigo-300 hover:bg-slate-50"
                                                    }`}
                                            >
                                                <p className="text-sm font-semibold">{option.label}</p>
                                                <p className="text-xs text-slate-500 mt-1">{option.description}</p>
                                            </button>
                                        );
                                    })}
                                </div>
                            </div>
                        ))}
                    </div>
                ) : (
                    <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
                        질문지 로딩에 실패했습니다. 임시로 기본 점수(50) 기반으로 저장됩니다.
                    </div>
                )}

                <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
                    {[
                        { key: "structure", label: "구조성" },
                        { key: "evidence", label: "근거성" },
                        { key: "distance", label: "심리적 거리" },
                        { key: "criticism", label: "비판 수위" },
                        { key: "density", label: "문체 밀도" },
                    ].map((item) => (
                        <div key={item.key} className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-center">
                            <p className="text-[11px] text-slate-500">{item.label}</p>
                            <p className="text-lg font-bold text-indigo-700">
                                {questionnairePreview.scores[item.key as keyof QuestionnaireScores]}
                            </p>
                        </div>
                    ))}
                </div>

                <div className="rounded-xl border border-slate-200 bg-white p-4">
                    <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
                        <div>
                            <p className="text-xs uppercase tracking-wide text-indigo-600">Persona Result</p>
                            <h4 className="mt-1 text-lg font-bold text-slate-900">{personaSummary.title}</h4>
                            <p className="mt-1 text-sm text-slate-600">{personaSummary.subtitle}</p>
                            <div className="mt-2 flex flex-wrap gap-2">
                                {personaSummary.tags.map((tag) => (
                                    <span
                                        key={tag}
                                        className="rounded-full bg-indigo-50 px-2 py-1 text-xs font-semibold text-indigo-700"
                                    >
                                        {tag}
                                    </span>
                                ))}
                            </div>
                        </div>
                        <div className="mx-auto w-full max-w-[220px]">
                            <svg viewBox="0 0 180 180" className="h-[180px] w-full">
                                <circle cx="90" cy="90" r="68" fill="none" stroke="#E2E8F0" strokeWidth="1.2" />
                                <circle cx="90" cy="90" r="46" fill="none" stroke="#E2E8F0" strokeWidth="1" />
                                <circle cx="90" cy="90" r="24" fill="none" stroke="#E2E8F0" strokeWidth="1" />
                                <polygon points={radarGeometry.basePoints} fill="rgba(99,102,241,0.04)" stroke="#CBD5E1" strokeWidth="1" />
                                {radarGeometry.axes.map((axis) => (
                                    <g key={`${axis.label}-${axis.x2}`}>
                                        <line x1={axis.x1} y1={axis.y1} x2={axis.x2} y2={axis.y2} stroke="#CBD5E1" strokeWidth="1" />
                                        <text
                                            x={axis.labelX}
                                            y={axis.labelY}
                                            textAnchor="middle"
                                            dominantBaseline="middle"
                                            fontSize="10"
                                            fill="#475569"
                                        >
                                            {axis.label}
                                        </text>
                                    </g>
                                ))}
                                <polygon
                                    points={radarGeometry.dataPoints}
                                    fill="rgba(79,70,229,0.32)"
                                    stroke="#4338CA"
                                    strokeWidth="2"
                                />
                                <circle cx="90" cy="90" r="2.5" fill="#4338CA" />
                            </svg>
                        </div>
                    </div>
                </div>
                <p className="text-xs text-slate-500">
                    저장 조건: 최소 {questionnairePreview.requiredCount}개 이상 응답
                </p>
            </div>

            <div className="space-y-4">
                <div className="grid grid-cols-3 gap-4">
                    <div>
                        <label className="font-semibold text-slate-800 block mb-1">성별</label>
                        <select value={gender} onChange={(e) => setGender(e.target.value)} className="w-full rounded-xl border border-slate-300 px-4 py-2 bg-white">
                            <option value="남성">남성</option>
                            <option value="여성">여성</option>
                            <option value="비공개">비공개</option>
                        </select>
                    </div>
                    <div>
                        <label className="font-semibold text-slate-800 block mb-1">연령대</label>
                        <select value={ageGroup} onChange={(e) => setAgeGroup(e.target.value)} className="w-full rounded-xl border border-slate-300 px-4 py-2 bg-white">
                            <option value="20대">20대</option>
                            <option value="30대">30대</option>
                            <option value="40대">40대</option>
                            <option value="50대 이상">50대 이상</option>
                        </select>
                    </div>
                    <div>
                        <label className="font-semibold text-slate-800 block mb-1">MBTI 보정 (선택)</label>
                        <label className="flex items-center gap-2 text-sm text-slate-600 mb-2">
                            <input
                                type="checkbox"
                                checked={mbtiEnabled}
                                onChange={(e) => {
                                    const enabled = e.target.checked;
                                    setMbtiEnabled(enabled);
                                    if (enabled && !mbti) {
                                        setMbti("ENFP");
                                    }
                                }}
                            />
                            MBTI를 질문지 결과에 보조 반영
                        </label>
                        <select
                            value={mbti}
                            onChange={(e) => setMbti(e.target.value)}
                            disabled={!mbtiEnabled}
                            className="w-full rounded-xl border border-slate-300 px-4 py-2 bg-white disabled:bg-slate-100 disabled:text-slate-400"
                        >
                            <option value="">선택 안함</option>
                            {["ISTJ", "ISFJ", "INFJ", "INTJ", "ISTP", "ISFP", "INFP", "INTP", "ESTP", "ESFP", "ENFP", "ENTP", "ESTJ", "ESFJ", "ENFJ", "ENTJ"].map((m) => (
                                <option key={m} value={m}>{m}</option>
                            ))}
                        </select>
                        {mbtiEnabled && (
                            <div className="mt-3">
                                <label className="text-xs text-slate-600 flex items-center justify-between">
                                    MBTI 확신도
                                    <span className="font-semibold text-indigo-700">{mbtiConfidence}</span>
                                </label>
                                <input
                                    type="range"
                                    min={0}
                                    max={100}
                                    value={mbtiConfidence}
                                    onChange={(e) => setMbtiConfidence(Number(e.target.value))}
                                    className="w-full accent-indigo-600"
                                />
                                <p className="text-[11px] text-slate-500 mt-1">
                                    반영 비율: 질문지 {100 - mbtiWeightPercent}% + MBTI {mbtiWeightPercent}%
                                </p>
                            </div>
                        )}
                    </div>
                </div>
                <div>
                    <label className="font-semibold text-slate-800 block mb-1">나는 누구인가요? (정체성 / 직업)</label>
                    <input type="text" value={identity} onChange={(e) => setIdentity(e.target.value)} placeholder="예: 5년 차 IT 개발자, 주식 투자 3년차 직장인" className="w-full rounded-xl border border-slate-300 px-4 py-2" />
                </div>

                <div>
                    <label className="font-semibold text-slate-800 block mb-1">말투는 어떤가요? (Tone)</label>
                    <input type="text" value={toneHint} onChange={(e) => setToneHint(e.target.value)} placeholder="예: 친절하고 전문적인 존댓말, 유머러스한 반말" className="w-full rounded-xl border border-slate-300 px-4 py-2" />
                </div>

                <div>
                    <label className="font-semibold text-slate-800 block mb-1">관심사 / 특징 (콤마로 구분)</label>
                    <input type="text" value={interestsText} onChange={(e) => setInterestsText(e.target.value)} placeholder="예: 최신 전자기기 탐구, 카페 인테리어" className="w-full rounded-xl border border-slate-300 px-4 py-2" />
                </div>
            </div>

            <div className="flex justify-between pt-4">
                <button onClick={onPrev} className="text-slate-500 font-semibold px-4 py-2 hover:bg-slate-100 rounded-lg transition-colors">← 이전</button>
                <button onClick={handleSavePersonaStep} disabled={saving} className="bg-gradient-to-r from-indigo-600 to-blue-600 text-white px-8 py-3 rounded-full font-bold shadow-md hover:shadow-lg transition-all active:scale-95 text-lg">
                    {saving ? "저장 중..." : "다음 단계로 →"}
                </button>
            </div>
            {stepMessage && <p className="text-red-500 text-sm mt-2">{stepMessage}</p>}
        </div>
    );
}
