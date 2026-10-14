# Appendix E — Sources

All accessed October 14, 2026 unless noted. **Primary** = the agency, court or statute itself. **Secondary** = reporting or analysis by others; used only where noted and labeled as such in the review.

## Documents reviewed
- **Plan (final):** *Long Island Continuum of Care: Strategic Plan for 2026–2030* (NY-603), 16-page scanned PDF supplied by the CoC lead agency. Transcribed in [Appendix A](A-plan-transcription.md).
- **Plan (draft):** *LICoC Strategic Plan DRAFT for Community Comment*, "DRAFT for Community Review 8.18.2025," prepared by Technical Assistance Collaborative.

## Rents, income and benefits
<a id="s1"></a>**S1** (Primary) HUD, FY2026 FMR Documentation System, Nassau-Suffolk, NY HUD Metro FMR Area (shows FY2025 and FY2026). https://www.huduser.gov/portal/datasets/fmr/fmrs/FY2026_code/2026summary.odn?&year=2026&fmrtype=Final&cbsasub=METRO35620MM5380 — FY2025: $1,848 / $2,241 / **$2,586** / $3,352 / $3,591. FY2026: $1,992 / $2,379 / **$2,747** / $3,563 / $3,768 (0–4 BR).

<a id="s2"></a>**S2** (Primary) HUD, FY2027 FMR Dataset, Nassau-Suffolk. https://www.huduser.gov/datasets/fmr/fmr2027/summary?year=2027&reporttype=hmfa&hmfa=METRO35620MM5380&sa_override=1 — FY2027: $1,964 / $2,285 / **$2,689** / $3,458 / $3,671. Base rent (ACS 2020–24, 2BR standard-quality gross rent) $2,168.

**S2a** (Primary) Federal Register, *Fair Market Rents... Fiscal Year 2027*, Sept 1, 2026 (FMRs effective Oct 1, 2026). https://www.federalregister.gov/documents/2026/09/01/2026-17891/fair-market-rents-for-the-housing-choice-voucher-program-moderate-rehabilitation-single-room

<a id="s3"></a>**S3** (Primary) NYS OTDA, *2026 SSI and SSP Maximum Monthly Benefit Levels*. https://www.otda.ny.gov/programs/ssp/2026-Maximum-Monthly-Benefit-Amounts.pdf — Living alone: $994 federal + $87 state = $1,081.

<a id="s4"></a>**S4** (Primary) NYS OTDA, *2024 SSI and SSP Maximum Monthly Benefit Levels* (24-INF-07 Att. 1). https://www.otda.ny.gov/policy/directives/2024/INF/24-INF-07-Attachment-1.pdf — Living alone: $943 + $87 = $1,030.

<a id="s5"></a>**S5** (Primary) SSA, *Understanding SSI: SSI Benefits, 2026 edition*. https://www.ssa.gov/ssi/text-benefits-ussi.htm — Federal benefit rate $994 individual, $1,491 couple.

<a id="s6"></a>**S6** (Primary) SSA, *SSI Recipients by State and County, 2024*, Table 3, New York (December 2024). https://www.ssa.gov/policy/docs/statcomps/ssi_sc/2024/ny.html — Nassau 15,132; Suffolk 17,772 recipients.

**S6a** (Secondary) CNBC, "Social Security announces 3.6% COLA for 2027," Oct 14, 2026. https://www.cnbc.com/2026/10/14/social-security-cola-2027.html (also reported by KCAU, same day).

<a id="s9"></a>**S9** (Primary) U.S. Census Bureau, ACS 2024 1-year, tables B25070 and B25091, Nassau (36059) and Suffolk (36103), retrieved via the Census Data API (`https://api.census.gov/data/2024/acs/acs1?get=group(B25070)&ucgid=0500000US36059`, and the same for 36103 and B25091). Values are hard-coded in `analysis/checks.py`.

