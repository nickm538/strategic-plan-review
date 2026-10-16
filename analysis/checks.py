"""Reproducible checks behind the review of the NY-603 Strategic Plan 2026-2030.

Every input is either (a) copied from the plan (see appendix/A-plan-transcription.md,
cited by page) or (b) copied from a public source listed in appendix/E-sources.md.
Run:  python3 analysis/checks.py > analysis/checks-output.txt
"""
import csv
import os
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DAYS_PER_MONTH = 365.25 / 12  # 30.44


def pct(n, d, places=1):
    return round(100.0 * n / d, places)


def header(title):
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


# ---------------------------------------------------------------------------
# 1. PIT table (plan p. 2, Table T1)
# ---------------------------------------------------------------------------
COLS = ["Emergency", "Transitional", "Safe Haven", "Unsheltered", "Total"]
PIT = {
    "Households":   [2286, 54, 18, 320, 2678],
    "Persons":      [4139, 63, 18, 320, 4540],
    "under 18":     [1483, 9, 0, 1, 1493],
    "18-24":        [293, 12, 2, 10, 317],
    "25-34":        [571, 8, 1, 41, 621],
    "35-44":        [615, 4, 4, 91, 714],
    "45-54":        [484, 3, 3, 80, 570],
    "55-64":        [487, 10, 1, 74, 572],
    "over 64":      [206, 17, 7, 23, 253],
}
AGES = ["under 18", "18-24", "25-34", "35-44", "45-54", "55-64", "over 64"]

header("1. PIT 2025 table arithmetic (plan p. 2)")
ok = True
for row, v in PIT.items():
    s = sum(v[:4])
    flag = "OK" if s == v[4] else "MISMATCH"
    ok &= s == v[4]
    print(f"  row {row:<11} {v[:4]} sum={s:>5} stated total={v[4]:>5}  {flag}")
for i, c in enumerate(COLS):
    s = sum(PIT[a][i] for a in AGES)
    flag = "OK" if s == PIT["Persons"][i] else "MISMATCH"
    ok &= s == PIT["Persons"][i]
    print(f"  col {c:<12} age-sum={s:>5} stated persons={PIT['Persons'][i]:>5}  {flag}")
print(f"  ALL TABLE SUMS RECONCILE: {ok}")

P = PIT["Persons"]
H = PIT["Households"]
sheltered_p = sum(P[:3])
sheltered_h = sum(H[:3])
print(f"\n  Sheltered persons {sheltered_p} ({pct(sheltered_p, P[4])}%), unsheltered {P[3]} ({pct(P[3], P[4])}%)")
print(f"  Children under 18: {PIT['under 18'][4]} = {pct(PIT['under 18'][4], P[4])}% of all persons "
      f"(Comptroller: 32.9%)")
print(f"  Persons per household: ES {P[0]/H[0]:.2f}, TH {P[1]/H[1]:.2f}, SH {P[2]/H[2]:.2f}, "
      f"Unsheltered {P[3]/H[3]:.2f}, All {P[4]/H[4]:.2f}")
u55_64 = PIT["55-64"][3]
u65 = PIT["over 64"][3]
print(f"  Unsheltered 55-64: {u55_64}/{P[3]} = {pct(u55_64, P[3])}%  (plan: 'just over 20%')")
print(f"  Unsheltered 55+:   {u55_64 + u65}/{P[3]} = {pct(u55_64 + u65, P[3])}%")
all55 = PIT["55-64"][4] + PIT["over 64"][4]
print(f"  All persons 55+:   {all55}/{P[4]} = {pct(all55, P[4])}%  "
      f"(press reports of LICH release: 'just over 670' in 2024 -> 825 in 2025)")
print(f"  55+ growth 2024->2025 if 2024 = 670: +{pct(all55 - 670, 670)}% (vs +13.4% overall)")
print(f"  Unsheltered adults (18+): {P[3] - PIT['under 18'][3]}; unaccompanied minors unsheltered: {PIT['under 18'][3]}")
print("  Age distribution of unsheltered:",
      ", ".join(f"{a} {pct(PIT[a][3], P[3])}%" for a in AGES))
