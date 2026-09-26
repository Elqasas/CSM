# Customer Health & Account Management Platform — Full Specification (ACM Version, v2)

> **What's new in v2 (ACM Version):** a unified **Deal** entity (Renewal / Upsell / Cross-sell / New Business) with negotiation rounds and a sales pipeline; Kanban board and weighted forecast; stale-deal alerts; quarterly ACM targets; mandatory loss reasons; a new **Admin** (`is_admin`) flag; configurable settings; Health Score contact factors now counted from CSM activities only. See Section 7A and the change log in Section 11.

## 1. Vision Overview

An internal web platform for managing the health and relationships of dPhish's ~60 client accounts. Its primary goal is to **prioritize daily follow-up** for Account Managers (not long-term churn prediction).

Built with a modular architecture that allows adding integrations (Jira, Teams, Outlook) and a chatbot later without rebuilding the foundation.

**Technology:**
- A real web app: Python backend + a real database, from day one
- Excel is used **for the initial seed import only** — after the first upload, all updates happen from within the platform
- Hosting will be decided later (won't affect the code design)

**Out of current scope (future vision):**
- Integration with Teams, Outlook, Jira
- Chatbot
- The architecture is left open for these integrations (example: a `Jira Ticket ID` field is ready and empty from now)

---

## 2. Users (dPhish Staff)

A table **completely separate** from the Contacts table. Users are dPhish employees who have login access (independent login credentials, not SSO). Contacts are people at the client's organization — they have no access to the system at all.

```
Users: id, name, email, password_hash, role, is_senior (boolean, default false), is_admin (boolean, default false)
```

### Roles (via role + is_senior + is_admin)

| Role | Scope | Permission | Notes |
|---|---|---|---|
| **CSM** (`is_senior=false`) | Assigned accounts only (`WHERE csm_id = user.id`) | Full edit: Health, Activity, Contacts, Deals | Cannot see colleagues' accounts |
| **CSM Senior** (`is_senior=true`) | All accounts | Full edit, no scope restriction | Can view and act on any account |
| **ACM** | Assigned accounts only | Deals (Renewal, Upsell, Cross-sell, New Business), pipeline, renewal-related tasks — **never sees Health Score** | Strict separation, no signal about account health whatsoever (unless `is_admin`) |
| **Manager** | All accounts | View-only, **with two exceptions**: can edit Settings and set ACM Targets | |
| **Admin** (`is_admin=true`, on top of any role) | All accounts | **Super Admin — all permissions without exception** | Includes user/role management, account assignment, settings. **Admin overrides role separation**: an ACM with `is_admin` sees Health Score |

### Admin

`is_admin` is a flag that can be set on any role (not a separate role), since in a small team the same person often holds an operational role and administers the system. Admin is responsible for managing users and roles (creating users, setting `is_senior` / `is_admin`), assigning CSM/ACM to accounts, and all settings.

### Settings (configurable)

| Setting | Default | Who can edit |
|---|---|---|
| Stale Deal thresholds per pipeline stage | Qualification 30 days / Proposal Sent 14 / Negotiation 7 | ACM, Manager, Admin |
| Forecast win probability per stage | Qualification 20% / Proposal Sent 50% / Negotiation 80% | ACM, Manager, Admin |
| Loss Reasons list | Price, Competitor, Budget Cut, No Need Anymore, Service Quality, Internal Decision, No Response, Other | ACM, Manager, Admin |

### Core rule: Access is separate from Notifications

- **Access** (view/edit): determined by `role` + `is_senior`
- **Notifications** (receiving tasks and alerts): determined **only** by actual assignment (`csm_id` / `acm_id` on the Account), regardless of `is_senior`

Example: A CSM Senior can open and work on any account, but does not receive Auto-HealthAlert or Auto-Renewal notifications except for accounts actually assigned to them.

### Manual Task routing from CSM Senior

- If created by the CSM assigned to that account → routes to them
- If created by a CSM Senior on an account **not** assigned to them → routes to the primary CSM assigned to that account (not to the creator)

---

## 2.1 Global Search

A fixed, visible search bar present on every screen of the platform, allowing quick search for any Account. It searches only Account fields (Client name, Client code) — it does not include Contacts.

---

## 3. Account (Core Table)

### Fields from the Original Sheet

**Identity & location:** Client code, Client name, Country, Location

**Technical/contractual:** License, Cloud/on-prem, Management Service, BOQ Users count, Added Users, Total Users Count

**Contract & lifecycle:** Start contract date, End contract date, Quarter (derived), Years of Renewal (derived)

**Responsible team (many-to-one, more than one person per client):** CSM (FK→Users), ACM (FK→Users), Partner, Focal point L1/L2 (→ converted into the first 2 Contacts at import time)

**Activity & engagement:** Quarterly meeting date, Number of Events, Digital content count, Has Event, Event Location

**Advocacy:** G2/Gartner Review Status, Gartner Review

**Misc:** URL

### New Fields Added in a Later Review Round

- **`Description`**: a simple free-text field for general notes about the account
- **`Logo`**: upload the client's logo image
- **`Tenant URL`**: text link
- **`Drive URL`**: text link

### 3 Independent Classifications (they don't overlap)

| Classification | Values | Purpose |
|---|---|---|
| **Class** | VIP > A > B > C > D | Strategic importance — used only for prioritization on the dashboard, **does not affect the Health Score calculation** |
| **Health Status** | Healthy / At Risk | Fully automatically computed (see Health Score Engine section). **No longer includes Churned — see Lifecycle Stage below** |
| **Size** | Enterprise / Mid / SMB | Operational size |

### Lifecycle Stage — Final Complete List

```
POC → Closed-Won → Onboarding → Active → Adoption → Renewal → Expansion
    ↘ Closed-Lost (permanent end, no return)

From any stage after Active → can transition to → Churned
From Churned → can manually return to → Onboarding (if a new contract is signed)
```

**Details of each stage:**

- **POC**: before the contract is signed, an opportunity under evaluation. **Explicit exception to the role-separation rule**: both CSM and ACM see and receive tasks for this account together. Health Score = N/A
- **Closed-Won**: a positive POC outcome, moves on to Onboarding afterward
- **Closed-Lost**: a negative POC outcome (the client declined) — **a permanent end, no return from it**. Remains visible in the system as a stage (for reporting purposes), it is not archived
- **Onboarding**: contract signed, relationship being established. Health Score = N/A
- **Active / Adoption / Renewal / Expansion**: the client is actually operating; the calculation works with exactly the same rules across all four stages, with no computational distinction between them (explicit confirmation: Renewal does not need higher computational sensitivity)
- **Churned**: the relationship ended after having been active (unlike Closed-Lost, which means a relationship never began at all). Health Score = N/A. **Can be reversed** manually back to Onboarding if a new contract is signed later

**General rule:** Manual (set by CSM/CSM Senior from a selection screen), **except for the automatic transitions driven by Deal outcomes** below. Default = **Active** if left empty at import time.

**Automatic transitions driven by Deals (v2):**

| Deal Type | Closed-Won | Closed-Lost |
|---|---|---|
| New Business | POC → Closed-Won → Onboarding | POC → Closed-Lost (not counted as Churn) |
| Renewal | Automatic value comparison vs. current contract value: higher → **Expansion**; same → **Active**; lower → outcome "Renewed with Downgrade" + **Active** | **Churned** immediately; open automatic tasks are cancelled; manual Next-Action tasks stay open |
| Upsell / Cross-sell | **Expansion** | No change to the account |

**Churn has exactly one source:** a Renewal Deal closed as Lost. A lost New Business deal is Closed-Lost (the client never existed); a lost Upsell leaves the account unaffected.

**Correcting a mistaken Churned status** is done manually from the Lifecycle Stage (no automatic reopening of the Deal). *Known risk:* the Deal remains recorded as Lost, so renewal reports may diverge from the actual stage unless both are corrected.

### `Required Meeting Frequency`

(Weekly = 4x/month, Bi-weekly = 2x/month) — specific to Managed Service clients only, set individually per client (not uniform)

### Health Status — Separate from Lifecycle Stage

`Health Status` (Healthy/At Risk) is a flag entirely independent of `Lifecycle Stage` — a client can be in the "Adoption" stage and have an "At Risk" status at the same time. The Stage describes where the client is in their journey, and the Status describes their current health while in a stage eligible for calculation. **"A client permanently leaving after an active relationship" is now described exclusively via Lifecycle Stage = Churned, not via Health Status.**

### Health Status Override

- Manual, with a mandatory reason
- **Temporary only**: automatically cleared with **any** change in the computed number (Final Score), even a very small change (±1 point) that doesn't change the final color
- This is a deliberate decision: it favors high sensitivity over letting an old, unreviewed override persist for a long time

---

## 4. Contacts (Stakeholder Management)

A separate table, **one-to-many** relationship with Account (one client can have multiple Contacts).

```
Contact:
  - Name, Phone, Email, Title/Role
  - Influence Level: High / Medium / Low
  - Interest Level: High / Medium / Low
  - Communication Channels: multi-select (Email / Phone / WhatsApp / Teams / In-person)
  - Preferred Channel: one choice among the selected channels
```

### Power/Interest Grid

A visual display of all Contacts for a given client, distributed across 4 quadrants:

- **High Influence + High Interest** → Manage Closely
- **High Influence + Low Interest** → Keep Satisfied
- **Low Influence + High Interest** → Keep Informed
- **Low Influence + Low Interest** → Monitor

### Contact Profile

Displays their basic data + their position on the Grid + **a full history of all Activities/Tasks linked to them** (chronologically, most recent first) + **last contact date** automatically derived from the most recent linked Activity — for quickly spotting neglected Contacts (especially those with High Influence).

### Linking a Contact to an Activity/Task

- **Functionally optional** (not required at the database level)
- But **visually prominent** in the form (a clear place, not the last small field) to encourage actual use

---

## 5. Activity Log

```
Activity: Account (required), Contact (optional, FK), Type (Call/Email/Meeting/...), free text, date
```

- Chronological (append-only)
- Type is a **mandatory classified field** — necessary because the Health Score Engine needs to filter activities by type precisely (example: excludes Meeting from the Weekly Contact calculation)

---

## 6. Health Score Engine — Final Complete Design

### Activation Rule

```
Lifecycle Stage ∈ {POC, Closed-Won, Closed-Lost, Onboarding, Churned}  → Health Status = N/A (nothing is calculated)
Lifecycle Stage ∈ {Active, Adoption, Renewal, Expansion}               → the calculation runs normally, with the same rules
```

This rule automatically covers the initial import scenario: if a client is imported with Stage = Active, their calculation starts from the moment of import (day 0 = default "last contact," a clean start without relying on old Activity data that doesn't exist).

### Factors

Entirely independent of `Class` — the same calculation rules for all clients regardless of VIP/A/B/C/D.

| Factor | Managed Service (20% weight/factor) | Non-Managed (25% weight/factor) | Rule and Thresholds |
|---|---|---|---|
| **Open Tickets** | ✅ | ✅ | Weighted Score = (number of Bugs × 3) + (number of General × 1); 0=green, 1-3=yellow, 4+=red. Source: manual currently, Jira in the future |
| **Monthly Adoption** | ✅ | ✅ | Days since last (phishing campaign) OR (new course assigned) — either is sufficient; <15 days=green, 15-30=yellow, >30=red |
| **CSAT** | ✅ | ✅ | Manual 1-5 rating from CSM/ACM currently (automated via survey in the future); 4-5=green, 3=yellow, 1-2=red |
| **General Contact** | ❌ | ✅ | Days since last Activity (any type); <15=green, 15-30=yellow, >30=red |
| **Weekly Contact** | ✅ | ❌ | Binary: is there an Activity (Call/Email, **excludes Meeting**) in the last 7 days? Yes=green / No=red |
| **Meeting Attendance** | ✅ | ❌ | Meeting Compliance % = (number of meetings logged in the last 30 days ÷ Required Meeting Frequency) × 100; 100%=green, 50-99%=yellow, <50%=red |

**Actual number of factors:**
- **Managed Service** client: 5 factors (Tickets, Adoption, CSAT, Weekly Contact, Meeting) — 20% each
- **Non-Managed** client: 4 factors (Tickets, Adoption, CSAT, General Contact) — 25% each

### Contact Factors Count CSM Activities Only (v2)

**Weekly Contact, General Contact, and Meeting Attendance** are calculated only from Activities logged by users with `role = CSM` (including CSM Senior, since senior is a flag on the CSM role). Activities logged by ACMs — typically commercial negotiation calls and emails — **do not count** toward any Health Score contact factor. This prevents commercial outreach from masking an absence of actual customer-success follow-up.

### Meeting Exclusion Mechanism from Weekly Contact

Activities of type "Meeting" are **explicitly excluded** from the Weekly Contact calculation (they are only counted under Meeting Attendance) — to avoid double-counting the same event across two different factors.

### Converting Colors to Numbers and Combining Them

```
Green = 100 | Yellow = 50 | Red = 0

Final Score = sum of (each factor's value × its weight)

80-100 → Green (Healthy)
50-79  → Yellow (Watch)
< 50   → Red (At Risk)
```

### Operating Mechanism: Dual Trigger

1. **Immediate (Event-driven):** every time an event feeding any factor is logged (new Activity, new/closed ticket, new CSAT) → the Health Score is recalculated for this client immediately, and the color updates instantly on the dashboard
2. **Daily (Scheduled):** every day, recalculate for all 60 clients — catches the natural decay in factors based on "number of days" (even if no one logged anything new)

Every calculation (from either Trigger) is recorded as a new row in **`Health_Score_History`** (Account, Date, Score, Color, breakdown per factor) — this later enables charting a client's trend and identifying exactly which factor is pulling them down.

---

## 7. Task/Reminder Engine

```
Task:
  - Account (required)
  - Contact (optional, visually prominent in the form)
  - Deal (optional, FK — for deal-related tasks)
  - Type: Auto-Renewal / Auto-Quarterly-Meeting / Manual-NextAction / Auto-HealthAlert / Auto-StaleDeal
  - Category: Internal / Client-Facing / Partner-Facing
  - Due date
  - Status: Pending / Done
  - Description (free text)
  - Jira Ticket ID (empty for now, ready for future integration)
```

### Types

- **Auto-Renewal**: generated automatically 30/60/90 days before End Contract Date
- **Auto-Quarterly-Meeting**: generated automatically from Quarterly meeting date
- **Manual-NextAction**: created manually by CSM/ACM at any time
- **Auto-StaleDeal** (v2): generated when a Deal stays in the same pipeline stage beyond its configured threshold with no new Negotiation Round or Activity — one open task per Deal until it moves (duplicate prevention)
- **Auto-HealthAlert**: generated **immediately** when a client turns red (whether from the immediate or daily calculation) — only one task per client, stays open until manually closed or the status changes away from red (duplicate prevention)

A single client can have **more than one open task at the same time** (example: a renewal follow-up + a separate Next Action).

### Task Routing (who receives each type)

| Type | Routes to |
|---|---|
| Auto-HealthAlert | **CSM only** |
| Auto-StaleDeal | **Deal Owner** (single person) |
| Auto-Renewal | CSM + ACM (both) |
| Auto-Quarterly-Meeting | CSM + ACM (both) |
| Manual-NextAction | By default, whoever created it; exception: if created by a CSM Senior on an account not assigned to them → routes to the primary CSM |
| **Any task on an account in the POC stage** | **Explicit exception: CSM + ACM together**, regardless of task type |

### Edit and Delete Permissions (CRUD)

| Operation | Who can do it |
|---|---|
| Edit/delete a Task | Any role with edit access (CSM on their accounts, CSM Senior on all) |
| Create a new Account directly from the platform | CSM, CSM Senior |
| Edit Account data (after Excel import or after manual creation) | CSM (their accounts), CSM Senior (all) |
| **Delete an Account entirely** | **CSM Senior only** (a sensitive operation, deletes all linked Activities/Contacts/Tasks with it) |
| Create/edit Deals and Negotiation Rounds (v2) | CSM, CSM Senior, ACM (on assigned accounts); Admin (all) |
| Edit Settings (v2) | ACM, Manager, Admin |
| Set ACM Targets (v2) | Manager, Admin |
| Manage users, roles, and account assignment (v2) | Admin only |

### Notifications

In-platform notification + email together for every due task.

### Category's Relationship to Future Jira Integration

Only `Category = Internal` is eligible for conversion into a Jira ticket in the future — a fixed and simple rule, without complex conditional logic per case.

---

## 7A. Deals, Pipeline & ACM Daily Work (v2)

The ACM's daily work centres on commercial negotiation and preparing offers across renewals, upsells, cross-sells, and new business. All of it is managed through one unified entity: the **Deal**. (This replaces the earlier "Renewal Cycle" concept — renewals are now a Deal type.)

### 7A.1 Deal Entity

```
Deal:
  - Account (required)
  - Type: Renewal / Upsell / Cross-sell / New Business
  - Product/Service: Licenses/Users / Managed Service / Events / Digital Content / Other
  - Pipeline Stage: Qualification / Proposal Sent / Negotiation / Closed-Won / Closed-Lost
  - Expected Value
  - Current Contract Value (Renewal only — the baseline for automatic comparison)
  - Final Agreed Value
  - Expected Close Date
  - Owner (one person; defaults to the creator, can be changed)
  - Outcome (derived at close)
  - Loss Reason (mandatory when Closed-Lost) + optional note

  └─ Negotiation Round (one or more per Deal):
       - Date
       - Proposed Price
       - Terms / changes
       - Client Response
       - Round Status: Accepted / Rejected / Counter-offer
       - Contact (optional)
       - created_by (automatic)
```

- **Documents:** no file upload. Proposals and quotes live in external storage; the account-level `Drive URL` is sufficient.
- **Ownership:** Deals of every type are fully shared between CSM and ACM — both can view and edit. Each Deal still has a single **Owner** for follow-up accountability, and every round records `created_by` so it is always visible who is actually driving the deal.
- **After Closed-Won:** the system **suggests** updating the account based on Product/Service (e.g. set `Management Service` after a Managed Service upsell, update user counts after a Licenses/Users upsell). The update is confirmed by the user, not automatic.

### 7A.2 Pipeline Stages & Close Logic

```
Qualification → Proposal Sent → Negotiation → Closed-Won / Closed-Lost
```

Stages are uniform across all Deal types so the pipeline remains one screen. Effects of closing on the account's Lifecycle Stage are defined in Section 3 (Automatic transitions driven by Deals).

**Renewal outcome is computed automatically** from the Final Agreed Value vs. the Current Contract Value — the user never selects "Renewed" vs. "Downgrade" manually:

| Comparison | Outcome | Lifecycle Stage |
|---|---|---|
| Final > Current | Renewed | Expansion |
| Final = Current | Renewed | Active |
| Final < Current | Renewed with Downgrade | Active |
| Closed-Lost | Churned | Churned |

### 7A.3 Pipeline Screen

- **Kanban board:** one column per open stage (Qualification / Proposal Sent / Negotiation); deals are cards that can be dragged between columns.
- **Weighted forecast:** each stage has a configurable win probability (default 20% / 50% / 80%). Forecast value = Expected Value × stage probability, summed across open deals for the selected period.

### 7A.4 Stale Deal Alerts

A Deal that remains in the same stage beyond its configured threshold (default: Qualification 30 days, Proposal Sent 14, Negotiation 7) with no new Negotiation Round or Activity generates an **Auto-StaleDeal** task for the Deal Owner. One open task per Deal until the Deal moves.

### 7A.5 Loss Reasons

Mandatory for any Deal closed as Lost, selected from a configurable list (default: Price, Competitor, Budget Cut, No Need Anymore, Service Quality, Internal Decision, No Response, Other), with an optional free-text note.

### 7A.6 ACM Targets

- A **quarterly target per ACM**, set by **Admin or Manager** (not by the ACM).
- The target is **locked at the start of the quarter** so achievement is measured against a fixed number.
- **Achievement** = total Final Agreed Value of all Closed-Won Deals (all types: Renewal + Upsell + Cross-sell + New Business) on accounts **assigned to that ACM**, regardless of who the Deal Owner is. This keeps renewals inside the target (so they aren't neglected in favour of new sales) and ensures CSM-owned renewals still count toward the account's ACM.

### 7A.7 Deliberate Decisions with Known Risks

- **ACM does not see Health, even when upselling.** An ACM may pitch an upsell to an At-Risk client without knowing it. A protective "check with the CSM" hint was considered and rejected in favour of strict separation.
- **Shared renewal with no task split.** Both CSM and ACM can do everything on a renewal; risk of each waiting for the other is partially mitigated by the single Deal Owner and `created_by` on rounds.
- **Manual correction of a mistaken Churned status** updates the Lifecycle Stage only; the Deal stays Lost (see Section 3).

---

## 8. Dashboard (Analytics Overview)

An aggregated KPI screen at the level of all 60 clients as a whole (not an individual client browsing screen).

### Global Filter

One filter at the top of the dashboard (Country / Class / Health Status) affects **all** cards together.

### Cards

| # | Card | Display Type |
|---|---|---|
| 1 | Total number of accounts | Big number |
| 2 | Class distribution (VIP/A/B/C/D) | Bar/Donut |
| 3 | Health Status distribution | Traffic-light colored donut |
| 4 | Renewals in a selected quarter (independent quarter filter within the card) + list of names | Number + list |
| 5 | Number of accounts with Managed Service | Number/percentage |
| 6 | Country distribution | Bar chart |
| 7 | Total number of users | Big number (sum) |
| 8 | Cloud vs On-prem | Simple donut |
| 9 | Number of Reviews (G2/Gartner) | Number |
| 10 | Lifecycle Stage distribution | Bar/Funnel |
| 11 | Events + Digital content trend over time | Line chart |
| 12 | Overdue Tasks by Owner | Number + breakdown by type |
| 13 | Open Pipeline Value (v2) | Big number + breakdown by Deal type |
| 14 | Weighted Forecast for the quarter (v2) | Big number |
| 15 | Target Achievement per ACM (v2) | % + progress bar |
| 16 | Win Rate (v2) | Percentage |
| 17 | Top Loss Reasons (v2) | Bar chart |

### Dashboard Differs by Role

| Role | What they see |
|---|---|
| **CSM** | All cards, in full |
| **ACM** | All cards except Health Status distribution (unless `is_admin`); the Overdue Tasks card **filtered to Renewal and Deal-related tasks**; all pipeline cards (13–17) |
| **Manager** | All cards, fully view-only |

Strict separation: ACM never sees any signal about Health Status even in the aggregated cards, preserving the complete functional separation between Renewal responsibility and daily account-health responsibility.

---

## 9. Excel Import Template (Initial Upload Only)

### Required at Import Time

All original fields from the current sheet: Client code, Client name, License, Users counts, Class, Country, Location, CSM, ACM, Partner, Focal points, Contract dates, Management Service, etc.

### Optional at Import Time (completed later from within the platform)

- `Lifecycle Stage` — if empty → Default = **Active** (calculation starts immediately)
- `Required Meeting Frequency` — if empty (for a Managed client) → pending, the Meeting Attendance factor is not calculated until it's set
- `Contacts` details (Influence/Interest/Channels) — added gradually per client from their profile after import

### Baseline Rule (Import Baseline)

At import time, each client's "last contact" is considered = **the import date itself** (a clean start), rather than attempting to infer a date from old data that doesn't actually exist.

---

## 10. Deferred or Cancelled Decisions

- ❌ **Playbooks** (text checklist per Lifecycle Stage): removed from the current MVP — can be revisited later once the actual system is tried and the real need for it becomes clear
- ⏳ **Hosting**: a deferred decision, won't affect the current code design (a regular Python backend works on any environment)
- ⏳ **Actual Teams / Outlook / Jira integration + chatbot**: entirely out of the current scope, but the architecture is ready for it (Jira Ticket ID field is ready, generally modular design)

---

## 11. Log of Logical Gaps Discovered and Resolved During Review

Documenting the review process, these gaps were discovered and explicitly resolved:

1. Meeting would have been counted twice (Weekly Contact + Meeting Attendance) → explicit exclusion of Meeting from Weekly Contact
2. Weekly Contact was assumed for everyone, but a strict weekly requirement doesn't make sense for non-Managed clients → restricted to Managed only
3. Override would have been cleared too quickly → confirmed decision: cleared with any change in the number (final, intentional)
4. Non-Managed clients had only two factors (statistically weak) → added CSAT + General Contact, bringing them to 4 factors
5. The calculation would have been daily only → Dual Trigger (immediate + daily)
6. The Contact field in the form might be neglected → stays optional but visually prominent
7. No clear table for routing each task type → a complete Task Routing table
8. Two roles alone (CSM/Manager) weren't enough to represent actual responsibilities → 3 roles (CSM/ACM/Manager), then 4 effective levels by adding is_senior
9. The dashboard was unified despite differing roles → dashboard views customized per role
10. The Overdue Tasks card exposed all tasks to all roles → ACM sees only Renewal tasks from it
11. ACM might receive a renewal task without sufficient context → deliberate decision: strict separation, no signal about Health whatsoever
12. A new client in Onboarding would have shown as red automatically (a statistical injustice) → no calculation during Onboarding
13. Question about whether Renewal needs higher computational sensitivity → no, the same rules for all stages after Onboarding
14. Absence of Activity history at the time of the initial import → automatically covered by the unified Onboarding/Active rule, without a separate exception
15. Question about Class's effect on Health Score → entirely independent, Class is for prioritization only
16. CSM/ACM were not architecturally separated from Contacts → complete separation: a Users table (with login) separate from a Contacts table (without login)
17. Every CSM would have seen every account (no hierarchy) → added an is_senior flag to control scope
18. ACM's scope was unclear → restricted to their assigned accounts, exactly like CSM
19. Question about whether CSM Senior receives all notifications → no, separation between Access (broad) and Notifications (restricted to actual assignment)
20. Routing a Manual Task from a CSM Senior on an account not assigned to them → routes to the primary CSM, not to the creator
21. Playbooks: the actual benefit turned out to be unclear → removed from the current phase
22. New fields (Lifecycle Stage, etc.) don't exist in the original sheet → remain optional at import time
23. Default value for an empty Lifecycle Stage → Active (since most accounts are already active)
24. No unified place to quickly search for an account across screens → added a fixed Global Search (searches Account fields only)
25. No field for a general description, important links (Tenant/Drive), or a client logo → added Description, Logo, Tenant URL, Drive URL
26. No stage preceding Onboarding to represent an opportunity under evaluation → added a POC stage, with an explicit exception: CSM+ACM share its tasks
27. No way to document the POC outcome (accepted/rejected) → added Closed-Won (→ Onboarding) and Closed-Lost (permanent end, remains visible for reporting)
28. Churned was only a Health Status flag, not distinguishing "actually left after a relationship" from "temporarily struggling" → became an independent Lifecycle Stage, Health Status limited to Healthy/At Risk only
29. Can a Churned client return? → yes, manually to Onboarding upon signing a new contract
30. Edit/delete permissions weren't documented in detail → fully documented: editing open to anyone with edit access, deleting an Account entirely restricted to CSM Senior only

**v2 — ACM Version:**

31. ACM's daily work wasn't modelled beyond renewal dates → identified core ACM work as commercial negotiation and preparing offers
32. Offers/quotes needed a home → no file upload; the account-level Drive URL is sufficient
33. Negotiation wasn't tracked → Negotiation Rounds, one record per round (price, terms, response, status)
34. Rounds across different renewal years would mix → parent entity grouping rounds with a final outcome
35. Renewal ownership was ambiguous (ACM-only vs. CSM) → renewal is shared by CSM and ACM, with no task split
36. Renewal and other sales opportunities had the same shape → unified **Deal** entity with types Renewal / Upsell / Cross-sell / New Business
37. Renewal outcome categories → Renewed / Renewed with Downgrade / Churned, computed automatically by value comparison
38. Not renewed vs. churn → a lost Renewal = Churned (automatic); a lost New Business = Closed-Lost; a lost Upsell doesn't affect the account
39. Open tasks on a churned account → automatic tasks cancelled, manual Next-Action tasks stay open
40. Lifecycle after renewal → higher value → Expansion; same or lower → Active
41. POC stage could drift from the actual deal → New Business Deal outcome moves the account out of POC automatically
42. Deals could go silent → Auto-StaleDeal task to the Deal Owner, thresholds configurable per stage
43. Accountability on shared deals → single Deal Owner per Deal
44. ACM needed a working view → Kanban pipeline board + weighted forecast with configurable probabilities
45. No one was defined as managing users and settings → `is_admin` flag = Super Admin, overrides role separation
46. ACM commercial calls would inflate Health contact factors → contact factors (Weekly, General, Meeting) count CSM activities only
47. Loss insight → mandatory Loss Reason from a configurable list
48. Performance tracking → quarterly Target per ACM, set by Admin/Manager, locked at quarter start, achievement counted for the account's assigned ACM
49. What a Deal sells was unspecified → Product/Service field, with a suggested account update after Closed-Won
50. Dashboard had no commercial view → five pipeline cards (Pipeline Value, Forecast, Target Achievement, Win Rate, Top Loss Reasons)
51. Health visibility for ACM during upsell was revisited → strict separation retained as a deliberate decision (risk documented in 7A.7)

---

*This document (v2 — ACM Version) reflects the state of the architectural design as discussed so far. Next step: detailed Database Schema design (tables, relationships, data types), then beginning actual implementation.*