**S32** (Primary) TAC, *Priced Out: Where the Numbers Come From* (December 2024 update). https://tacinc.org/wp-content/uploads/2025/01/2025-01-09D-December-2024-Priced-Out-Update-Data-Sources.pdf — Used to try to trace the $1,009 SSI figure; no match.

**S37** (Secondary) New York minimum wage 2026 ($17.00 NYC, Long Island, Westchester; indexed from 2027): Erie News Now, https://www.erienewsnow.com/news/minimum-wage-sees-increase-in-new-york-state-in-2026/article_67aea580-26db-4c10-b3cb-1e66a1fc60f7.html. Confirm against NYS DOL for payroll use.

## Homelessness counts
<a id="s7"></a>**S7** (Primary) Office of the NYS Comptroller, *Homelessness Falls Statewide, But Rises in Many New York Regions* (data released end of May 2026 for the January 2025 count). https://www.osc.ny.gov/reports/homelessness-falls-statewide-rises-many-new-york-regions — NY −7.9% to 145,560; growth in 17 of 25 CoCs; Nassau, Suffolk Counties CoC +13.4%; under-18 share 32.9% (state 31.7%).

<a id="s8"></a>**S8** (Secondary) *New York Post* report syndicated by AOL, "Long Island homelessness explodes to highest...". https://www.aol.com/news/long-island-homelessness-explodes-highest-232310821.html — More than 4,500 in 2025, +13%, highest since 2007; 55+ from just over 670 (2024) to 825 (2025).

<a id="s42"></a>**S42** (Secondary) *Long Island Press*, "Non-profits count unhoused Long Islanders amid federal grant confusion," Feb 2, 2026. https://www.longislandpress.com/2026/02/02/unhoused-long-islander-count/ — 2026 count held Jan 27, 2026.

## Federal law and regulation
<a id="s10"></a>**S10** (Primary) 24 CFR 578.3, Definitions (Collaborative applicant; Continuum of Care; HMIS Lead). https://www.ecfr.gov/current/title-24/subtitle-B/chapter-V/subchapter-C/part-578/subpart-A/section-578.3

<a id="s11"></a>**S11** (Primary) 24 CFR 578.7, Responsibilities of the Continuum of Care: (a)(8) coordinated assessment; (a)(9) written standards; (c)(3) annual gaps analysis. https://www.ecfr.gov/current/title-24/subtitle-B/chapter-V/subchapter-C/part-578/subpart-B/section-578.7

**S11a** (Primary) 24 CFR 578.1, Purpose and scope. https://www.ecfr.gov/current/title-24/subtitle-B/chapter-V/subchapter-C/part-578/subpart-A/section-578.1

<a id="s12"></a>**S12** (Primary) 2 CFR 200.450, Lobbying. https://www.ecfr.gov/current/title-2/subtitle-A/chapter-II/part-200/subpart-E/subject-group-ECFRed1f39f9b3d4e72/section-200.450

## New York law and regulation
<a id="s13"></a>**S13** (Primary text via Justia) 18 NYCRR 900.18, Facility charges (family shelters). https://regulations.justia.com/states/new-york/title-18/chapter-ii/subchapter-l/part-900/section-900-18

<a id="s14"></a>**S14** (Primary text via Justia) 18 NYCRR 491.11, Facility charges (adult shelters). https://regulations.justia.com/states/new-york/title-18/chapter-ii/subchapter-d/part-491/section-491-11

<a id="s15"></a>**S15** (Primary) NYS OTDA, 18-ADM-05, *30 Percent Income Contribution for Individuals Diagnosed with AIDS or HIV...* (Aug 16, 2018); SSL §131-a(15). https://otda.ny.gov/policy/directives/2018/ADM/18-ADM-05.pdf

<a id="s16"></a>**S16** (Primary) NYS OTDA, 25-LCM-12 (Code Blue; 18 NYCRR 304.1). https://www.otda.ny.gov/policy/directives/2025/LCM/25-LCM-12.pdf