print("  Age distribution of sheltered:  ",
      ", ".join(f"{a} {pct(sum(PIT[a][:3]), sheltered_p)}%" for a in AGES))

# 2024 PIT implied by the Comptroller's +13.4%
implied_2024 = P[4] / 1.134
print(f"\n  Implied 2024 PIT total from Comptroller's +13.4%: ~{implied_2024:,.0f} persons")

# ---------------------------------------------------------------------------
# 2. Length-of-time statements (plan pp. 1-2, 7)
# ---------------------------------------------------------------------------
header("2. Length-of-time statements")
LOS_ALL = 251
print(f"  251 days = {LOS_ALL / DAYS_PER_MONTH:.2f} months (plan says 'almost 7 months'; "
      f"7 months = {7 * DAYS_PER_MONTH:.0f} days)")
subgroups = {"Families with 3+ children (8% of households)": 403,
             "Single adults 55+ (23% of households)": 246,
             "Households with a disabled member (40%)": 219}
for k, v in subgroups.items():
    d = v - LOS_ALL
    print(f"  {k:<46} {v} days  {d:+d} vs overall ({pct(d, LOS_ALL):+.1f}%)  "
          f"{'LONGER' if d > 0 else 'SHORTER'} than average")
print("  -> Plan introduces all three as groups whose challenges 'prolonged their homelessness';"
      " two of the three are below the overall average.")

ES_SHARE, LOS_SHELTER = 0.92, 261
implied_other = (LOS_ALL - ES_SHARE * LOS_SHELTER) / (1 - ES_SHARE)
print(f"\n  p.1 says average episode = 251 days; p.7 says shelter users stayed 261 days.")
print(f"  If both are right, the 8% who never used shelter averaged "
      f"(251 - 0.92*261)/0.08 = {implied_other:.0f} days. Possible, but confirm 261 is not a typo for 251.")

# ---------------------------------------------------------------------------
# 3. Flow consistency (Little's Law: stock = inflow x average stay)
# ---------------------------------------------------------------------------
header("3. Flow consistency (Little's Law)")
L = sheltered_h  # households in shelter on PIT night
W = LOS_ALL
lam = L / W
annual_est = L + lam * 365
print(f"  Sheltered households on PIT night (2025): {L}")
print(f"  Implied entries = {L}/{W} = {lam:.2f} households/day = {lam * 365:,.0f}/year")
print(f"  Implied households served in a year = stock + entries = {annual_est:,.0f}")
L24 = L / 1.134
print(f"  Same calculation at the implied 2024 level (/1.134): {L24 + (L24 / W) * 365:,.0f}")
print("  Plan: 5,447 households assisted in FY2024 -> sits between the two estimates; "
      "the plan's flow numbers hang together.")

print("\n  Unsheltered: 472 adults over Apr 2023-Apr 2025 (24 months) vs 319 unsheltered adults on one night (Jan 2025).")
L_u, N_u, T_u = 319, 472, 730
entries = N_u - L_u
W_u = L_u / (entries / T_u)
print(f"  For both to be complete counts of the same group, only ~{entries} people entered in two years,")
print(f"  implying an average unsheltered spell of ~{W_u:,.0f} days (~{W_u / 365:.1f} years).")
print("  More likely the 472 counts only adults recorded in HMIS (e.g., outreach-enrolled); "
      "the plan should define it.")

print("\n  Scenario (illustrative, holds inflow constant at the 2025 rate):")
for target in (225, 200, 180):
    new_L = lam * target
    print(f"    cut average stay {W} -> {target} days: sheltered households on a given night "
          f"{L} -> {new_L:,.0f} ({pct(new_L - L, L):+.1f}%)")

