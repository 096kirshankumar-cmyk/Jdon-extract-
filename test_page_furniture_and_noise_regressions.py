#!/usr/bin/env python3
"""RUN-34 regressions: page furniture, OCR noise, and the too-short solution.

All three come from reading OPH-001's actual questions.jsonl against the
source PDF:

    q1 solution  "...leading to\\n12 Sold by @itachibot\\n\\nhypermetropia."
    q7 stem      "...appearsby_\\n5 Sold by @itachibot"
    q9 option D  "Middle cerebral artery 6 Sold by @itachibot PRunebdinn IN"
    q9 solution  "...superior hypophyseal artery - ventral...\\n\u00a9MARROW"
    q3 solution  ". X"                      (the whole explanation, lost)
    q2 stem      "oe SS \u00ab\\ni i ity?\\nAt what age would a child attain full a \u00ab"

Every one of those shipped as qa_status=READY with [GATE] CLEAN.

Run:  python3 test_page_furniture_and_noise_regressions.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

_TMP = Path(tempfile.mkdtemp(prefix="run34_env_"))
os.environ["OUTPUT_DIR"] = str(_TMP / "out")

import qbank_pipeline as qp          # noqa: E402

# The book's shortest genuine explanation (q7) -- the margin the
# MIN_SOLUTION_CHARS threshold has to stay inside.
REAL_SHORT_SOLUTION = ("The canal of Schlemm appears by the 4th month after "
                       "conception.")


class TestStripPageFurniture(unittest.TestCase):
    def test_reseller_stamp_with_page_number_is_removed(self):
        src = ("This reduced axial length causes light to converge behind the "
               "retina, leading to\n12 Sold by @itachibot\n\nhypermetropia.")
        out, n = qp.strip_page_furniture(src)
        self.assertEqual(n, 1)
        self.assertNotIn("itachibot", out)
        self.assertIn("leading to", out)
        self.assertIn("hypermetropia.", out)

    def test_bare_stamp_is_removed(self):
        out, n = qp.strip_page_furniture("Answer is B.\nSold by @itachibot")
        self.assertEqual(n, 1)
        self.assertEqual(out, "Answer is B.")

    def test_publisher_mark_is_removed(self):
        out, n = qp.strip_page_furniture(
            "Superior hypophyseal artery - ventral.\n\u00a9MARROW")
        self.assertEqual(n, 1)
        self.assertEqual(out, "Superior hypophyseal artery - ventral.")

    def test_bare_page_number_before_a_stamp_is_removed(self):
        """The footer really renders as two lines: '11' then the stamp."""
        out, n = qp.strip_page_furniture(
            "Real content here.\n11\nSold by @itachibot\nMore content.")
        self.assertEqual(n, 2)
        self.assertNotIn("11", out.split("\n"))
        self.assertIn("Real content here.", out)
        self.assertIn("More content.", out)

    def test_a_line_carrying_real_text_is_never_eaten(self):
        """Conservative on purpose: q9's option had the stamp INLINE. Eating
        the line would delete 'Middle cerebral artery', which is the answer."""
        src = "Middle cerebral artery 6 Sold by @itachibot"
        out, n = qp.strip_page_furniture(src)
        self.assertEqual(n, 0)
        self.assertEqual(out, src)

    def test_bare_number_with_no_stamp_after_it_survives(self):
        """A solution may legitimately contain a lone number."""
        src = "The count is:\n3\nBecause of the reasons above."
        out, n = qp.strip_page_furniture(src)
        self.assertEqual(n, 0)
        self.assertIn("3", out)

    def test_clean_text_is_returned_unchanged(self):
        src = REAL_SHORT_SOLUTION
        out, n = qp.strip_page_furniture(src)
        self.assertEqual(n, 0)
        self.assertEqual(out, src)

    def test_leading_bare_page_number_is_removed_even_without_a_stamp(self):
        """RUN-41 (OPH-001 q15 live): a solution header at the bottom of a
        page puts the footer under it; the model transcribed the footer as a
        bare '18' line BEFORE the real content. The next-line-is-stamp rule
        missed it and the page number shipped READY."""
        out, n = qp.strip_page_furniture(
            "18\n\nIn the given clinical scenario, a fracture of the optic "
            "canal is most likely to damage the ophthalmic artery.")
        self.assertEqual(n, 1)
        self.assertFalse(out.startswith("18"))
        self.assertTrue(out.startswith("In the given clinical scenario"))

    def test_leading_page_number_with_blank_lines_between_is_removed(self):
        out, n = qp.strip_page_furniture("12\n\n12\n\nReal content here.")
        self.assertEqual(n, 2)
        self.assertEqual(out, "Real content here.")

    def test_leading_stamp_then_content_is_removed(self):
        out, n = qp.strip_page_furniture(
            "14 Sold by @itachibot\n\nSol body of q7...")
        self.assertEqual(n, 1)
        self.assertTrue(out.startswith("Sol body of q7"))

    def test_mid_text_bare_number_still_survives(self):
        """A lone digit line AFTER real content is ambiguous (list item) --
        the conservative rule stays."""
        out, n = qp.strip_page_furniture("The types are:\n1\n2\nDone.")
        self.assertEqual(n, 0)
        self.assertIn("\n1\n", out)

    def test_empty_input(self):
        self.assertEqual(qp.strip_page_furniture(""), ("", 0))
        self.assertEqual(qp.strip_page_furniture(None), ("", 0))


class TestSanitizeStripsFurniture(unittest.TestCase):
    def test_furniture_inside_a_solution_is_stripped_and_reported(self):
        text = ("A newborn is usually hypermetropic by +2D to +3D.\n"
                "12 Sold by @itachibot\n"
                "The AP diameter at birth is about 16.5 mm.")
        out, notes = qp.sanitize_solution_text(text, own_qn=1)
        self.assertNotIn("itachibot", out)
        self.assertIn("16.5 mm", out)
        self.assertTrue(any("page-furniture" in n for n in notes), notes)

    def test_a_clean_solution_reports_no_furniture_note(self):
        out, notes = qp.sanitize_solution_text(REAL_SHORT_SOLUTION, own_qn=7)
        self.assertFalse(any("page-furniture" in n for n in notes), notes)


class TestForeignSolutionHeadIsDropped(unittest.TestCase):
    """RUN-36. OPH-001 q4 shipped READY with q3's explanation prepended:

        "Uveal melanomas arise from uveal melanocytes...   <- q3's text

         Solution to Question 4:

         The macula is fully developed by 4-6 months of age..."

    The crop bled over the page break. sanitize only stripped a LEADING
    header, so the foreign head survived and nothing flagged it."""

    Q3_TEXT = ("Uveal melanomas arise from uveal melanocytes. Uveal and "
               "conjunctival melanocytes are derived from the neural crest "
               "cells.")
    Q4_TEXT = ("The macula is fully developed by 4-6 months of age. Apart "
               "from the macula, the rest of the retina is fully developed "
               "at birth.")

    def _bled(self):
        return (self.Q3_TEXT + "\n\nSolution to Question 4:\n\n" + self.Q4_TEXT)

    def test_foreign_head_is_dropped_when_the_header_names_this_question(self):
        out, notes = qp.sanitize_solution_text(self._bled(), own_qn=4)
        self.assertNotIn("Uveal melanomas", out)
        self.assertTrue(out.startswith("The macula is fully developed"))
        self.assertIn("4-6 months", out)
        self.assertTrue(any("PREVIOUS question" in n for n in notes), notes)

    def test_a_neighbours_header_is_never_used_to_cut(self):
        """If the embedded header names a DIFFERENT question, the old
        conservative behaviour must stand -- we cannot prove which side is
        ours, so nothing is cut."""
        text = ("The macula is fully developed by 4-6 months of age and the "
                "rest of the retina is developed at birth in the normal eye.\n"
                "Solution to Question 5:\nThe optic groove appears by 3 weeks.")
        out, notes = qp.sanitize_solution_text(text, own_qn=4)
        self.assertIn("macula is fully developed", out)
        self.assertIn("optic groove", out)
        self.assertTrue(any("needs model/review" in n for n in notes), notes)

    def test_duplicate_dump_still_truncates_to_the_head(self):
        """The run-4 behaviour must survive: a re-recited dump is cut."""
        text = ("The optic groove appears by 3 weeks in the developing "
                "forebrain of the embryo at about day 22 of gestation.\n"
                "Solution to Question 5:\nThe optic groove appears by 3 "
                "weeks in the developing forebrain of the embryo.")
        out, notes = qp.sanitize_solution_text(text, own_qn=9)
        self.assertTrue(out.startswith("The optic groove appears by 3 weeks"))
        self.assertTrue(any("duplicated" in n for n in notes), notes)

    def test_own_header_match_wins_over_the_duplicate_rule(self):
        """Same-qn takes priority: even if the tail looks like a repeat, the
        header naming this question is the stronger evidence."""
        text = (self.Q3_TEXT + "\n\nSolution to Question 4:\n" + self.Q3_TEXT)
        out, _ = qp.sanitize_solution_text(text, own_qn=4)
        self.assertEqual(out.strip(), self.Q3_TEXT)

    def test_without_own_qn_nothing_is_cut(self):
        out, notes = qp.sanitize_solution_text(self._bled(), own_qn=None)
        self.assertIn("Uveal melanomas", out)
        self.assertTrue(any("needs model/review" in n for n in notes), notes)


class TestSolutionImgCrossReference(unittest.TestCase):
    """RUN-44 (OPH-001 q23 live): a solution that explains a figure-based
    question refers to THAT figure, and the figure is owned by the question
    side. q23's solution walks the marked diagram and owns no file of its own,
    which read as a missing figure and flagged the row."""

    def test_solution_token_explained_by_question_figure_is_not_flagged(self):
        # the stem carries its own [IMG] too, so the question side balances
        recs = {23: {"question_text": "Which muscles depress the eyeball? [IMG]",
                     "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                     "correct_option": "A",
                     "solution_text": "The depressors are [IMG] A and E as marked."}}
        qp.apply_img_placeholder_reconcile(
            recs, {23: {"question": ["OPH/OPH-001-023_Q_01.webp"],
                        "solution": []}})
        self.assertEqual(recs[23]["_review_reasons"], [],
                         recs[23]["_review_reasons"])

    def test_more_tokens_than_both_sides_own_is_still_flagged(self):
        """A solution cannot explain away figures nobody owns."""
        recs = {23: {"question_text": "Which muscles depress the eyeball? [IMG]",
                     "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                     "correct_option": "A",
                     "solution_text": "See [IMG] and also [IMG] and [IMG]."}}
        qp.apply_img_placeholder_reconcile(
            recs, {23: {"question": ["OPH/OPH-001-023_Q_01.webp"],
                        "solution": []}})
        self.assertTrue(recs[23]["_review_reasons"],
                        "3 tokens vs 1 owned figure must still flag")

    def test_token_with_no_figure_anywhere_is_still_flagged(self):
        """OPH-001 q15: the model emitted [IMG] but the book prints no figure
        for this question at all. That is a hallucination and must flag."""
        recs = {15: {"question_text": "Which structures are damaged?",
                     "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                     "correct_option": "C",
                     "solution_text": "The optic canal transmits [IMG] it."}}
        qp.apply_img_placeholder_reconcile(
            recs, {15: {"question": [], "solution": []}})
        self.assertTrue(any("img_placeholder_count_mismatch" in r
                            for r in recs[15]["_review_reasons"]),
                        recs[15]["_review_reasons"])

    def test_attached_figure_with_no_token_is_advisory_not_a_review_reason(self):
        """RUN-45 (OPH-001 q11 live, owner's decision): text with no [IMG]
        token but files attached is not a defect here. The converter reads
        images from images[] and attaches them directly; the token is only an
        inline-positioning hint. The figure is correctly owned, nothing lost."""
        recs = {11: {"question_text": "A girl is found to have this abnormality.",
                     "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                     "correct_option": "B",
                     "solution_text": "Coloboma is most common inferonasally."}}
        qp.apply_img_placeholder_reconcile(
            recs, {11: {"question": ["OPH/OPH-001-011_Q_01.webp"],
                        "solution": []}})
        self.assertEqual(recs[11]["_review_reasons"], [],
                         recs[11]["_review_reasons"])

    def test_same_rule_applies_to_the_solution_side(self):
        recs = {7: {"question_text": "Stem, no placeholder.",
                    "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                    "correct_option": "A",
                    "solution_text": "Solution, no placeholder."}}
        qp.apply_img_placeholder_reconcile(
            recs, {7: {"question": [], "solution": ["OPH/OPH-001-007_SOL_01.webp"]}})
        self.assertEqual(recs[7]["_review_reasons"], [],
                         recs[7]["_review_reasons"])

    def test_unequal_nonzero_counts_still_flag(self):
        """The converter would position images ambiguously: 2 markers, 1 file."""
        recs = {9: {"question_text": "See [IMG] and [IMG] here.",
                    "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                    "correct_option": "A",
                    "solution_text": "Because."}}
        qp.apply_img_placeholder_reconcile(
            recs, {9: {"question": ["OPH/OPH-001-009_Q_01.webp"],
                       "solution": []}})
        self.assertTrue(any("img_placeholder_count_mismatch" in r
                            for r in recs[9]["_review_reasons"]),
                        recs[9]["_review_reasons"])

    def test_question_side_gets_no_cross_reference_allowance(self):
        """A question stem cannot borrow the solution's figures."""
        recs = {5: {"question_text": "See [IMG] the diagram below.",
                    "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                    "correct_option": "A", "solution_text": "Because."}}
        qp.apply_img_placeholder_reconcile(
            recs, {5: {"question": [], "solution": ["OPH/OPH-001-005_SOL_01.webp"]}})
        self.assertTrue(any("img_placeholder_count_mismatch" in r
                            for r in recs[5]["_review_reasons"]),
                        recs[5]["_review_reasons"])


class TestOcrNoiseNote(unittest.TestCase):
    def test_normal_prose_is_not_flagged(self):
        self.assertIsNone(qp._ocr_noise_note(REAL_SHORT_SOLUTION, "solution"))

    def test_short_option_is_not_flagged(self):
        """A per-field check must not call a legitimate '2.4 cm' empty."""
        for opt in ("2.4 cm", "Mesoderm", "Neural crest", "6 months"):
            self.assertIsNone(qp._ocr_noise_note(opt, "option"), opt)

    def test_damaged_glyphs_are_flagged(self):
        bad = ("Astigmatic \ufffd\ufffd es _ ne 0 n * * XW e Niactinn "
               "= a ws +. \u25a0\u25a0")
        note = qp._ocr_noise_note(bad, "option D")
        self.assertIsNotNone(note)
        self.assertIn("option D", note)
        self.assertIn("damaged glyphs", note)

    def test_legitimate_typography_is_never_flagged(self):
        """RUN-35 regression. The first version counted ANY non-ASCII char as
        damage, so OPH-001 q9's ordinary three-bullet list scored 3/137 = 0.022
        and was flagged -- pushing the chapter from 6 REVIEW_NEEDED to 11 on
        text that was perfectly correct. Medical books legitimately use
        degrees, dashes, curly quotes, bullets and Greek."""
        legit = {
            "bullets": ("\u2022 Anterior cerebral artery - dorsal chiasma "
                        "\u2022 Anterior communicating artery - ventral "
                        "chiasma \u2022 Internal carotid artery - ventral "
                        "chiasma"),
            "en-dash": ("Sphincter pupillae: Short ciliary nerve\u2013Ciliary "
                        "ganglion. Ciliary muscle\u2013Accessory ganglion. "
                        "Dilator pupillae and Muller muscle."),
            "degrees": ("The orbit is more divergent (50\u00b0) as compared "
                        "to adult (45\u00b0). Note: The lacrimal glands are "
                        "underdeveloped in a newborn."),
            "curly+greek": ("Mittendorf\u2019s dot \u2014 remnant of the "
                            "hyaloid artery. The angle \u03b1 and \u03ba "
                            "are both measured here."),
            "units": ("The anteroposterior diameter is 24 mm/2.4 cm. At birth "
                      "the axial length is 16.5 mm, growing to 23 mm by 3 "
                      "years of age."),
        }
        for name, text in legit.items():
            self.assertIsNone(qp._ocr_noise_note(text, name), name)

    def test_real_garbage_is_still_caught(self):
        """The typo-detection must survive the tightening."""
        bad = ("Astigmatic x es _ ne 0 n *  * XW e Niactinn = a ws + "
               "z q 1 2 3 4 5 - = + ~ ^ % $ # @")
        self.assertIsNotNone(qp._ocr_noise_note(bad, "question stem"))

    def test_low_alphabetic_text_is_flagged(self):
        bad = ("oe SS \u00ab i i ity At what age would a child attain full a "
               "\u00ab mnths se n ef 1 2 3 4 5 6 7 8 9 0 - - = = + +")
        note = qp._ocr_noise_note(bad, "question stem")
        self.assertIsNotNone(note)
        self.assertIn("question stem", note)

    def test_empty_text_is_not_flagged(self):
        self.assertIsNone(qp._ocr_noise_note("", "solution"))
        self.assertIsNone(qp._ocr_noise_note(None, "solution"))


class TestTooShortSolutionIsAGateViolation(unittest.TestCase):
    """OPH-001 q3 shipped '. X' as its entire explanation and the gate said
    CLEAN, because missing_solution only tested for an empty string."""

    def _records(self, sol):
        return {1: {"question_text": "Which of the following is correct here?",
                    "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                    "correct_option": "B",
                    "solution_text": sol}}

    def test_two_char_residue_is_a_violation(self):
        v = qp._export_gate_violations(self._records(". X"), {}, [], "OPH-001")
        kinds = [k for k, _q, _d in v]
        self.assertIn("solution_too_short", kinds, v)

    def test_the_books_shortest_real_solution_passes(self):
        self.assertEqual(len(REAL_SHORT_SOLUTION), 63)
        self.assertGreater(len(REAL_SHORT_SOLUTION), qp.MIN_SOLUTION_CHARS)
        v = qp._export_gate_violations(
            self._records(REAL_SHORT_SOLUTION), {}, [], "OPH-001")
        self.assertEqual(v, [], v)

    def test_empty_solution_is_still_missing_not_too_short(self):
        v = qp._export_gate_violations(self._records(""), {}, [], "OPH-001")
        kinds = [k for k, _q, _d in v]
        self.assertIn("missing_solution", kinds)
        self.assertNotIn("solution_too_short", kinds)


class TestNoiseReachesQaStatus(unittest.TestCase):
    def _row(self, stem, options, sol):
        rec = {"question_text": stem, "options": options,
               "correct_option": "B", "solution_text": sol}
        return qp.build_final_question(
            "OPH", "OPH-001", 1, 1, rec, {"question": [], "solution": []},
            source_pages=[4], ownership_pages={})

    def test_garbled_stem_makes_the_row_review_needed(self):
        row = self._row(
            "oe SS \u00ab i i ity At what age would a child attain full a "
            "\u00ab mnths se n ef 1 2 3 4 5 6 7 8 9 0 - - = = + +",
            {"A": "6 months", "B": "1 year", "C": "3 years", "D": "6 years"},
            REAL_SHORT_SOLUTION)
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED")
        self.assertTrue(any("question stem" in r for r in row["qa_reasons"]),
                        row["qa_reasons"])

    def test_garbled_option_is_reported_by_letter(self):
        """The verbatim option D that OPH-001 shipped as READY. 30/57
        alphabetic (0.526); the book's legitimate options score ~0.76."""
        row = self._row(
            "At what age would a child attain full visual acuity in years?",
            {"A": "6 months", "B": "1 year", "C": "3 years",
             "D": "Astigmatic x\u201c es _ ne 0 n\\* \\ \u00bb\\* XW e Niactinn = a ws +."},
            REAL_SHORT_SOLUTION)
        self.assertEqual(row["qa_status"], "REVIEW_NEEDED")
        self.assertTrue(any("option D" in r for r in row["qa_reasons"]),
                        row["qa_reasons"])

    def test_a_clean_row_stays_ready(self):
        row = self._row(
            "At what age would a child attain full visual acuity in years?",
            {"A": "6 months", "B": "1 year", "C": "3 years", "D": "6 years"},
            REAL_SHORT_SOLUTION)
        self.assertEqual(row["qa_status"], "READY", row["qa_reasons"])


if __name__ == "__main__":
    import shutil
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
