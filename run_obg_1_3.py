#!/usr/bin/env python3
"""Live OBG chapters 1-3. Does not print secrets."""
import os
import sys
from pathlib import Path

KEY = Path("/home/user/.gkey").read_text().strip()
os.environ["GEMINI_API_KEY"] = KEY
os.environ.setdefault("OUTPUT_DIR", "/home/user/obg_ch1_3_run")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gemini_keys
import qbank_pipeline as qp
import google.generativeai as genai
import boundary_phased as bph

PDF = "/home/user/book2/book.pdf"
SUBJECT = "OBG"
PAGE_OFFSET = 0

def main():
    qp.DATA_DIR.mkdir(parents=True, exist_ok=True)
    qp.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    state = qp.load_state()
    gemini_keys.init(state, qp.MAX_CALLS_PER_DAY)
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = gemini_keys.track(genai.GenerativeModel(qp.GEMINI_MODEL))
    qp.reset_daily_counter_if_needed(state)
    wm = qp.find_watermark_object_ids(PDF)
    print(f"[RUN] watermark ids: {sorted(wm)[:8]}... n={len(wm)}")
    print(f"[RUN] model={qp.GEMINI_MODEL} out={qp.OUTPUT_ROOT}")
    results = []
    for ch_no, start, end in ((1, 5, 31), (2, 32, 47), (3, 48, 65)):
        print(f"\n========== OBG-{ch_no:03d} pages {start}-{end} ==========", flush=True)
        runner = bph.ChapterRunner(
            PDF, SUBJECT, ch_no, qp.OUTPUT_ROOT, model=model,
            page_offset=PAGE_OFFSET, state=state)
        runner.watermark_ids = wm
        try:
            res = runner.run(start, end)
        except bph.QuotaPaused:
            print("[RUN] quota paused", flush=True)
            qp.save_state(state)
            results.append({"chapter": ch_no, "error": "quota"})
            break
        except Exception as e:
            print(f"[RUN] chapter {ch_no} ERROR: {type(e).__name__}: {e}", flush=True)
            results.append({"chapter": ch_no, "error": str(e)[:400]})
            qp.save_state(state)
            continue
        results.append(res)
        qp.save_state(state)
        print(f"[RUN] ch{ch_no} done locked={res.get('locked')} "
              f"Q={res.get('questions')} A={res.get('answers')} "
              f"S={res.get('solutions')} notes={len(res.get('notes') or [])}",
              flush=True)
    print("\n========== SUMMARY ==========")
    for r in results:
        print(r if isinstance(r, dict) else r)
    print(f"calls_today={state.get('calls_today')}")

if __name__ == "__main__":
    main()
