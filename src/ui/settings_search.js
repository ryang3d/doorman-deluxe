/* Pure settings-search helpers — no DOM, so unit-testable under node.
 * Loaded as a classic <script> (defines a global) before app.js in index.html.
 * In node the same file is require-able via the guarded module.exports below.
 */
'use strict';

// Does a single field match a free-text query? Case-insensitive substring
// against label, key, and help. Empty/whitespace query matches everything
// (restores the full list).
function fieldMatchesQuery(field, query) {
  const q = (query || '').trim().toLowerCase();
  if (!q) return true;
  const hay = [field.label, field.key, field.help || '']
    .join(' ').toLowerCase();
  return hay.includes(q);
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { fieldMatchesQuery };
}
