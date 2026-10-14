"""Sentence-level comparison of the Aug 18, 2025 community-review draft and the
published plan (Appendix A transcription). Prints sentences that were added,
removed, or changed, with a similarity score, so no wording change is missed.

Usage: python3 analysis/draft_vs_final.py <draft.txt> appendix/A-plan-transcription.md
"""
import re
import sys
from difflib import SequenceMatcher


def sentences(text):
    # Drop transcriber notes, markdown markup, IDs and table pipes.
    text = re.sub(r"\*\[[^\]]*\]\*", " ", text)
    text = re.sub(r"\*\*(P\d+\.\d+|G\d-Y(1|25)-[0-9A-Za-z]+)[^*]*\*\*", " ", text)
    text = re.sub(r"[#*|✓○>`]", " ", text)
    text = text.replace("–", "-").replace("’", "'").replace("“", '"').replace("”", '"')
    out = []
    for line in text.splitlines():
        line = line.strip(" -\t")
        if len(line) < 25:
            continue
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            s = re.sub(r"\s+", " ", s).strip(" .")
            if len(s) >= 25:
                out.append(s)
    return out


def norm(s):
    return re.sub(r"[^a-z0-9%$]+", " ", s.lower()).strip()


def best(s, pool):
    scores = [(SequenceMatcher(None, norm(s), norm(p)).ratio(), p) for p in pool]
    return max(scores) if scores else (0.0, "")


def main(draft_path, final_path):
    draft = sentences(open(draft_path, encoding="utf-8").read())
    final_text = open(final_path, encoding="utf-8").read()
    final_text = final_text.split("## Cover", 1)[1]  # skip transcription preamble
    final = sentences(final_text)

    print("== REMOVED or heavily changed (draft sentence, best final match < 0.60) ==")
    for s in draft:
        r, m = best(s, final)
        if r < 0.60:
            print(f"[{r:.2f}] DRAFT: {s}\n       FINAL≈ {m}\n")

    print("== ADDED or heavily changed (final sentence, best draft match < 0.60) ==")
    for s in final:
        r, m = best(s, draft)
        if r < 0.60:
            print(f"[{r:.2f}] FINAL: {s}\n       DRAFT≈ {m}\n")

    print("== EDITED (0.60 <= match < 0.97) ==")
    for s in final:
        r, m = best(s, draft)
        if 0.60 <= r < 0.97:
            print(f"[{r:.2f}] FINAL: {s}\n       DRAFT: {m}\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
