// Hidden test cases for the usage-billing eval. Each group targets one
// spec rule; expected invoices are exact objects, worked by hand.
type Plan = { code: string; baseCents: number; tiers: { upTo: number | null; centsPerUnit: number }[] };

const FLAT: Plan = { code: "flat", baseCents: 1000, tiers: [{ upTo: null, centsPerUnit: 2 }] };
const TIERED: Plan = {
  code: "tiered",
  baseCents: 3000,
  tiers: [
    { upTo: 100, centsPerUnit: 0 },
    { upTo: 1000, centsPerUnit: 2.5 },
    { upTo: null, centsPerUnit: 1 },
  ],
};
const PRO: Plan = { code: "pro", baseCents: 6200, tiers: [{ upTo: null, centsPerUnit: 1 }] };

// A 30-day March-alike period used throughout: 2026-04-01 .. 2026-05-01.
const P = { periodStart: "2026-04-01", periodEnd: "2026-05-01" };
const seg = (startsOn: string, plan: Plan) => ({ startsOn, plan });
const rec = (at: string, units: number) => ({ at, units });

export const cases = [
  {
    group: "base_case",
    name: "single plan, flat usage, no credits",
    // base 1000; usage 50*2=100; subtotal 1100; tax 8.75% = 96.25 -> 96
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT)],
        usage: [rec("2026-04-10T12:00:00Z", 50)],
        credits: [],
        taxRate: 0.0875,
      }),
    expect: {
      lines: [
        { kind: "base", planCode: "flat", cents: 1000 },
        { kind: "usage", planCode: "flat", cents: 100 },
      ],
      subtotalCents: 1100,
      taxCents: 96,
      totalCents: 1196,
      creditAppliedCents: 0,
      amountDueCents: 1196,
    },
  },
  {
    group: "base_case",
    name: "no usage means no usage line",
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT)],
        usage: [],
        credits: [],
        taxRate: 0,
      }),
    expect: {
      lines: [{ kind: "base", planCode: "flat", cents: 1000 }],
      subtotalCents: 1000,
      taxCents: 0,
      totalCents: 1000,
      creditAppliedCents: 0,
      amountDueCents: 1000,
    },
  },
  {
    group: "tier_bounds",
    name: "unit exactly at upTo stays in the free tier",
    // 100 units: all inside upTo=100 at 0 cents.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", TIERED)],
        usage: [rec("2026-04-05T00:00:00Z", 100)],
        credits: [],
        taxRate: 0,
      }).lines,
    expect: [
      { kind: "base", planCode: "tiered", cents: 3000 },
      { kind: "usage", planCode: "tiered", cents: 0 },
    ],
  },
  {
    group: "tier_bounds",
    name: "unit 101 is the first paid unit",
    // 101 units: 1 unit at 2.5 -> 2.5 -> rounds to 3.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", TIERED)],
        usage: [rec("2026-04-05T00:00:00Z", 101)],
        credits: [],
        taxRate: 0,
      }).lines[1].cents,
    expect: 3,
  },
  {
    group: "tier_bounds",
    name: "cumulative bound at 1000 inclusive",
    // 1000 units: 900 paid at 2.5 = 2250 exactly; unit 1001 would hit tier 3.
    run: (m: any) => {
      const at1000 = m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", TIERED)],
        usage: [rec("2026-04-05T00:00:00Z", 1000)],
        credits: [],
        taxRate: 0,
      }).lines[1].cents;
      const at1001 = m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", TIERED)],
        usage: [rec("2026-04-05T00:00:00Z", 1001)],
        credits: [],
        taxRate: 0,
      }).lines[1].cents;
      return [at1000, at1001];
    },
    expect: [2250, 2251],
  },
  {
    group: "proration",
    name: "mid-period change bills change day to the new plan",
    // 30-day period. flat active Apr 1-15 (14 days), pro active Apr 15-May 1 (16 days).
    // base flat: 1000*14/30 = 466.67 -> 467; base pro: 6200*16/30 = 3306.67 -> 3307.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT), seg("2026-04-15", PRO)],
        usage: [],
        credits: [],
        taxRate: 0,
      }).lines,
    expect: [
      { kind: "base", planCode: "flat", cents: 467 },
      { kind: "base", planCode: "pro", cents: 3307 },
    ],
  },
  {
    group: "proration",
    name: "usage on the change day prices on the new plan",
    // 10 units on Apr 15: pro plan (1c/unit) -> 10, not flat (2c/unit) -> 20.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT), seg("2026-04-15", PRO)],
        usage: [rec("2026-04-15T08:00:00Z", 10)],
        credits: [],
        taxRate: 0,
      }).lines.filter((l: any) => l.kind === "usage"),
    expect: [{ kind: "usage", planCode: "pro", cents: 10 }],
  },
  {
    group: "proration",
    name: "tier progression restarts per segment",
    // 80 units under tiered before change, 80 after change back to tiered:
    // each segment prices 80 units in the free tier -> 0 + 0.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [
          seg("2026-04-01", TIERED),
          seg("2026-04-10", FLAT),
          seg("2026-04-20", TIERED),
        ],
        usage: [rec("2026-04-05T00:00:00Z", 80), rec("2026-04-25T00:00:00Z", 80)],
        credits: [],
        taxRate: 0,
      }).lines.filter((l: any) => l.kind === "usage"),
    expect: [
      { kind: "usage", planCode: "tiered", cents: 0 },
      { kind: "usage", planCode: "tiered", cents: 0 },
    ],
  },
  {
    group: "rounding",
    name: "sum exactly then round once",
    // 3 records of 1 unit at 0.25c: per-record rounding gives 0 or 1s;
    // correct is round(0.75) = 1.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [
          seg("2026-04-01", {
            code: "micro",
            baseCents: 0,
            tiers: [{ upTo: null, centsPerUnit: 0.25 }],
          }),
        ],
        usage: [
          rec("2026-04-02T00:00:00Z", 1),
          rec("2026-04-03T00:00:00Z", 1),
          rec("2026-04-04T00:00:00Z", 1),
        ],
        credits: [],
        taxRate: 0,
      }).lines[1].cents,
    expect: 1,
  },
  {
    group: "rounding",
    name: "half rounds up in tax",
    // subtotal 1000 with 4.45% tax: 44.5 -> 45.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT)],
        usage: [],
        credits: [],
        taxRate: 0.0445,
      }).taxCents,
    expect: 45,
  },
  {
    group: "period_boundaries",
    name: "record at period start is in, at period end is out",
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT)],
        usage: [
          rec("2026-04-01T00:00:00Z", 5),
          rec("2026-05-01T00:00:00Z", 7),
          rec("2026-03-31T23:59:59Z", 11),
        ],
        credits: [],
        taxRate: 0,
      }).lines,
    expect: [
      { kind: "base", planCode: "flat", cents: 1000 },
      { kind: "usage", planCode: "flat", cents: 10 },
    ],
  },
  {
    group: "credits",
    name: "expiry order then size, applied after tax",
    // total 1196 (from base_case). Credits: 500 exp 2026-06-01, 300 exp
    // 2026-05-01, 400 exp 2026-05-01, 900 exp 2026-04-30 (expired -> unusable).
    // Order: 400 (05-01), 300 (05-01), 500 (06-01) -> applies 400+300+496.
    run: (m: any) =>
      m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT)],
        usage: [rec("2026-04-10T12:00:00Z", 50)],
        credits: [
          { cents: 500, expiresOn: "2026-06-01" },
          { cents: 300, expiresOn: "2026-05-01" },
          { cents: 400, expiresOn: "2026-05-01" },
          { cents: 900, expiresOn: "2026-04-30" },
        ],
        taxRate: 0.0875,
      }),
    expect: {
      lines: [
        { kind: "base", planCode: "flat", cents: 1000 },
        { kind: "usage", planCode: "flat", cents: 100 },
      ],
      subtotalCents: 1100,
      taxCents: 96,
      totalCents: 1196,
      creditAppliedCents: 1196,
      amountDueCents: 0,
    },
  },
  {
    group: "credits",
    name: "amount due floors at zero and credit application stops",
    // total 1000; credits 800 + 800 -> apply 800 + 200, due 0.
    run: (m: any) => {
      const inv = m.computeInvoice({
        ...P,
        segments: [seg("2026-04-01", FLAT)],
        usage: [],
        credits: [
          { cents: 800, expiresOn: "2026-05-02" },
          { cents: 800, expiresOn: "2026-05-03" },
        ],
        taxRate: 0,
      });
      return [inv.creditAppliedCents, inv.amountDueCents];
    },
    expect: [1000, 0],
  },
];
