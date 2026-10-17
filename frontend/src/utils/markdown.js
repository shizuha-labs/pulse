import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({
  html: true,         // allow mixed content: HTML blocks pass through, markdown gets converted.
                      // backticks are parsed before HTML so `<span>` in code blocks stays safe.
  linkify: true,
  breaks: true,       // convert single newlines to <br>
  typographer: false,
})

/**
 * Un-escape JSON-style escape sequences when content was double-encoded.
 * Agents sometimes pass JSON-serialized strings through the API, which
 * leaves literal `\n`, `\t`, `\"` etc. in the stored content.
 * We only un-escape if there are literal `\n` and NO real newlines
 * (strong signal of double-encoding) — this avoids corrupting code samples
 * that legitimately contain the two-character sequence `\n`.
 */
function maybeUnescape(content) {
  if (typeof content !== 'string') return content
  const literalNewlines = (content.match(/\\n/g) || []).length
  const realNewlines = (content.match(/\n/g) || []).length
  if (literalNewlines >= 2 && realNewlines === 0) {
    return content
      .replace(/\\r\\n/g, '\n')
      .replace(/\\n/g, '\n')
      .replace(/\\t/g, '\t')
      .replace(/\\"/g, '"')
      .replace(/\\'/g, "'")
      .replace(/\\\\/g, '\\')
  }
  return content
}

/**
 * Detect if content is raw markdown (not HTML) and convert to HTML.
 * TipTap and dangerouslySetInnerHTML expect HTML input.
 * Agent-created content (via MCP tools) is often raw markdown.
 *
 * Heuristic: if content starts with an HTML tag like <p>, <div>, <h1> etc.
 * we trust it's already HTML. Otherwise we run it through markdown-it,
 * which is robust enough to handle plain text, mixed content, and edge
 * cases like literal `<span>` inside backtick code blocks.
 */
export function ensureHtml(content) {
  if (!content) return content
  // Fix double-escaped newlines from agents that pass JSON-serialized strings
  const cleaned = maybeUnescape(content)
  // Always run through markdown-it — with html:true it handles mixed HTML+markdown
  // content correctly (HTML blocks pass through, markdown gets converted).
  return md.render(cleaned)
}

/**
 * Render markdown to HTML — same as ensureHtml but explicit for read-only contexts.
 */
export function renderMarkdown(content) {
  return ensureHtml(content)
}
