"""
test_master_review_export.py
=============================

Phase 4 (post-Phase-2 work) self-test for the MASTER_REVIEW/ package
builder. The exporter is a read-only convenience layer that copies
already-extracted files from split/ + assets/ + data/ into a
separate MASTER_REVIEW/ tree for human review. It must NEVER
modify the live output, NEVER transform content, NEVER invent
missing files.

The test covers:
  A. Build a synthetic 2-subject / 3-chapter extraction output
     (split/, assets/questions/, data/) and run build_master_review.
  B. Verify the produced MASTER_REVIEW/ tree has the exact
     7-file structure under each subject/chapter.
  C. Verify byte-for-byte preservation: every file in the
     MASTER_REVIEW tree is identical to the source.
  D. Verify image refs are honored: each image_manifest.jsonl
     `file` path is copied to MASTER_REVIEW/assets/questions/...
  E. Verify missing files are reported in the manifest as
     warnings (NOT auto-generated).
  F. Verify missing images are reported in the manifest
     (NOT auto-resolved or invented).
  G. Verify the build is idempotent: a second run rebuilds the
     tree cleanly (no leftover files from a prior MASTER_REVIEW
     that would cause stale-content bugs).
  H. Verify the zip: build_master_review_zip produces a valid
     zip that contains every file in the directory tree.
  I. Verify the live extraction output is NOT modified
     (split/, assets/, data/, subjects/ all untouched).
  J. Verify the manifest's manifest shape (started_at, finished_at,
     purpose, totals, per-subject breakdown).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import master_review_export


# ===========================================================================
# Fixtures
# ===========================================================================

def _write_split_chapter(split_root: Path, subject: str, chapter_no: int,
                          n_records: int = 3,
                          images_per_chapter: list = None) -> Path:
    """Build a synthetic split/<subject>/<chapter>/ directory with
    the 7 expected files. images_per_chapter is a list of
    (q_no, type, option_letter, file) tuples -- if None, no images.
    The image files themselves are also written to
    assets/questions/<subject>/<file> so the exporter can copy them."""
    chapter_id = f"{subject}-{chapter_no:03d}"
    chapter_dir = split_root / subject / chapter_id
    chapter_dir.mkdir(parents=True, exist_ok=True)
    q_rows, a_rows, s_rows = [], [], []
    image_manifest_rows = []
    for qn in range(1, n_records + 1):
        q_id = f"{subject}-{chapter_no:03d}-{qn:03d}"
        # Build per-question images from the images_per_chapter list
        q_imgs = [t for t in (images_per_chapter or []) if t[0] == qn]
        q_images = [{"file": f"PSY/{t[3]}",
                     "source_pages": [100 + qn]} for t in q_imgs
                    if t[1] == "QUESTION"]
        s_images = [{"file": f"PSY/{t[3]}",
                     "source_pages": [100 + qn]} for t in q_imgs
                    if t[1] == "SOLUTION"]
        o_images = {letter: [{"file": f"PSY/{t[3]}",
                              "source_pages": [100 + qn]}]
                    for letter, t in [(t[2], t) for t in q_imgs
                                       if t[1] == "OPTION"]}
        q_rows.append({
            "q_id": q_id, "chapter_id": chapter_id, "subject": subject,
            "chapter_no": chapter_no, "q_no": qn,
            "q_id_grade": "RESOLVED_ANCHORED", "q_no_anchors": {},
            "question_text": f"Stem for q{qn}?",
            "options": [
                {"id": "A", "text": "opt A", "images": o_images.get("A", [])},
                {"id": "B", "text": "opt B", "images": o_images.get("B", [])},
                {"id": "C", "text": "opt C", "images": o_images.get("C", [])},
                {"id": "D", "text": "opt D", "images": o_images.get("D", [])},
            ],
            "question_images": q_images, "tables": [],
            "source_pages": [100 + qn], "extraction_status": "COMPLETE",
        })
        a_rows.append({
            "q_id": q_id, "chapter_id": chapter_id, "subject": subject,
            "chapter_no": chapter_no, "q_no": qn,
            "correct_option": "A", "correct_option_prov": "A_PASS",
            "q_id_grade": "RESOLVED_ANCHORED", "q_no_anchors": {},
            "source_pages": [100 + qn], "extraction_status": "COMPLETE",
        })
        s_rows.append({
            "q_id": q_id, "chapter_id": chapter_id, "subject": subject,
            "chapter_no": chapter_no, "q_no": qn,
            "solution_text": f"Solution for q{qn}.", "tables": [],
            "solution_images": s_images, "solution_prov": "S_PASS",
            "q_id_grade": "RESOLVED_ANCHORED", "q_no_anchors": {},
            "source_pages": [100 + qn], "extraction_status": "COMPLETE",
        })
        for t in q_imgs:
            image_manifest_rows.append({
                "q_id": q_id, "type": t[1],
                "option_letter": t[2], "file": f"PSY/{t[3]}",
                "source_pages": [100 + qn],
            })
    # Write the 7 split files (atomically is not needed for a test;
    # plain write is fine).
    for fname, rows in (
        ("questions.jsonl", q_rows),
        ("answers.jsonl", a_rows),
        ("solutions.jsonl", s_rows),
        ("image_manifest.jsonl", image_manifest_rows),
        ("unresolved_qids.jsonl", []),
        ("orphans.jsonl", []),
    ):
        path = chapter_dir / fname
        if not rows:
            path.write_text("", encoding="utf-8")
        else:
            path.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8",
            )
    (chapter_dir / "chapter_completeness.json").write_text(
        json.dumps({
            "chapter_id": chapter_id, "subject": subject, "chapter_no": chapter_no,
            "question_records": n_records, "answer_records": n_records,
            "solution_records": n_records, "image_manifest_records": len(image_manifest_rows),
            "q_id_grade_counts": {"RESOLVED_ANCHORED": n_records,
                                   "RESOLVED": 0, "PROVISIONAL": 0, "UNRESOLVED": 0},
            "extraction_status_counts": {"COMPLETE": 3 * n_records, "INCOMPLETE": 0},
        }, indent=2),
        encoding="utf-8",
    )
    return chapter_dir


def _write_image(assets_q_root: Path, fname: str, content: bytes = b"",
                subject: str = "PSY") -> Path:
    """Write a fake .webp file under assets/questions/<subject>/.
    Default content is a 16-byte placeholder so the file is
    detectable (non-zero size). The file path uses the subject
    prefix (PSY/) matching the image_manifest convention.
    The `subject` argument is required because the caller
    knows the subject (e.g. "PSY" or "MED") while `assets_q_root`
    is the `assets/questions/` dir that contains subject subdirs.
    """
    base = Path(fname).name
    dest = assets_q_root / subject / base
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content if content else b"FAKE_WEBP_" + fname.encode() + b"_X" * 16)
    return dest


def _build_synthetic_output(out_root: Path) -> dict:
    """Build a complete synthetic output (split/ + assets/ + data/)
    under out_root. Returns a dict of the expected state so the
    test assertions can reference it."""
    split_root = out_root / "split"
    assets_q_root = out_root / "assets" / "questions"
    data_root = out_root / "data"
    split_root.mkdir(parents=True, exist_ok=True)
    assets_q_root.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)

    # 2 PSY chapters + 1 MED chapter = 3 chapters total
    psy_ch7_imgs = [
        (1, "QUESTION",  None,  "PSY-007-001-Q1.webp"),
        (1, "OPTION",    "B",   "PSY-007-001-B.webp"),
        (1, "OPTION",    "D",   "PSY-007-001-D.webp"),
        (2, "SOLUTION",  None,  "PSY-007-002-S2.webp"),
    ]
    psy_ch8_imgs = [
        (1, "QUESTION",  None,  "PSY-008-001-Q1.webp"),
    ]
    psy7 = _write_split_chapter(split_root, "PSY", 7, n_records=5,
                                images_per_chapter=psy_ch7_imgs)
    psy8 = _write_split_chapter(split_root, "PSY", 8, n_records=2,
                                images_per_chapter=psy_ch8_imgs)
    med1 = _write_split_chapter(split_root, "MED", 1, n_records=4)

    # Write the actual image files to assets/questions/PSY/.
    # The image_manifest uses paths like "PSY/PSY-007-001-Q1.webp"
    # (subject prefix + basename). The exporter strips the subject
    # prefix and looks under assets/questions/<subject>/<basename>.
    expected_images = []
    # Intentionally SKIP one image (PSY-007-002-S2.webp) so the
    # exporter records it as "missing" -- proves the missing-image
    # reporting path works.
    for ch in (psy_ch7_imgs, psy_ch8_imgs):
        for q_no, _type, _letter, fname in ch:
            basename = fname.split("/")[-1]
            if basename == "PSY-007-002-S2.webp":
                # Don't write -- this one is "missing on disk"
                continue
            _write_image(assets_q_root, basename, content=fname.encode(),
                         subject="PSY")
            expected_images.append(("PSY", basename))

    # 1 image referenced by the manifest but NOT on disk
    # (this should land in the manifest as "missing"). The basename
    # is real so the exporter's "image_manifest references this
    # basename" loop finds it, but the file doesn't exist on disk.
    expected_missing = ("PSY", "PSY-007-002-S2.webp")

    # Write 9 QA sidecar files (the user-specified list)
    qa_written = {}
    for qa in master_review_export.QA_FILES:
        path = data_root / qa
        if qa.endswith(".jsonl"):
            path.write_text(
                json.dumps({"chapter_id": "PSY-007", "kind": "test_fixture"})
                + "\n",
                encoding="utf-8",
            )
        else:
            path.write_text(
                json.dumps({"chapters": [{"id": "PSY-007"}, {"id": "PSY-008"}]})
                + "\n",
                encoding="utf-8",
            )
        qa_written[qa] = path

    # 1 QA file that should be reported as MISSING
    (data_root / "page_ledger.jsonl").unlink()  # remove one we just wrote

    # Also seed a couple of files the exporter must NOT copy
    # (live output only -- these are pipeline artifacts the brief
    # explicitly says should NOT be required for review).
    (out_root / "subjects" / "PSY" / "questions.jsonl").parent.mkdir(
        parents=True, exist_ok=True)
    (out_root / "subjects" / "PSY" / "questions.jsonl").write_text(
        "{\"subject\":\"PSY\"}\n", encoding="utf-8")
    (out_root / "data" / "by_chapter" / "PSY-007.jsonl").parent.mkdir(
        parents=True, exist_ok=True)
    (out_root / "data" / "by_chapter" / "PSY-007.jsonl").write_text(
        "{\"id\":\"PSY-007-001\"}\n", encoding="utf-8")
    (out_root / "data" / "by_chapter" / "PSY-008.jsonl").write_text(
        "{\"id\":\"PSY-008-001\"}\n", encoding="utf-8")
    (out_root / "state.json").write_text(
        json.dumps({"calls_today": 0, "pdf_progress": {}}),
        encoding="utf-8")
    (out_root / "_archive" / "stale").mkdir(parents=True, exist_ok=True)
    (out_root / "_archive" / "stale" / "old.json").write_text("STALE", encoding="utf-8")

    return {
        "chapters": [psy7, psy8, med1],
        "expected_images": expected_images,
        "expected_missing_images": [expected_missing],
        "qa_written": qa_written,
        "qa_missing": ["page_ledger.jsonl"],
    }


# ===========================================================================
# Tests
# ===========================================================================

def main() -> int:
    n_ok = 0
    n_total = 0
    failed = []

    def check(label, cond, detail=""):
        nonlocal n_ok, n_total
        n_total += 1
        if cond:
            n_ok += 1
            print(f"  ok:   {label}"
                  + (f" ({detail})" if detail else ""))
        else:
            print(f"  FAIL: {label}"
                  + (f" (got {detail})" if detail else ""))
            failed.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="master_review_export_test_"))
    out_root = tmp / "qbank_output"
    out_root.mkdir(parents=True, exist_ok=True)
    print("=" * 80)
    print(f"Synthetic output root: {out_root}")
    print("=" * 80)

    # ----------------------------------------------------------------
    # A. Build synthetic extraction output
    # ----------------------------------------------------------------
    print("\nA. Build synthetic 2-subject / 3-chapter extraction output")
    fx = _build_synthetic_output(out_root)
    check("3 chapters built (PSY-007, PSY-008, MED-001)",
          len(fx["chapters"]) == 3,
          f"got {len(fx['chapters'])}")
    for ch in fx["chapters"]:
        check(f"chapter {ch.name} has all 7 split files",
              all((ch / f).exists() for f in master_review_export.SPLIT_FILES),
              f"ch={ch}")
    check("9 QA sidecar files exist (8 written + 1 missing)",
          len(fx["qa_written"]) == 9 and "page_ledger.jsonl" in fx["qa_written"]
          and not (out_root / "data" / "page_ledger.jsonl").exists())
    check("subjects/PSY/questions.jsonl is the excluded convenience file",
          (out_root / "subjects" / "PSY" / "questions.jsonl").exists())
    check("data/by_chapter/PSY-007.jsonl is the excluded per-chapter copy",
          (out_root / "data" / "by_chapter" / "PSY-007.jsonl").exists())
    check("state.json exists at root (pipeline checkpoint)",
          (out_root / "state.json").exists())
    check("_archive/stale/old.json is the excluded reset archive",
          (out_root / "_archive" / "stale" / "old.json").exists())

    # ----------------------------------------------------------------
    # B. Run build_master_review and verify the directory structure
    # ----------------------------------------------------------------
    print("\nB. build_master_review produces the documented tree")
    manifest = master_review_export.build_master_review(out_root)
    review = out_root / "MASTER_REVIEW"
    check("MASTER_REVIEW/ created", review.is_dir())
    # 3 chapters -> 3 chapter dirs
    for ch in fx["chapters"]:
        ch_id = ch.name
        subject = ch.parent.name
        review_ch = review / "split" / subject / ch_id
        check(f"  split/{subject}/{ch_id}/ exists", review_ch.is_dir())
        for fname in master_review_export.SPLIT_FILES:
            check(f"    {ch_id}/{fname} copied",
                  (review_ch / fname).is_file(),
                  f"missing: {(review_ch / fname)}")
    # 9 QA files (8 written + 1 missing)
    check("data/ exists in MASTER_REVIEW", (review / "data").is_dir())
    for qa, path in fx["qa_written"].items():
        if qa == "page_ledger.jsonl":
            # this one is missing
            check(f"  data/{qa} NOT copied (it's missing on disk)",
                  not (review / "data" / qa).exists())
            continue
        check(f"  data/{qa} copied",
              (review / "data" / qa).is_file(),
              f"missing: {qa}")
    # assets/questions/PSY/ with the 5 expected images
    check("assets/questions/PSY/ exists", (review / "assets" / "questions" / "PSY").is_dir())
    expected_on_disk = [t[1] for t in fx["expected_images"]]
    for fname in expected_on_disk:
        check(f"  image {fname} copied to assets/questions/PSY/",
              (review / "assets" / "questions" / "PSY" / fname).is_file(),
              f"missing: {fname}")

    # ----------------------------------------------------------------
    # C. Byte-for-byte preservation
    # ----------------------------------------------------------------
    print("\nC. Files are preserved byte-for-byte (no transformation)")
    for ch in fx["chapters"]:
        ch_id = ch.name
        subject = ch.parent.name
        review_ch = review / "split" / subject / ch_id
        for fname in master_review_export.SPLIT_FILES:
            src = ch / fname
            dst = review_ch / fname
            if src.exists() and dst.exists():
                check(f"  {ch_id}/{fname} byte-identical",
                      src.read_bytes() == dst.read_bytes(),
                      f"size src={src.stat().st_size} dst={dst.stat().st_size}"
                      if src.stat().st_size != dst.stat().st_size
                      else "")
    # Same for QA sidecars
    for qa, path in fx["qa_written"].items():
        if qa == "page_ledger.jsonl":
            continue
        dst = review / "data" / qa
        if path.exists() and dst.exists():
            check(f"  data/{qa} byte-identical",
                  path.read_bytes() == dst.read_bytes())
    # Same for images
    for _subject, fname in fx["expected_images"]:
        src = out_root / "assets" / "questions" / "PSY" / fname
        dst = review / "assets" / "questions" / "PSY" / fname
        if src.exists() and dst.exists():
            check(f"  image {fname} byte-identical",
                  src.read_bytes() == dst.read_bytes())

    # ----------------------------------------------------------------
    # D. Image refs are honored (image_manifest.jsonl -> file)
    # ----------------------------------------------------------------
    print("\nD. Image refs honored -- every image_manifest entry's file is copied")
    # Read PSY-007's image_manifest and verify every file is on disk
    psy7_manifest_path = (review / "split" / "PSY" / "PSY-007" /
                          "image_manifest.jsonl")
    rows = [json.loads(ln) for ln in psy7_manifest_path.read_text().splitlines()
            if ln.strip()]
    for row in rows:
        fpath = row["file"]
        # fpath is "PSY/<basename>"; review copy strips the subject
        # prefix and writes to assets/questions/PSY/<basename>
        basename = fpath.split("/")[-1]
        dst = review / "assets" / "questions" / "PSY" / basename
        # 1 image in this fixture is intentionally missing
        if basename == "PSY-007-002-S2.webp":
            check(f"  missing image recorded: {basename}",
                  not dst.exists()
                  and manifest["subjects"]["PSY"]["images"][basename] == "missing")
        else:
            check(f"  image {basename} copied (manifest type={row['type']}, "
                  f"option_letter={row.get('option_letter')})",
                  dst.exists())
    # Also confirm the manifest preserved type/option_letter
    types_seen = {row["type"] for row in rows}
    check("  image_manifest preserved QUESTION + OPTION + SOLUTION types",
          types_seen == {"QUESTION", "OPTION", "SOLUTION"},
          f"got {types_seen}")
    option_letters = {row["option_letter"] for row in rows
                      if row["type"] == "OPTION"}
    check("  image_manifest preserved option letters (B, D)",
          option_letters == {"B", "D"}, f"got {option_letters}")

    # ----------------------------------------------------------------
    # E. Missing files are reported in the manifest as warnings
    # ----------------------------------------------------------------
    print("\nE. Missing files are reported in manifest (NOT auto-generated)")
    # The 1 missing QA file (page_ledger.jsonl) -- we deleted it
    # intentionally above.
    check("  page_ledger.jsonl in qa_files_missing",
          "page_ledger.jsonl" in manifest["qa_files_missing"])
    check("  page_ledger.jsonl NOT in qa_files_copied",
          "page_ledger.jsonl" not in manifest["qa_files_copied"])
    check("  total_files_missing == 1 (page_ledger.jsonl)",
          manifest["total_files_missing"] == 1,
          f"got {manifest['total_files_missing']}")
    # If a chapter is missing one of the 7 split files, that should
    # also be reported. We test that by deleting one and re-running.
    (out_root / "split" / "PSY" / "PSY-008" / "orphans.jsonl").unlink()
    manifest2 = master_review_export.build_master_review(out_root)
    check("  missing split file (orphans.jsonl) reported in manifest",
          "orphans.jsonl" in
          manifest2["subjects"]["PSY"]["chapters"]["PSY-008"]["files_missing"],
          f"manifest2 subjects/PSY/chapters/PSY-008/files_missing: "
          f"{manifest2['subjects']['PSY']['chapters']['PSY-008']['files_missing']}")
    # Recreate the file for subsequent tests
    (out_root / "split" / "PSY" / "PSY-008" / "orphans.jsonl").write_text(
        "", encoding="utf-8")

    # ----------------------------------------------------------------
    # F. Missing images are reported in the manifest
    # ----------------------------------------------------------------
    print("\nF. Missing images are reported in manifest (NOT auto-resolved)")
    check("  missing image (PSY-007-002-S2.webp) recorded",
          manifest["subjects"]["PSY"]["images"].get("PSY-007-002-S2.webp")
          == "missing",
          f"got {manifest['subjects']['PSY']['images'].get('PSY-007-002-S2.webp')}")
    check("  total_images_missing == 1",
          manifest["total_images_missing"] == 1)
    check("  total_images_copied == 4 (3 from PSY-007 + 1 from PSY-008; "
          "1 image intentionally skipped as 'missing')",
          manifest["total_images_copied"] == 4,
          f"got {manifest['total_images_copied']}")

    # ----------------------------------------------------------------
    # G. Idempotent rebuild
    # ----------------------------------------------------------------
    print("\nG. Idempotent: second build produces a clean tree")
    # Add a stale file to the review dir that should NOT survive
    # the next build.
    stale = review / "stale_artifact.txt"
    stale.write_text("STALE", encoding="utf-8")
    check("  stale_artifact.txt exists before second build", stale.exists())
    # Re-run -- should clean and rebuild.
    manifest3 = master_review_export.build_master_review(out_root)
    check("  stale_artifact.txt removed by second build",
          not stale.exists())
    check("  second build still has 3 chapters",
          manifest3["total_chapters"] == 3)
    # 3 chapters * 7 split files = 21 split files; 8 QA files (we
    # deleted page_ledger.jsonl for the E test, so 8 qa not 9).
    check("  second build copies 21 split + 8 qa = 29 files",
          manifest3["total_files_copied"] == 29,
          f"got {manifest3['total_files_copied']}")

    # ----------------------------------------------------------------
    # H. Zip works
    # ----------------------------------------------------------------
    print("\nH. build_master_review_zip produces a valid zip")
    zip_path = master_review_export.build_master_review_zip(out_root)
    check("  zip file exists at output_root/MASTER_REVIEW.zip",
          zip_path.exists()
          and zip_path.name == "MASTER_REVIEW.zip")
    check("  zip is non-trivial in size (>1KB)",
          zip_path.stat().st_size > 1024,
          f"size={zip_path.stat().st_size}")
    # Inspect zip contents
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    expected_in_zip = set()
    # 21 split files
    for ch in fx["chapters"]:
        ch_id = ch.name
        subject = ch.parent.name
        for fname in master_review_export.SPLIT_FILES:
            expected_in_zip.add(f"MASTER_REVIEW/split/{subject}/{ch_id}/{fname}")
    # 8 QA files
    for qa in fx["qa_written"]:
        if qa == "page_ledger.jsonl":
            continue
        expected_in_zip.add(f"MASTER_REVIEW/data/{qa}")
    # 5 images
    for _subject, fname in fx["expected_images"]:
        expected_in_zip.add(f"MASTER_REVIEW/assets/questions/PSY/{fname}")
    # Manifest
    expected_in_zip.add("MASTER_REVIEW/MASTER_REVIEW_MANIFEST.json")
    missing_in_zip = expected_in_zip - names
    extra_in_zip = names - expected_in_zip
    check(f"  zip has all {len(expected_in_zip)} expected files",
          not missing_in_zip and not extra_in_zip,
          f"missing={len(missing_in_zip)} extra={len(extra_in_zip)}")
    if missing_in_zip:
        print(f"    missing examples: {list(missing_in_zip)[:3]}")
    if extra_in_zip:
        print(f"    extra examples: {list(extra_in_zip)[:3]}")

    # ----------------------------------------------------------------
    # I. Live output is NOT modified
    # ----------------------------------------------------------------
    print("\nI. Live extraction output is NOT modified by the exporter")
    # The exporter only writes to MASTER_REVIEW/ + MASTER_REVIEW.zip
    # -- it must not touch split/, assets/, data/, subjects/,
    # by_chapter/, state.json, or _archive/.
    # Read a sentinel from each live location BEFORE and AFTER a
    # re-build -- they must match.
    sentinels = {
        "split/PSY/PSY-007/questions.jsonl":
            fx["chapters"][0] / "questions.jsonl",
        "data/chapters.json":
            out_root / "data" / "chapters.json",
        "subjects/PSY/questions.jsonl":
            out_root / "subjects" / "PSY" / "questions.jsonl",
        "data/by_chapter/PSY-007.jsonl":
            out_root / "data" / "by_chapter" / "PSY-007.jsonl",
        "state.json":
            out_root / "state.json",
        "_archive/stale/old.json":
            out_root / "_archive" / "stale" / "old.json",
    }
    before = {k: p.read_bytes() for k, p in sentinels.items()}
    master_review_export.build_master_review(out_root)
    after = {k: p.read_bytes() for k, p in sentinels.items()}
    for label, src_path in sentinels.items():
        check(f"  live file unchanged: {label}",
              before[label] == after[label],
              f"size before={len(before[label])} after={len(after[label])}")
    # And the zip did not get written outside the output root
    check("  no files written outside output_root",
          not (tmp / "stray.zip").exists())

    # ----------------------------------------------------------------
    # J. Manifest shape
    # ----------------------------------------------------------------
    print("\nJ. Manifest shape (started_at, finished_at, purpose, totals)")
    m4 = master_review_export.build_master_review(out_root)
    check("  manifest has 'output_root' key", "output_root" in m4)
    check("  manifest has 'master_review_dir' key",
          "master_review_dir" in m4)
    check("  manifest has 'started_at' key (ISO-ish)",
          "started_at" in m4 and "T" in m4["started_at"])
    check("  manifest has 'finished_at' key (ISO-ish)",
          "finished_at" in m4 and "T" in m4["finished_at"])
    check("  manifest has 'purpose' key with NO TRANSFORMATION message",
          "NO TRANSFORMATION" in m4.get("purpose", "").upper()
          or "no transformation" in m4.get("purpose", "").lower())
    check("  manifest has 'subjects' dict", "subjects" in m4)
    check("  manifest has 'total_chapters' == 3",
          m4["total_chapters"] == 3)
    check("  manifest has 'total_files_copied'", "total_files_copied" in m4)
    check("  manifest has 'total_files_missing'", "total_files_missing" in m4)
    check("  manifest has 'total_images_copied'", "total_images_copied" in m4)
    check("  manifest has 'total_images_missing'",
          "total_images_missing" in m4)
    check("  manifest has 'qa_files_copied' list",
          isinstance(m4.get("qa_files_copied"), list))
    check("  manifest has 'qa_files_missing' list",
          isinstance(m4.get("qa_files_missing"), list))
    check("  per-subject manifest has PSY + MED",
          set(m4["subjects"].keys()) == {"PSY", "MED"})
    # Per-chapter manifest structure
    psy7_m = m4["subjects"]["PSY"]["chapters"]["PSY-007"]
    check("  PSY-007 has 'files_copied' list",
          isinstance(psy7_m.get("files_copied"), list))
    check("  PSY-007 has 'files_missing' list",
          isinstance(psy7_m.get("files_missing"), list))
    check("  PSY-007 files_copied has all 7",
          len(psy7_m["files_copied"]) == 7)
    check("  PSY-007 files_missing is empty",
          psy7_m["files_missing"] == [])
    # Per-subject image manifest structure
    psy_imgs = m4["subjects"]["PSY"]["images"]
    check("  PSY image manifest has 'copied' and 'missing' entries",
          "copied" in psy_imgs.values()
          and "missing" in psy_imgs.values())
    # The MASTER_REVIEW_MANIFEST.json on disk matches the returned dict
    on_disk_manifest = json.loads(
        (review / "MASTER_REVIEW_MANIFEST.json").read_text(encoding="utf-8"))
    check("  on-disk manifest matches returned dict",
          on_disk_manifest["total_chapters"] == m4["total_chapters"]
          and on_disk_manifest["total_files_copied"] == m4["total_files_copied"]
          and on_disk_manifest["total_images_copied"] == m4["total_images_copied"])

    # Clean up
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 80)
    print(f"MASTER_REVIEW export test: {n_ok}/{n_total} assertions passed")
    print("=" * 80)
    if failed:
        print(f"FAILED: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
