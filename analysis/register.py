"""Action-step register for the NY-603 Strategic Plan 2026-2030.

One row per action-step line in the published plan: 68 checkmarked or labeled
steps and 13 sub-bullets (81 rows). Text is verbatim from Appendix A. The tags
are the reviewer's coding and are defined below.

  type        H = Year One "Housing" step, E = Year One "Education" step,
              step = other checkmarked step, sub = sub-bullet under the step above
  control     who has to act for the step to happen:
                CoC       the CoC or its lead agency can do it directly
                          (convene, map, train, write standards, run a campaign)
                partner   needs a named partner to act (DSS, PHAs, hospitals,
                          libraries, employers, funders, courts)
                policy    needs a change in law, regulation, zoning or court rules
  funding     Y if the step cannot happen without new or redirected money
  measure     how the step is written:
                target       has a number or a yes/no completion standard
                deliverable  produces a discrete product (guide, map, list, pilot)
                directional  increase/expand/reduce/improve with no quantity

Usage: python3 analysis/register.py   (writes appendix/C-action-step-register.csv)
"""
import csv
import os

# (id, goal, horizon, type, page, text, themes, control, funding, measure, overlaps, flags)
ROWS = [
    # ---------------- Goal 1: Reduce Unsheltered Homelessness ----------------
    ("G1-Y1-H", 1, "Y1", "H", 4,
     "Quantify the need for more affordable housing on Long Island through a review of currently existing data reports. Analyze the gaps and opportunities for impact, including opportunities for coordination with local public housing authorities.",
     "data-analysis;PHA-vouchers", "CoC", "N", "deliverable", "",
     "Overlaps the annual gaps analysis a CoC must already do (24 CFR 578.7(c)(3)); general housing-need study sits under a goal about unsheltered homelessness"),
    ("G1-Y1-E", 1, "Y1", "E", 4,
     "Develop an information campaign and distribute materials to raise awareness of housing and resources available to people experiencing unsheltered homelessness. Tailor informational materials to key audiences, including people seeking housing and services, people who may be reluctant to seek services, public officials, private funders, business owners, and everyday citizens.",
     "communications-campaign", "CoC", "N", "deliverable", "G2-Y1-E;G2-Y1-Ea;G1-Y25-05;G4-Y1-E;G6-Y1-E",
     "One of five separate campaigns/trainings; no audience-level reach target"),
    ("G1-Y1-01", 1, "Y1", "step", 5,
     "Remove barriers to crisis housing for special populations that currently struggle to access this resource. This includes people with pets, people without documentation, and people with untreated behavioral and mental health issues. In year one, create and update the CoC Standards of Care, and analyze the reasons people living unsheltered are not taking offered shelter opportunities.",
     "low-barrier-access;standards-training;immigration-status;data-analysis", "partner", "Y", "directional", "G3-Y25-08;G3-Y1-H;G5-Y25-02",
     "'people without documentation' is ambiguous (ID documents vs immigration status); written standards are already required (24 CFR 578.7(a)(9)), so 'create' should read 'update'"),
    ("G1-Y1-01a", 1, "Y1", "sub", 5,
     "A potential resource can be found in current housing advocate groups like the Long Island Housing Coalition.",
     "partners", "CoC", "N", "directional", "",
     "Verified Oct 16, 2026: a coalition of Long Island nonprofits chaired by ERASE Racism, the group the draft named; separate from LICH. Name a contact"),
    ("G1-Y1-02", 1, "Y1", "step", 5,
     "Create a system map of the location and extent of supportive services on Long Island, including mental health care, substance use treatment, employment assistance, financial management, transportation, and food assistance.",
     "system-map", "CoC", "N", "deliverable", "G5-Y1-01",
     "Second system map in G5-Y1-01; build once"),
    ("G1-Y1-02a", 1, "Y1", "sub", 5,
     "Identify program entry restrictions and opportunities to streamline access.",
     "low-barrier-access;data-analysis", "CoC", "N", "deliverable", "G1-Y1-01", ""),
    ("G1-Y25-01", 1, "Y2-5", "step", 5,
     "Reduce inflow into unsheltered homelessness by increasing flexible prevention options (grants; loans).",
     "prevention-diversion;funding", "partner", "Y", "directional", "G6-Y25-02;G6-Y1-02", ""),
    ("G1-Y25-02", 1, "Y2-5", "step", 5,
     "Pilot a program that diverts shelter fees into savings accounts for tenants.",
     "shelter-fees-savings", "policy", "Y", "deliverable", "G3-Y25-01b;G4-Y25-07",
     "Same pilot appears in G3 and G4; draft cited an existing local model (Pax Christi) as a 'promising practice'"),
    ("G1-Y25-03", 1, "Y2-5", "step", 5,
     "Increase programs like the housing choice voucher (available through public housing authorities) to combine deeply affordable housing options with supportive services to help vulnerable neighbors remain housed.",
     "PHA-vouchers;health-behavioral", "partner", "Y", "directional", "G2-Y25-05",
     "Voucher supply is set by HUD appropriations and PHA decisions, not the CoC"),
    ("G1-Y25-04", 1, "Y2-5", "step", 5,
     "Remove barriers to crisis housing through a pilot program that reduces tenant shelter fees. Analyze if this improves housing placement and stability outcomes.",
     "shelter-fees-savings", "policy", "Y", "deliverable", "G3-Y25-01;G3-Y25-01a",
     "One of only two steps that name outcomes to test (with G3-Y25-01a), which it duplicates"),
    ("G1-Y25-05", 1, "Y2-5", "step", 5,
     "Focus the education campaign on specific resources and access paths for households with different citizenship statuses across all parts of the homeless response system (front door, crisis housing, permanent housing, etc.).",
     "communications-campaign;immigration-status", "CoC", "N", "deliverable", "G1-Y1-E",
     "Federal eligibility rules for HUD-assisted housing are in flux (Feb 2026 proposed rule); materials need legal review before release"),
    ("G1-Y25-06", 1, "Y2-5", "step", 5,
     "Expand day center/drop-in program options for people experiencing unsheltered homelessness, ensuring they are geographically spaced across Long Island and include evening, weekend, and holiday hours. Libraries are an opportunity to explore.",
     "hours-access;libraries;low-barrier-access", "partner", "Y", "directional", "G2-Y25-07;G3-Y25-02", ""),
    ("G1-Y25-07", 1, "Y2-5", "step", 5,
     "Pilot a program providing free transportation to support access to supportive services for people experiencing unsheltered homelessness",
     "transportation", "partner", "Y", "deliverable", "G4-Y25-04;G5-Y25-04", ""),

    # ------- Goal 2: Optimize Coordination in the Homelessness Response System -------
    ("G2-Y1-H", 2, "Y1", "H", 6,
     "Create a regional discharge planning guide in partnership with hospitals, foster care, and jails.",
     "discharge-planning", "CoC", "N", "deliverable", "G5-Y1-02",
     "Duplicates G5-Y1-02 (regional triage and discharge planning guide)"),
    ("G2-Y1-E", 2, "Y1", "E", 6,
     "Develop an education campaign to foster community compassion and political will, thereby reducing stigma and increasing housing opportunities for individuals exiting homelessness, allowing them to live in neighborhoods of their choice.",
     "communications-campaign", "CoC", "N", "deliverable", "G1-Y1-E", ""),
    ("G2-Y1-Ea", 2, "Y1", "sub", 6,
     "Create easy-to-read information cards/bulletins to increase access to information for the public – and housing service providers - on how to access and navigate the homeless response system. Engage the library system in information distribution.",
     "communications-campaign;libraries", "CoC", "N", "deliverable", "G1-Y1-E;G3-Y1-E", ""),
    ("G2-Y1-01", 2, "Y1", "step", 6,
     "Establish mechanisms to center the voices of people with lived experience in the planning and decision-making to drive solutions.",
     "lived-experience-peers", "CoC", "N", "directional", "G3-Y25-03;G3-Y25-05;G5-Y25-06", ""),
    ("G2-Y1-01a", 2, "Y1", "sub", 6,
     "Identify important decision-making places and spaces and expand access and input from people with lived experience. Train and empower youth in these spaces to develop housing and service programs tailored to their unique needs, while building their planning skills.",
     "lived-experience-peers;youth", "CoC", "N", "directional", "",
     "Only youth-specific step in the plan"),
    ("G2-Y25-01", 2, "Y2-5", "step", 6,
     "Create a centralized, searchable database for housing openings on Long Island with landlord and unit information organized in a way that is easy to read and understand.",
     "landlords-units;data-analysis", "CoC", "Y", "deliverable", "",
     "NYS HCR already runs a free listing service (HousingSearch.ny.gov); consider partnering instead of building"),
    ("G2-Y25-02", 2, "Y2-5", "step", 6,
     "Remove barriers to affordable housing construction, including zoning restrictions that make the development of affordable housing challenging (e.g., height restrictions, how many unrelated adults can live together, etc.).",
     "zoning-development;advocacy", "policy", "N", "directional", "G6-Y25-01", "Zoning is set by towns and villages"),
    ("G2-Y25-02a", 2, "Y2-5", "sub", 6,
     "Build coalitions of stakeholders to address zoning expansion concerns. Potential resources include a relationship (asset) map and the Regional Plan Association's Zoning Atlas, which can be used to identify targets.",
     "zoning-development", "CoC", "N", "deliverable", "",
     "The Long Island Zoning Atlas is a multi-partner project (part of the NY Zoning Atlas, launched Dec 2023); credit it accurately"),
    ("G2-Y25-03", 2, "Y2-5", "step", 6,
     "Create and enact a legislative advocacy campaign to support protections against rent increases for tenants on Long Island.",
     "advocacy", "policy", "N", "deliverable", "",
     "Lobbying costs are unallowable on federal awards (2 CFR 200.450); fund from unrestricted dollars. Good Cause Eviction requires local opt-in outside NYC"),
    ("G2-Y25-03a", 2, "Y2-5", "sub", 6,
     "Enlist Town Boards, Faith-Based Organizations, and informal leaders.",
     "advocacy", "partner", "N", "directional", "", ""),
    ("G2-Y25-04", 2, "Y2-5", "step", 6,
     "Revitalize abandoned buildings to increase affordable housing options.",
     "zoning-development;landlords-units", "policy", "Y", "directional", "", ""),
    ("G2-Y25-05", 2, "Y2-5", "step", 7,
     "Examine housing and services from a regional perspective, building coordination across nonprofit organizations and public housing authorities to leverage each other's strengths, de-duplicate efforts, and minimize working in silos.",
     "PHA-vouchers;coordination", "partner", "N", "directional", "G1-Y25-03", ""),
    ("G2-Y25-06", 2, "Y2-5", "step", 7,
     "Build partnerships with faith-based communities for volunteers/donors (e.g., rent arrear assistance, furniture assistance, etc.).",
     "funding;prevention-diversion", "partner", "N", "directional", "G6-Y1-01", ""),
    ("G2-Y25-07", 2, "Y2-5", "step", 7,
     "Conduct a feasibility study on the creation of one-stop service hubs that bring service providers to one location for ease of access. Explore libraries as key partners in this work.",
     "libraries;hours-access", "CoC", "N", "deliverable", "G1-Y25-06", ""),

    # ---------------- Goal 3: Transform the Shelter System ----------------
    ("G3-Y1-H", 3, "Y1", "H", 7,
     "Create CoC Community Standards and universal training for Case Managers on housing-focused problem-solving, trauma-informed engagement, and health network integration.",
     "standards-training", "CoC", "N", "deliverable", "G1-Y1-01;G5-Y25-02;G5-Y25-07",
     "Third separate 'standards' item; labeled Housing but is staff training"),
    ("G3-Y1-E", 3, "Y1", "E", 7,
     "Simplify the information and access steps to enter emergency shelter, providing all eligibility criteria and necessary documentation upfront.",
     "communications-campaign;low-barrier-access;DSS", "partner", "N", "directional", "G2-Y1-Ea",
     "Eligibility is set by DSS under state rules; needs DSS sign-off"),
    ("G3-Y1-01", 3, "Y1", "step", 7,
     "Initiate a conversation with the Office of Temporary Disability Assistance (OTDA) to explore the possibility of changing OTDA regulations that require shelter guest payment percentages.",
     "shelter-fees-savings;DSS;advocacy", "policy", "N", "deliverable", "G3-Y25-01",
     "Agency name is the Office of Temporary and Disability Assistance. The governing rules (18 NYCRR 491.11 and 900.18, budgeted under Part 352) require residents with income to pay toward the actual cost of care; they do not state a percentage"),
    ("G3-Y1-02", 3, "Y1", "step", 7,
     "Expand the transition period to exit when shelter residents obtain additional income to prevent loss of housing and income.",
     "shelter-fees-savings;employment-income", "partner", "N", "directional", "G4-Y1-02",
     "Duplicates G4-Y1-02. Pulls against reducing length of stay (HUD SPM 1) unless tied to a move-out plan"),
    ("G3-Y25-01", 3, "Y2-5", "step", 7,
     "Work with DSS to decrease the required guest payment from 80% of income to 50% of income.",
     "shelter-fees-savings;DSS", "policy", "N", "target", "G3-Y1-01;G1-Y25-04",
     "Source of the 80% figure is not cited"),
    ("G3-Y25-01a", 3, "Y2-5", "sub", 8,
     "Pilot a program to test outcomes for families paying less than 80% of their income.",
     "shelter-fees-savings", "policy", "Y", "deliverable", "G1-Y25-04", "Duplicates G1-Y25-04"),
    ("G3-Y25-01b", 3, "Y2-5", "sub", 8,
     "Pilot a program to divert shelter fees into a savings account.",
     "shelter-fees-savings", "policy", "Y", "deliverable", "G1-Y25-02;G4-Y25-07", "Duplicates G1-Y25-02"),
    ("G3-Y25-02", 3, "Y2-5", "step", 8,
     "Expand shelter access to weekend and evening hours.",
     "hours-access;DSS", "partner", "Y", "directional", "G1-Y25-06",
     "State whether this means DSS intake/placement hours or shelter day-time access; Code Blue hours rules already apply (18 NYCRR 304.1)"),
    ("G3-Y25-03", 3, "Y2-5", "step", 8,
     "Develop a continuous quality improvement model that creates opportunities for people experiencing homelessness to provide feedback on shelter system experience.",
     "lived-experience-peers;data-analysis", "CoC", "N", "deliverable", "G2-Y1-01", ""),
    ("G3-Y25-04", 3, "Y2-5", "step", 8,
     "Expand housing navigation specialists.",
     "landlords-units", "partner", "Y", "directional", "", ""),
    ("G3-Y25-05", 3, "Y2-5", "step", 8,
     "Recruit and support staff with lived experience to be peer mentors across the crisis response system.",
     "lived-experience-peers", "partner", "Y", "directional", "G5-Y25-06", "Overlaps G5-Y25-06 peer support program"),
    ("G3-Y25-06", 3, "Y2-5", "step", 8,
     "Analyze safety and security concerns in emergency shelters and create a roadmap to address them.",
     "data-analysis", "CoC", "N", "deliverable", "", ""),
    ("G3-Y25-07", 3, "Y2-5", "step", 8,
     "Streamline communication between housing authorities and shelter workers.",
     "PHA-vouchers;DSS", "partner", "N", "directional", "",
     "Sub-bullet names DSS, which is not a housing authority; clarify which partner is meant"),
    ("G3-Y25-07a", 3, "Y2-5", "sub", 8,
     "Start with more communication from DSS and more guidance/enforcement from DSS.",
     "DSS", "partner", "N", "directional", "", ""),
    ("G3-Y25-08", 3, "Y2-5", "step", 8,
     "Expand the number of low-barrier shelters to accommodate more people with medical and behavioral health needs, pets, and non-traditional family compositions.",
     "low-barrier-access;health-behavioral", "partner", "Y", "directional", "G1-Y1-01;G5-Y25-03", "Duplicates G1-Y1-01 populations"),

    # --------- Goal 4: Improve Access to Employment and Economic Opportunity ---------
    ("G4-Y1-H", 4, "Y1", "H", 8,
     "Increase access to formal workforce development programs that teach marketable skills for jobs paying above minimum wage and offering benefits.",
     "employment-income", "partner", "N", "directional", "",
     "Labeled Housing but is an employment step"),
    ("G4-Y1-E", 4, "Y1", "E", 8,
     "Co-create an employment program messaging campaign with individuals experiencing homelessness, focusing on motivation to work and common challenges to starting new employment, such as fear of benefit loss.",
     "communications-campaign;employment-income;lived-experience-peers", "CoC", "N", "deliverable", "G1-Y1-E", ""),
    ("G4-Y1-01", 4, "Y1", "step", 8,
     "Create a partnership with a legal services program to assist people with criminal records with expungement when possible.",
     "employment-income;legal-services", "partner", "N", "deliverable", "",
     "NY mostly seals records (Clean Slate Act; CPL 160.59); expungement applies to certain marijuana convictions (MRTA). Indented under the Education icon in the PDF"),
    ("G4-Y1-02", 4, "Y1", "step", 8,
     "Work with shelters to increase the amount of time shelter guests may remain in shelter after obtaining additional income.",
     "shelter-fees-savings;employment-income", "partner", "N", "directional", "G3-Y1-02", "Duplicates G3-Y1-02"),
    ("G4-Y25-01", 4, "Y2-5", "step", 9,
     "Create a database of career opportunities with requirements and wage ranges",
     "employment-income;data-analysis", "CoC", "Y", "deliverable", "", ""),
    ("G4-Y25-01a", 4, "Y2-5", "sub", 9,
     "Identify strengths-based training programs that also teach soft skills",
     "employment-income", "CoC", "N", "deliverable", "", ""),
    ("G4-Y25-02", 4, "Y2-5", "step", 9,
     "Cultivate employers who offer higher wages, benefits, paid apprenticeships, and do not require background checks, etc. Host quarterly job fairs to connect individuals experiencing homelessness with job opportunities.",
     "employment-income", "partner", "N", "target", "", "Quarterly frequency is the only cadence target in the plan"),
    ("G4-Y25-03", 4, "Y2-5", "step", 9,
     "Pilot in-shelter workforce development program that pays guests for their time and labor and does not count this income toward the calculation of shelter contribution.",
     "employment-income;shelter-fees-savings", "policy", "Y", "deliverable", "",
     "Excluding earned income from the shelter contribution needs a basis in Part 352 budgeting rules or a DSS/OTDA-approved disregard"),
    ("G4-Y25-04", 4, "Y2-5", "step", 9,
     "Create and fund a package of new employment transitional support services, such as childcare, transportation, flexible funding to pay for uniforms and other work needs, and benefit navigators to discuss options to avoid/bridge the \"benefit cliff\".",
     "employment-income;transportation", "partner", "Y", "deliverable", "G5-Y25-04;G1-Y25-07", ""),
    ("G4-Y25-05", 4, "Y2-5", "step", 9,
     "Increasing enforcement of existing statutes prohibiting source-of-income discrimination.",
     "landlords-units;advocacy", "partner", "N", "directional", "G6-Y25-05",
     "Enforcement sits with the NYS Division of Human Rights, the Attorney General and county human rights commissions. People v Commons West (3d Dept, Mar 5, 2026) held the state law unconstitutional as applied to Section 8 vouchers; on appeal to the Court of Appeals (APL-2026-00035)"),
    ("G4-Y25-06", 4, "Y2-5", "step", 9,
     "Expand diversion and reentry programs. Pilot a mandatory diversion program at eviction proceedings.",
     "prevention-diversion;advocacy", "policy", "Y", "deliverable", "G6-Y25-02",
     "A prevention step placed under the employment goal; a mandatory court diversion needs the Unified Court System"),
    ("G4-Y25-07", 4, "Y2-5", "step", 9,
     "Pilot a savings enrollment program for shelter guests.",
     "shelter-fees-savings", "partner", "Y", "deliverable", "G1-Y25-02;G3-Y25-01b", "Third savings pilot"),
    ("G4-Y25-08", 4, "Y2-5", "step", 9,
     "Expand services related to furthering education (higher ed, vocational, etc.) for people staying in emergency shelters.",
     "employment-income", "partner", "Y", "directional", "", "Added after the August 2025 draft"),

    # ---------------- Goal 5: Improve Access to Healthcare and Services ----------------
    ("G5-Y1-H", 5, "Y1", "H", 9,
     "Establish a working group to explore the needs of people experiencing homelessness with significant health needs and make recommendations for a blend of housing and services to serve people with significant health challenges (senior citizens, people with chronic health and behavioral health needs, medically fragile, severe disability).",
     "health-behavioral;older-adults-55plus", "CoC", "N", "deliverable", "G5-Y25-01", ""),
    ("G5-Y1-Ha", 5, "Y1", "sub", 10,
     "Creation of a medical respite program, transport/enrollment in ambulatory care/Federally Qualified Health Centers (FQHCs), hospital-housing partnerships to reduce healthcare costs, etc.",
     "health-behavioral", "partner", "Y", "deliverable", "", ""),
    ("G5-Y1-E", 5, "Y1", "E", 10,
     "Ensure every CoC-funded housing program has an SSI/SSDI, Outreach, Access and Recovery (SOAR)-trained employee.",
     "health-behavioral;benefits", "CoC", "N", "target", "",
     "The plan's clearest measurable step (100% of CoC-funded housing programs). Official name: SSI/SSDI Outreach, Access, and Recovery"),
    ("G5-Y1-01", 5, "Y1", "step", 10,
     "Create a system map highlighting shelters and housing programs with medical supports available, clinics and healthcare programs with homelessness triage and services, and programs effectively using Medicaid to support housing-related social needs.",
     "system-map;health-behavioral", "CoC", "N", "deliverable", "G1-Y1-02", "Duplicates G1-Y1-02"),
    ("G5-Y1-02", 5, "Y1", "step", 10,
     "Create a regional triage and discharge planning guide in partnership with systems of care.",
     "discharge-planning;health-behavioral", "CoC", "N", "deliverable", "G2-Y1-H", "Duplicates G2-Y1-H"),
    ("G5-Y1-02a", 5, "Y1", "sub", 10,
     "Priority populations include high utilizers of emergency departments, medical discharges for people experiencing homelessness, young adults aging out of foster care, and adults released from jail without housing.",
     "discharge-planning;youth", "CoC", "N", "directional", "", ""),
    ("G5-Y25-01", 5, "Y2-5", "step", 10,
     "Establish a working group to assess the needs of adults 55+ who are at risk of homelessness. Develop recommendations for homelessness prevention and crisis housing programs that address the specific needs of this population.",
     "older-adults-55plus;prevention-diversion", "CoC", "N", "deliverable", "G6-Y25-03;G5-Y1-H",
     "Second working group; 55+ is the fastest-growing PIT group (about 670 in 2024 to 825 in 2025), which argues for Year One"),
    ("G5-Y25-02", 5, "Y2-5", "step", 10,
     "Create CoC-wide standards for minimum supportive services required to be provided in conjunction with housing programs.",
     "standards-training", "CoC", "N", "deliverable", "G1-Y1-01;G3-Y1-H", ""),
    ("G5-Y25-03", 5, "Y2-5", "step", 10,
     "Add crisis housing/beds for people with serious mental illness, medically fragile people, and other people who need options with specialized healthcare services.",
     "health-behavioral;low-barrier-access", "partner", "Y", "directional", "G3-Y25-08", ""),
    ("G5-Y25-04", 5, "Y2-5", "step", 10,
     "Increase access to supportive services that include affordable childcare, transportation, employment assistance, financial management, landlord mediation, legal aid, etc.",
     "transportation;employment-income;legal-services", "partner", "Y", "directional", "G4-Y25-04;G1-Y25-07", ""),
    ("G5-Y25-05", 5, "Y2-5", "step", 10,
     "Increase housing options for people who are actively using substances.",
     "health-behavioral;low-barrier-access", "partner", "Y", "directional", "",
     "FY2026 CoC NOFO conditions and EO 14321 restrict harm-reduction approaches in federally funded projects; likely needs non-federal money"),
    ("G5-Y25-06", 5, "Y2-5", "step", 10,
     "Create a peer support program for people transitioning from interim/transitional housing into permanent housing.",
     "lived-experience-peers", "partner", "Y", "deliverable", "G3-Y25-05", ""),
    ("G5-Y25-07", 5, "Y2-5", "step", 10,
     "Self-paced on-demand training program to healthcare and housing placement personnel regarding the unique challenges faced by individuals and families experiencing homelessness.",
     "standards-training", "CoC", "N", "deliverable", "G3-Y1-H", ""),
    ("G5-Y25-08", 5, "Y2-5", "step", 10,
     "Identify resources for expansion of street medicine programs to increase access to healthcare for people experiencing unsheltered homelessness.",
     "health-behavioral", "partner", "Y", "directional", "",
     "Added after the August 2025 draft. The lead agency operates a street medicine program, so resource decisions should go through the CoC conflict-of-interest process"),

    # ---- Goal 6: Sustain and Diversify Funding Streams and Resources to Better Align with Local Needs ----
    ("G6-Y1-H", 6, "Y1", "H", 11,
     "Create a flexible funding pool for landlord incentive payments.",
     "landlords-units;funding", "partner", "Y", "deliverable", "", "Needs a named funder and administrator for a Year One start"),
    ("G6-Y1-E", 6, "Y1", "E", 11,
     "Launch a tenant rights training to help prevent people from becoming homeless.",
     "communications-campaign;prevention-diversion", "partner", "N", "deliverable", "", "Draft named the American Bar Association as partner"),
    ("G6-Y1-01", 6, "Y1", "step", 11,
     "Compile a list of private funders, their funding priorities, and funding cycles.",
     "funding", "CoC", "N", "deliverable", "", ""),
    ("G6-Y1-01a", 6, "Y1", "sub", 11,
     "Identify and apply for private foundation funding to support CoC-wide prevention needs.",
     "funding;prevention-diversion", "CoC", "N", "deliverable", "", ""),
    ("G6-Y1-02", 6, "Y1", "step", 11,
     "Identify a subset of state and local partners willing to invest in eviction prevention programs. Explore the library system as an access point for this service.",
     "prevention-diversion;funding;libraries", "partner", "Y", "deliverable", "G1-Y25-01;G6-Y25-02", ""),
    ("G6-Y25-01", 6, "Y2-5", "step", 11,
     "Increase deeply affordable housing stock – housing that is affordable to people making less than 25% of Area Median Income (AMI). Launch a technical assistance program to recruit and assist developers with moving forward with housing development.",
     "zoning-development;funding", "policy", "Y", "directional", "G2-Y25-02",
     "HUD's extremely-low-income line is 30% AMI and most gap data use it; 25% limits comparability"),
    ("G6-Y25-02", 6, "Y2-5", "step", 11,
     "Increase access to prevention funding to cover the cost of rental arrears and other one-time needs for people at-risk of homelessness.",
     "prevention-diversion;funding", "partner", "Y", "directional", "G1-Y25-01", "Duplicates G1-Y25-01"),
    ("G6-Y25-03", 6, "Y2-5", "step", 11,
     "Create a prevention strategy aimed at aging adults 55+ who are at-risk of homelessness.",
     "older-adults-55plus;prevention-diversion", "CoC", "N", "deliverable", "G5-Y25-01", "Duplicates G5-Y25-01"),
    ("G6-Y25-04", 6, "Y2-5", "step", 11,
     "Simplify eligibility and remove administrative barriers to rental assistance options to increase exits to permanent housing (e.g., DSS restrictions on apartment types, etc.).",
     "DSS;landlords-units", "partner", "N", "directional", "", ""),
    ("G6-Y25-05", 6, "Y2-5", "step", 11,
     "Work with landlords to improve fair housing practices, address discrimination, and increase the pool of available units.",
     "landlords-units", "partner", "N", "directional", "G4-Y25-05", ""),
    ("G6-Y25-06", 6, "Y2-5", "step", 11,
     "Change the rapid rehousing rental assistance model to provide a more gradual decrease in rent payments.",
     "rapid-rehousing", "CoC", "N", "directional", "",
     "Wording is inverted: in RRH the subsidy decreases and the tenant's share rises. Likely means a more gradual step-down in assistance. RRH rules sit in the CoC written standards"),
    ("G6-Y25-07", 6, "Y2-5", "step", 11,
     "Increase training in funding opportunities, work to align funder goals and priorities, and encourage leveraging/match for sustainability.",
     "funding", "CoC", "N", "directional", "", "Added after the August 2025 draft"),
]

