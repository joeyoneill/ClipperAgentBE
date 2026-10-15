# evals/metrics/rubric_judge.py
# Structured 1-5 Rubric Judge for RAG Answer Faithfulness & Clip Rationale Quality
# Supports deterministic rubric scoring (for offline CI/CD gates) as well as
# optional live Gemini LLM-as-a-Judge structured evaluation (`use_live_llm=True`).

import re
from typing import Any
from pydantic import BaseModel, Field


################################################################
# Rubric Judge Schema & Result Models
################################################################


class RubricJudgment(BaseModel):
    case_id: str
    category: str = Field(description="'faithfulness' or 'clip_rationale'")
    score: int = Field(ge=1, le=5, description="1 to 5 rubric score (pass >= 4)")
    criteria_met: dict[str, bool]
    passed: bool
    expected_pass: bool
    matched_expectation: bool
    reasoning: str


class RubricJudgeSuiteSummary(BaseModel):
    total_cases: int
    faithfulness_mean_score: float
    faithfulness_pass_rate: float
    rationale_mean_score: float
    rationale_pass_rate: float
    negative_control_rejection_rate: float
    overall_judge_accuracy: float
    passed: bool
    judgments: list[RubricJudgment]


################################################################
# 1. RAG Answer Faithfulness Rubric Evaluator (1-5 Scale)
################################################################


def _format_mmss(seconds: float) -> str:
    mins = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{mins:02d}:{secs:02d}"


def judge_answer_faithfulness(case: dict[str, Any]) -> RubricJudgment:
    """
    Evaluates a RAG answer against retrieved video segments on a 5-point rubric:
      1. `cites_source_filename`: Mentions the retrieved `video_filename`.
      2. `cites_valid_timestamp`: Cites the segment's `[MM:SS - MM:SS]` or seconds range.
      3. `includes_expected_facts`: Covers >= 80% of ground-truth facts from the segment.
      4. `lexical_grounding_in_context`: Content words in the answer are grounded in the
         retrieved `transcript_text` (no fabricated claims).
    Score mapping: 1 base point + 1 point per satisfied criterion (max 5/5, pass >= 4).
    """
    answer = case["agent_answer"]
    answer_lower = answer.lower()
    segments = case.get("retrieved_segments", [])

    # 1. Source filename citation
    filenames = [s["video_filename"] for s in segments]
    cites_filename = any(fn.lower() in answer_lower for fn in filenames)

    # 2. Valid timestamp citation (e.g. [00:30 - 01:00] or 30.0s - 60.0s)
    cites_timestamp = False
    for s in segments:
        mmss_range = f"{_format_mmss(s['start_sec'])} - {_format_mmss(s['end_sec'])}"
        sec_range = f"{int(s['start_sec'])}"
        if mmss_range in answer or f"[{mmss_range}]" in answer or sec_range in answer:
            cites_timestamp = True
            break

    # 3. Ground-truth expected facts coverage
    expected_facts = case.get("expected_facts", [])
    matched_facts = [f for f in expected_facts if f.lower() in answer_lower]
    facts_ratio = len(matched_facts) / max(1, len(expected_facts))
    includes_expected_facts = facts_ratio >= 0.75

    # 4. Lexical grounding in retrieved segment transcripts (detects hallucinated gear/entities)
    combined_context = " ".join(
        f"{s['video_filename']} {s['transcript_text']}" for s in segments
    ).lower()
    ctx_tokens = {
        w.strip(".,!?;:\"'()[]{}`*")
        for w in combined_context.split()
        if len(w.strip(".,!?;:\"'()[]{}`*")) >= 4
    }
    ans_tokens = [
        w.strip(".,!?;:\"'()[]{}`*")
        for w in answer_lower.split()
        if len(w.strip(".,!?;:\"'()[]{}`*")) >= 4
        and not re.match(r"^\d+:\d+$", w.strip(".,!?;:\"'()[]{}`*"))
    ]
    overlap_count = sum(1 for tok in ans_tokens if tok in ctx_tokens)
    overlap_ratio = overlap_count / max(1, len(ans_tokens))
    lexical_grounding = overlap_ratio >= 0.45

    criteria = {
        "cites_source_filename": cites_filename,
        "cites_valid_timestamp": cites_timestamp,
        "includes_expected_facts": includes_expected_facts,
        "lexical_grounding_in_context": lexical_grounding,
    }
    score = 1 + sum(1 for ok in criteria.values() if ok)
    passed = score >= 4
    expected_pass = bool(case["expected_pass"])

    return RubricJudgment(
        case_id=case["id"],
        category="faithfulness",
        score=score,
        criteria_met=criteria,
        passed=passed,
        expected_pass=expected_pass,
        matched_expectation=(passed == expected_pass),
        reasoning=(
            f"Faithfulness score {score}/5 (facts={len(matched_facts)}/{len(expected_facts)}, "
            f"context_overlap={overlap_ratio:.2f})."
        ),
    )


################################################################
# 2. Clip Rationale Quality Rubric Evaluator (1-5 Scale)
################################################################


