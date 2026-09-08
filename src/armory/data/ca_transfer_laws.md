# California Firearm Transfer Law Playbook

Reference for armory's deal valuations — the transaction rules a California
buyer actually faces on calguns/caguns listings. Written for both humans and
the valuation model (this file is injected into the valuation prompt).

**Current as of 2026-09-08.** Laws and litigation move — verify before
relying on anything here for a real transaction. Not legal advice.

## Quick reference

| Topic | Rule |
|---|---|
| PPT cost (first firearm) | **$47.19 all-in** — $37.19 state DROS + $10 max dealer fee (Pen. Code §§ 27545, 28055) |
| Each additional gun, same PPT | **+$10** dealer fee (no second DROS state fee) |
| Purchase frequency | **3 firearms per 30 days, any type** (AB 1078, eff. 2026-04-01, Pen. Code § 27535) |
| Waiting period | **10×24h** from DROS submission (Pen. Code § 26225); pickup within 30 days or re-do everything |
| Roster applies to PPT? | **No** — roster limits dealer sales only; private-party transfers are the legal off-roster channel |
| Handgun age | 21+ (18+ long guns only with hunting license / LE / military exemptions) |
| Safety card | FSC ~$25 / 10 years, required for dealer sales and PPTs |
| Magazines >10 rds | Cannot lawfully buy/sell/import/possess (Gov. Code § 32310; Duncan upheld 2025, SCOTUS petition pending) |
| Ammo | Background check (~$1 with existing DROS record; $19 first time); no out-of-state mail order |
| Sales tax on PPTs | None on the gun price between private parties |

## 1. Private party transfers (PPT) — the default for these listings

Both buyer and seller must appear **together, in person** at the same
California licensed dealer (FFL01). The dealer runs the DROS background
check; the gun is released after the 10-day wait.

- **Fees are fixed by statute.** First firearm: $37.19 DROS (incl. safety
  fees) + dealer fee capped at $10 = **$47.19**. Multiple guns in one
  transaction: **$10 per additional firearm**. A dealer charging more for a
  PPT is overcharging — a seller insisting you use a "transfer service" with
  $75+ fees is describing an out-of-state-style transfer, not a PPT.
  *Valuation: budget exactly $47.19 + $10/gun extra — never "$35–50ish."*
- A bundle of 3 guns in one PPT costs $67.19 total in fees. Bundles also
  interact with the 3-per-30 purchase cap (below).
- Both parties bring CA ID/residency proof. Buyer needs a valid FSC unless
  exempt (C&R+COE holders, LE, military).
- No sales tax on the firearm price between private parties.
- The 10-day wait: firearm must be picked up within 30 days of DROS
  submission or the transaction voids and must be restarted with new fees.

## 2. Purchase frequency — 3 per 30 days

- The old one-handgun-per-30-days rule was struck down (*Rhodes v. Bonta*,
  9th Cir., June 2025).
- **AB 1078 (eff. April 1, 2026)** replaced it: no more than **three
  firearms of any type per rolling 30-day period** (Pen. Code § 27535,
  counted by DROS submissions incl. dealer sales).
- Exemption: C&R license (FFL03) + COE holders buying C&R handguns.
- *Valuation: a multi-gun bundle can consume the whole monthly quota — worth
  a caveat on 3+ gun packages. Single purchases are unconstrained.*

## 3. The roster and the off-roster premium

The Roster of Handguns Certified for Sale limits what **dealers may sell**.
It does **not** apply to private party transfers — that asymmetry is the
entire basis of the CA off-roster premium: an off-roster handgun is
acquirable in-state only via PPT (or other exemption), so supply is
second-hand-only.

- Roster status comes from our deterministic DOJ lookup (in the valuation
  context); the site's own "Roster" tag (caguns) is usually reliable too.
- 2025–2026 market shift: **Glock discontinued Gen 3 production** and
  roster certifications are expiring (through Jan 1, 2027), shrinking
  dealer-sellable supply. On-roster used Gen 3s are firming in value;
  off-roster modern handguns (Gen 4/5, P320 X-Frames, etc.) remain
  PPT-premium items. Typical off-roster premium: +20–100% over national
  used value, model-dependent — judge per model.
- Off-roster handguns **cannot** be bought out-of-state and shipped in
  (the receiving CA dealer is bound by the roster). Hence "no shipping,
  FTF only" on most off-roster listings — that is legally required, not a
  seller preference.
- Long guns are not rostered at all.

## 4. Curio & Relic (C&R) transactions

Federal rule: any firearm **50+ years old in original configuration** is a
C&R (27 CFR § 478.11), no list membership needed.

With a federal C&R license (type 03) **plus a California Certificate of
Eligibility (COE)**:
- **C&R long guns may transfer in-state without a dealer** — face-to-face
  between CA residents, no DROS, no $47.19 (report on the C&R long-gun
  report per Pen. Code § 27966 within 19 days).
- Exempt from the **10-day waiting period** for C&R firearms only.
- Exempt from the purchase-frequency cap for C&R handguns.
- C&R handguns are **roster-exempt** (Pen. Code § 32110) — a 50+ year old
  handgun transfers by PPT even if off-roster.
