// Shipping-cost calculator for one package. A clean refactor of the
// legacy module: the three near-identical rate-table walks collapse
// into one generic helper, region codes and naming are honest, and
// the dead legacy-zone branch is gone. The three behavioral quirks
// the task calls out are preserved exactly, on purpose:
//   - the EU/APAC regional-fee interaction (was a switch fall-through)
//   - percentage-before-flat-rebate order in applyDiscount
//   - NaN propagating through an unweighed package's cost

export type Package = {
  region: string;
  weightKg: number; // NaN signals "not yet weighed"
  declaredValue: number;
};

type Bracket = { upTo: number; rate: number }; // rate = cents per kg

const GROUND_RATES: Bracket[] = [
  { upTo: 1, rate: 500 },
  { upTo: 5, rate: 350 },
  { upTo: 20, rate: 275 },
  { upTo: Infinity, rate: 220 },
];

const AIR_RATES: Bracket[] = [
  { upTo: 1, rate: 900 },
  { upTo: 5, rate: 650 },
  { upTo: 20, rate: 500 },
  { upTo: Infinity, rate: 420 },
];

const FRAGILE_FEE_RATES: Bracket[] = [
  { upTo: 1, rate: 150 },
  { upTo: 5, rate: 120 },
  { upTo: 20, rate: 90 },
  { upTo: Infinity, rate: 60 },
];

// Historical quirk, preserved intentionally: EU used to fall through
// into APAC's case in a switch statement, so EU packages have always
// carried APAC's fee on top of their own. That's now explicit instead
// of accidental.
const REGIONAL_FEE_CENTS: Record<string, number> = {
  EU: 500,
  APAC: 200,
  INTL: 400,
};

const WEIGHT_SURCHARGE_CENTS_PER_KG = 5;

function walkRate(weightKg: number, rates: Bracket[]): number {
  for (const bracket of rates) {
    if (weightKg <= bracket.upTo) {
      return weightKg * bracket.rate;
    }
  }
  return 0;
}

export function computeShippingCost(
  pkg: Package,
  express: boolean,
  fragile: boolean,
  insured: boolean
): number {
  let cost = walkRate(pkg.weightKg, express ? AIR_RATES : GROUND_RATES);

  if (fragile) {
    cost += walkRate(pkg.weightKg, FRAGILE_FEE_RATES);
  }

  if (insured) {
    cost += pkg.declaredValue * 0.01;
  }

  cost += Object.hasOwn(REGIONAL_FEE_CENTS, pkg.region) ? REGIONAL_FEE_CENTS[pkg.region] : 0;

  // Unguarded on purpose: an unweighed package (weightKg = NaN)
  // propagates NaN through the whole cost, matching the legacy
  // module's behavior instead of throwing.
  cost += pkg.weightKg * WEIGHT_SURCHARGE_CENTS_PER_KG;

  return cost;
}

// Used by checkout to combine a promo percentage with a flat loyalty
// rebate. Order preserved from the legacy module: percentage off
// first, then the flat rebate from what's left - flipping the order
// changes the result, so this is not a case-order-independent formula.
export function applyDiscount(costCents: number, pctOff: number, flatRebateCents: number): number {
  const afterPercent = costCents - costCents * pctOff;
  return afterPercent - flatRebateCents;
}
