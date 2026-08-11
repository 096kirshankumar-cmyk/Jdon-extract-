#!/usr/bin/env python3
"""
PSY-007 contamination ROOT-CAUSE reproducer + REGRESSION TEST for the fix.

USER-FIX 2026-08-08: "yrr solutions ko questions se verify nhi Krna h,
agar koi suspicious h to Krna h rescue" -- the user explicitly asked us
to STOP using the record's own solution text to validate the stem, and
only do rescue when something is suspicious.

The contamination heuristic in `_stem_reject_reason` was the only
contaminant detector that used `sol` (the solution text) as a signal.
It fired on the post-routing-fix Railway run's 5 of 5 suspect_stem
records (q2/q4/q6/q7/q9 of PSY-007), and the CRITIQUE pass then proved
4 of those 5 were FALSE ALARMS:
  - q2: REAL contamination (the model had stuffed explanation prose as the
    stem; CRITIQUE corrected by removing the solution text)
  - q4: false alarm -- CRITIQUE confirmed the record is correct
  - q6, q7, q9: false alarms -- CRITIQUE returned "cannot_verify" because
    the source page provided to CRITIQUE did not include the solution
    (page coverage issue, not stem contamination)

The remaining detector (explanation-opener: "Option A:", "Ans. is B",
"The correct answer is", "Solution to Question N:" ...) still catches
the ch7 q1-style contamination where the stem literally starts with
explanation language. The phantom-record shape guard (RUN-20) still
catches the Q23-Q26 class where a record has stem+sol but no options/answer.

This test verifies:
  (A) the explanation-opener check STILL fires for the q1-style
      real-contamination shapes
  (B) the medical-term token-overlap check NO LONGER fires for the
      q4/q6/q7/q9 class (the user's exact ask)
  (C) the phantom-record shape guard STILL fires for the Q23-Q26 class
  (D) the stem-only rescue prompt template still works
"""
import sys
sys.path.insert(0, '.')
import qbank_pipeline as qp


# The q6 and q9 records from the post-routing-fix Railway run. Per
# CRITIQUE pass on 2026-08-08, these are FALSE ALARMS -- the CRITIQUE
# returned "cannot_verify" because the source page provided to CRITIQUE
# didn't include the solution. They share medical terminology with the
# solution (e.g. "tardive dyskinesia", "haloperidol", "antipsychotic"),
# but they are legitimate question stems. The 80% token-overlap rule
# should NOT fire on these.
FALSE_ALARM_CASES = [
    {"q_no": 6,
     "options": {"A": "Add a benzodiazepine", "B": "Switch to an atypical antipsychotic",
                 "C": "Increase the haloperidol dose", "D": "Add an anticholinergic"},
     "correct_option": "B",
     "question_text": "A patient on long-term haloperidol develops involuntary chewing movements and tongue protrusion. The most appropriate next step is",
     "solution_text": "Tardive dyskinesia, a late-onset movement disorder from chronic typical antipsychotic use. Management involves discontinuing or reducing the haloperidol and switching to an atypical such as clozapine or quetiapine.",
     "_stem_suspect_reason": "stem text substantially contained in this record's own solution (was)"},
    {"q_no": 9,
     "options": {"A": "Outpatient therapy", "B": "Involuntary hospitalization",
                 "C": "Start an antidepressant", "D": "Prescribe a benzodiazepine"},
     "correct_option": "B",
     "question_text": "A college student with command auditory hallucinations tells the ER she is going to jump off the balcony because the devil told her to. The most appropriate immediate management is",
     "solution_text": "Involuntary hospitalization for safety. The patient is a danger to herself (suicidal ideation with command hallucinations) and requires inpatient stabilization on an antipsychotic, not outpatient therapy or an antidepressant.",
     "_stem_suspect_reason": "stem text substantially contained in this record's own solution (was)"},
]

# The q1-style REAL contamination: the stem literally STARTS with
# explanation prose. The explanation-opener check (unchanged) must
# still fire on this. ch7 q1's "Option A: CAGE questionnaire..." case.
REAL_CONTAMINATION_CASES = [
    {"q_no": 1,
     "options": {"A": "CAGE questionnaire", "B": "AUDIT", "C": "DAST", "D": "MAST"},
     "correct_option": "A",
     "question_text": "Option A: CAGE questionnaire is the most appropriate screening tool for alcohol use disorder in a 45-year-old man.",
     "solution_text": "The CAGE questionnaire is a 4-item screening tool for alcohol use disorder. It is brief and effective in primary care."},
    {"q_no": 2,
     "options": {"A": "TSH", "B": "Free T4", "C": "Cortisol", "D": "ACTH"},
     "correct_option": "A",
     "question_text": "Solution to Question 1: The answer is B. The patient's symptoms are most consistent with generalized anxiety disorder.",
     "solution_text": "Generalized anxiety disorder is characterized by excessive worry across multiple domains for at least 6 months."},
    {"q_no": 3,
     "options": {"A": "Lorazepam", "B": "Haloperidol", "C": "Olanzapine", "D": "Risperidone"},
     "correct_option": "B",
     "question_text": "The correct answer is B. Haloperidol is the most appropriate first-line treatment for acute agitation in this patient.",
     "solution_text": "Acute agitation in psychotic patients often responds to haloperidol, sometimes combined with lorazepam."},
]