<a id="s17"></a>**S17** (Primary) NYS Unified Court System, Criminal Record Sealing (Clean Slate, CPL 160.57; sealing by Nov 16, 2027). https://nycourts.gov/FORMS/criminal-record-sealing.shtml. Also LawHelpNY, https://www.lawhelpny.org/resource/new-yorks-clean-slate-act-and-sealing-convictions

<a id="s18"></a>**S18** (Primary) NYS Unified Court System, *Destroy Expunged Marihuana Conviction Records* (CPL 160.50(5)). https://nycourts.gov/FORMS/destroy-expunged-marihuana-conviction/index.shtml

<a id="s35"></a>**S35** (Primary) NYS HCR, *Good Cause Eviction Law Notice* (Aug 2026; localities outside NYC opt in under RPL §213). https://hcr.ny.gov/good-cause-eviction-law-notice-august-2026

<a id="s36"></a>**S36** (Primary) *Matter of People of the State of N.Y. v Commons West, LLC*, App Div, 3d Dept, Mar 5, 2026 (CV-23-1255). https://decisions.courts.state.ny.us/ad3/Decisions/2026/CV-23-1255.pdf. Appeal (Court of Appeals APL-2026-00035): Ithaca.com, https://www.ithaca.com/news/ithaca/ny-attorney-general-appeals-court-decision-eliminating-section-8-mandate-favoring-ithaca-landlord-s-constitutional/article_3062591d-70c5-41d3-8c5c-9adc7e8b4a21.html; PRRAC amicus (Aug 2026), https://www.prrac.org/commons-west-amicus-brief-2026/

## Federal CoC competition and policy
<a id="s19"></a>**S19** (Primary) Congressional Research Service, *HUD's FY2025 Continuum of Care Program Competition* (IN12626). https://www.congress.gov/crs-product/IN12626

**S19a** (Secondary) National Alliance to End Homelessness, *FY2025 CoC Program NOFO Full Analysis* (updated Dec 19, 2025). https://endhomelessness.org/wp-content/uploads/2025/12/FY2025-CoC-Program-NOFO-Full-Analysis-updated-12.19.2025.pdf — Tier 1 at 30% of ARD; PH capped at 30% of ARD.

<a id="s20"></a>**S20** (Secondary) National Alliance to End Homelessness, *FY2026 Continuum of Care Competition NOFO Information* (June 2026). https://endhomelessness.org/wp-content/uploads/2026/06/FY2026-Continuum-of-Care-Competition-NOFO-Analysis.pdf — $4.040B; Tier 1 60% (appropriations floor); ≥$430M PH for families; up to $1.3B Tier 2 set-aside for new TH/SSO; Tier 2 renewals "unlikely to be awarded"; new affirmations; merit review.

<a id="s21"></a>**S21** (Secondary) National Network for Youth, "Lawsuit Halts Continuum of Care NOFO," Aug 12, 2026. https://nn4youth.org/2026/08/12/lawsuit-halts-continuum-of-care-nofo/ (links the Aug 7, 2026 order: https://www.courtlistener.com/docket/73568402/34/national-alliance-to-end-homelessness-v-united-states-department-of/)

<a id="s22"></a>**S22** (Secondary) National Association of Counties, "HUD releases funding opportunity for Continuum of Care Program – Deadline Sept. 30" (updated Sept 24, 2026). https://www.naco.org/news/hud-releases-funding-opportunity-continuum-care-program-deadline-sept-30

**S22a** (Primary) HUD, *FY 2026 CoC Program Project Application FAQs* (awards by Dec 1, 2026). https://www.hud.gov/sites/dfiles/CPD/documents/CoC/FY-2026-Project-Application-FAQs.pdf

<a id="s23"></a>**S23** (Secondary) *Lynnwood Times*, "AG Brown wins federal lawsuit to restore Housing First approach for Continuum of Care funding," July 1, 2026 (June 30, 2026 ruling). https://lynnwoodtimes.com/2026/07/01/continuum/; *Shelterforce*, Jan 23, 2026, https://shelterforce.org/2026/01/23/judge-blocks-hud-overhaul-federal-funding-for-homelessness-services/

