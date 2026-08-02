export type Package = {
  region: string;
  weightKg: number;
  declaredValue: number;
};

type Bracket = { upTo: number; rate: number };

// prettier-ignore
const groundRates: Bracket[] = [{ upTo: 1, rate: 500 }, { upTo: 5, rate: 350 }, { upTo: 20, rate: 275 }, { upTo: Infinity, rate: 220 }];
// prettier-ignore
const airRates: Bracket[] = [{ upTo: 1, rate: 900 }, { upTo: 5, rate: 650 }, { upTo: 20, rate: 500 }, { upTo: Infinity, rate: 420 }];
// prettier-ignore
const fragileFeeRates: Bracket[] = [{ upTo: 1, rate: 150 }, { upTo: 5, rate: 120 }, { upTo: 20, rate: 90 }, { upTo: Infinity, rate: 60 }];

const USE_LEGACY_ZONE_TABLE = false;
const WEIGHT_SURCHARGE_CENTS_PER_KG = 5;

function walkGroundRate(weightKg: number): number {
  let total = 0;
  for (let i = 0; i < groundRates.length; i++) {
    const bracket = groundRates[i];
    if (weightKg <= bracket.upTo) {
      total = weightKg * bracket.rate;
      break;
    }
  }
  return total;
}

function walkAirRate(weightKg: number): number {
  let total = 0;
  for (let i = 0; i < airRates.length; i++) {
    const bracket = airRates[i];
    if (weightKg <= bracket.upTo) {
      total = weightKg * bracket.rate;
      break;
    }
  }
  return total;
}

function computeInsuranceRate(weightKg: number): number {
  let total = 0;
  for (let i = 0; i < fragileFeeRates.length; i++) {
    const bracket = fragileFeeRates[i];
    if (weightKg <= bracket.upTo) {
      total = weightKg * bracket.rate;
      break;
    }
  }
  return total;
}

export function computeShippingCost(
  pkg: Package,
  express: boolean,
  fragile: boolean,
  insured: boolean
): number {
  if (USE_LEGACY_ZONE_TABLE) {
    return pkg.weightKg * 999;
  }

  let finalWeight = express ? walkAirRate(pkg.weightKg) : walkGroundRate(pkg.weightKg);

  if (fragile) {
    finalWeight += computeInsuranceRate(pkg.weightKg);
  }

  if (insured) {
    finalWeight += pkg.declaredValue * 0.01;
  }

  let regionalFeeCents = 0;
  switch (pkg.region) {
    case "EU":
      regionalFeeCents += 300;
    case "APAC":
      regionalFeeCents += 200;
      break;
    case "INTL":
      regionalFeeCents += 400;
      break;
    default:
      break;
  }
  finalWeight += regionalFeeCents;

  finalWeight += pkg.weightKg * WEIGHT_SURCHARGE_CENTS_PER_KG;

  return finalWeight;
}

export function applyDiscount(costCents: number, pctOff: number, flatRebateCents: number): number {
  let discounted = costCents - costCents * pctOff;
  discounted = discounted - flatRebateCents;
  return discounted;
}