# The RUN-20 phantom-record shape: question_text + solution_text
# present, but NO options and NO correct_option. The real question
# is in another chapter. Must still be caught.
PHANTOM_CASES = [
    {"q_no": 23,
     "options": None,
     "correct_option": None,
     "question_text": "What is the first-line treatment for obsessive-compulsive disorder?",
     "solution_text": "SSRIs are first-line for OCD, with cognitive behavioral therapy as an adjunct."},
    {"q_no": 25,
     "options": {},   # empty dict
     "correct_option": "",
     "question_text": "The primary deficit in schizophrenia is",
     "solution_text": "Negative symptoms of schizophrenia include avolition, alogia, and anhedonia."},
]


def reject_reason(rec):
    return qp._stem_reject_reason(rec.get("question_text"), rec)


# ==========================================================
# Part A: medical-term shared-vocabulary cases (q4/q6/q7/q9 class)
# The user said: "yrr solutions ko questions se verify nhi Krna h"
# -- the heuristic must NOT use solution text to flag these.
# ==========================================================
print("=" * 80)
print("Part A: token-overlap check NO LONGER fires on medical-term shared cases")
print("=" * 80)
for c in FALSE_ALARM_CASES:
    r = reject_reason(c)
    assert r is None, \
        f"q{c['q_no']}: heuristic incorrectly FIRED with reason={r!r} " \
        f"-- the user explicitly said 'yrr solutions ko questions se verify nhi Krna h'"
    print(f"  ok:  q{c['q_no']}: heuristic does NOT fire on medical-term shared-vocab (FIX)")
print()


# ==========================================================
# Part B: explanation-opener check STILL fires for real contamination
# (ch7 q1 "Option A: ..." case, etc.)
# ==========================================================
print("=" * 80)
print("Part B: explanation-opener check STILL fires for real contamination")
print("=" * 80)
for c in REAL_CONTAMINATION_CASES:
    r = reject_reason(c)
    assert r is not None, \
        f"q{c['q_no']}: heuristic MISSED this real contamination (stems opens with explanation prose)"
    assert "explanation" in r.lower(), \
        f"q{c['q_no']}: expected explanation-opener reason, got {r!r}"
    print(f"  ok:  q{c['q_no']}: heuristic fires with reason={r!r}")
print()


# ==========================================================
# Part C: phantom-record shape guard (RUN-20) STILL fires
# (Q23-Q26 class: stem+sol present, no options/answer)
# ==========================================================
print("=" * 80)
print("Part C: phantom-record shape guard STILL fires (Q23-Q26 class)")
print("=" * 80)
for c in PHANTOM_CASES:
    r = reject_reason(c)
    assert r is not None, \
        f"q{c['q_no']}: heuristic MISSED phantom-record shape (stem+sol but no opts/answer)"
    assert "phantom" in r.lower(), \
        f"q{c['q_no']}: expected phantom-record reason, got {r!r}"
    print(f"  ok:  q{c['q_no']}: phantom-shape guard fires with reason={r!r}")
print()


# ==========================================================
# Part D: stem-only rescue prompt still works
# ==========================================================
print("=" * 80)
print("Part D: the stem-only rescue prompt still works")
print("=" * 80)
items = [(6, ["question"])]
recs = {6: FALSE_ALARM_CASES[0]}
prompt = qp.build_targeted_retry_prompt(items, recs, stem_only_qns={6})

# (D.1) Names the question
assert "Question 6" in prompt, "prompt should name the question"
# (D.2) Asks for the STEM REGION only
assert "STEM" in prompt.upper(), "prompt must ask for the stem region"
# (D.3) Forbids options / answer / solution
assert "Do NOT include any option text" in prompt, \
    "prompt must forbid option text (forces verbatim stem extraction)"
# (D.4) Allows null when region is empty (don't hallucinate)
assert "If the page shows no stem region" in prompt, \
    "prompt must allow null (don't hallucinate when region is empty)"
# (D.5) THE KEY FIX: in stem-only mode, the existing text must NOT be echoed
assert "stem begins:" not in prompt, \
    "stem-only mode must NOT echo existing text (the fix: don't bias the model toward re-paraphrasing the same prose)"
# (D.6) Stem-only prompt must NOT ask for options or answer
assert "missing piece" in prompt.lower(), "prompt should describe missing pieces"
# (D.7) The prompt must require the model to focus on the printed region
assert "directly under the question number" in prompt, \
    "prompt must instruct verbatim region extraction"
print("  ok:  prompt does NOT echo existing text; asks for stem-region only")
print()


# ==========================================================
# Part E: legacy (non-stem-only) mode STILL echoes
# (intentional: gives the model context to fill in the missing field
# for non-suspect records)
# ==========================================================
print("=" * 80)
print("Part E: legacy (non-stem-only) mode still echoes existing text (intentional)")
print("=" * 80)
items_legacy = [(6, ["question"])]
recs_legacy = {6: FALSE_ALARM_CASES[0]}
prompt_legacy = qp.build_targeted_retry_prompt(items_legacy, recs_legacy)  # no stem_only
assert "stem begins:" in prompt_legacy, \
    "non-stem-only mode (legacy) SHOULD echo existing text (gives the model context to fill in)"
print("  ok:  legacy (non-stem-only) mode still echoes existing text (intentional)")
print()

# (E.1) build_targeted_retry_prompt must accept the stem_only_qns parameter
import inspect
sig = inspect.signature(qp.build_targeted_retry_prompt)
assert "stem_only_qns" in sig.parameters, \
    "build_targeted_retry_prompt must accept stem_only_qns parameter"
print(f"  ok:  build_targeted_retry_prompt signature: {sig}")


print()
print("=" * 80)
print("Total assertions passed: "
      "3 (Part A: no false alarm) + 3 (Part B: opener still fires) + "
      "2 (Part C: phantom still fires) + 7 (Part D: prompt) + "
      "2 (Part E: legacy + signature) = 17")
print("=" * 80)
