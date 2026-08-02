// Hidden test cases for the interval-set eval. Grouped so that a
// failed group names the misunderstanding (fails_<group> tag).
type Iv = { lo: number | null; hi: number | null; loOpen: boolean; hiOpen: boolean };
const iv = (lo: number | null, hi: number | null, loOpen = false, hiOpen = false): Iv =>
  ({ lo, hi, loOpen, hiOpen });

export const cases = [
  {
    group: "basics",
    name: "single interval round-trips",
    run: (m: any) => new m.IntervalSet([iv(1, 2)]).spans(),
    expect: [iv(1, 2)],
  },
  {
    group: "basics",
    name: "disjoint intervals sort",
    run: (m: any) => new m.IntervalSet([iv(5, 6), iv(1, 2)]).spans(),
    expect: [iv(1, 2), iv(5, 6)],
  },
  {
    group: "basics",
    name: "overlap merges",
    run: (m: any) => new m.IntervalSet([iv(1, 3), iv(2, 5)]).spans(),
    expect: [iv(1, 5)],
  },
  {
    group: "merge_touching",
    name: "closed-closed touch merges",
    run: (m: any) => new m.IntervalSet([iv(1, 2), iv(2, 3)]).spans(),
    expect: [iv(1, 3)],
  },
  {
    group: "merge_touching",
    name: "half-open touch merges",
    run: (m: any) => new m.IntervalSet([iv(1, 2, false, true), iv(2, 3)]).spans(),
    expect: [iv(1, 3)],
  },
  {
    group: "merge_touching",
    name: "open meets closed start merges",
    run: (m: any) => new m.IntervalSet([iv(1, 2), iv(2, 3, true, false)]).spans(),
    expect: [iv(1, 3)],
  },
  {
    group: "no_merge_open_touching",
    name: "open-open touch stays split",
    run: (m: any) =>
      new m.IntervalSet([iv(1, 2, false, true), iv(2, 3, true, false)]).spans(),
    expect: [iv(1, 2, false, true), iv(2, 3, true, false)],
  },
  {
    group: "no_merge_open_touching",
    name: "excluded point is not contained",
    run: (m: any) =>
      new m.IntervalSet([iv(1, 2, false, true), iv(2, 3, true, false)]).contains(2),
    expect: false,
  },
  {
    group: "remove_splitting",
    name: "open removal leaves closed edges",
    run: (m: any) => {
      const s = new m.IntervalSet([iv(1, 4)]);
      s.remove(iv(2, 3, true, true));
      return s.spans();
    },
    expect: [iv(1, 2), iv(3, 4)],
  },
  {
    group: "remove_splitting",
    name: "closed removal leaves open edges",
    run: (m: any) => {
      const s = new m.IntervalSet([iv(1, 4)]);
      s.remove(iv(2, 3));
      return s.spans();
    },
    expect: [iv(1, 2, false, true), iv(3, 4, true, false)],
  },
  {
    group: "remove_splitting",
    name: "removing a point splits open",
    run: (m: any) => {
      const s = new m.IntervalSet([iv(0, 10)]);
      s.remove(iv(5, 5));
      return s.spans();
    },
    expect: [iv(0, 5, false, true), iv(5, 10, true, false)],
  },
  {
    group: "degenerate",
    name: "point interval is one point",
    run: (m: any) => {
      const s = new m.IntervalSet([iv(5, 5)]);
      return [s.contains(5), s.contains(5.0001), s.spans()];
    },
    expect: [true, false, [iv(5, 5)]],
  },
  {
    group: "degenerate",
    name: "empty forms vanish",
    run: (m: any) =>
      new m.IntervalSet([
        iv(5, 5, true, true),
        iv(5, 5, false, true),
        iv(5, 5, true, false),
        iv(7, 3),
      ]).spans(),
    expect: [],
  },
  {
    group: "degenerate",
    name: "point fills an open gap",
    run: (m: any) =>
      new m.IntervalSet([iv(1, 2, false, true), iv(2, 3, true, false), iv(2, 2)]).spans(),
    expect: [iv(1, 3)],
  },
  {
    group: "contains_intersects",
    name: "open boundary excluded",
    run: (m: any) => {
      const s = new m.IntervalSet([iv(1, 2, true, true)]);
      return [s.contains(1), s.contains(1.5), s.contains(2)];
    },
    expect: [false, true, false],
  },
  {
    group: "contains_intersects",
    name: "touching closed intervals intersect",
    run: (m: any) => new m.IntervalSet([iv(1, 2)]).intersects(iv(2, 3)),
    expect: true,
  },
  {
    group: "contains_intersects",
    name: "touching at excluded point does not intersect",
    run: (m: any) => new m.IntervalSet([iv(1, 2, false, true)]).intersects(iv(2, 3)),
    expect: false,
  },
  {
    group: "unbounded",
    name: "lower ray contains everything below",
    run: (m: any) => {
      const s = new m.IntervalSet([iv(null, 0, true, false)]);
      return [s.contains(-1e9), s.contains(0), s.contains(0.001)];
    },
    expect: [true, true, false],
  },
  {
    group: "unbounded",
    name: "rays merge into the full line",
    run: (m: any) =>
      new m.IntervalSet([iv(null, 0), iv(0, null)]).spans(),
    expect: [iv(null, null, true, true)],
  },
  {
    group: "unbounded",
    name: "unbounded flags normalize to open",
    run: (m: any) => new m.IntervalSet([iv(null, 5, false, false)]).spans(),
    expect: [iv(null, 5, true, false)],
  },
  {
    group: "set_ops",
    name: "union does not modify operands",
    run: (m: any) => {
      const a = new m.IntervalSet([iv(1, 2)]);
      const b = new m.IntervalSet([iv(3, 4)]);
      const u = a.union(b);
      return [u.spans(), a.spans(), b.spans()];
    },
    expect: [[iv(1, 2), iv(3, 4)], [iv(1, 2)], [iv(3, 4)]],
  },
  {
    group: "set_ops",
    name: "intersection respects open edges",
    run: (m: any) =>
      new m.IntervalSet([iv(1, 3, false, true)])
        .intersection(new m.IntervalSet([iv(2, 4, true, false)]))
        .spans(),
    expect: [iv(2, 3, true, true)],
  },
  {
    group: "set_ops",
    name: "touching closed sets intersect in a point",
    run: (m: any) =>
      new m.IntervalSet([iv(1, 2)])
        .intersection(new m.IntervalSet([iv(2, 3)]))
        .spans(),
    expect: [iv(2, 2)],
  },
];