# Proposed consolidation (REVIEW.md section 11): every row maps to one initiative.
INITIATIVE_NAMES = {
    1: "Shelter income, fees and savings reform",
    2: "One standards-and-training framework",
    3: "One system map and front-door information",
    4: "Health, discharge and triage",
    5: "Low-barrier crisis capacity and access",
    6: "Older adults (55+)",
    7: "Prevention and eviction diversion",
    8: "Landlords, units and exits to housing",
    9: "Income, employment and benefits",
    10: "Housing supply and policy advocacy",
    11: "Lived experience and peer workforce",
    12: "One communications strategy",
    13: "Funding diversification and federal contingency",
}
INITIATIVE = {
    "G1-Y1-H": 10, "G1-Y1-E": 12, "G1-Y1-01": 5, "G1-Y1-01a": 5, "G1-Y1-02": 3, "G1-Y1-02a": 3,
    "G1-Y25-01": 7, "G1-Y25-02": 1, "G1-Y25-03": 10, "G1-Y25-04": 1, "G1-Y25-05": 12,
    "G1-Y25-06": 5, "G1-Y25-07": 5,
    "G2-Y1-H": 4, "G2-Y1-E": 12, "G2-Y1-Ea": 3, "G2-Y1-01": 11, "G2-Y1-01a": 11,
    "G2-Y25-01": 8, "G2-Y25-02": 10, "G2-Y25-02a": 10, "G2-Y25-03": 10, "G2-Y25-03a": 10,
    "G2-Y25-04": 10, "G2-Y25-05": 10, "G2-Y25-06": 7, "G2-Y25-07": 5,
    "G3-Y1-H": 2, "G3-Y1-E": 3, "G3-Y1-01": 1, "G3-Y1-02": 1,
    "G3-Y25-01": 1, "G3-Y25-01a": 1, "G3-Y25-01b": 1, "G3-Y25-02": 5, "G3-Y25-03": 11,
    "G3-Y25-04": 8, "G3-Y25-05": 11, "G3-Y25-06": 5, "G3-Y25-07": 8, "G3-Y25-07a": 8, "G3-Y25-08": 5,
    "G4-Y1-H": 9, "G4-Y1-E": 12, "G4-Y1-01": 9, "G4-Y1-02": 1,
    "G4-Y25-01": 9, "G4-Y25-01a": 9, "G4-Y25-02": 9, "G4-Y25-03": 1, "G4-Y25-04": 9,
    "G4-Y25-05": 8, "G4-Y25-06": 7, "G4-Y25-07": 1, "G4-Y25-08": 9,
    "G5-Y1-H": 4, "G5-Y1-Ha": 4, "G5-Y1-E": 9, "G5-Y1-01": 3, "G5-Y1-02": 4, "G5-Y1-02a": 4,
    "G5-Y25-01": 6, "G5-Y25-02": 2, "G5-Y25-03": 5, "G5-Y25-04": 9, "G5-Y25-05": 5,
    "G5-Y25-06": 11, "G5-Y25-07": 2, "G5-Y25-08": 5,
    "G6-Y1-H": 8, "G6-Y1-E": 7, "G6-Y1-01": 13, "G6-Y1-01a": 13, "G6-Y1-02": 7,
    "G6-Y25-01": 10, "G6-Y25-02": 7, "G6-Y25-03": 6, "G6-Y25-04": 8, "G6-Y25-05": 8,
    "G6-Y25-06": 2, "G6-Y25-07": 13,
}

