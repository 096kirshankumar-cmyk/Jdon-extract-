# FILE: verifier.py  (from the external "vision-first" audit, verbatim)
# Isolated. test_proposed_redesign_audit.py shows what this 5-check verifier
# silently lets through that the production gate catches.
from typing import List, Dict


class Verifier:
    @staticmethod
    def verify_and_flag(questions, answers, solutions) -> List[Dict]:
        flags = []
        for q, a, s in zip(questions, answers, solutions):
            q_id = q["q_id"]
            if not q["question_text"]:
                flags.append({"q_id": q_id, "reason": "missing_stem", "severity": "BLOCKER"})
            if len(q["options"]) < 2:
                flags.append({"q_id": q_id, "reason": "missing_options", "severity": "BLOCKER"})
            if not a["correct_option"]:
                flags.append({"q_id": q_id, "reason": "missing_answer", "severity": "BLOCKER"})
            if not s["solution_text"]:
                flags.append({"q_id": q_id, "reason": "missing_solution", "severity": "REVIEW"})
            if s["solution_text"] and q["question_text"] and s["solution_text"] == q["question_text"]:
                flags.append({"q_id": q_id, "reason": "solution_equals_stem", "severity": "REVIEW"})
        return flags
