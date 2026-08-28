# FILE: assembler.py  (from the external "vision-first" audit, verbatim)
# Isolated so production is untouched. test_proposed_redesign_audit.py proves
# the specific regressions this sticky-state design reintroduces.
import re
from typing import List, Dict, Any, Tuple


class Assembler:
    def __init__(self, subject: str, chapter_no: int):
        self.subject = subject
        self.chapter_no = chapter_no
        self.questions_db = {}  # q_no -> {stem, options, figures, answer, solution}
        self.current_q_no = None

    def _get_q_id(self, q_no: int) -> str:
        return f"{self.subject}-{self.chapter_no:03d}-{q_no:03d}"

    def process_page_blocks(self, page_data, extraction_result):
        page_type = extraction_result.get("page_type", "OTHER")
        blocks = extraction_result.get("blocks", [])
        page_no = page_data["page_no"]
        raw_images = page_data.get("raw_images", [])

        for block in blocks:
            b_type = block.get("block_type")
            q_no = block.get("q_no")
            text = block.get("text", "")
            opt_id = block.get("option_id")
            has_fig = block.get("has_figure", False)

            if q_no is not None:
                self.current_q_no = q_no
                if q_no not in self.questions_db:
                    self.questions_db[q_no] = {
                        "stem": "", "options": {}, "figures": [],
                        "answer": None, "solution": "", "sol_figures": []}

            active_q = self.current_q_no
            if active_q is None:
                continue
            if active_q not in self.questions_db:
                self.questions_db[active_q] = {
                    "stem": "", "options": {}, "figures": [],
                    "answer": None, "solution": "", "sol_figures": []}

            q_data = self.questions_db[active_q]

            if page_type == "QUESTIONS":
                if b_type == "stem":
                    q_data["stem"] += " " + text
                elif b_type == "option" and opt_id:
                    q_data["options"][opt_id] = text
                elif b_type == "figure" or has_fig:
                    for img in raw_images:
                        if img not in q_data["figures"]:
                            q_data["figures"].append(img)
            elif page_type == "ANSWER_KEY":
                if b_type == "answer_row":
                    match = re.search(r'([A-E])', text)
                    if match:
                        q_data["answer"] = match.group(1)
            elif page_type == "SOLUTIONS":
                if b_type == "solution":
                    q_data["solution"] += " " + text
                elif b_type == "figure" or has_fig:
                    for img in raw_images:
                        if img not in q_data["sol_figures"]:
                            q_data["sol_figures"].append(img)

        if raw_images and self.current_q_no and self.current_q_no in self.questions_db:
            q_data = self.questions_db[self.current_q_no]
            if page_type == "SOLUTIONS":
                for img in raw_images:
                    if img not in q_data["sol_figures"]:
                        q_data["sol_figures"].append(img)
            else:
                for img in raw_images:
                    if img not in q_data["figures"]:
                        q_data["figures"].append(img)

    def build_records(self) -> Tuple[List[Dict], List[Dict], List[Dict], List[Dict]]:
        questions, answers, solutions, manifests = [], [], [], []
        for q_no in sorted(self.questions_db.keys()):
            data = self.questions_db[q_no]
            q_id = self._get_q_id(q_no)
            opts = []
            for opt_id in ["A", "B", "C", "D", "E"]:
                if opt_id in data["options"]:
                    opts.append({"id": opt_id, "text": data["options"][opt_id], "images": []})
            status = "COMPLETE"
            if not data["stem"].strip() or len(opts) < 2:
                status = "INCOMPLETE"
            questions.append({
                "q_id": q_id, "subject": self.subject, "chapter_no": self.chapter_no,
                "q_no": q_no, "question_text": data["stem"].strip(),
                "options": opts, "question_images": data["figures"],
                "tables": [], "extraction_status": status})
            ans_status = "COMPLETE" if data["answer"] else "INCOMPLETE"
            answers.append({"q_id": q_id, "q_no": q_no,
                            "correct_option": data["answer"], "extraction_status": ans_status})
            sol_status = "COMPLETE" if data["solution"].strip() else "INCOMPLETE"
            solutions.append({
                "q_id": q_id, "q_no": q_no,
                "solution_text": data["solution"].strip(), "tables": [],
                "solution_images": data["sol_figures"], "extraction_status": sol_status})
            for img in data["figures"]:
                manifests.append({"q_id": q_id, "type": "QUESTION", "option_letter": None,
                                  "file": img["file"], "source_pages": img["source_pages"],
                                  "extraction_page": img["extraction_page"]})
            for img in data["sol_figures"]:
                manifests.append({"q_id": q_id, "type": "SOLUTION", "option_letter": None,
                                  "file": img["file"], "source_pages": img["source_pages"],
                                  "extraction_page": img["extraction_page"]})
        return questions, answers, solutions, manifests
