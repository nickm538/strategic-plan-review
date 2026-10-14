# Partner Review: Long Island CoC Strategic Plan 2026–2030 (NY-603)

**What this is.** An independent, line-by-line review of the published *Long Island Continuum of Care: Strategic Plan for 2026–2030*. It is written as a partner review for the CoC team and the Governing Board, and is meant to feed a short errata (v1.1), Year One close-out, and the first annual update.
**As of:** October 14, 2026. Federal and state facts are current to that date and cited in [Appendix E](appendix/E-sources.md).
**Reproducibility:** every number in this review is recomputed by [`analysis/checks.py`](analysis/checks.py) (output: [`analysis/checks-output.txt`](analysis/checks-output.txt)).

| Appendix | Contents |
|---|---|
| [A](appendix/A-plan-transcription.md) | Full transcription of the plan, with an ID on every paragraph and action step |
| [B](appendix/B-line-by-line-audit.md) | Line-by-line audit of every narrative sentence and figure |
| [C](appendix/C-action-step-register.md) | Register of all 68 action steps and 13 sub-bullets, coded and cross-referenced ([CSV](appendix/C-action-step-register.csv)) |
| [D](appendix/D-draft-vs-final.md) | What changed between the August 18, 2025 community-review draft and the final |
| [E](appendix/E-sources.md) | Sources, with links and access dates |

---

## 0. Bottom line

1. **The strategy fits Long Island.** The six goals cover the right ground. Unsheltered homelessness, coordination, shelter, income, health and funding are where NY-603's data point. The plan also listens to people with lived experience and front-line staff.
2. **As written, the plan is not yet accountable.** It promises "measurable outcomes" (p. 1), but only 3 of 68 action steps carry a number or a yes/no standard. None of the six goals has a baseline or target, and no step has an owner or a due date. The document itself has no adoption date or version number.
3. **The core data hold up.** The 2025 Point-in-Time (PIT) table reconciles in every row and column. Its totals are consistent with the State Comptroller's figures (NY-603 up 13.4% from 2024; children 32.9% of the count). The flow figures are consistent with each other (section 5.2).
4. **A v1.1 errata needs ten factual corrections and a dozen clarity fixes (section 3).** Several errors are visible to any reader who checks: the agency name "Office of Temporary Disability Assistance," "almost 7 months" for 251 days, a HUD Fair Market Rent presented as a median and an average, an untraceable SSI figure, and a renter statistic presented as "55% of Long Islanders." Every data error in the final was already in the August 2025 draft. Community review did not catch them, and the agency-name error was introduced during finalization ([Appendix D](appendix/D-draft-vs-final.md)).
5. **The environment changed after the plan was drafted.** Since then:
   - HUD's FY2026 CoC competition set guaranteed renewal funding (Tier 1) at 60% of each CoC's renewal demand.
   - It reserved up to $1.3 billion of Tier 2 for *new* transitional housing and services-only projects. A national analysis judged that renewals ranked in Tier 2 are unlikely to be funded.
   - The competition has been through a vacatur and a stay. Awards are due by December 1, 2026.

   Alongside that came a proposed HUD rule on immigration-status verification, federal limits on harm reduction, SNAP work-rule changes, upcoming Medicaid work requirements, and a pending court challenge to New York's source-of-income law. Goal 6 has no step that addresses losing federal renewals, which is the biggest near-term threat to the housing the CoC already funds.
6. **Biggest structural fix:** consolidate the 68 steps into about 13 initiatives (section 11). Give each one an owner, a Year One deliverable and one KPI tied to HUD's System Performance Measures (section 12). Several steps repeat across goals; the shelter-fee/savings idea alone appears in 10 lines across three goals.

**Ten recommendations, in priority order**

| # | Recommendation | Why | Section |
|---|---|---|---|
| 1 | Issue a v1.1 errata with an adoption date, a version number and the corrections in section 3 | The errors are checkable from public data and undercut credibility with funders and legislators | 3 |
| 2 | Add a one-page measurement table: baseline, target and data source for each goal, mapped to HUD SPMs | Turns "measurable outcomes" into something the Board can track | 12 |
| 3 | Add a Goal 6 federal-funding contingency: renewal ranking strategy, bridge funding, county/state backfill asks | Up to 40% of renewal demand now sits outside guaranteed Tier 1 | 9.1 |
| 4 | Consolidate the 68 steps into ~13 initiatives, each with an owner (Board committee or staff role) and a lead partner | Removes 8 duplicate clusters and makes Year Two planning possible | 6.1, 11 |
| 5 | Close out Year One: status-check the 23 steps (78 days remain in 2026) and carry unfinished ones forward explicitly | The plan defines Year One as calendar 2026 | 7 |
| 6 | Move the older-adult (55+) strategy into Year One/Two | 55+ grew about 23% in a year versus 13% overall; the plan defers it to Years 2–5 | 5.1, 8 |
| 7 | Add a large-family step | Families with 3+ children stay longest (403 days), yet no step targets them | 10 |
| 8 | Add steps for Coordinated Entry, street outreach, DV survivors, youth and veterans | Required CoC functions and named populations have no action steps; "veteran" does not appear in the plan | 10 |
| 9 | Rewrite the shelter-fee steps around the actual rules (18 NYCRR 491.11/900.18, Part 352 budgeting) and cite where "80%" comes from | Clarifies who must change what. Even the plan's 50% goal is above the 30%-of-income rent share used in local housing programs, which limits move-out savings. | 6.3, 8 |
| 10 | Define the two weakest data points: the 472 unsheltered adults and the 261-day shelter stay | One implies an average unsheltered spell of about 4 years; the other conflicts with the 251-day figure | 5.2 |

---

## 1. The CoC and its lead agency: who is who

The plan is a **CoC** document, written with staff support from the CoC's **lead agency**, the Long Island Coalition for the Homeless (LICH). The two are easy to blur, and the distinction matters for reading the plan.

