// Run the YAML attack corpus (cases.json) through studio.html's real pure
// block (yamlParse/yamlEmit/applyEdit/scalarEnvVars), extracted at run time
// from the STUDIO_PURE markers - never a duplicated copy of the code.
//
// Prints a JSON report to stdout for tests/test_studio.py to classify
// against real PyYAML: every case must parse to PyYAML's value (MATCH) or
// refuse cleanly with a reason (REFUSE) - a silent divergence is corruption.
//
// Run standalone: node tests/studio_pure/run_corpus.mjs [path/to/studio.html]
import { readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const here = dirname(fileURLToPath(import.meta.url));
const htmlPath = process.argv[2] ?? join(here, "..", "..", "src", "smevals", "studio.html");

const html = readFileSync(htmlPath, "utf8");
const begin = html.indexOf("// STUDIO_PURE_BEGIN");
const end = html.indexOf("// STUDIO_PURE_END");
if (begin < 0 || end < 0) throw new Error("STUDIO_PURE markers not found in " + htmlPath);
const module_ = { exports: {} };
new Function("module", html.slice(begin, end))(module_);
const pure = module_.exports;

const cases = JSON.parse(readFileSync(join(here, "cases.json"), "utf8"));

const rows = cases.map(({ name, yaml }) => {
  const p = pure.yamlParse(yaml);
  const row = { name, yaml };
  if (!p.ok) {
    row.jsOk = false;
    row.jsError = p.error;
    row.jsLine = p.line;
    return row;
  }
  row.jsOk = true;
  row.jsDoc = p.doc;
  // Every scalar entry as an env var, mirroring cli.scalar_env_vars - the
  // corpus's non-alnum/unicode keys and non-scalar siblings (lists, maps)
  // exercise the same key-derivation and scalar-only rules the task and
  // check forms rely on for their inline annotations.
  if (p.doc && typeof p.doc === "object" && !Array.isArray(p.doc)) {
    row.scalarEnvVars = pure.scalarEnvVars("SMEVALS_TASK_", p.doc, p.floats);
  }
  const emitted = pure.yamlEmit(p.doc);
  row.emitted = emitted;
  const p2 = pure.yamlParse(emitted);
  row.reparseOk = p2.ok;
  if (p2.ok) {
    row.reparseStable = JSON.stringify(p2.doc) === JSON.stringify(p.doc);
  } else {
    row.reparseError = p2.error;
  }
  // The data-loss channel: applyEdit an UNRELATED key, then ask (in the
  // Python side) whether the attacked field still means the same thing.
  const edit = pure.applyEdit(yaml, { op: "set", path: "__untouched_marker__", value: "z" });
  row.editOk = edit.ok;
  if (edit.ok) row.editedEmit = edit.text;
  return row;
});

process.stdout.write(JSON.stringify({ cases: rows }));
