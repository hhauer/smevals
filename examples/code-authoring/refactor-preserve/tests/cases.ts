// Hidden test cases for the refactor-preserve eval. core_paths pins
// ordinary behavior; the quirk_* groups prove the model preserved the
// three deliberate quirks instead of "fixing" them; duplication reads
// the solution's own source to prove the three copy-pasted rate-table
// walks were actually collapsed.
//
// Rate tables (cents per kg), all with brackets <=1, <=5, <=20, else:
//   ground:  500 / 350 / 275 / 220
//   air:     900 / 650 / 500 / 420
//   fragile: 150 / 120 /  90 /  60
// Every package also picks up an unconditional weight surcharge of
// 5 cents/kg. Regional fee: EU 500 (300 own + 200 inherited from
// APAC's case via the fall-through), APAC 200, INTL 400, US/other 0.
import { readFileSync } from "node:fs";

const pkg = (region: string, weightKg: number, declaredValue = 0) => ({
  region,
  weightKg,
  declaredValue,
});

function normalizeLines(src: string): string[] {
  return src
    .split("\n")
    .map((line) => line.trim().replace(/\s+/g, " "))
    .filter((line) => line.length > 0);
}

// The 4 normalized lines every one of legacy.ts's three rate-table
// walks share verbatim (only the table name differs, and that's on a
// different line). A real dedup collapses this to at most one copy.
const WALK_SKELETON = [
  "if (weightKg <= bracket.upTo) {",
  "total = weightKg * bracket.rate;",
  "break;",
  "}",
];

function countSkeletonOccurrences(lines: string[]): number {
  let count = 0;
  for (let i = 0; i + WALK_SKELETON.length <= lines.length; i++) {
    let matches = true;
    for (let j = 0; j < WALK_SKELETON.length; j++) {
      if (lines[i + j] !== WALK_SKELETON[j]) {
        matches = false;
        break;
      }
    }
    if (matches) count++;
  }
  return count;
}

