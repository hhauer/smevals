// Rebuild of the lost scenes.js: load + sanity-check a scenes.yaml for
// record.js. The schema is documented in pipeline/README.md; validation
// here is deliberately shallow - the actions are checked by runAction at
// execution time, this just catches structural mistakes before a long
// recording starts.
"use strict";

const fs = require("fs");
const yaml = require("js-yaml");

function loadScenes(scenesPath) {
  const doc = yaml.load(fs.readFileSync(scenesPath, "utf8"));
  if (!doc || typeof doc !== "object") throw new Error(`${scenesPath}: not a mapping`);
  if (!doc.resolution || !doc.resolution.width || !doc.resolution.height) {
    throw new Error(`${scenesPath}: resolution {width, height} required`);
  }
  if (!doc.fps) throw new Error(`${scenesPath}: fps required`);
  if (!Array.isArray(doc.scenes) || !doc.scenes.length) {
    throw new Error(`${scenesPath}: scenes list required`);
  }
  const seen = new Set();
  for (const scene of doc.scenes) {
    if (!scene.id) throw new Error(`${scenesPath}: scene without id`);
    if (seen.has(scene.id)) throw new Error(`${scenesPath}: duplicate scene id ${scene.id}`);
    seen.add(scene.id);
    if (scene.type === "title") {
      if (!scene.duration) throw new Error(`scene ${scene.id}: title scenes need duration`);
    } else if (scene.type === "browser") {
      if (!Array.isArray(scene.actions) || !scene.actions.length) {
        throw new Error(`scene ${scene.id}: browser scenes need actions`);
      }
    } else {
      throw new Error(`scene ${scene.id}: unknown type ${scene.type}`);
    }
  }
  return doc;
}

module.exports = { loadScenes };
