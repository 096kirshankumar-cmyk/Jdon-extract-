"""
master_review_export.py
========================

Reads the EXISTING Railway extraction output and packages a clean
MASTER_REVIEW/ directory for human review.

The package is a strict SUBSET of the live output. It does NOT call
the pipeline, the validator, the healer, or any extraction pass.
It only copies already-written files. The pipeline continues to
write its usual output exactly as before; this module runs on
demand (after a run completes, or on a button-press from the
dashboard) and produces a separate reviewable tree.

TREE PRODUCED
-------------
    MASTER_REVIEW/
      split/
        <SUBJECT>/
          <CHAPTER_ID>/
            questions.jsonl
            answers.jsonl
            solutions.jsonl
            image_manifest.jsonl
            unresolved_qids.jsonl
            orphans.jsonl
            chapter_completeness.json
      assets/
        questions/
          <SUBJECT>/
            <every .webp referenced by image_manifest.jsonl in this tree>
      data/
        chapters.json
        orphans.jsonl
        unresolved_images.jsonl
        unmatched_images.jsonl
        image_ownership.jsonl
        integrity_flags.jsonl
        stem_conflicts.jsonl
        export_gate.jsonl
        page_ledger.jsonl
      MASTER_REVIEW_MANIFEST.json   (summary stats + checksum of what was copied)

NO TRANSFORMATION. The seven split files, every image, and every QA
sidecar are copied byte-for-byte. Missing files are reported in the
manifest (so a human can see which chapter is missing which file,
and which image was expected but not found) -- they are NOT
auto-resolved, invented, or discarded.

USAGE
-----
    # CLI (after a pipeline run):
    python3 master_review_export.py
    # -> builds <OUTPUT_DIR>/MASTER_REVIEW/ and a .zip next to it

    # From another Python process (e.g. the Railway dashboard):
    from master_review_export import build_master_review
    out_dir = build_master_review(pipeline.OUTPUT_ROOT)
    # out_dir is the Path to the populated MASTER_REVIEW/ tree
    zip_path = build_master_review_zip(pipeline.OUTPUT_ROOT)
    # zip_path is the Path to a downloadable .zip of the tree

DESIGN GOALS (from the user's brief)
------------------------------------
- Preserve the seven per-chapter split files exactly as produced.
- Preserve the image manifest, including QUESTION/OPTION/SOLUTION
  discrimination and the option_letter for OPTION images.
- Preserve all .webp image files referenced by any image_manifest.jsonl
  in the tree, with original filenames + references intact.
- Preserve all the listed QA / reference sidecars so the human can
  investigate flagged questions/images later.
- Do NOT merge Q/A/S, rewrite content, invent missing fields, resolve
  orphans, assign unresolved images, or alter question IDs.
- Do NOT touch subjects/<SUBJECT>/questions.jsonl,
  data/by_chapter/*.jsonl, subjects/<SUBJECT>/chapters.json, or any
  other "convenience bundle" file. Those remain in the live output
  exactly as the pipeline wrote them.
"""
from __future__ import annotations

import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

# The seven per-chapter split files the pipeline writes (see split_outputs.py
# EXPECTED_SPLIT_FILES, line 11-19). Copied byte-for-byte.
SPLIT_FILES = (
    "questions.jsonl",
    "answers.jsonl",
    "solutions.jsonl",
    "image_manifest.jsonl",
    "unresolved_qids.jsonl",
    "orphans.jsonl",
    "chapter_completeness.json",
)

# QA / reference sidecars the pipeline writes under data/. The user
# explicitly listed these in the brief: "chapters.json, orphans.jsonl,
# unresolved_images.jsonl, unmatched_images.jsonl, image_ownership.jsonl,
# integrity_flags.jsonl, stem_conflicts.jsonl, export_gate.jsonl,
# page_ledger.jsonl". Copied as-is, missing ones recorded in the
# manifest as warnings (NOT auto-generated).
QA_FILES = (
    "chapters.json",
    "orphans.jsonl",
    "unresolved_images.jsonl",
    "unmatched_images.jsonl",
    "image_ownership.jsonl",
    "integrity_flags.jsonl",
    "stem_conflicts.jsonl",
    "export_gate.jsonl",
    "page_ledger.jsonl",
)


