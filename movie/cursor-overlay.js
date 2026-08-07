// Rebuild of the lost cursor-overlay.js: exports the source string
// record.js injects into every page (addInitScript). Playwright's mouse
// is invisible in recordings - this ring IS the on-camera cursor,
// following mousemove with a press pulse on mousedown (v1's QA described
// it as the pink/red ring near the cursor).
"use strict";

module.exports = `
(() => {
  if (window.__movieCursor) return;
  window.__movieCursor = true;
  const ring = document.createElement("div");
  ring.style.cssText = [
    "position: fixed", "left: -100px", "top: -100px",
    "width: 20px", "height: 20px",
    "border: 3px solid rgba(255, 64, 129, 0.9)",
    "border-radius: 50%",
    "background: rgba(255, 64, 129, 0.15)",
    "pointer-events: none",
    "z-index: 2147483647",
    "transform: translate(-50%, -50%) scale(1)",
    "transition: transform 0.08s ease-out",
  ].join(";");
  const attach = () => document.body && document.body.appendChild(ring);
  if (document.body) attach();
  else document.addEventListener("DOMContentLoaded", attach);
  document.addEventListener("mousemove", e => {
    ring.style.left = e.clientX + "px";
    ring.style.top = e.clientY + "px";
  }, true);
  document.addEventListener("mousedown", () => {
    ring.style.transform = "translate(-50%, -50%) scale(0.6)";
  }, true);
  document.addEventListener("mouseup", () => {
    ring.style.transform = "translate(-50%, -50%) scale(1)";
  }, true);
})();
`;