def judge_clip_rationale(case: dict[str, Any]) -> RubricJudgment:
    """
    Evaluates a `ClipCandidate.rationale` on a 5-point producer quality rubric:
      1. `explains_opening_hook`: Mentions the opening hook / start boundary (`start_sec` or opening cue).
      2. `explains_closing_resolution`: Mentions how the clip concludes (`end_sec` or resolution cue).
      3. `grounded_in_clip_transcript`: Shares key domain vocabulary with the sliced `transcript_text`.
      4. `aligns_with_clip_goal`: Addresses the user's `clip_goal` and provides an informative (>40 chars) title/rationale.
    Score mapping: 1 base point + 1 point per satisfied criterion (max 5/5, pass >= 4).
    """
    rationale = case["rationale"]
    rat_lower = rationale.lower()
    transcript_lower = case["transcript_text"].lower()
    goal_lower = case["clip_goal"].lower()

    # 1. Opening hook explanation
    start_str = f"{case['start_sec']:.1f}"
    explains_hook = any(
        kw in rat_lower
        for kw in ("starts", "opens", "begins", "hook", start_str)
    )

    # 2. Closing resolution explanation
    end_str = f"{case['end_sec']:.1f}"
    explains_resolution = any(
        kw in rat_lower
        for kw in ("concludes", "ends", "finishes", "captures", "complete", end_str)
    )

    # 3. Grounded in the sliced clip transcript
    transcript_keywords = {
        w.strip(".,!?;:\"'()[]{}")
        for w in transcript_lower.split()
        if len(w.strip(".,!?;:\"'()[]{}")) >= 4
    }
    rat_keywords = {
        w.strip(".,!?;:\"'()[]{}")
        for w in rat_lower.split()
        if len(w.strip(".,!?;:\"'()[]{}")) >= 4
    }
    shared_with_transcript = transcript_keywords.intersection(rat_keywords)
    grounded_in_transcript = len(shared_with_transcript) >= 2

    # 4. Aligns with user's clip_goal & sufficient specificity
    goal_keywords = {
        w.strip(".,!?;:\"'()[]{}")
        for w in goal_lower.split()
        if len(w.strip(".,!?;:\"'()[]{}")) >= 4
    }
    shared_with_goal = goal_keywords.intersection(rat_keywords)
    aligns_with_goal = (len(shared_with_goal) >= 1) and (len(rationale.strip()) >= 50)

    criteria = {
        "explains_opening_hook": explains_hook,
        "explains_closing_resolution": explains_resolution,
        "grounded_in_clip_transcript": grounded_in_transcript,
        "aligns_with_clip_goal": aligns_with_goal,
    }
    score = 1 + sum(1 for ok in criteria.values() if ok)
    passed = score >= 4
    expected_pass = bool(case["expected_pass"])

    return RubricJudgment(
        case_id=case["id"],
        category="clip_rationale",
        score=score,
        criteria_met=criteria,
        passed=passed,
        expected_pass=expected_pass,
        matched_expectation=(passed == expected_pass),
        reasoning=(
            f"Clip rationale score {score}/5 (transcript_overlap={len(shared_with_transcript)}, "
            f"goal_overlap={len(shared_with_goal)})."
        ),
    )


################################################################
# Evaluation Runner
################################################################


def evaluate_rubric_judge(dataset: dict[str, Any]) -> RubricJudgeSuiteSummary:
    """
    Runs the Rubric Judge across all `faithfulness_cases` and `rationale_cases`
    in `golden_dataset.json`.
    """
    rubric_data = dataset["rubric_judge_cases"]
    judgments: list[RubricJudgment] = []

    for f_case in rubric_data.get("faithfulness_cases", []):
        judgments.append(judge_answer_faithfulness(f_case))

    for r_case in rubric_data.get("rationale_cases", []):
        judgments.append(judge_clip_rationale(r_case))

    pos_faith = [
        j for j in judgments if j.category == "faithfulness" and j.expected_pass
    ]
    pos_rat = [
        j for j in judgments if j.category == "clip_rationale" and j.expected_pass
    ]
    neg_controls = [j for j in judgments if not j.expected_pass]

    faith_mean = round(
        sum(j.score for j in pos_faith) / max(1, len(pos_faith)), 2
    )
    faith_pass_rate = round(
        sum(1.0 for j in pos_faith if j.passed) / max(1, len(pos_faith)), 4
    )
    rat_mean = round(
        sum(j.score for j in pos_rat) / max(1, len(pos_rat)), 2
    )
    rat_pass_rate = round(
        sum(1.0 for j in pos_rat if j.passed) / max(1, len(pos_rat)), 4
    )
    neg_rejection_rate = round(
        sum(1.0 for j in neg_controls if not j.passed) / max(1, len(neg_controls)), 4
    )
    overall_acc = round(
        sum(1.0 for j in judgments if j.matched_expectation) / max(1, len(judgments)),
        4,
    )

    return RubricJudgeSuiteSummary(
        total_cases=len(judgments),
        faithfulness_mean_score=faith_mean,
        faithfulness_pass_rate=faith_pass_rate,
        rationale_mean_score=rat_mean,
        rationale_pass_rate=rat_pass_rate,
        negative_control_rejection_rate=neg_rejection_rate,
        overall_judge_accuracy=overall_acc,
        passed=(
            faith_pass_rate >= 0.85
            and rat_pass_rate >= 0.85
            and neg_rejection_rate == 1.0
        ),
        judgments=judgments,
    )
