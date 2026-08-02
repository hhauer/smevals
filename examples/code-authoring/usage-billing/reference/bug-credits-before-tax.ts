// Planted bug: credits applied before tax, so tax is computed on the
// reduced balance.
export type Tier = { upTo: number | null; centsPerUnit: number };
export type Plan = { code: string; baseCents: number; tiers: Tier[] };
export type InvoiceInput = {
  periodStart: string;
  periodEnd: string;
  segments: { startsOn: string; plan: Plan }[];
  usage: { at: string; units: number }[];
  credits: { cents: number; expiresOn: string }[];
  taxRate: number;
};
export type Line = { kind: "base" | "usage"; planCode: string; cents: number };
export type Invoice = {
  lines: Line[];
  subtotalCents: number;
  taxCents: number;
  totalCents: number;
  creditAppliedCents: number;
  amountDueCents: number;
};

const DAY_MS = 86_400_000;
const dayNumber = (isoDate: string): number => Date.parse(`${isoDate}T00:00:00Z`) / DAY_MS;
const halfUp = (x: number): number => Math.floor(x + 0.5);

export function computeInvoice(input: InvoiceInput): Invoice {
  const start = dayNumber(input.periodStart);
  const end = dayNumber(input.periodEnd);
  const totalDays = end - start;

  // Each segment's active day range within the period. The segment is
  // active from max(startsOn, periodStart) until the next segment
  // starts (the change day belongs to the new plan) or the period ends.
  const active = input.segments.map((seg, i) => {
    const from = Math.max(dayNumber(seg.startsOn), start);
    const next = input.segments[i + 1];
    const to = Math.min(next ? dayNumber(next.startsOn) : end, end);
    return { seg, from, to };
  });

  const lines: Line[] = [];
  for (const { seg, from, to } of active) {
    if (to <= from) continue;
    lines.push({
      kind: "base",
      planCode: seg.plan.code,
      cents: halfUp((seg.plan.baseCents * (to - from)) / totalDays),
    });
  }

  // Total in-period units per segment; instants compare against
  // midnight-UTC period bounds, calendar day picks the segment.
  const unitsBySegment = new Map<number, number>();
  for (const rec of input.usage) {
    const instant = Date.parse(rec.at);
    if (instant < start * DAY_MS || instant >= end * DAY_MS) continue;
    const day = Math.floor(instant / DAY_MS);
    const index = active.findIndex(({ from, to }) => from <= day && day < to);
    if (index === -1) continue;
    unitsBySegment.set(index, (unitsBySegment.get(index) ?? 0) + rec.units);
  }

  for (const [index, units] of [...unitsBySegment.entries()].sort((a, b) => a[0] - b[0])) {
    const { seg } = active[index];
    let remaining = units;
    let covered = 0;
    let exactCents = 0;
    for (const tier of seg.plan.tiers) {
      if (remaining <= 0) break;
      const capacity = tier.upTo === null ? Infinity : tier.upTo - covered;
      const inTier = Math.min(remaining, capacity);
      exactCents += inTier * tier.centsPerUnit;
      covered += inTier;
      remaining -= inTier;
    }
    lines.push({ kind: "usage", planCode: seg.plan.code, cents: halfUp(exactCents) });
  }

  const subtotalCents = lines.reduce((sum, line) => sum + line.cents, 0);
  const usable = input.credits
    .filter((c) => dayNumber(c.expiresOn) >= end)
    .sort(
      (a, b) => dayNumber(a.expiresOn) - dayNumber(b.expiresOn) || b.cents - a.cents
    );
  let balance = subtotalCents;
  let creditAppliedCents = 0;
  for (const credit of usable) {
    const applied = Math.min(credit.cents, balance);
    creditAppliedCents += applied;
    balance -= applied;
    if (balance === 0) break;
  }
  const taxCents = halfUp(balance * input.taxRate);
  const totalCents = subtotalCents + taxCents;

  return {
    lines,
    subtotalCents,
    taxCents,
    totalCents,
    creditAppliedCents,
    amountDueCents: balance + taxCents,
  };
}
