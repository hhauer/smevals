// Child process of the run-tests checker. Imports a cases module and a
// solution module (node strips TS types natively), runs every case, and
// emits one NDJSON line per case so the parent can salvage partial
// results if it has to kill us on timeout.
import { pathToFileURL } from "node:url";

const [casesPath, solutionPath] = process.argv.slice(2);
const emit = (obj) => console.log(JSON.stringify(obj));

function sortKeys(v) {
  if (Array.isArray(v)) return v.map(sortKeys);
  if (v && typeof v === "object")
    return Object.fromEntries(
      Object.keys(v)
        .sort()
        .map((k) => [k, sortKeys(v[k])])
    );
  if (typeof v === "number" && !isFinite(v)) {
    if (Number.isNaN(v)) return "__NaN__";
    if (v === Infinity) return "__Infinity__";
    if (v === -Infinity) return "__-Infinity__";
  }
  return v;
}
const canon = (v) => JSON.stringify(sortKeys(v));

const { cases } = await import(pathToFileURL(casesPath));
let solution;
try {
  solution = await import(pathToFileURL(solutionPath));
} catch (err) {
  emit({ importError: String(err) });
  process.exit(0);
}

// Emit total count first for timeout handling
emit({ total: cases.length });

for (const c of cases) {
  try {
    const got = c.run(solution);
    if (canon(got) === canon(c.expect)) {
      emit({ group: c.group, name: c.name, pass: true });
    } else {
      emit({ group: c.group, name: c.name, pass: false, expected: c.expect, got });
    }
  } catch (err) {
    emit({ group: c.group, name: c.name, pass: false, error: String(err) });
  }
}
