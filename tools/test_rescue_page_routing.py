#!/usr/bin/env python3
"""
PSY-007 RESCUE PAGE-ROUTING test (2026-08-08, hardening after Railway run).

Real Railway bug observed in the post-390a9fe Railway run: the rescue
pass sent the contaminated-stem records to SOLUTION-side pages
(105-107) and returned 0 fields filled, because the model found no
stem region on a solutions page.

The fix is to ROUTE by gap type:
  * "question" gap          -> question-side pages (where "N." is printed)
  * "options" gap           -> question-side pages (options are under the Q)
  * "answer" gap            -> any key-table row page (Answer Key)
  * "solution" gap          -> solution-side pages (Solution to Question N:)

User's words from the Railway log review (translated):
  "yrr solutions to us page pr h hi nhi, verify kiu Krna h bs jesa h
  de de q id honi chahiye"
  = "Solutions us page pe hain hi nahi; solutions verify karne ki
    zaroorat nahi, bas jaisa hai waisa de do, q_id honi chahiye"
  = "That page has solutions, not question stems; don't waste calls
    verifying solutions, just return what we have; q_id must remain"

This test verifies the new routing WITHOUT requiring a real PDF or
poppler. It patches subprocess.run so the locator sees the right text
per page and returns categorized page lists per q_no.
"""
import sys
sys.path.insert(0, '.')
import qbank_pipeline as qp
from unittest import mock
import subprocess

# pdftotext_page() invokes:
#   subprocess.run(["pdftotext", "-f", str(true_page), "-l", str(true_page),
#                   "-layout", str(pdf_path), "-"], capture_output=True, text=True)
# We return a CompletedProcess with stdout = the page text.
PAGE_TEXTS = {
    # page 100: questions 1, 2, 3 (question side)
    100: "\n1. First question stem text.\n2. Second question stem text.\n3. Third question stem text.\n",
    # page 101: questions 4, 5, 6
    101: "\n4. Fourth question stem text.\n5. Fifth question stem text.\n6. Sixth question stem text.\n",
    # page 102: questions 7, 8, 9
    102: "\n7. Seventh question stem text.\n8. Eighth question stem text.\n9. Ninth question stem text.\n",
    # page 103: question 10
    103: "\n10. Tenth question stem text.\n",
    # page 104: questions 11, 12
    104: "\n11. Eleventh question stem text.\n12. Twelfth question stem text.\n",
    # page 105: solutions 1, 2, 3 (solution side -- NO question stems)
    105: "\nSolution to Question 1: First solution text.\nSolution to Question 2: Second solution text.\nSolution to Question 3: Third solution text.\n",
    # page 106: solutions 4, 5, 6, 7
    106: "\nSolution to Question 4: Fourth solution text.\nSolution to Question 5: Fifth solution text.\nSolution to Question 6: Sixth solution text.\nSolution to Question 7: Seventh solution text.\n",
    # page 107: solutions 8, 9, 10
    107: "\nSolution to Question 8: Eighth solution text.\nSolution to Question 9: Ninth solution text.\nSolution to Question 10: Tenth solution text.\n",
}


def fake_subprocess_run(cmd, **kwargs):
    page_no = int(cmd[2])
    text = PAGE_TEXTS.get(page_no, "")
    return subprocess.CompletedProcess(args=cmd, returncode=0,
                                       stdout=text, stderr="")


class _PF:
    """A mock page_file matching pdftoppm's output: 'page-NNN.jpg'.
    Path('page-100.jpg').stem == 'page-100' (strips .jpg), so the locator's
    int(pf.stem.split("-")[-1]) correctly parses the page number.
    """
    def __init__(self, n):
        self._n = n
    @property
    def stem(self):
        return f"page-{self._n:03d}"


def test_locate_returns_categorized_pages():
    """Verify the NEW locate_missing_record_pages returns pages categorized
    by gap type. The OLD version returned {qn: [all pages]} which led to
    the rescue pass sending stem-recovery asks to solution pages."""

    qn_missing = {qn: {"question"} for qn in [2, 4, 6, 7, 9]}
    page_files = [_PF(n) for n in sorted(PAGE_TEXTS.keys())]

    with mock.patch('subprocess.run', side_effect=fake_subprocess_run):
        located = qp.locate_missing_record_pages(
            "/fake/pdf.pdf", page_files, qn_missing, None)

    print(f"locate output keys: {sorted(located.keys())}")
    assert sorted(located.keys()) == [2, 4, 6, 7, 9], \
        f"expected q2/q4/q6/q7/q9 in result, got {sorted(located.keys())}"

    for qn in [2, 4, 6, 7, 9]:
        loc = located[qn]
        print(f"  q{qn}: question={loc.get('question')} "
              f"answer={loc.get('answer')} solution={loc.get('solution')}")
        assert isinstance(loc, dict), \
            f"q{qn}: expected dict (categorized pages), got {type(loc).__name__}"
        # The CATEGORIZED dict only contains NON-EMPTY categories. q_no may
        # legitimately have no answer-key page or no solution-side page
        # if the chapter's text layer doesn't carry those. The rescue
        # pass must handle missing categories by skipping that gap type.
        for cat in ("question", "answer", "solution"):
            v = loc.get(cat)
            assert v is None or isinstance(v, list), \
                f"q{qn}.{cat}: expected None or list, got {type(v).__name__}"

    # q2 should be on question-side (page 100) and solution-side (page 105)
    assert 100 in located[2]["question"], \
        "q2's stem should be on page 100 (question side)"
    assert 105 in located[2]["solution"], \
        "q2's solution should be on page 105 (solution side)"

    # THE CRITICAL ASSERTION (the bug fix):
    # q2's "question" page list MUST NOT include solution-side pages,
    # otherwise the rescue pass sends stem-recovery asks to page 105/106/107
    # and the model returns null (the bug the user reported).
    for page in located[2]["question"]:
        assert page not in (105, 106, 107), \
            f"BUG: q2's stem-side routing includes solution page {page}; " \
            f"the rescue pass would send a stem-recovery ask there and the " \
            f"model would return null (no stem region on a solutions page)."

    # q4's stem is on page 101 (question side), not in solution pages
    assert 101 in located[4]["question"]
    for page in located[4]["question"]:
        assert page not in (105, 106, 107), \
            f"BUG: q4's stem-side routing includes solution page {page}"

    # q6's stem is on page 101
    assert 101 in located[6]["question"]
    # q7's stem is on page 102
    assert 102 in located[7]["question"]
    # q9's stem is on page 102
    assert 102 in located[9]["question"]

    # And NONE of the stems should be on solution pages
    for qn in [2, 4, 6, 7, 9]:
        for page in located[qn]["question"]:
            assert page not in (105, 106, 107), \
                f"BUG: q{qn} stem page {page} is a solution page"

    # Solutions ARE on the solution side (sanity check the dual routing)
    assert 105 in located[2]["solution"]
    assert 106 in located[4]["solution"]
    assert 106 in located[6]["solution"]
    assert 106 in located[7]["solution"]
    assert 107 in located[9]["solution"]

    # Solutions MUST NOT include question-side pages
    for qn in [2, 4, 6, 7, 9]:
        for page in located[qn]["solution"]:
            assert page not in (100, 101, 102, 103, 104), \
                f"BUG: q{qn} solution page {page} is a question page"

    print()
    print("All routing assertions pass.")
    return 5 + 15 + 22


if __name__ == "__main__":
    n = test_locate_returns_categorized_pages()
    print(f"\n=== {n} assertions passed ===")