# ---------------------------------------------------------------------------
# 4. Rent, SSI and wages (plan pp. 8-9)
# ---------------------------------------------------------------------------
header("4. Rent, SSI and wage checks")
FMR = {  # HUD Nassau-Suffolk, NY HUD Metro FMR Area (huduser.gov)
    "FY2025": {"0BR": 1848, "1BR": 2241, "2BR": 2586, "3BR": 3352, "4BR": 3591},
    "FY2026": {"0BR": 1992, "1BR": 2379, "2BR": 2747, "3BR": 3563, "4BR": 3768},
    "FY2027": {"0BR": 1964, "1BR": 2285, "2BR": 2689, "3BR": 3458, "4BR": 3671},
}
print("  Plan's $2,586 = HUD FY2025 2BR FMR for Nassau-Suffolk exactly:", FMR["FY2025"]["2BR"] == 2586)
print("  (FMR is a 40th-percentile gross rent for standard-quality units, not a median or average.)")
for a, b in (("FY2025", "FY2026"), ("FY2026", "FY2027")):
    x, y = FMR[a]["2BR"], FMR[b]["2BR"]
    print(f"  2BR {a} ${x:,} -> {b} ${y:,}: {pct(y - x, x):+.1f}%")

SSI = {  # NY individual living alone: federal + state supplement (OTDA charts)
    2024: (943, 87), 2025: (967, 87), 2026: (994, 87),
}
for yr, (fed, ssp) in SSI.items():
    print(f"  SSI {yr} NY living alone: ${fed} + ${ssp} = ${fed + ssp:,}")
print("  Plan's $1,009 ('average SSI payment in 2024') matches none of these; the 2024 maximum was $1,030.")

ssi26 = sum(SSI[2026])
for fy in ("FY2026", "FY2027"):
    for br in ("0BR", "1BR"):
        r = FMR[fy][br]
        print(f"  {fy} {br} FMR ${r:,} = {pct(r, ssi26)}% of 2026 SSI (${ssi26:,}); "
              f"30%-of-income rent on SSI = ${0.3 * ssi26:,.0f}")
print("  A one-person SSI household is better compared to a studio/1BR than the 2BR the plan uses.")

MIN_WAGE_LI_2026 = 17.00
annual_mw = MIN_WAGE_LI_2026 * 2080
two_br = FMR["FY2027"]["2BR"]
need_income = two_br * 12 / 0.30
print(f"\n  LI minimum wage 2026 ${MIN_WAGE_LI_2026:.2f}/hr -> full-time ${annual_mw:,.0f}/yr; "
      f"affordable rent ${annual_mw * 0.30 / 12:,.0f}/mo")
print(f"  Income needed for FY2027 2BR FMR ${two_br:,} at 30%: ${need_income:,.0f}/yr = "
      f"${need_income / 2080:,.2f}/hr full-time")
print(f"  Minimum-wage hours/week needed: {two_br / (0.30 * MIN_WAGE_LI_2026 * 52 / 12):.0f}")

# ---------------------------------------------------------------------------
# 4b. FY2024 HUD CoC awards to NY-603 and the FY2026 60% Tier 1 line (REVIEW 9.1)
# ---------------------------------------------------------------------------
header("4b. NY-603 FY2024 CoC awards (HUD award report) and a 60% Tier 1 illustration")
AWARDS_FY2024 = {  # hud.gov CoC-2024-NY_Press.pdf, NY-603 section, retrieved 2026-10-16
    "AHAL II FY2024": 126942, "Beacon House III": 245024, "EOC 2024 PSH Renewal": 245044,
    "EOC RRH Program": 1029644, "EOC RRH Program II": 369870, "Estella (New)": 361100,
    "FSL RRH Program FY2024": 1062485, "FY 2024 Casa Salva FN": 448444, "FY 2024 Casa Serenidad FN": 438463,
    "HFY TH-RRH Renewal FY2024": 429836, "HUD Senior Quarters Renewal 2024": 112996, "HUD-Coram 2024": 65021,
    "HUD-Nassau 2024": 457081, "LI COC Planning Project 2024": 1173818, "LI Consolidated RRH FY24 (NY1174)": 1581009,
    "LI Coordinated Entry Expansion 2024": 430100, "LI Coordinated Entry Renewal 2024": 835812,
    "LI HMIS 2024": 489513, "Long Island Coordinated Entry System for Domestic Violence Survivors": 248975,
    "Nassau Scattered Site Permanent Housing Program": 163714, "OPP": 370792, "OPPII": 993377,
    "Options NCC 2024": 516596, "Project Homestart": 521161, "Project Independence Renewal 2024": 1540885,
    "Project Veterans' Independence Renewal 2024": 378176, "RRH 2024": 636766,
    "SAIL Shelter Plus Care III 2024": 506817, "SHP/Islip": 199899, "Summit Renewal FY 2024": 84260,
    "Supportive Housing Subsidy Program": 357915, "W&H Renewal YE 8.31.26": 677810,
    "WellLife Network Medford Gardens": 554000,
}
total_aw = sum(AWARDS_FY2024.values())
print(f"  {len(AWARDS_FY2024)} grants, total ${total_aw:,} (HUD report total: $17,653,345; match: {total_aw == 17653345})")
renew_proxy = total_aw - AWARDS_FY2024["LI COC Planning Project 2024"] - AWARDS_FY2024["Estella (New)"]
print(f"  Excluding the planning grant and the one grant marked new: ${renew_proxy:,} (proxy for renewal demand)")
print(f"  At a 60% Tier 1: ${renew_proxy * 0.6:,.0f} inside Tier 1, ${renew_proxy * 0.4:,.0f} outside (illustration only)")