- C&R handguns acquired must be reported to DOJ within 5 days (BCIA 4100A,
  $19).
- Without a COE (license only, or neither), everything routes through a
  normal PPT.

*Valuation: listings marked "FFL03/COE only" are C&R sales; the buyer pool
is smaller (licensed collectors) which can soften price, but transfer is
cheaper/faster (no DROS/wait for long guns). Age matters: 50-year threshold
— do the math from the model year (e.g., a 1976-made gun crossed it in 2026).*

## 5. Waiting period

10 × 24-hour periods from DROS submission. Exemptions (not general public):
dealers/special permit holders; **C&R+COE for C&R firearms**; peace officers
with agency authorization. Nothing shortens it for ordinary PPT buyers.

## 6. Magazines

- Gov. Code § 32310 bans buying/selling/manufacturing/importing/possessing
  magazines holding **more than 10 rounds**. *Duncan v. Bonta*: 9th Circuit
  en banc upheld the ban (2025); a SCOTUS cert petition is pending — the
  law is fully enforced meanwhile.
- *Valuation: 11+ round magazines bundled with a gun cannot be lawfully
  transferred in-state today. Do NOT add their value to the package; flag
  them as a caveat (seller usually keeps/swaps them for 10-rd). "Freedom
  Week" grandfathering is contested — treat as zero value, not a bonus.*

## 7. Ammunition

- Purchases require a background check: ~$1 when matched to an existing
  DROS/AIRS record, $19 for a first-time check (may not be instant).
- No out-of-state ammunition mail order into CA without an in-state
  transaction intermediary.
- No waiting period on ammo.
- *Valuation: bundled ammo is a legitimate value-add (~$0.25–0.45/rd for
  common pistol calibers, price-check current rates) but must physically
  transfer with the gun at the FFL — no shipping.*

## 8. Interstate / shipping rules

- Any firearm bought from an out-of-state private seller must transfer
  through a CA FFL (federal law). For **handguns**, that dealer transfer is
  roster-bound — off-roster handguns effectively cannot come in from out of
  state. On-roster handguns and all long guns can, at unregulated dealer
  transfer fees (commonly $50–125).
- Long guns may ship common carrier to the receiving FFL; handguns
  dealer-to-dealer only.
- *Valuation: for a SoCal buyer, out-of-area listings are only actionable
  for long guns or on-roster handguns; an off-roster "sale pending shipment
  anywhere" listing is a red flag.*

## 9. Intrafamilial transfers

Parent/child and grandparent/grandchild (and spouses) may transfer without
a dealer; file the report (BOF 4544A) with DOJ within 30 days, $19.
Roster-exempt. Not applicable to marketplace strangers — but explains some
"like new, one owner" supply.

## 10. Other valuation-relevant rules

- **Age to buy:** 21 for everything; 18–20 only long guns with hunting
  license / LE / military exemptions.
- **FSC:** Firearm Safety Certificate ~$25 / 10 yr, needed for PPTs and
  dealer sales (C&R+COE exempt).
- **Assault-weapon configuration:** centerfire semiauto rifles with
  "features" (pistol grip + detachable mag, etc.) must be featureless,
  fixed-mag, or registered-AW; a listing built as a featured rifle that
  isn't registered is a serious red flag. Pistol-format AR/AK types
  ("Ghetto Blaster"-style) fall under pistol rules + AW pistol rules
  (overall length constraints) — flag builds of this shape for scrutiny.
- **Serial numbers / home-builds:** check that serialized receivers are
  DOJ-recorded (home-built must be engraved + registered via the
  unique-serial process). Unserialized "80%" frames are not transferable.
- **Seller credibility:** caliber of story matters — "selling for a
  cousin," no-CAL-IDs, gift-payment demands raise scam risk already scored
  by the model.

## How this should shape valuations

1. Transfer cost is **$47.19 + $10/extra gun** — exact, never estimated.
2. Off-roster premium is real and supply-driven; on-roster Gen 3 supply is
   shrinking into 2027 — used Gen 3 prices are firm, not soft.
3. >10rd magazines contribute **zero** package value and earn a caveat.
4. 3-per-30 cap: caveat bundles of 3+ guns.
5. C&R/FFL03 listings: verify the 50-year math from model year; note the
   smaller buyer pool and cheaper transfer.
6. "No shipping" on off-roster handguns is legally required — don't
   discount a listing for it, and don't suggest buying it remotely.
7. Bundled ammo adds value at local rates but must move FTF.

## Sources

- CA AG Firearms FAQs (oag.ca.gov/firearms/pubfaqs) — PPT fees, waiting
  period, exemptions, age
- AB 1078 (Berman, 2025) — 3-per-30, eff. 2026-04-01; AG Information
  Bulletin 2026-DLE-02
- *Rhodes v. Bonta* (9th Cir. 2025) — 1-per-30 struck
- *Duncan v. Bonta* (9th Cir. en banc 2025), SCOTUS cert petition pending
- Pen. Code §§ 27535, 26225, 27545, 28055, 32110, 27966; Gov. Code § 32310;
  27 CFR § 478.11
- ATF C&R guidance (atf.gov); CA DOJ roster pages (oag.ca.gov)
