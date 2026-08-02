// Reference implementation for the interval-set eval. Never shown to
// models; pytest asserts it scores 1.0 under the hidden cases.
//
// Every bound maps to a cut point [value, side] ordered
// lexicographically. A start bound: closed [x -> [x,0], open (x ->
// [x,1]. An end bound: open x) -> [x,0], closed x] -> [x,1]. An
// interval is the half-open cut range [startKey, endKey) over this
// ordering, which makes empty/merge/split decisions exact comparisons.
export type Interval = {
  lo: number | null;
  hi: number | null;
  loOpen: boolean;
  hiOpen: boolean;
};

type Key = readonly [number, 0 | 1];

const startKey = (iv: Interval): Key =>
  iv.lo === null ? [-Infinity, 0] : [iv.lo, iv.loOpen ? 1 : 0];
const endKey = (iv: Interval): Key =>
  iv.hi === null ? [Infinity, 1] : [iv.hi, iv.hiOpen ? 0 : 1];
const cmp = (a: Key, b: Key): number =>
  a[0] !== b[0] ? (a[0] < b[0] ? -1 : 1) : a[1] - b[1];

type Span = { start: Key; end: Key };

const toSpan = (iv: Interval): Span | null => {
  const span = { start: startKey(iv), end: endKey(iv) };
  return cmp(span.start, span.end) < 0 ? span : null;
};

const toInterval = (s: Span): Interval => ({
  lo: s.start[0] === -Infinity ? null : s.start[0],
  hi: s.end[0] === Infinity ? null : s.end[0],
  loOpen: s.start[0] === -Infinity ? true : s.start[1] === 1,
  hiOpen: s.end[0] === Infinity ? true : s.end[1] === 0,
});

export class IntervalSet {
  private list: Span[] = [];

  constructor(intervals?: Interval[]) {
    for (const iv of intervals ?? []) this.add(iv);
  }

  add(iv: Interval): void {
    const span = toSpan(iv);
    if (!span) return;
    const merged: Span[] = [];
    let { start, end } = span;
    for (const s of this.list) {
      // Overlapping or touching spans coalesce: touching means one's
      // end cut equals the other's start cut.
      if (cmp(s.end, start) < 0 || cmp(end, s.start) < 0) {
        merged.push(s);
      } else {
        if (cmp(s.start, start) < 0) start = s.start;
        if (cmp(end, s.end) < 0) end = s.end;
      }
    }
    merged.push({ start, end });
    merged.sort((a, b) => cmp(a.start, b.start));
    this.list = merged;
  }

  remove(iv: Interval): void {
    const cut = toSpan(iv);
    if (!cut) return;
    const kept: Span[] = [];
    for (const s of this.list) {
      if (cmp(cut.start, s.start) > 0) {
        // Left remainder ends where the removal starts: flip the
        // removal's start cut into an end cut (same coordinate).
        kept.push({ start: s.start, end: min(cut.start, s.end) });
      }
      if (cmp(cut.end, s.end) < 0) {
        kept.push({ start: max(cut.end, s.start), end: s.end });
      }
    }
    this.list = kept.filter((s) => cmp(s.start, s.end) < 0);
  }

  contains(x: number): boolean {
    return this.intersects({ lo: x, hi: x, loOpen: false, hiOpen: false });
  }

  intersects(iv: Interval): boolean {
    const span = toSpan(iv);
    if (!span) return false;
    return this.list.some(
      (s) => cmp(s.start, span.end) < 0 && cmp(span.start, s.end) < 0
    );
  }

  spans(): Interval[] {
    return this.list.map(toInterval);
  }

  union(other: IntervalSet): IntervalSet {
    const result = new IntervalSet(this.spans());
    for (const iv of other.spans()) result.add(iv);
    return result;
  }

  intersection(other: IntervalSet): IntervalSet {
    const result = new IntervalSet();
    for (const a of this.list) {
      for (const b of other.list) {
        const start = max(a.start, b.start);
        const end = min(a.end, b.end);
        if (cmp(start, end) < 0) result.add(toInterval({ start, end }));
      }
    }
    return result;
  }
}

const min = (a: Key, b: Key): Key => (cmp(a, b) <= 0 ? a : b);
const max = (a: Key, b: Key): Key => (cmp(a, b) >= 0 ? a : b);