FIELDS = ["id", "goal", "horizon", "type", "page", "text", "themes", "control",
          "new_funding_needed", "measure", "overlaps_with", "review_flags",
          "proposed_initiative"]


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ids = [r[0] for r in ROWS]
    assert len(ids) == len(set(ids)), "duplicate row id"
    missing = [i for i in ids if i not in INITIATIVE]
    extra = [i for i in INITIATIVE if i not in ids]
    assert not missing and not extra, f"initiative map mismatch: missing={missing} extra={extra}"

    out = os.path.join(here, "..", "appendix", "C-action-step-register.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(FIELDS)
        for row in ROWS:
            n = INITIATIVE[row[0]]
            w.writerow(list(row) + [f"{n}. {INITIATIVE_NAMES[n]}"])
    print(f"wrote {len(ROWS)} rows to {os.path.normpath(out)}")

    md = os.path.join(here, "..", "appendix", "C-action-step-register.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("# Appendix C — Action-step register\n\n")
        f.write("Generated by `analysis/register.py` from the same rows as "
                "[`C-action-step-register.csv`](C-action-step-register.csv). One row per action-step "
                "line in the plan (68 steps + 13 sub-bullets). Step text is in the CSV and in "
                "[Appendix A](A-plan-transcription.md). Tag definitions are at the top of `register.py`.\n\n")
        f.write("Columns: **Ctl** = who must act (CoC / partner / policy); **$** = needs new money; "
                "**Measure** = target / deliverable / directional; **Init.** = proposed initiative "
                "(see REVIEW.md section 11).\n\n")
        for g in range(1, 7):
            f.write(f"## Goal {g}\n\n| ID | Ctl | $ | Measure | Init. | Overlaps | Review flag |\n"
                    "|---|---|---|---|---|---|---|\n")
            for r in ROWS:
                if r[1] != g:
                    continue
                flag = r[11].replace("|", "/") or "—"
                ov = r[10].replace(";", ", ") or "—"
                f.write(f"| {r[0]} | {r[7]} | {r[8]} | {r[9]} | {INITIATIVE[r[0]]} | {ov} | {flag} |\n")
            f.write("\n")
        f.write("## Proposed initiatives\n\n| # | Initiative | Rows |\n|---|---|---|\n")
        for n, name in INITIATIVE_NAMES.items():
            members = [i for i in ids if INITIATIVE[i] == n]
            f.write(f"| {n} | {name} | {', '.join(members)} |\n")
    print(f"wrote {os.path.normpath(md)}")


if __name__ == "__main__":
    main()
