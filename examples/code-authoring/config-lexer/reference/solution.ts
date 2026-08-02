// Reference implementation for the config-lexer eval. Never shown to
// models; pytest asserts it scores 1.0 under the hidden cases.
//
// Scans by Unicode code point (via Array.from, which iterates a
// string's code points rather than its UTF-16 code units) so that
// astral-plane characters count as exactly one column, per spec.
export type Token =
  | { kind: "identifier"; text: string; value: string; line: number; col: number }
  | { kind: "int"; text: string; value: number; line: number; col: number }
  | { kind: "string"; text: string; value: string; line: number; col: number }
  | { kind: "punct"; text: string; value: string; line: number; col: number }
  | {
      kind: "error";
      reason: "unterminated_string" | "bad_escape" | "unterminated_comment" | "bad_number";
      line: number;
      col: number;
    };

const PUNCT = new Set(["=", "[", "]", "{", "}", ","]);

const isDigit = (ch: string): boolean => ch >= "0" && ch <= "9";
const isIdentStart = (ch: string): boolean =>
  (ch >= "a" && ch <= "z") || (ch >= "A" && ch <= "Z") || ch === "_";
const isIdentPart = (ch: string): boolean => isIdentStart(ch) || isDigit(ch);
const isHex = (ch: string): boolean =>
  isDigit(ch) || (ch >= "a" && ch <= "f") || (ch >= "A" && ch <= "F");

// A valid integer literal is "0" or a leading-zero-free digit run
// optionally split into groups by single underscores.
const isValidInt = (text: string): boolean => /^0$|^[1-9][0-9]*(_[0-9]+)*$/.test(text);

// Parses a "{H...H}" body (1-6 hex digits) starting at chars[at],
// which must be "{". Returns the decoded character and how many
// characters (including the braces) it consumed, or null if the body
// is malformed in any way.
function parseUnicodeEscapeBody(
  chars: string[],
  at: number
): { char: string; length: number } | null {
  if (chars[at] !== "{") return null;
  let j = at + 1;
  let hex = "";
  while (j < chars.length && isHex(chars[j]) && hex.length < 6) {
    hex += chars[j];
    j++;
  }
  if (hex.length === 0 || chars[j] !== "}") return null;
  const codePoint = parseInt(hex, 16);
  if (codePoint > 0x10ffff) return null;
  return { char: String.fromCodePoint(codePoint), length: j + 1 - at };
}

export function tokenize(source: string): Token[] {
  const chars = Array.from(source);
  const tokens: Token[] = [];
  let i = 0;
  let line = 1;
  let col = 1;

  if (chars[0] === "\uFEFF") i = 1; // BOM: skipped, no effect on line/col

  const advance = (): string => {
    const ch = chars[i];
    i++;
    if (ch === "\n") {
      line++;
      col = 1;
    } else {
      col++;
    }
    return ch;
  };

  while (i < chars.length) {
    const startLine = line;
    const startCol = col;
    const ch = chars[i];

    if (ch === " " || ch === "\t" || ch === "\n") {
      advance();
      continue;
    }

    if (ch === "#") {
      while (i < chars.length && chars[i] !== "\n") advance();
      continue;
    }

    if (ch === "/" && chars[i + 1] === "*") {
      advance();
      advance();
      let depth = 1;
      while (i < chars.length && depth > 0) {
        if (chars[i] === "/" && chars[i + 1] === "*") {
          advance();
          advance();
          depth++;
        } else if (chars[i] === "*" && chars[i + 1] === "/") {
          advance();
          advance();
          depth--;
        } else {
          advance();
        }
      }
      if (depth > 0) {
        tokens.push({ kind: "error", reason: "unterminated_comment", line: startLine, col: startCol });
      }
      continue;
    }

    if (isDigit(ch)) {
      let text = "";
      while (i < chars.length && isIdentPart(chars[i])) text += advance();
      if (isValidInt(text)) {
        tokens.push({
          kind: "int",
          text,
          value: Number(text.replace(/_/g, "")),
          line: startLine,
          col: startCol,
        });
      } else {
        tokens.push({ kind: "error", reason: "bad_number", line: startLine, col: startCol });
      }
      continue;
    }

    if (isIdentStart(ch)) {
      let text = "";
      while (i < chars.length && isIdentPart(chars[i])) text += advance();
      tokens.push({ kind: "identifier", text, value: text, line: startLine, col: startCol });
      continue;
    }

    if (ch === '"') {
      const strStart = i;
      advance();
      let value = "";
      let closed = false;
      let badEscape = false;
      while (i < chars.length) {
        const c = chars[i];
        if (c === "\n") break; // unterminated: newline not consumed, resumes there
        if (c === '"') {
          advance();
          closed = true;
          break;
        }
        if (c === "\\") {
          advance();
          if (i >= chars.length) break; // dangling backslash at EOF: unterminated
          const esc = chars[i];
          if (esc === "n") {
            advance();
            value += "\n";
            continue;
          }
          if (esc === "t") {
            advance();
            value += "\t";
            continue;
          }
          if (esc === "\\") {
            advance();
            value += "\\";
            continue;
          }
          if (esc === '"') {
            advance();
            value += '"';
            continue;
          }
          if (esc === "u") {
            advance(); // resync point if what follows is malformed
            const parsed = parseUnicodeEscapeBody(chars, i);
            if (parsed) {
              for (let k = 0; k < parsed.length; k++) advance();
              value += parsed.char;
              continue;
            }
            badEscape = true;
            break;
          }
          advance(); // consume the one bad escape character; resync point is right after it
          badEscape = true;
          break;
        }
        advance();
        value += c;
      }
      if (badEscape) {
        tokens.push({ kind: "error", reason: "bad_escape", line: startLine, col: startCol });
      } else if (closed) {
        tokens.push({
          kind: "string",
          text: chars.slice(strStart, i).join(""),
          value,
          line: startLine,
          col: startCol,
        });
      } else {
        tokens.push({ kind: "error", reason: "unterminated_string", line: startLine, col: startCol });
      }
      continue;
    }

    if (ch === "'") {
      const strStart = i;
      advance();
      let value = "";
      let closed = false;
      while (i < chars.length) {
        const c = chars[i];
        if (c === "'") {
          advance();
          if (chars[i] === "'") {
            advance();
            value += "'";
            continue;
          }
          closed = true;
          break;
        }
        advance();
        value += c;
      }
      if (closed) {
        tokens.push({
          kind: "string",
          text: chars.slice(strStart, i).join(""),
          value,
          line: startLine,
          col: startCol,
        });
      } else {
        tokens.push({ kind: "error", reason: "unterminated_string", line: startLine, col: startCol });
      }
      continue;
    }

    if (PUNCT.has(ch)) {
      advance();
      tokens.push({ kind: "punct", text: ch, value: ch, line: startLine, col: startCol });
      continue;
    }

    // Outside the graded alphabet (see prompt); skip defensively so
    // tokenize always terminates instead of looping or throwing.
    advance();
  }

  return tokens;
}