# ---------------------------------------------------------------------------
# 5. Cost burden (plan p. 8: '55% of Long Islanders are cost-burdened')
# ---------------------------------------------------------------------------
header("5. Housing cost burden, ACS 2024 1-year (B25070 renters, B25091 owners)")
ACS = {  # api.census.gov/data/2024/acs/acs1, retrieved 2026-10-14
    "Nassau":  {"R_tot": 85953, "R_30_35": 9433, "R_35_40": 6091, "R_40_50": 6503, "R_50p": 20161, "R_nc": 5659,
                "M_tot": 222720, "M_30p": (19444, 14556, 17705, 35101), "M_nc": 742,
                "NM_tot": 151496, "NM_30p": (6640, 4646, 6192, 18151), "NM_nc": 1668},
    "Suffolk": {"R_tot": 89181, "R_30_35": 6845, "R_35_40": 6515, "R_40_50": 7765, "R_50p": 27123, "R_nc": 8562,
                "M_tot": 262756, "M_30p": (21276, 16214, 20289, 42115), "M_nc": 397,
                "NM_tot": 157955, "NM_30p": (7394, 6321, 6503, 19527), "NM_nc": 2291},
}
tot = defaultdict(int)
for county, d in ACS.items():
    r_den = d["R_tot"] - d["R_nc"]
    r_burd = d["R_30_35"] + d["R_35_40"] + d["R_40_50"] + d["R_50p"]
    o_den = (d["M_tot"] - d["M_nc"]) + (d["NM_tot"] - d["NM_nc"])
    o_burd = sum(d["M_30p"]) + sum(d["NM_30p"])
    print(f"  {county:<8} renters burdened {r_burd:,}/{r_den:,} = {pct(r_burd, r_den)}% "
          f"(severe 50%+: {pct(d['R_50p'], r_den)}%); owners {pct(o_burd, o_den)}%; "
          f"all households {pct(r_burd + o_burd, r_den + o_den)}%")
    for k, v in (("r_den", r_den), ("r_burd", r_burd), ("o_den", o_den), ("o_burd", o_burd), ("r_sev", d["R_50p"])):
        tot[k] += v
print(f"  Long Island renters burdened: {pct(tot['r_burd'], tot['r_den'])}% "
      f"(severe: {pct(tot['r_sev'], tot['r_den'])}%)")
print(f"  Long Island owners burdened:  {pct(tot['o_burd'], tot['o_den'])}%")
print(f"  Long Island ALL households:   {pct(tot['r_burd'] + tot['o_burd'], tot['r_den'] + tot['o_den'])}%")
print("  -> '55%' matches renter households (~56%), not 'Long Islanders' overall (~37%).")
print("  (1-year ACS estimates carry margins of error; the 5-year table is steadier for planning.)")

