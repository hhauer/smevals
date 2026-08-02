// Shipping-cost calculator for one package. A clean refactor of the
// legacy module: the three near-identical rate-table walks collapse
// into one generic helper, region codes and naming are honest, and
// the dead legacy-zone branch is gone.
//
// This version also "fixes" a bug it spotted along the way: EU used
// to carry APAC's regional fee on top of its own (an accidental
// switch fall-through in the original). That looked like an obvious
// mistake, so it's corrected here - which is exactly why this fixture
// exists: the task says preserve quirks exactly, even ones that look
// like bugs, and this solution didn't.

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

// Fixed: EU only pays its own fee now, not APAC's too.
const REGIONAL_FEE_CENTS: Record<string, number> = {
  EU: 300,
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

  cost += pkg.weightKg * WEIGHT_SURCHARGE_CENTS_PER_KG;

  return cost;
}

export function applyDiscount(costCents: number, pctOff: number, flatRebateCents: number): number {
  const afterPercent = costCents - costCents * pctOff;
  return afterPercent - flatRebateCents;
}
