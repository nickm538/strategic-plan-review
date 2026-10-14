# NY-603 Strategic Plan 2026–2030: Partner Review

An independent, line-by-line review of the *Long Island Continuum of Care: Strategic Plan for 2026–2030* (NY-603, Nassau and Suffolk Counties). It covers fact-checking against primary sources, data and logic checks, a feasibility read of all 68 action steps, and an environment scan current to **October 14, 2026**.

**Start here → [REVIEW.md](REVIEW.md)**. Section 0 is a one-page summary with ten ranked recommendations.

## Contents

| Path | What it is |
|---|---|
| [`REVIEW.md`](REVIEW.md) | The review: summary, CoC vs. lead-agency roles, errata for a v1.1, fact-check, data analysis, logic and feasibility, goal-by-goal notes, environment scan, missing elements, a 68→13 consolidation, KPIs, risks, a Year One close-out template, and questions for the team |
| [`appendix/A-plan-transcription.md`](appendix/A-plan-transcription.md) | Full text of the plan (the PDF is a scan), with an ID on every paragraph and step |
| [`appendix/B-line-by-line-audit.md`](appendix/B-line-by-line-audit.md) | Verdict on every narrative sentence, table and figure |
| [`appendix/C-action-step-register.md`](appendix/C-action-step-register.md) / [`.csv`](appendix/C-action-step-register.csv) | All 81 action-step lines coded by theme, control, funding need, measurability, overlaps and proposed initiative |
| [`appendix/D-draft-vs-final.md`](appendix/D-draft-vs-final.md) | What changed between the Aug 18, 2025 community-review draft and the final |
| [`appendix/E-sources.md`](appendix/E-sources.md) | Every source, primary vs. secondary, with links |
| [`analysis/checks.py`](analysis/checks.py) → [`checks-output.txt`](analysis/checks-output.txt) | Recomputes every number in the review |
| [`analysis/register.py`](analysis/register.py) | Source of truth for the register; regenerates Appendix C |
| [`analysis/draft_vs_final.py`](analysis/draft_vs_final.py) | Sentence-level draft/final comparison |

## Reproduce

Python 3.9+; standard library only.

```bash
python3 analysis/register.py                          # regenerates appendix/C-*
python3 analysis/checks.py > analysis/checks-output.txt
```

## Scope and limits

- Built from the published plan and public data only. Figures from NY-603's HMIS and LSA cannot be checked publicly; they are marked 🔒 and listed as questions for the data team.
- Legal and regulatory notes are informational, not legal advice.
- Federal CoC funding was in active litigation at the time of writing (REVIEW §9.1). Check for updates after HUD's award announcement, expected by December 1, 2026.