# ---------------------------------------------------------------------------
# 6. Methods appendix (plan pp. 12-13) and draft survey counts
# ---------------------------------------------------------------------------
header("6. Community input")
sessions = {"in-person, people experiencing homelessness": 3,
            "virtual, people experiencing homelessness": 2,
            "virtual, providers and partners": 7}
print(f"  Listening sessions: {sum(sessions.values())} (plan says twelve) -> "
      f"{'OK' if sum(sessions.values()) == 12 else 'MISMATCH'}")
SURVEY = {  # response counts printed in the Aug 18, 2025 community-review draft, dropped in final
    "Librarians/library staff": 40, "Homeless response system partners": 33,
    "Healthcare partners": 16, "McKinney-Vento liaisons/education": 9,
    "Local elected officials": 2, "Philanthropic partners": 1, "Local law enforcement": 1,
}
n = sum(SURVEY.values())
print(f"  Survey responses (draft table): total {n} (final text says 102) -> {'OK' if n == 102 else 'MISMATCH'}")
for k, v in SURVEY.items():
    print(f"    {k:<36} {v:>3}  {pct(v, n):>5}%")
print(f"  Library + homeless-system partners = {pct(40 + 33, n)}% of responses; "
      f"elected officials + philanthropy + law enforcement = {2 + 1 + 1} responses ({pct(4, n)}%)")
print("  No survey of landlords, PHAs, employers or DSS staff is listed.")

# ---------------------------------------------------------------------------
# 7. Action-step register (appendix C)
# ---------------------------------------------------------------------------
header("7. Action-step register (appendix/C-action-step-register.csv)")
with open(os.path.join(HERE, "..", "appendix", "C-action-step-register.csv"), encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
steps = [r for r in rows if r["type"] != "sub"]
subs = [r for r in rows if r["type"] == "sub"]
print(f"  Rows {len(rows)} = {len(steps)} steps + {len(subs)} sub-bullets")
by = Counter((r["goal"], r["horizon"]) for r in steps)
for g in "123456":
    print(f"    Goal {g}: Year One {by[(g, 'Y1')]:>2}  Years 2-5 {by[(g, 'Y2-5')]:>2}")
print(f"  Year One steps: {sum(v for (g, h), v in by.items() if h == 'Y1')}; "
      f"Years 2-5: {sum(v for (g, h), v in by.items() if h == 'Y2-5')}")

for field in ("control", "measure", "new_funding_needed"):
    c = Counter(r[field] for r in steps)
    print(f"  {field:<19} " + ", ".join(f"{k} {v} ({pct(v, len(steps))}%)" for k, v in sorted(c.items())))
y1 = [r for r in steps if r["horizon"] == "Y1"]
print("  Year One only:      control " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(r['control'] for r in y1).items()))
      + " | measure " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(r['measure'] for r in y1).items())))

theme_rows = defaultdict(list)
for r in rows:
    for t in r["themes"].split(";"):
        if t:
            theme_rows[t].append(r)
print("\n  Themes appearing under more than one goal (rows incl. sub-bullets):")
for t, rs in sorted(theme_rows.items(), key=lambda kv: -len(kv[1])):
    goals = sorted({r["goal"] for r in rs})
    if len(goals) > 1:
        print(f"    {t:<24} {len(rs):>2} rows across goals {','.join(goals)}")

dups = [r for r in rows if "Duplicates" in r["review_flags"]]
print(f"\n  Rows flagged as duplicating another row: {len(dups)} -> " + ", ".join(r["id"] for r in dups))

init = Counter(r["proposed_initiative"] for r in rows)
print(f"\n  Proposed initiatives: {len(init)}; every row mapped: {sum(init.values()) == len(rows)}")
for k in sorted(init, key=lambda s: int(s.split('.')[0])):
    print(f"    {k:<55} {init[k]:>2} rows")

labels = [r for r in rows if r["type"] in ("H", "E")]
print("\n  Year One 'Housing'/'Education' labels (the plan's two cross-cutting themes):")
for r in labels:
    print(f"    {r['id']:<8} {r['text'][:88]}...")

print("\nDone.")
