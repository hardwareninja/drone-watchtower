/* Context encoders for radio-derived strings.
 *
 * The dashboard interpolates serials, callsigns, self-id text, and other
 * broadcast fields into HTML. esc() is the encoder for HTML text and for
 * double-quoted attributes. It is not an encoder for a DOM text property
 * such as Node.textContent, which does not interpret markup.
 */
function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* Render a key/value panel. Values are escaped; "mono" wraps the escaped
 * text in <code>. An entry is [key, value] or [key, value, "mono"]. */
function kvHtml(kv) {
  return kv.map(([k, v, style]) => {
    const val = style === "mono" ? `<code>${esc(v)}</code>` : esc(v);
    return `<div class="kv"><span>${esc(k)}</span><span>${val}</span></div>`;
  }).join("");
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { esc, kvHtml };
}
