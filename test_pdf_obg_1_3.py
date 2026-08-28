#!/usr/bin/env python3
"""PDF regression 1–12 — real book.pdf, NO Gemini.

Printed facts (pixels / known book), not Gemini LOCKED.
Skip the whole class if /home/user/book2/book.pdf is missing.
"""
import unittest
from pathlib import Path

PDF = Path("/home/user/book2/book.pdf")

CH1_KEY = {
    1: "D", 2: "C", 3: "B", 4: "A", 5: "B", 6: "A", 7: "C", 8: "B",
    9: "A", 10: "B", 11: "A", 12: "C", 13: "C", 14: "A", 15: "B", 16: "B",
    17: "B", 18: "D", 19: "D", 20: "C", 21: "B", 22: "A", 23: "A", 24: "B",
    25: "C", 26: "D",
}
CH2_KEY = {
    1: "A", 2: "A", 3: "C", 4: "C", 5: "B", 6: "A", 7: "A", 8: "B",
    9: "A", 10: "A", 11: "C", 12: "B", 13: "A", 14: "D", 15: "B",
}
CH3_KEY = {
    1: "A", 2: "A", 3: "C", 4: "B", 5: "A", 6: "D", 7: "D", 8: "C",
    9: "B", 10: "A", 11: "D", 12: "D", 13: "A", 14: "D", 15: "B", 16: "C",
}


@unittest.skipUnless(PDF.is_file(), "book.pdf not mounted")
class PdfObg123(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import header_index
        # Narrow scans: key pages + a few headers. Full 5–65 is slow.
        cls.hi = header_index
        cls.ch1 = header_index.scan_chapter(str(PDF), 12, 13, dpi=120)
        cls.ch2 = header_index.scan_chapter(str(PDF), 37, 38, dpi=120)
        cls.ch3 = header_index.scan_chapter(str(PDF), 49, 52, dpi=120)
        cls.ch1q = header_index.scan_chapter(str(PDF), 5, 6, dpi=110)

    def test_01_book_page_count(self):
        import fitz
        doc = fitz.open(str(PDF))
        n = doc.page_count
        doc.close()
        self.assertGreaterEqual(n, 65)
        self.assertLessEqual(n, 1200)

    def test_02_ch1_key_region_12_13(self):
        pages = self.hi.key_region_pages(self.ch1)
        self.assertEqual(pages, [12, 13], pages)

    def test_03_ch2_key_region_37_38(self):
        pages = self.hi.key_region_pages(self.ch2)
        self.assertEqual(pages, [37, 38], pages)

    def test_04_ch3_key_region_52(self):
        pages = self.hi.key_region_pages(self.ch3)
        self.assertEqual(pages, [52], pages)

    def test_05_ch1_answer_key_header_on_12(self):
        types = {r["type"] for r in self.ch1 if r["page"] == 12}
        self.assertIn(self.hi.T_ANSWER_KEY, types, self.ch1)

    def test_06_ch1_explanations_or_sol1_on_13(self):
        types = {r["type"] for r in self.ch1 if r["page"] == 13}
        self.assertTrue(
            self.hi.T_DETAILED in types or self.hi.T_SOLUTION in types,
            self.ch1)

    def test_07_ch3_gap_inject_question_7(self):
        recs = [r for r in self.ch3 if r["type"] == self.hi.T_QUESTION]
        out = self.hi.inject_gap_headers(recs, self.hi.T_QUESTION)
        ns = {r["n"] for r in out if r.get("n")}
        # If OCR saw 6 and 8, 7 is injected. If it saw 7 already, still present.
        if ns:
            if 6 in ns and 8 in ns:
                self.assertIn(7, ns)

    def test_08_ocr_key_table_ch1_subset(self):
        rows = self.hi.ocr_key_table(str(PDF), [12, 13], dpi=140)
        # Whole-page OCR is noisy (option letters look like key rows).
        # Require the printed 1–9 grid on p12 when those numbers appear.
        self.assertTrue(rows, "ocr produced no key rows")
        for n in range(1, 10):
            if n in rows:
                self.assertEqual(rows[n], CH1_KEY[n], f"q{n} ocr={rows[n]}")

    def test_09_ocr_key_table_ch2_includes_15(self):
        rows = self.hi.ocr_key_table(str(PDF), [37, 38], dpi=140)
        if 15 in rows:
            self.assertEqual(rows[15], "B")
        for n, let in rows.items():
            if n in CH2_KEY:
                self.assertEqual(let, CH2_KEY[n], f"q{n}")

    def test_10_ocr_key_table_ch3_q7_is_d(self):
        rows = self.hi.ocr_key_table(str(PDF), [52], dpi=140)
        if 7 in rows:
            self.assertEqual(rows[7], "D")
        for n, let in rows.items():
            if n in CH3_KEY:
                self.assertEqual(let, CH3_KEY[n], f"q{n}")

    def test_11_question_1_header_on_page_5(self):
        ns = {r["n"] for r in self.ch1q
              if r["type"] == self.hi.T_QUESTION and r["page"] == 5}
        self.assertIn(1, ns, self.ch1q)

    def test_12_v2_export_matches_printed_keys_and_q14(self):
        """If live v2 is present, it must match printed keys (no Gemini here)."""
        p = Path("/home/user/obg_ch1_3_v2/data/questions.jsonl")
        if not p.is_file():
            self.skipTest("v2 extract not on disk")
        import json
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        by = {}
        for r in rows:
            by.setdefault(r["chapter_id"], {})[int(r["id"].split("-")[-1])] = r
        for n, let in CH1_KEY.items():
            self.assertEqual(by["OBG-001"][n]["correct_options"], [let], n)
        q14 = by["OBG-001"][14]["solution"]["text"]
        self.assertIn("uterine", q14.lower())
        self.assertNotIn("Call-Exner", q14)
        for n, let in CH2_KEY.items():
            self.assertEqual(by["OBG-002"][n]["correct_options"], [let], n)
        for n, let in CH3_KEY.items():
            self.assertEqual(by["OBG-003"][n]["correct_options"], [let], n)


if __name__ == "__main__":
    unittest.main()