<a id="s24"></a>**S24** (Primary) Federal Register, *Housing and Community Development Act of 1980: Verification of Eligible Status* (proposed rule), Feb 20, 2026. https://www.federalregister.gov/documents/2026/02/20/2026-03405/housing-and-community-development-act-of-1980-verification-of-eligible-status

<a id="s25"></a>**S25** (Secondary) NLIHC, "HUD Publishes Mixed-Status Families Proposed Rule with Comments Due April 21." https://nlihc.org/resource/hud-publishes-mixed-status-families-proposed-rule-comments-due-april-21-keep-families

<a id="s26"></a>**S26** (Primary) USDA FNS, *OBBB ABAWD Exemptions Implementation*. https://www.fns.usda.gov/snap/obbb-abawd-exemptions-implementation

<a id="s27"></a>**S27** (Primary) NYC DOHMH, *Medicaid Coverage Changes* (work requirements Jan 2027; six-month renewals). https://www.nyc.gov/site/doh/health/health-topics/medicaid-coverage-changes.page; (Secondary) National Health Care for the Homeless Council, letter to states on H.R. 1, https://nhchc.org/wp-content/uploads/2026/07/Updated-Letter-to-States.pdf

<a id="s28"></a>**S28** (Secondary) National Alliance to End Homelessness, summary of EO 14321 *Ending Crime and Disorder on America's Streets* (July 24, 2025). https://endhomelessness.org/wp-content/uploads/2025/11/0731-Ending-Crime-Executive-Order-Summary-2.pdf

## State health and housing programs
<a id="s29"></a>**S29** (Primary) Office of the Governor, Social Care Networks announcement, Aug 7, 2024 (HWCLI named Long Island lead). https://www.governor.ny.gov/news/governor-hochul-announces-500-million-new-social-care-networks-program-deliver-social-services

<a id="s30"></a>**S30** (Secondary) Barclay Damon, "CMS Approves NYS 1115 Waiver Amendment: New York Health Equity Reform" (Jan 9, 2024 – Mar 31, 2027). https://barclaydamon.com/alerts/cms-approves-nys-1115-waiver-amendment-new-york-health-equity-reform

<a id="s34"></a>**S34** (Primary) NYS HCR, announcement of New York Housing Search (HousingSearch.ny.gov), Oct 9, 2025. https://hcr.ny.gov/news/new-york-state-homes-and-community-renewal-announces-new-website-new-yorkers-searching

<a id="s40"></a>**S40** (Secondary) Legal Services of Long Island, *Suffolk DSS Rental Supplement Program* flyers (Jan 2024; households pay 30% of income). https://legalservicesli.org/wp-content/uploads/2024/01/RSP-Flyer-families-01.03.24-1.pdf

## Organizations named in the plan
<a id="s31"></a>**S31** (Primary) Technical Assistance Collaborative, About. https://www.tacinc.org/about/ (Boston-based national nonprofit)

<a id="s33"></a>**S33** (Secondary) NY Trust / Long Island Press on the Long Island Zoning Atlas (launched Dec 2023; part of the New York Zoning Atlas; multifamily as-of-right on under 10% of land). https://thenytrust.org/news/the-first-ever-long-island-zoning-atlas-comes-to-life; https://longislandpress.com/2025/06/05/long-island-zoning-atlas-finds-multi-family-largely-absent; https://zoningatlas.org/new-york

<a id="s41"></a>**S41** (Secondary) Pax Christi Hospitality Center listing (255 Oakland Ave, Port Jefferson; 25-bed men's emergency shelter), https://www.longisland.com/business/pax-christie-hospitality-center.html; *Long Island Press*, Nov 4, 2024, https://longislandpress.com/2024/11/04/homeless-population-li

**S43** (Search, no match) "Long Island Housing Coalition": web searches on Oct 14, 2026 returned no organization by this name.