export const cases = [
  // --- core_paths ---------------------------------------------------
  {
    group: "core_paths",
    name: "ground rate, first bracket boundary",
    // weight 1kg is <= the first bracket's upTo (1), so it's priced
    // at the first bracket's rate: 1 * 500 = 500. Surcharge: 1 * 5 = 5.
    // US has no regional fee. Total: 500 + 5 = 505.
    run: (m: any) => m.computeShippingCost(pkg("US", 1), false, false, false),
    expect: 505,
  },
  {
    group: "core_paths",
    name: "ground rate, second bracket upper boundary",
    // weight 5kg is <= upTo 5, still the second bracket: 5 * 350 = 1750.
    // Surcharge: 5 * 5 = 25. Total: 1775.
    run: (m: any) => m.computeShippingCost(pkg("US", 5), false, false, false),
    expect: 1775,
  },
  {
    group: "core_paths",
    name: "express selects the air table, not ground",
    // Same weight (5kg) as the previous case, but express=true picks
    // the air table's second bracket: 5 * 650 = 3250. Surcharge: 25.
    // Total: 3275 (not 1775 - proves express actually switches tables).
    run: (m: any) => m.computeShippingCost(pkg("US", 5), true, false, false),
    expect: 3275,
  },
  {
    group: "core_paths",
    name: "fragile adds the fragile-fee walk on top of the base cost",
    // weight 20kg: ground third bracket (upTo 20): 20 * 275 = 5500.
    // Fragile fee, same weight, fragile table third bracket:
    // 20 * 90 = 1800. Surcharge: 20 * 5 = 100.
    // Total: 5500 + 1800 + 100 = 7400.
    run: (m: any) => m.computeShippingCost(pkg("US", 20), false, true, false),
    expect: 7400,
  },
  {
    group: "core_paths",
    name: "insured adds 1% of declared value",
    // weight 10kg: ground third bracket: 10 * 275 = 2750.
    // Insurance: 10000 * 0.01 = 100. Surcharge: 10 * 5 = 50.
    // Total: 2750 + 100 + 50 = 2900.
    run: (m: any) => m.computeShippingCost(pkg("US", 10, 10000), false, false, true),
    expect: 2900,
  },
  {
    group: "core_paths",
    name: "INTL regional fee applies with no fall-through interaction",
    // weight 3kg: ground second bracket: 3 * 350 = 1050. INTL fee: 400.
    // Surcharge: 3 * 5 = 15. Total: 1050 + 400 + 15 = 1465.
    run: (m: any) => m.computeShippingCost(pkg("INTL", 3), false, false, false),
    expect: 1465,
  },
  {
    group: "core_paths",
    name: "top (unbounded) bracket",
    // weight 25kg: past every finite upTo, so the last bracket:
    // 25 * 220 = 5500. Surcharge: 25 * 5 = 125. Total: 5625.
    run: (m: any) => m.computeShippingCost(pkg("US", 25), false, false, false),
    expect: 5625,
  },

  // --- quirk_fallthrough ---------------------------------------------
  {
    group: "quirk_fallthrough",
    name: "EU carries APAC's fee on top of its own",
    // weight 1kg ground: 1 * 500 = 500. Regional fee for EU is 300
    // (its own) + 200 (inherited from APAC) = 500. Surcharge: 5.
    // Total: 500 + 500 + 5 = 1005.
    run: (m: any) => m.computeShippingCost(pkg("EU", 1), false, false, false),
    expect: 1005,
  },
  {
    group: "quirk_fallthrough",
    name: "APAC alone only ever pays its own fee",
    // Same weight, region APAC directly (not via EU): fee is just its
    // own 200, not doubled. Base 500 + fee 200 + surcharge 5 = 705.
    // This is what discriminates "removed the fall-through" from
    // "generally broke regional fees" - APAC alone must stay correct.
    run: (m: any) => m.computeShippingCost(pkg("APAC", 1), false, false, false),
    expect: 705,
  },
  {
    group: "quirk_fallthrough",
    name: "INTL is untouched by the EU/APAC interaction",
    // Base 500 + INTL fee 400 + surcharge 5 = 905, same as core_paths'
    // INTL case (different weight) would predict - a sanity control.
    run: (m: any) => m.computeShippingCost(pkg("INTL", 1), false, false, false),
    expect: 905,
  },

  // --- quirk_discount_order -------------------------------------------
  {
    group: "quirk_discount_order",
    name: "percentage applied before the flat rebate (case 1)",
    // 1000 - 1000*0.1 = 900; 900 - 50 = 850. (Flat-first would give
    // 1000-50=950, 950-950*0.1=855 - a different number, which is
    // exactly why this case pins the order.)
    run: (m: any) => m.applyDiscount(1000, 0.1, 50),
    expect: 850,
  },
  {
    group: "quirk_discount_order",
    name: "percentage applied before the flat rebate (case 2)",
    // 2000 - 2000*0.25 = 1500; 1500 - 200 = 1300. (Flat-first: 2000-200
    // =1800, 1800-1800*0.25=1350 - again a different number.)
    run: (m: any) => m.applyDiscount(2000, 0.25, 200),
    expect: 1300,
  },
  {
    group: "quirk_discount_order",
    name: "percentage applied before the flat rebate (case 3)",
    // 500 - 500*0.5 = 250; 250 - 100 = 150. (Flat-first: 500-100=400,
    // 400-400*0.5=200 - again different.)
    run: (m: any) => m.applyDiscount(500, 0.5, 100),
    expect: 150,
  },

  // --- quirk_nan ---------------------------------------------------
  {
    group: "quirk_nan",
    name: "unweighed package yields NaN, not a throw",
    // weightKg=NaN reaches the unconditional weight-surcharge term
    // unguarded (NaN * 5 = NaN), which then poisons the running total
    // via addition. The function must not throw and must not coerce
    // the missing weight to 0.
    run: (m: any) => Number.isNaN(m.computeShippingCost(pkg("US", NaN), false, false, false)),
    expect: true,
  },
  {
    group: "quirk_nan",
    name: "NaN wins regardless of which flags or region are set",
    // Same NaN weight, but with every boolean on and a region that
    // also exercises the fall-through fee - still NaN, not some
    // partially-computed number.
    run: (m: any) => Number.isNaN(m.computeShippingCost(pkg("EU", NaN, 1000), true, true, true)),
    expect: true,
  },
  {
    group: "quirk_nan",
    name: "NaN wins even when insurance is the only extra fee",
    run: (m: any) =>
      Number.isNaN(m.computeShippingCost(pkg("INTL", NaN, 5000), false, false, true)),
    expect: true,
  },

  // --- duplication ---------------------------------------------------
  {
    group: "duplication",
    name: "the tripled rate-table walk skeleton was collapsed",
    run: (_m: any) => {
      const src = readFileSync(process.env.SMEVALS_SOLUTION ?? "solution.ts", "utf8");
      return countSkeletonOccurrences(normalizeLines(src)) <= 1;
    },
    expect: true,
  },
];