- **The CoC is the system, not an organization with staff.** Federal rule defines a Continuum of Care as "the group organized to carry out the responsibilities required under this part," composed of representatives of providers, governments, PHAs, schools, hospitals, law enforcement, people with lived experience and others ([24 CFR 578.3](appendix/E-sources.md#s10)). The plan describes NY-603 the same way: "a local planning body" guided by a Governing Board and committees (P1.3).
- **LICH is the agency the CoC designated to run it.** The rule defines the *Collaborative Applicant* as "the eligible applicant that has been designated by the Continuum of Care to apply for a grant... on behalf of the Continuum" and the *HMIS Lead* as "the entity designated by the Continuum of Care... to operate the Continuum's HMIS on its behalf" (578.3). The designation belongs to the CoC; HUD recognizes it through registration. The plan shows this split in Figure F1 (p. 3): the Governing Board decided, and the "Planning Team" was LICH staff with TAC consultants.
- **Two refinements to common shorthand:**
  - The CoC is not a liaison *to* the agencies; it *is* the agencies, acting together. The convening work (funding rounds, PIT, plans, conferences) is the lead agency doing its job on the CoC's behalf.
  - TAC (Technical Assistance Collaborative) is a national nonprofit based in **Boston**, not a New York State entity ([S31](appendix/E-sources.md#s31)). The plan describes it accurately as serving "our nation's" systems (P3.1).
- **Where the plan could make the line clearer.** The plan never states LICH's formal roles (Collaborative Applicant, HMIS Lead, Coordinated Entry lead) or which Board committee owns which goal. Because LICH also runs programs that appear in the plan (for example street medicine, G5-Y25-08), a short "roles and governance" box would make the CoC/LICH line visible. It would also show readers that funding decisions touching LICH's own programs go through the CoC's conflict-of-interest process.

---

## 2. How this review was done

1. **Read twice, transcribed once.** The PDF is a scan with no text layer, so it was transcribed from 200-dpi renders and proofread ([Appendix A](appendix/A-plan-transcription.md)). Every paragraph and step has a stable ID (for example `P1.5`, `G3-Y25-01a`).
2. **Compared to the August 18, 2025 community-review draft** with a sentence-level diff ([Appendix D](appendix/D-draft-vs-final.md); script: [`analysis/draft_vs_final.py`](analysis/draft_vs_final.py)).
3. **Audited every sentence and figure** ([Appendix B](appendix/B-line-by-line-audit.md)). Each was classed as verified, error, needs clarification, or internal (HMIS/LSA figures that cannot be checked publicly).
4. **Fact-checked against primary sources:** HUD Fair Market Rent tables, OTDA SSI charts, SSA county tables, Census ACS 2024, the State Comptroller, eCFR, NYCRR, OTDA directives, court records, and HUD/NAEH/CRS material on the CoC competition ([Appendix E](appendix/E-sources.md)).
5. **Recomputed all math and ran consistency tests** (Little's Law flow checks, subgroup comparisons, affordability ratios) in a script anyone can rerun.
6. **Coded all 81 action-step lines** for theme, who controls the step, funding need and measurability, then cross-referenced them for duplicates ([Appendix C](appendix/C-action-step-register.md)).

**Limits.** No access to NY-603's HMIS, LSA or internal documents. Figures from those sources are marked *internal* and listed for the data team in section 15. The 2026 PIT count (held January 27, 2026) had no public totals as of this review. A few environment items rest on secondary reporting and are labeled as such.

---

## 3. Errata for a v1.1

### 3.1 Factual corrections

| # | Where | Plan says | Finding | Suggested fix |
|---|---|---|---|---|
| E1 | p. 1, P1.5 | 251 days is "almost 7 months" | 251 ÷ 30.44 = **8.2 months**. Seven months is about 213 days. | "about 8 months" |
| E2 | p. 7, G3-Y1-01 | "Office of Temporary Disability Assistance" | The agency is the **Office of Temporary and Disability Assistance**. The draft said only "OTDA"; the error came in when the name was spelled out. | Correct the name and the hyperlink text |
| E3 | p. 8, P8.1 | USHousingData.com "lists the median rent for a 2-bedroom unit on Long Island as $2,586" | $2,586 is exactly HUD's **FY2025 two-bedroom Fair Market Rent** for the Nassau-Suffolk FMR area. An FMR is a 40th-percentile gross rent for standard-quality units, not a median. The current FY2027 figure (effective Oct 1, 2026) is **$2,689**. | "HUD's FY2027 Fair Market Rent for a two-bedroom unit in Nassau-Suffolk is $2,689 a month," citing HUD directly |
| E4 | p. 9, P9.1 | "the average rent on Long Island is currently $2,586" | Same FMR figure, now described as an "average," and out of date | Use the FMR wording from E3. For an SSI comparison, use the studio or 1-bedroom FMR (FY2027: $1,964 / $2,285) |
| E5 | p. 9, P9.1 | "The average SSI payment in 2024 was $1,009 per month" | Could not be matched to any published figure. The 2024 SSI maximum for a New Yorker living alone was **$1,030** ($943 federal + $87 state). In 2026 it is **$1,081** ($994 + $87). SSA announced a 3.6% COLA for 2027 on Oct 14, 2026. | Cite the OTDA chart: "In 2026 the maximum SSI benefit for a New Yorker living alone is $1,081 a month" |
| E6 | p. 8, P8.1 | "55% of Long Islanders are cost-burdened" | ACS 2024: **56.2% of Long Island renter households** pay 30%+ of income for housing (Nassau 52.5%, Suffolk 59.8%; 29.4% pay 50%+). Across all households (renters and owners) the share is **37.1%**. | "More than half of Long Island renter households (56%) are cost-burdened, and nearly 3 in 10 pay half their income or more" |
| E7 | p. 8, G4-Y1-01 | "assist people with criminal records with expungement when possible" | New York mostly **seals** records (Clean Slate Act, automatic by Nov 16, 2027; CPL 160.59 on petition). Expungement applies to certain marijuana convictions under the MRTA. | "record sealing (including Clean Slate) and, where eligible, marijuana expungement" |
| E8 | p. 5, G1-Y1-01a | "housing advocate groups like the Long Island Housing Coalition" | No organization by that exact name could be verified. The draft named ERASE Racism. Candidates include Long Island Housing Services (fair housing), Long Island Housing Partnership, and the Housing for All Long Island coalition. | Name the intended organization |
| E9 | p. 11, G6-Y25-06 | RRH model "to provide a more gradual decrease in rent payments" | Inverted. In rapid re-housing the *subsidy* steps down and the tenant's share rises. | "a more gradual step-down in rental assistance" |
| E10 | p. 6, G2-Y25-02a | "the Regional Plan Association's Zoning Atlas" | The Long Island Zoning Atlas (launched December 2023) is a multi-partner project within the New York Zoning Atlas. RPA is one partner. | "the Long Island Zoning Atlas (part of the New York Zoning Atlas)" |

### 3.2 Clarity and consistency fixes

| # | Where | Issue | Suggested fix |
|---|---|---|---|
| C1 | p. 1, P1.5 | Introduces three groups whose challenges "prolonged their homelessness," but two of them stay *less* than the 251-day average: single adults 55+ (246 days) and households with a disabled member (219). Only families with 3+ children (403) stay longer. | Present the 55+ and disability figures as *share-of-caseload* findings, not as prolonged stays |
| C2 | p. 2 vs p. 7 | "25% of **families** exiting shelter" (P2.2) vs "25% of these **households**" (P7.1) | Use one unit and name the LSA population and period |
| C3 | p. 1 vs p. 7 | Average episode **251** days (P1.5) vs shelter stay **261** days (P7.1) | Confirm whether 261 is a distinct measure. If both are right, non-shelter users averaged about 136 days. |
| C4 | p. 2, P2.3 | "Just over 20%" of unsheltered people were 55–64 | 74 of 320 = **23.1%**. Adding people over 64, **30.3%** of the unsheltered count is 55+. |
| C5 | p. 2, P2.3 | "not enough units... **As a result**, 472 adults experienced unsheltered homelessness" | Causal claim conflicts with G1-Y1-01, which asks why people decline offered shelter. Soften to "Partly as a result" and define the 472 (see 5.2). |
| C6 | p. 2, T1 | PIT table has no title, date or source | "NY-603 Point-in-Time Count, January 2025 (HUD PIT/HIC)" |
| C7 | p. 8, G3-Y25-07 | "communication between **housing authorities** and shelter workers," but the sub-bullet is about **DSS** | Name the intended partner(s) |
| C8 | p. 10, G5-Y1-E | "SSI/SSDI, Outreach, Access and Recovery" | Official name: "SSI/SSDI Outreach, Access, and Recovery (SOAR)" |
| C9 | p. 3 vs p. 12 | "focus groups, interviews, and surveys" vs "listening sessions" | Use one term |
| C10 | p. 1, P1.1 | Plan "prioritizes time-limited and actionable interventions," but no step has a time limit beyond "Year One" or "Years 2–5" | Add dates or drop "time-limited" |
| C11 | Layout | Large blank space at the bottom of p. 2; G4-Y1-01 indented under the Education icon; G1's heading alone says "ADDITIONAL"; three steps lack closing periods; G4-Y25-05 starts "Increasing" | Typesetting pass |
| C12 | Cover | No date, version, adoption date or approving body | "Adopted by the NY-603 Governing Board on [date]. Version 1.1, [date]." |

---

## 4. Fact-check summary

Full detail is in [Appendix B](appendix/B-line-by-line-audit.md). Status key: ✅ verified · ❌ error · ⚠️ partly right / needs wording · 🔒 internal HMIS/LSA figure (not publicly checkable) · ❓ could not verify.

| Claim | Page | Status | Evidence |
|---|---|---|---|
| CoC is a "local planning body" for Nassau and Suffolk | 1 | ✅ | 24 CFR 578.3 |
| 2025 PIT: 4,540 persons, 2,678 households, 320 unsheltered | 2 | ✅ | Table reconciles; Comptroller: +13.4% vs 2024, 32.9% children; press: highest since tracking began in 2007 |
| 55+ in PIT (572 + 253 = 825) | 2 | ✅ | Press coverage of LICH's release: "just over 670" (2024) → 825 (2025) |
| 5,447 households assisted in FY2024 | 1 | 🔒 | Consistent with flow math (5.2) |
| 251 / 403 / 246 / 219 days; 8% / 23% / 40% | 1–2 | 🔒 | Internal LSA; see C1 |
| 92% used shelter; 25% exit to PH; 7% returned after ~140 days | 2, 7 | 🔒 | Internal; see C2, C3 |
| 472 adults unsheltered Apr 2023–Apr 2025 | 2, 4 | 🔒⚠️ | Hard to square with 319 unsheltered adults on one night; see 5.2 |
| $2,586 "median" / "average" rent | 8, 9 | ⚠️ | Value = HUD FY2025 2BR FMR; label wrong; now $2,689 |
| 55% of Long Islanders cost-burdened | 8 | ⚠️ | True of renters (56%), not all households (37%) |
| Average SSI 2024 = $1,009 | 9 | ❓❌ | NY max 2024 = $1,030; no source found for $1,009 |
| "Office of Temporary Disability Assistance" | 7 | ❌ | Office of Temporary **and** Disability Assistance |
| OTDA regulations "require shelter guest payment percentages" | 7 | ⚠️ | 18 NYCRR 491.11 (adult shelters) and 900.18 (family shelters) require residents with income to pay toward the actual cost of care, budgeted under Part 352. They do not state a percentage. |
| Guest payment is "80% of income" | 7 | ❓ | Not cited; likely a local DSS practice. Confirm the source. |
| Expungement for people with records | 8 | ⚠️ | Mostly sealing in NY; MRTA expungement for certain marijuana convictions |
| Existing statutes prohibit source-of-income discrimination | 9 | ✅⚠️ | NY Human Rights Law covers lawful source of income. A March 2026 Appellate Division ruling held it unconstitutional as applied to Section 8; the case is on appeal (9.2). |
| "Long Island Housing Coalition" | 5 | ❓ | Not found |
| RPA's Zoning Atlas | 6 | ⚠️ | Long Island Zoning Atlas is multi-partner |
| TAC description | 3 | ✅ | Boston-based national nonprofit |
| SOAR, FQHCs, HCV through PHAs | 5, 10 | ✅ | Standard program names (SOAR punctuation, C8) |
| 12 listening sessions (3 + 2 + 7); 7 surveys; 102 responses | 12 | ✅ | Arithmetic checks; the draft's per-survey counts sum to 102 |

---

## 5. Data analysis

### 5.1 What the 2025 PIT table actually shows

All 9 rows and 5 columns reconcile. Beyond the plan's own reading:

- **Two different populations.** Sheltered people (4,220) are 35.4% children, with 1.81 people per household in emergency shelter. Unsheltered people (320) are 99.7% adults, all single-person households (320 households = 320 people), and 76.6% are aged 35–64.
- **Older adults are the fastest-growing group.** 825 people were 55+ (18.2% of the count). If press reports of "just over 670" in 2024 are right, that is about **+23% in one year versus +13.4% overall**. Among unsheltered people, **30.3% are 55+** (97 of 320), and 23.1% are 55–64.
- **One unaccompanied minor** was counted unsheltered. That warrants a youth-specific protocol (section 10).
- **Implied 2024 total:** 4,540 ÷ 1.134 ≈ **4,004** persons.

### 5.2 Do the flow numbers hang together?

**Little's Law** says: people in the system on a given night = rate of entry × average stay. It is a simple way to check whether a count, a length of stay and an annual total are consistent. An analogy: a parking garage with 2,358 cars on an average night, where cars stay about 251 days, must be taking in about 9 new cars a day.

| Check | Result | Reading |
|---|---|---|
| Sheltered households on PIT night (2,358) ÷ 251-day stay | ≈ 9.4 entries/day ≈ 3,429/year | — |
| Implied households served in a year (stock + entries) | ≈ 5,787 at 2025 level; ≈ 5,103 at implied 2024 level | The plan's **5,447** sits between: **consistent** |
| Unsheltered: 319 adults on one night vs 472 unique adults over 24 months | Only ~153 new entries in two years, implying an average unsheltered spell of **~4.2 years** | **Not plausible as two complete counts of the same group.** The 472 most likely counts only adults recorded in HMIS (for example, outreach-enrolled). Define it. |
| 251-day average episode vs 261-day average shelter stay | If both are right, the 8% who never used shelter averaged ~136 days | Possible, but confirm (C3) |

**Scenario: why length of stay is the system's biggest lever.** Holding the 2025 entry rate constant, cutting the average stay from 251 to 200 days would cut sheltered households on a given night by about **20%** (2,358 → ~1,879). Cutting to 180 days would cut them about **28%**. This is an illustration, not a forecast, but it shows why HUD SPM 1 (length of time homeless) belongs at the center of Goal 3.

### 5.3 Affordability, in current numbers

| Measure | Value | Source |
|---|---|---|
| 2BR FMR, Nassau-Suffolk | FY2025 $2,586 → FY2026 $2,747 (+6.2%) → **FY2027 $2,689** (−2.1%) | HUD |
| Studio / 1BR FMR, FY2027 | $1,964 / $2,285 | HUD |
| SSI, NY individual living alone, 2026 | $1,081/month; 30% of that = **$324** for rent | OTDA |
| 1BR FMR as a share of SSI | **211%** (FY2027) | computed |
| LI minimum wage, 2026 | $17.00/hr → $35,360/yr full-time → $884/month affordable rent | NY DOL (via press) |
| Income needed for a 2BR at FMR | **$107,560/yr ($51.71/hr)**, or ~**122 hours/week** at minimum wage | computed |
| Renter cost burden, ACS 2024 | 56.2% (Nassau 52.5%, Suffolk 59.8%); severe (50%+) 29.4% | Census |
| All-household cost burden | 37.1% | Census |
| SSI recipients, Dec 2024 | Nassau 15,132; Suffolk 17,772 | SSA |

For Goal 5, a single SSI recipient is better compared with a studio or 1-bedroom than with the 2-bedroom the plan uses. Even then, the 1-bedroom FMR is more than twice the entire SSI check.

### 5.4 Who the community input came from

The final plan dropped the per-survey response counts that the August 2025 draft showed. They matter for interpreting the plan:

| Survey audience | Responses | Share |
|---|---:|---:|
| Librarians and library staff | 40 | 39.2% |
| Homeless response system partners | 33 | 32.4% |
| Healthcare partners | 16 | 15.7% |
| McKinney-Vento liaisons / education | 9 | 8.8% |
| Local elected officials | 2 | 2.0% |
| Philanthropic partners | 1 | 1.0% |
| Local law enforcement | 1 | 1.0% |
| **Total** | **102** | |

- Library staff were the single largest group of respondents, which likely explains why libraries appear in four action steps (and were added to three of them after the draft).
- Elected officials, philanthropy and law enforcement together gave **4 responses**. Yet Goal 2 depends on legislators, Goal 6 on private funders, and Goal 1 touches law enforcement.
- No survey targeted landlords, PHAs, employers or DSS staff, though Goals 3, 4 and 6 depend on them.
- Recommendation: restore the counts in the appendix, and run short targeted consultations with the missing groups before the Year Two update.

---

## 6. Logic and internal consistency

### 6.1 Duplicate and overlapping steps

| Cluster | Rows | Note |
|---|---|---|
| Discharge planning guide | G2-Y1-H, G5-Y1-02 (+02a) | Same guide, two goals, both Year One |
| System map | G1-Y1-02 (+02a), G5-Y1-01 | Two maps; build one with a health layer |
| Income grace period in shelter | G3-Y1-02, G4-Y1-02 | Same step, both Year One |
| Shelter fees → savings pilot | G1-Y25-02, G3-Y25-01b, G4-Y25-07 | Three pilots of one idea |
| Lower shelter fees pilot | G1-Y25-04, G3-Y25-01a | Same pilot |
| Low-barrier crisis housing | G1-Y1-01, G3-Y25-08 | Same populations (pets, behavioral health, non-traditional families) |
| Prevention funding | G1-Y25-01, G6-Y25-02 | — |
| Older adults 55+ | G5-Y25-01, G6-Y25-03 | Working group and strategy for the same population |
| *Overlap:* standards | G1-Y1-01, G3-Y1-H, G5-Y25-02 | Three "standards" items; fold into the CoC written standards |
| *Overlap:* peers | G3-Y25-05, G5-Y25-06 | Peer mentors and peer support program |
| *Overlap:* training | G3-Y1-H, G5-Y25-07 | Case-manager training and self-paced training |

The shelter-fee/savings theme appears in **10 lines across Goals 1, 3 and 4**. Communications campaigns appear in **7 lines across 5 goals**.

### 6.2 Steps that sit under the wrong goal or label

- **G4-Y25-06** (eviction diversion) is a prevention step placed under the employment goal.
- **G1-Y1-H** (quantify Long Island's affordable-housing need) is a region-wide study placed under the unsheltered goal. It also overlaps the annual gaps analysis the CoC is already required to do ([24 CFR 578.7(c)(3)](appendix/E-sources.md#s11)).
- **Goal 6 is "funding,"** but four of its seven Years 2–5 steps are programmatic (housing stock, landlord practices, RRH design, rental-assistance rules).
- **"Housing" labels that aren't housing:** G3-Y1-H is staff training; G4-Y1-H is workforce development. The plan says each goal has "both a housing and an awareness/education action step" (P4.3). The labels are present, but the housing theme is thin in Goals 3 and 4.

### 6.3 Tensions inside the plan

- **Grace periods vs. shorter stays.** G3-Y1-02 and G4-Y1-02 lengthen shelter stays after income starts, while Goal 2 and HUD SPM 1 aim to shorten them. Tie any grace period to a written move-out plan and a cap, and track it separately.
- **"Not enough units" vs. declined shelter.** P2.3 attributes unsheltered homelessness to a shortage of units. G1-Y1-01 recognizes that people decline offered shelter, and New York's Code Blue rule requires shelter access in freezing weather. Both causes are real; the narrative should say so.
- **"Measurable outcomes" vs. how the steps are written.** Of 68 steps, 3 have a target (G3-Y25-01, G4-Y25-02, G5-Y1-E), 35 produce a deliverable, and 30 are directional ("increase," "expand," "improve").
- **Shelter-fee ambition.** The plan aims to cut emergency-shelter guest payments from 80% to 50% of income. Local *housing* programs use 30% of income as the tenant's rent share:
  - Suffolk DSS's Rental Supplement Program, for shelter residents moving to apartments, asks households to pay 30% of income toward rent.
  - State law lets districts cap Temporary Assistance recipients' housing-cost contribution at 30% of income for people with HIV/AIDS (SSL §131-a(15); 18-ADM-05).

  These are rent rules, not shelter-fee rules, so they are benchmarks rather than precedents. But they show the tension: at 50%, a household would still pay a larger share of income in shelter than it will once housed, and that limits move-out savings. Whatever the target, the steps should cite which rule or county policy sets "80%."

---

## 7. Feasibility and ambition

**Who controls the 68 steps** (coding in [Appendix C](appendix/C-action-step-register.md)):

| | All steps | Year One |
|---|---:|---:|
| CoC/lead agency can act directly | 25 (37%) | 13 |
| Needs a named partner to act (DSS, PHAs, hospitals, libraries, employers, courts, funders) | 33 (49%) | 9 |
| Needs a change in law, regulation, zoning or court rules | 10 (15%) | 1 |
| Needs new or redirected money | 28 (41%) | — |

**Year One is calendar 2026** (P4.1). As of October 14, **78 days remain**. Thirteen of the 23 Year One steps are within the CoC's direct control: a needs study, three campaigns, a system map and a health map, two guides, lived-experience mechanisms, standards and training, a working group, SOAR coverage and a funder list. A status check on those 13 is the fastest credibility win.

**Likely over-ambitious as written**
- G2-Y25-03 "create and **enact**" rent protections: the CoC can advocate but cannot enact. Outside NYC, Good Cause Eviction applies only where a locality opts in by local law.
- G4-Y25-06 "**mandatory** diversion at eviction proceedings": requires the Unified Court System.
- G1-Y25-03 "increase programs like the housing choice voucher": voucher supply is set by HUD appropriations and PHAs.
- G6-Y1-H landlord incentive pool in Year One: no funder or administrator is named.

**Likely under-ambitious or already partly done**
- G3-Y25-01's 50% shelter-fee target, which is still above the 30% rent share households pay once housed under local programs (6.3).
- 55+ strategy deferred to Years 2–5 despite fastest growth (5.1).
- G2-Y25-01 builds a housing-listing database, but New York HCR already runs a free one (HousingSearch.ny.gov, relaunched October 2025). Partner instead of building.
- G1-Y1-01 says "create" Standards of Care, but CoCs must already "establish and consistently follow written standards" ([578.7(a)(9)](appendix/E-sources.md#s11)). This is an update.

**Compliance note.** Advocacy steps (G2-Y25-03/03a, G3-Y1-01) should be paid from unrestricted funds. Lobbying costs are unallowable on federal awards ([2 CFR 200.450](appendix/E-sources.md#s12)). Have counsel confirm which activities count.

---

## 8. Goal-by-goal review

Each goal gets its strengths, its problems and one or two KPIs. Baselines come from the plan where it gives them; HUD System Performance Measures (SPMs) are named where they apply.

### Goal 1 — Reduce Unsheltered Homelessness
- **Works:** Names real barriers (pets, documents, behavioral health). Asks *why* people decline shelter, which is the right question. Day centers with evening, weekend and holiday hours.
- **Problems:** No street-outreach step, even though outreach is the front door for this population. "People without documentation" is ambiguous (ID documents vs. immigration status), and that matters under the proposed HUD rule. The 472 figure needs a definition. The housing step (G1-Y1-H) is a general study, not an unsheltered-specific action.
- **KPIs:** unsheltered PIT count (baseline 320, January 2025; SPM 3). Share of unsheltered people offered shelter who accept, a new measure produced by G1-Y1-01's analysis. Exits from street outreach to sheltered or permanent destinations (SPM 7a).

### Goal 2 — Optimize Coordination
- **Works:** Discharge planning with hospitals, jails and foster care targets a known inflow. Lived-experience voice is built in. The step on youth training is the plan's only youth item.
- **Problems:** Duplicated discharge guide (with G5). Database duplicates the state's listing site. Zoning and rent steps depend on towns, villages and the Legislature; frame them as advocacy with named asks.
- **KPIs:** entries to homelessness with a prior living situation of hospital, jail/prison or foster care (HMIS prior-living-situation field). First-time homeless (SPM 5). Returns within 6/12/24 months (SPM 2; plan baseline 7%, window not stated).

### Goal 3 — Transform the Shelter System
- **Works:** Clear eligibility information, safety analysis, a feedback (CQI) loop, peer staff, and recognition that both county DSS offices are essential partners (added after the draft).
- **Problems:** Fee steps are scattered and don't cite the rule they want changed (6.1, 6.3). "Weekend and evening access" needs a definition: DSS placement hours or daytime shelter access? Code Blue hours already apply in freezing weather ([18 NYCRR 304.1; 25-LCM-12](appendix/E-sources.md#s16)). G3-Y25-07 names housing authorities but means DSS.
- **KPIs:** length of time homeless (SPM 1; plan baseline 251 days episode, 261 days in shelter). Exits from shelter to permanent housing (SPM 7b; plan baseline 25%).

### Goal 4 — Employment and Economic Opportunity
- **Works:** Benefit-cliff navigation, fair-chance employers, paid in-shelter work that doesn't raise the shelter fee, education access.
- **Problems:**
  - The cost-burden statistic is mislabeled (E6).
  - "Expungement" should be sealing (E7).
  - Eviction diversion is misplaced (6.2).
  - The source-of-income step must now account for *People v. Commons West* (section 9.2).
  - SNAP changes in effect since November 2025 removed the work-rule exemptions for people experiencing homelessness, veterans and former foster youth, and extended the rules to age 64. Benefit navigators need that in their script.
- **KPIs:** increase in earned and total income for stayers and leavers (SPM 4). Number of people assisted with record sealing.

### Goal 5 — Healthcare and Services
- **Works:** The SOAR step is the plan's best-written step: universal, binary and checkable. Medical respite, FQHC linkage and Medicaid-funded housing supports are the right tools. Street medicine was added after the draft.
- **Problems:**
  - SSI figure (E5) and the "average rent" label (E4).
  - Two working groups for overlapping populations.
  - Housing for people actively using substances (G5-Y25-05) will likely need non-federal money under current federal conditions (9.1).
  - Medicaid changes start January 2027: work requirements, and 6-month renewals for expansion adults (NY DOH). Homelessness is not itself a federal exemption, but "medically frail" can be. SOAR and benefits staff should be ready.
  - New York's waiver authority for health-related social needs, which funds the Long Island Social Care Network, runs through March 31, 2027.
- **KPIs:** share of CoC-funded housing programs with a SOAR-trained staff member (target 100%). SOAR approvals and decision time. Medicaid enrollment at project entry.

### Goal 6 — Sustain and Diversify Funding
- **Works:** Landlord incentive pool, a funder map, tenant-rights training, and an eviction-prevention partner search.
- **Problems:** The goal's purpose is "continuity of services in the face of funding changes or decreases" (P11.1), but no step addresses the federal renewal risk that has materialized (9.1). Four of the seven Years 2–5 steps are programmatic. "Deeply affordable" is set at 25% AMI; HUD's extremely-low-income line and most gap data use 30% AMI, so 25% limits comparability.
- **KPIs:** share of CoC-system funding from non-HUD sources. Landlord pool dollars and units secured. Prevention households served who do not enter shelter within 12 months.

---

## 9. What changed around the plan (as of Oct 14, 2026)

### 9.1 Federal

| Development | Date | What it means for the plan |
|---|---|---|
| **FY2025 CoC NOFO** (Tier 1 cut to 30% of renewal demand; 30% cap on permanent housing) | Released Nov 13, 2025. Suits filed Nov 25 and Dec 1. Withdrawn mid-December. Preliminary injunction Dec 23 directing renewals under the FY2024–25 NOFO. | Renewals were processed under the old rules. A June 30, 2026 ruling reportedly vacated both FY2025 notices (secondary report). |
| **FY2026 CoC NOFO** | Published June 1, 2026. $4.04B. **Tier 1 = 60% of ARD** (the FY2026 appropriations act set 60% as the floor). At least $430M for permanent housing for families with children. **Up to $1.3B Tier 2 set-aside for new transitional housing and services-only projects.** New pass/fail affirmations on drug-consumption sites and harm reduction. Merit review scores encampment reduction and on-site substance-use treatment. | **Any renewal ranked below the 60% line competes in Tier 2, where NAEH judged renewals unlikely to be awarded.** That puts existing PSH/RRH at risk. Goal 6 needs a contingency. G5-Y25-05 and low-barrier steps may need non-federal funds. |
| FY2026 NOFO litigation | Aug 7, 2026: the district court held the $1.3B set-aside unlawful, invalidating the NOFO. Sept 16: an appeals court let HUD proceed during the appeal. HUD reinstated the NOFO with a Sept 30 deadline. HUD says awards will be announced by Dec 1, 2026. | Expect award news within weeks of this review. Plan the Year Two update around the outcome. |
| **Executive Order 14321**, "Ending Crime and Disorder on America's Streets" | July 24, 2025 | Directs HUD and HHS away from Housing First and harm reduction, toward treatment requirements. |
| **Proposed rule: Verification of Eligible Status** (HUD) | Published Feb 20, 2026; comments closed Apr 21, 2026; no final rule found as of this review | Would end prorated assistance for mixed-status families in HUD-assisted housing. Affects G1-Y25-05 and "people without documentation." Have materials legally reviewed. |
| **H.R. 1 (2025): SNAP** | Effective Nov 1, 2025; NY implementation 2026 | Removed time-limit exemptions for people experiencing homelessness, veterans and former foster youth; age range now 18–64 |
| **H.R. 1: Medicaid** | Work requirements from Jan 1, 2027 (NY DOH). Six-month renewals for expansion adults (NY DOH dates: applications after Jan 1 and renewals after Mar 1, 2027). | Coverage-loss risk for adults experiencing homelessness. Strengthens the case for Goal 5 benefits navigation. |
| FY2027 Fair Market Rents | Effective Oct 1, 2026 | Nassau-Suffolk 2BR fell 2.1% to $2,689. Affects payment standards and RRH rent reasonableness. |
| 2027 Social Security COLA | Announced Oct 14, 2026: 3.6% | Update SSI figures in January |

### 9.2 State and local

| Development | What it means |
|---|---|
| **Comptroller:** NY homelessness fell 7.9% statewide in January 2025, but rose in 17 of 25 CoCs; NY-603 was up 13.4% | Long Island is moving against the state trend. Useful framing for state asks. |
| **Code Blue** is now codified in regulation (18 NYCRR 304.1): alert below 32°F including wind chill for 2+ hours; about $20M a year in state support | The plan mentions extreme weather once and has no Code Blue step |
| **Clean Slate Act**: automatic sealing of eligible convictions by Nov 16, 2027 | Legal-services partnership (G4-Y1-01) should help people check status and use CPL 160.59 in the meantime |
| ***People v. Commons West*** (3d Dept, Mar 5, 2026) held the source-of-income law unconstitutional as applied to Section 8 vouchers; the Attorney General appealed (APL-2026-00035) | Directly affects G4-Y25-05. Long Island is in the Second Department, but until the Court of Appeals rules, enforcement for voucher holders is uncertain. Ask counsel how to frame the step. |
| **Good Cause Eviction** applies outside NYC only where a locality opts in by local law (HCR publishes the list); Long Island tenant advocates have pressed village boards | Gives G2-Y25-03 a concrete, local ask |
| **Long Island Social Care Network:** the Health and Welfare Council of Long Island is the SCN lead (named Aug 2024); NYHER waiver authority runs through Mar 31, 2027; renewal pending | G5-Y1-01's "programs effectively using Medicaid" should start with the SCN network. Plan for the waiver cliff. |
| **HousingSearch.ny.gov** (HCR), relaunched October 2025 | Use for G2-Y25-01 |
| **Long Island Zoning Atlas**: multifamily is allowed as-of-right on under 10% of land | Strong data for G2-Y25-02 |
| **Minimum wage** $17.00/hr on Long Island (2026), indexed from 2027 | Use in G4 messaging (5.3) |
| **2026 PIT count** held Jan 27, 2026; totals not yet public | Refresh section "Current State" in the Year Two update |

---

## 10. What's missing

- **Accountability scaffolding:** baselines, targets, owners, timelines, adoption date, and a described annual update process (who drafts, who approves, when).
- **Roles and governance:** which Board committee owns each goal; LICH's roles as Collaborative Applicant and HMIS Lead; the conflict-of-interest process for steps that fund lead-agency programs.
- **Coordinated Entry.** Required by [578.7(a)(8)](appendix/E-sources.md#s11) and named as a committee (P1.3), but there are no CE steps: prioritization, assessment, a by-name list, or CE data quality.
- **Street outreach.** No outreach step under the unsheltered goal.
- **Domestic violence survivors.** DV providers had a listening session, but there is no DV step (VAWA protections, comparable database, DV-specific capacity).
- **Youth.** 317 young adults aged 18–24 in the PIT; McKinney-Vento liaisons were surveyed; the only youth item is a sub-bullet (G2-Y1-01a).
- **Veterans.** The word "veteran" does not appear.
- **Large families.** Families with 3+ children stay longest (403 days), and the 3-bedroom FMR is $3,458, but no step targets larger units or family-specific exits.
- **Extreme weather and Code Blue.**
- **Encampments.** Now scored by HUD; no plan measure.
- **Outcomes by population.** LSA supports breakdowns by household type, age, race/ethnicity, gender and disability. The plan reports only a few. Reporting differences in outcomes is a data practice, separate from service preferences; check wording against current federal conditions.
- **Federal funding contingency** (9.1) and a rough **cost estimate** for the steps that need money (28 of 68).

---

## 11. Proposed consolidation: 68 steps → 13 initiatives

Every one of the 81 lines maps to exactly one initiative (verified by `register.py`; full mapping in [Appendix C](appendix/C-action-step-register.md#proposed-initiatives)).

| # | Initiative | Absorbs | Year One deliverable (suggested) |
|---|---|---|---|
| 1 | Shelter income, fees and savings reform | 10 lines from G1, G3, G4 | Memo citing the governing rules and each county's current fee policy; one pilot design with DSS |
| 2 | One standards-and-training framework | G3-Y1-H, G5-Y25-02, G5-Y25-07, G6-Y25-06 | Updated CoC written standards (incl. RRH step-down) and a core training curriculum |
| 3 | One system map and front-door information | G1-Y1-02/02a, G5-Y1-01, G2-Y1-Ea, G3-Y1-E | One map with health and Medicaid layers; plain-language shelter-entry card |
| 4 | Health, discharge and triage | G2-Y1-H, G5-Y1-02/02a, G5-Y1-H/Ha | One regional discharge and triage guide; respite feasibility |
| 5 | Low-barrier crisis capacity and access | 11 lines incl. G1-Y1-01, G3-Y25-08, G5-Y25-03/05/08 | Analysis of why shelter is declined; low-barrier bed gap estimate |
| 6 | Older adults (55+) | G5-Y25-01, G6-Y25-03 | Move to Year One/Two; 55+ prevention and crisis-housing recommendations |
| 7 | Prevention and eviction diversion | G1-Y25-01, G6-Y25-02, G6-Y1-02, G6-Y1-E, G4-Y25-06, G2-Y25-06 | Funder and court-partner map; tenant-rights training pilot |
| 8 | Landlords, units and exits to housing | G6-Y1-H, G6-Y25-04/05, G4-Y25-05, G2-Y25-01, G3-Y25-04/07/07a | Landlord pool term sheet; HousingSearch.ny.gov partnership |
| 9 | Income, employment and benefits | 9 lines incl. G5-Y1-E (SOAR) | 100% SOAR coverage; benefits-cliff and SNAP/Medicaid script |
| 10 | Housing supply and policy advocacy | 9 lines incl. G1-Y1-H, G2-Y25-02/03, G6-Y25-01 | Annual gaps analysis (578.7(c)(3)); one-page state and local asks (non-federal funds) |
| 11 | Lived experience and peer workforce | G2-Y1-01/01a, G3-Y25-03/05, G5-Y25-06 | Paid lived-experience advisory seats; CQI survey |
| 12 | One communications strategy | G1-Y1-E, G1-Y25-05, G2-Y1-E, G4-Y1-E | Segmented plan for public, providers, officials and funders |
| 13 | Funding diversification and federal contingency | G6-Y1-01/01a, G6-Y25-07, plus a new step | Renewal-risk analysis and bridge plan |

---

## 12. Measurement framework

Targets are the Board's to set. This table supplies the baselines the plan already contains and the measure each goal should report.

| Goal | KPI | HUD SPM | Plan baseline | Data source |
|---|---|---|---|---|
| 1 | Unsheltered persons on PIT night | 3 | 320 (Jan 2025) | PIT |
| 1 | Share of offered shelter accepted by unsheltered people | — | none (new) | Outreach/HMIS |
| 2 | Returns to homelessness within 6/12/24 months | 2 | 7% (window not stated) | SPM report |
| 2 | First-time homeless | 5 | none | SPM report |
| 3 | Average length of time homeless | 1 | 251 days (LSA) / 261 in shelter | SPM / LSA |
| 3 | Exits from shelter to permanent housing | 7b | 25% | SPM / LSA |
| 4 | Income growth, stayers and leavers | 4 | none | SPM report |
| 5 | CoC-funded housing programs with SOAR-trained staff | — | unknown → target 100% | CoC survey |
| 5/6 | Persons 55+ in PIT | 3 | 825 (2025) | PIT |
| 6 | Share of system funding from non-HUD sources | — | none | Finance |
| All | Total persons on PIT night | 3 | 4,540 (2025) | PIT |

Note that the LSA "average episode" and SPM 1 use different methods. Pick one per measure and keep it for five years.

---

## 13. Risk register

| # | Risk | Likelihood | Impact | Goals | Mitigation |
|---|---|---|---|---|---|
| R1 | Loss of renewals ranked in Tier 2 under the FY2026 NOFO | High | High | 6, all | Ranking strategy; bridge funds; county/state backfill asks; contingency in Goal 6 |
| R2 | No owners or capacity for 68 steps | High | High | All | Consolidate to 13 initiatives; assign owners |
| R3 | Federal limits on Housing First and harm reduction | High | Medium | 1, 3, 5 | Identify non-federal funding for low-barrier and active-use housing |
| R4 | Medicaid work requirements and 6-month renewals (2027) | High | Medium | 5 | SOAR and benefits navigation; medically-frail documentation |
| R5 | SNAP time-limit changes cut food benefits | High | Medium | 4, 5 | Benefit navigator script; food-access mapping |
| R6 | HUD mixed-status rule finalized | Medium | High | 1, 3 | Legal review of materials; non-federal options |
| R7 | Commons West upheld on appeal | Medium | Medium | 4, 6 | Landlord incentives; HQS inspection consent workflow |
| R8 | NYHER/SCN funding gap after Mar 31, 2027 | Medium | Medium | 5 | Track renewal; diversify housing-support funding |
| R9 | DSS/OTDA decline fee changes | Medium | Medium | 3 | Cite rules; pilot with one county; use the 30% rent benchmark to make the case |
| R10 | Local opposition to zoning and rent protections | High | Medium | 2 | Use the Zoning Atlas; target opt-in localities |
| R11 | Data definitions unclear (472; 251 vs 261) | Medium | Medium | 1, 3 | Data dictionary appendix |
| R12 | Lobbying costs charged to federal awards | Low | Medium | 2, 3 | Track advocacy on unrestricted funds |
| R13 | Conflict of interest where lead-agency programs gain resources | Low | Medium | 5 | Document recusals |

---

## 14. Year One close-out template

Copy one row per Year One step (23 rows; IDs from [Appendix C](appendix/C-action-step-register.md)).

| Step ID | Definition of done | Owner (committee / role) | Lead partner | Status (not started / in progress / done / carried to 2027) | Evidence | KPI | Notes |
|---|---|---|---|---|---|---|---|
| G5-Y1-E | Every CoC-funded housing program lists ≥1 SOAR-trained staff member | | | | Roster | SOAR coverage % | |
| G2-Y1-H + G5-Y1-02 | One regional discharge and triage guide published | | Hospitals, county jails, foster care agencies | | Published guide | Entries from institutions | Merge the two steps |
| G1-Y1-02 + G5-Y1-01 | One system map with health layer published | | SCN lead | | Map link | — | Merge the two steps |
| … | | | | | | | |

---

## 15. Questions for the CoC team and data staff

1. What is the plan's adoption date, and which Governing Board meeting approved it?
2. What exactly is the 472? Which projects, which data element, and is it adults with any unsheltered night or adults enrolled in street outreach?
3. Is the 261-day shelter stay a separate LSA measure from the 251-day episode, or a typo? Which LSA period and population (households or persons)?
4. Where does "80% of income" come from: Nassau DSS, Suffolk DSS, or both, and for which shelter types?
5. Which organization is the "Long Island Housing Coalition"?
6. What is the source for the $1,009 SSI figure?
7. Which of the 23 Year One steps are done or underway, and who owns each?
8. How was NY-603's FY2026 ranking built around the 60% Tier 1 line, and which renewals sit in Tier 2?
9. Why are there no Coordinated Entry or DV steps? Are they handled in another plan?
10. Who drafts the Year Two update, when, and with which data (2026 PIT, FY2025 LSA, SPMs)?
11. Does the Board want outcomes reported by population (age, household type, disability, race/ethnicity, gender) in the annual update?
12. Should the CoC publish a simple public dashboard tied to section 12?