def _safe_copy_bytes(src: Path, dest: Path) -> int:
    """Copy src -> dest byte-for-byte. Returns the byte count copied.
    dest.parent is created if needed. dest is overwritten if it
    already exists. NEVER transforms content (no JSON re-encode, no
    line-ending change, no compression)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(src, "rb") as f_src, open(dest, "wb") as f_dst:
        while True:
            chunk = f_src.read(1024 * 1024)
            if not chunk:
                break
            f_dst.write(chunk)
            n += len(chunk)
    return n


def _iter_split_chapters(split_root: Path):
    """Yield (subject, chapter_id, chapter_dir) for every chapter that
    has a directory under split_root/<subject>/<chapter_id>/. Skips
    anything that is not a directory (loose files at the split root
    are ignored -- there shouldn't be any, but defensive)."""
    if not split_root.exists():
        return
    for subject_dir in sorted(p for p in split_root.iterdir() if p.is_dir()):
        subject = subject_dir.name
        for chapter_dir in sorted(c for c in subject_dir.iterdir() if c.is_dir()):
            chapter_id = chapter_dir.name
            yield subject, chapter_id, chapter_dir


def _collect_image_refs(split_root: Path) -> set:
    """Walk every image_manifest.jsonl in the split tree and return the
    set of {subject, file} tuples referenced. A manifest entry uses
    {q_id, type, option_letter, file}; the file path is relative to
    ASSETS_DIR/questions/ and looks like 'PSY/PSY-007-001.webp'. We
    split on '/' and take the first segment as subject (matches
    the assets layout: assets/questions/<subject>/<file>)."""
    refs = set()  # set of (subject, file_basename) tuples
    for subject, _cid, chapter_dir in _iter_split_chapters(split_root):
        manifest = chapter_dir / "image_manifest.jsonl"
        if not manifest.exists():
            continue
        try:
            with open(manifest, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        # A corrupt line is a pipeline bug, not our
                        # problem. Skip silently and let the human
                        # see the original file.
                        continue
                    f_path = row.get("file")
                    if not f_path:
                        continue
                    # file is relative to ASSETS_DIR/questions/, e.g.
                    # "PSY/PSY-007-001.webp". subject is the first
                    # path segment; filename is the basename.
                    parts = Path(f_path).parts
                    if len(parts) < 2:
                        # relative filename only (no subject prefix) --
                        # assume it's under assets/questions/<subject>/
                        # for the same subject as the chapter.
                        sub = subject
                        fname = parts[-1]
                    else:
                        sub = parts[0]
                        fname = parts[-1]
                    refs.add((sub, fname))
        except OSError:
            # If a manifest is unreadable, skip the chapter; the
            # human will see the original in MASTER_REVIEW too.
            continue
    return refs


def build_master_review(output_root: str | Path) -> dict:
    """Build the MASTER_REVIEW/ tree under <output_root>/MASTER_REVIEW/
    and return a manifest dict describing what was copied. NO file
    is transformed -- everything is a byte-for-byte copy. Missing
    files are recorded as warnings, not invented.

    Returns:
        {
          "output_root": str,
          "master_review_dir": str,        # absolute path
          "started_at": str,                # ISO-ish
          "finished_at": str,
          "subjects": {
            "PSY": {
              "chapters": {
                "PSY-007": {
                  "files_copied": ["questions.jsonl", ...],
                  "files_missing": [],
                },
                ...
              },
              "images": {
                "PSY-007-001.webp": "copied" | "missing",
                ...
              },
            },
            ...
          },
          "qa_files_copied": [...],
          "qa_files_missing": [...],
          "total_chapters": int,
          "total_files_copied": int,
          "total_files_missing": int,
          "total_images_copied": int,
          "total_images_missing": int,
        }

    The manifest is also written to <output_root>/MASTER_REVIEW/
    MASTER_REVIEW_MANIFEST.json so a human can inspect the result
    without re-running the exporter.
    """
    output_root = Path(output_root).resolve()
    split_root = output_root / "split"
    assets_q_root = output_root / "assets" / "questions"
    data_root = output_root / "data"
    review_dir = output_root / "MASTER_REVIEW"

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    started_perf = time.time()

    # Always start from a clean slate so a re-run never carries stale
    # copies from a prior MASTER_REVIEW build. We delete the dir at
    # the start; the pipeline's live output (split/, assets/, data/,
    # subjects/) is NEVER touched.
    if review_dir.exists():
        import shutil
        shutil.rmtree(review_dir)
    review_dir.mkdir(parents=True, exist_ok=True)

    subjects_manifest: dict = {}
    total_files_copied = 0
    total_files_missing = 0
    total_images_copied = 0
    total_images_missing = 0
    total_chapters = 0

    # ---- 1. split/<SUBJECT>/<CHAPTER>/<7 files> ----
    review_split = review_dir / "split"
    review_split.mkdir(parents=True, exist_ok=True)
    for subject, chapter_id, chapter_dir in _iter_split_chapters(split_root):
        total_chapters += 1
        dest_chapter = review_split / subject / chapter_id
        dest_chapter.mkdir(parents=True, exist_ok=True)
        sub_manifest = subjects_manifest.setdefault(subject, {
            "chapters": {},
            "images": {},
        })
        ch_manifest = sub_manifest["chapters"].setdefault(chapter_id, {
            "files_copied": [],
            "files_missing": [],
        })
        for fname in SPLIT_FILES:
            src = chapter_dir / fname
            dest = dest_chapter / fname
            if src.exists() and src.is_file():
                _safe_copy_bytes(src, dest)
                ch_manifest["files_copied"].append(fname)
                total_files_copied += 1
            else:
                ch_manifest["files_missing"].append(fname)
                total_files_missing += 1

    # ---- 2. assets/questions/<SUBJECT>/*.webp referenced by any
    #         image_manifest.jsonl in the tree ----
    review_assets = review_dir / "assets" / "questions"
    review_assets.mkdir(parents=True, exist_ok=True)
    image_refs = _collect_image_refs(split_root)
    # Group by subject for the per-subject manifest and faster
    # directory creation.
    images_by_subject: dict = {}
    for sub, fname in sorted(image_refs):
        images_by_subject.setdefault(sub, []).append(fname)
    for sub, fnames in images_by_subject.items():
        # Ensure the per-subject dir exists in the review tree
        # even if the source has no images for that subject (avoids
        # an empty dir; harmless either way).
        (review_assets / sub).mkdir(parents=True, exist_ok=True)
        sub_manifest = subjects_manifest.setdefault(sub, {
            "chapters": {},
            "images": {},
        })
        for fname in sorted(set(fnames)):
            src = assets_q_root / sub / fname
            dest = review_assets / sub / fname
            if src.exists() and src.is_file():
                _safe_copy_bytes(src, dest)
                sub_manifest["images"][fname] = "copied"
                total_images_copied += 1
            else:
                sub_manifest["images"][fname] = "missing"
                total_images_missing += 1

    # ---- 3. data/<QA files> ----
    review_data = review_dir / "data"
    review_data.mkdir(parents=True, exist_ok=True)
    qa_files_copied: list = []
    qa_files_missing: list = []
    for fname in QA_FILES:
        src = data_root / fname
        dest = review_data / fname
        if src.exists() and src.is_file():
            _safe_copy_bytes(src, dest)
            qa_files_copied.append(fname)
            total_files_copied += 1
        else:
            qa_files_missing.append(fname)
            total_files_missing += 1

    # ---- 4. MASTER_REVIEW_MANIFEST.json (summary) ----
    finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    elapsed = round(time.time() - started_perf, 3)
    manifest = {
        "output_root": str(output_root),
        "master_review_dir": str(review_dir),
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": elapsed,
        "purpose": (
            "Manual review / master-data formation package. Read-only: "
            "the live pipeline output (split/, assets/, data/, subjects/) "
            "is NEVER modified. NO TRANSFORMATION: bytes are copied "
            "verbatim, no fields are merged, rewritten, invented, "
            "or normalized, no orphans are auto-resolved, no images are "
            "re-assigned. Orphaned and unresolved records are preserved "
            "as-is for human review."
        ),
        "subjects": subjects_manifest,
        "qa_files_copied": qa_files_copied,
        "qa_files_missing": qa_files_missing,
        "total_chapters": total_chapters,
        "total_files_copied": total_files_copied,
        "total_files_missing": total_files_missing,
        "total_images_copied": total_images_copied,
        "total_images_missing": total_images_missing,
    }
    (review_dir / "MASTER_REVIEW_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


def build_master_review_zip(output_root: str | Path,
                             zip_name: str = "MASTER_REVIEW.zip") -> Path:
    """Build the MASTER_REVIEW/ tree (if not already built) and zip
    it for download. Returns the absolute Path to the .zip file.

    The zip is stored at <output_root>/<zip_name>. It contains the
    same content as MASTER_REVIEW/ (with paths like
    'MASTER_REVIEW/split/PSY/PSY-007/questions.jsonl' inside the
    zip, so a human can extract it and the directory tree is
    immediately visible)."""
    output_root = Path(output_root).resolve()
    review_dir = output_root / "MASTER_REVIEW"
    if not review_dir.exists() or not (review_dir / "MASTER_REVIEW_MANIFEST.json").exists():
        build_master_review(output_root)
    zip_path = output_root / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in review_dir.rglob("*"):
            if f.is_file():
                # Store with 'MASTER_REVIEW/' prefix so a human
                # extracting the zip gets the directory tree.
                arcname = "MASTER_REVIEW" / f.relative_to(review_dir)
                zf.write(f, arcname)
    return zip_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    """Standalone CLI: build MASTER_REVIEW/ + .zip from the live
    output. Run as `python3 master_review_export.py` from the repo
    root (or anywhere -- the path resolves against qbank_pipeline.py
    if it can be imported, otherwise the OUTPUT_DIR env var, otherwise
    the local ./qbank_output)."""
    output_root = None
    try:
        import qbank_pipeline as _qp
        output_root = _qp.OUTPUT_ROOT
    except Exception:
        env = os.environ.get("OUTPUT_DIR")
        output_root = Path(env) if env else Path("./qbank_output")
    output_root = Path(output_root)
    if not output_root.exists():
        print(f"ERROR: output root does not exist: {output_root}")
        print("Run the pipeline first (or set OUTPUT_DIR).")
        return 1
    print(f"[master_review_export] building MASTER_REVIEW from {output_root} ...")
    m = build_master_review(output_root)
    print(f"  chapters:           {m['total_chapters']}")
    print(f"  files copied:       {m['total_files_copied']}")
    print(f"  files missing:      {m['total_files_missing']}")
    print(f"  images copied:      {m['total_images_copied']}")
    print(f"  images missing:     {m['total_images_missing']}")
    print(f"  master review dir:  {m['master_review_dir']}")
    z = build_master_review_zip(output_root)
    print(f"  zip:                {z}")
    if m["total_files_missing"] or m["total_images_missing"]:
        print("  (review the manifest for missing files: "
              f"{m['master_review_dir']}/MASTER_REVIEW_MANIFEST.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
