// Hidden test cases for the config-lexer eval. Grouped so that a
// failed group names the misunderstanding (fails_<group> tag). Every
// expected value is hand-derived in the comment above its case.
//
// String.raw`...` is used wherever the source text itself must
// contain literal backslashes (so what's written is exactly what
// tokenize sees, with no JS escape processing). Plain string/template
// literals are used where a real newline, tab, or Unicode character
// needs to land IN the source (JS's own escapes, e.g. \n or
// \u{1F600}, cook those down to the real character).

export const cases = [
  // ---------------------------------------------------------------
  // basics
  // ---------------------------------------------------------------
  {
    group: "basics",
    name: "identifiers, punctuation, and whitespace",
    // "abc def_2 = { } [ ] ,"
    //  1234567890123456789012 (1-based columns)
    // abc@1, def_2@5, =@11, {@13, }@15, [@17, ]@19, ,@21
    run: (m: any) => m.tokenize("abc def_2 = { } [ ] ,"),
    expect: [
      { kind: "identifier", text: "abc", value: "abc", line: 1, col: 1 },
      { kind: "identifier", text: "def_2", value: "def_2", line: 1, col: 5 },
      { kind: "punct", text: "=", value: "=", line: 1, col: 11 },
      { kind: "punct", text: "{", value: "{", line: 1, col: 13 },
      { kind: "punct", text: "}", value: "}", line: 1, col: 15 },
      { kind: "punct", text: "[", value: "[", line: 1, col: 17 },
      { kind: "punct", text: "]", value: "]", line: 1, col: 19 },
      { kind: "punct", text: ",", value: ",", line: 1, col: 21 },
    ],
  },
  {
    group: "basics",
    name: "line comment produces no token",
    // "foo " (cols 1-4) then "# comment here" runs to end of line
    // (not consumed by any token); newline; "bar" at line 2 col 1.
    run: (m: any) => m.tokenize("foo # comment here\nbar"),
    expect: [
      { kind: "identifier", text: "foo", value: "foo", line: 1, col: 1 },
      { kind: "identifier", text: "bar", value: "bar", line: 2, col: 1 },
    ],
  },
  {
    group: "basics",
    name: "single-level block comment produces no token",
    // "x " (cols 1-2), "/* c */" (cols 3-9), " y" -> y at col 11.
    run: (m: any) => m.tokenize("x /* c */ y"),
    expect: [
      { kind: "identifier", text: "x", value: "x", line: 1, col: 1 },
      { kind: "identifier", text: "y", value: "y", line: 1, col: 11 },
    ],
  },

  // ---------------------------------------------------------------
  // numbers
  // ---------------------------------------------------------------
  {
    group: "numbers",
    name: "single zero and a plain multi-digit run",
    // "0 1234": 0@col1, 1234@col3.
    run: (m: any) => m.tokenize("0 1234"),
    expect: [
      { kind: "int", text: "0", value: 0, line: 1, col: 1 },
      { kind: "int", text: "1234", value: 1234, line: 1, col: 3 },
    ],
  },
  {
    group: "numbers",
    name: "uneven underscore grouping is still valid",
    // "1_2_34" -> digits with underscores removed: 1234.
    run: (m: any) => m.tokenize("1_2_34"),
    expect: [{ kind: "int", text: "1_2_34", value: 1234, line: 1, col: 1 }],
  },
  {
    group: "numbers",
    name: "the alnum run stops at punctuation",
    // "5,": the digit run is just "5" (comma isn't in [A-Za-z0-9_]).
    run: (m: any) => m.tokenize("5,"),
    expect: [
      { kind: "int", text: "5", value: 5, line: 1, col: 1 },
      { kind: "punct", text: ",", value: ",", line: 1, col: 2 },
    ],
  },

  // ---------------------------------------------------------------
  // string_escapes (double-quoted)
  // ---------------------------------------------------------------
  {
    group: "string_escapes",
    name: "all four simple escapes decode; text keeps the raw form",
    // Raw source (15 chars): " a \n b \t c \\ d \" e "
    // Decodes to 9 chars: a <LF> b <TAB> c \ d " e
    run: (m: any) => m.tokenize(String.raw`"a\nb\tc\\d\"e"`),
    expect: [
      {
        kind: "string",
        text: String.raw`"a\nb\tc\\d\"e"`,
        value: "a\nb\tc\\d\"e",
        line: 1,
        col: 1,
      },
    ],
  },
  {
    group: "string_escapes",
    name: "\\u{...} decodes to one code point; source is 11 ASCII columns",
    // Raw source (11 chars): " \ u { 1 F 6 0 0 } " - all ASCII, so
    // the column count is 11 even though the decoded value is a
    // single (astral) character.
    run: (m: any) => m.tokenize(String.raw`"\u{1F600}"`),
    expect: [
      {
        kind: "string",
        text: String.raw`"\u{1F600}"`,
        value: "\u{1F600}",
        line: 1,
        col: 1,
      },
    ],
  },
  {
    group: "string_escapes",
    name: "an escaped quote does not end the string",
    // Raw source (12 chars): " s a y sp \ " h i \ " " -> decodes to
    // say "hi" (9 chars, with the escaped quotes literal).
    run: (m: any) => m.tokenize(String.raw`"say \"hi\""`),
    expect: [
      {
        kind: "string",
        text: String.raw`"say \"hi\""`,
        value: 'say "hi"',
        line: 1,
        col: 1,
      },
    ],
  },

  // ---------------------------------------------------------------
  // raw_strings (single-quoted)
  // ---------------------------------------------------------------
  {
    group: "raw_strings",
    name: "backslash is a literal character, not an escape",
    run: (m: any) => m.tokenize(String.raw`'a\b'`),
    expect: [
      { kind: "string", text: String.raw`'a\b'`, value: "a\\b", line: 1, col: 1 },
    ],
  },
  {
    group: "raw_strings",
    name: "doubled quote escapes to one literal quote, string continues",
    // 'it''s': the '' at cols 4-5 contributes one ' to value and
    // does not close the string; the ' at col 7 does.
    run: (m: any) => m.tokenize(String.raw`'it''s'`),
    expect: [
      { kind: "string", text: String.raw`'it''s'`, value: "it's", line: 1, col: 1 },
    ],
  },
  {
    group: "raw_strings",
    name: "raw strings span newlines; line/col resume correctly after",
    // Line 1: 'ab (cols 1-3). Real newline ends line 1. Line 2:
    // cd' x - the closing ' is at line 2 col 3, so x is at col 5.
    run: (m: any) => m.tokenize("'ab\ncd' x"),
    expect: [
      { kind: "string", text: "'ab\ncd'", value: "ab\ncd", line: 1, col: 1 },
      { kind: "identifier", text: "x", value: "x", line: 2, col: 5 },
    ],
  },
  {
    group: "raw_strings",
    name: "a trailing doubled quote is consumed as an escape, leaving the string open",
    // 'abc'': after abc, the ' at col 5 is followed by another ' at
    // col 6, so together they are a literal-quote escape - and then
    // input ends with the string never closed.
    run: (m: any) => m.tokenize(String.raw`'abc''`),
    expect: [{ kind: "error", reason: "unterminated_string", line: 1, col: 1 }],
  },

  // ---------------------------------------------------------------
  // nested_comments
  // ---------------------------------------------------------------
  {
    group: "nested_comments",
    name: "an inner /* keeps the outer comment open across the first */",
    // "/* a /* b */ c */ x": depth 0->1 at col1, ->2 at col6, ->1 at
    // col11 (still inside; " c " is comment content), ->0 at col16.
    // Only token: identifier x at col19.
    run: (m: any) => m.tokenize("/* a /* b */ c */ x"),
    expect: [{ kind: "identifier", text: "x", value: "x", line: 1, col: 19 }],
  },
  {
    group: "nested_comments",
    name: "a comment that closes only its inner level is still unterminated",
    // "/* a /* b */": depth 0->1->2->1, EOF reached at depth 1 (not
    // 0), so the whole thing is unterminated_comment at the outer
    // /*'s position, col 1.
    run: (m: any) => m.tokenize("/* a /* b */"),
    expect: [{ kind: "error", reason: "unterminated_comment", line: 1, col: 1 }],
  },
  {
    group: "nested_comments",
    name: "three levels of nesting all have to close",
    // "/* /* /* deep */ */ */ z": three opens, three closes, comment
    // ends at col 22; z follows at col 24.
    run: (m: any) => m.tokenize("/* /* /* deep */ */ */ z"),
    expect: [{ kind: "identifier", text: "z", value: "z", line: 1, col: 24 }],
  },

  // ---------------------------------------------------------------
  // positions
  // ---------------------------------------------------------------
  {
    group: "positions",
    name: "astral char in a raw string counts as one column",
    // ' (col1) 😀 (col2, one code point) ' (col3) sp (col4) foo (col5).
    run: (m: any) => m.tokenize("'\u{1F600}' foo"),
    expect: [
      { kind: "string", text: "'\u{1F600}'", value: "\u{1F600}", line: 1, col: 1 },
      { kind: "identifier", text: "foo", value: "foo", line: 1, col: 5 },
    ],
  },
  {
    group: "positions",
    name: "astral char in a double-quoted string counts as one column",
    // " (col1) x (col2) 😀 (col3) y (col4) " (col5) sp (col6) z (col7).
    run: (m: any) => m.tokenize('"x\u{1F600}y" z'),
    expect: [
      { kind: "string", text: '"x\u{1F600}y"', value: "x\u{1F600}y", line: 1, col: 1 },
      { kind: "identifier", text: "z", value: "z", line: 1, col: 7 },
    ],
  },
  {
    group: "positions",
    name: "astral char in a block comment counts as one column",
    // "/* " (cols1-3) 😀 (col4) " */" (cols5-7... ) w follows at col9.
    run: (m: any) => m.tokenize("/* \u{1F600} */ w"),
    expect: [{ kind: "identifier", text: "w", value: "w", line: 1, col: 9 }],
  },
  {
    group: "positions",
    name: "newline resets column; a tab is exactly one column",
    // "a" line1 col1; real newline; tab occupies col1 of line 2; "b" col2.
    run: (m: any) => m.tokenize("a\n\tb"),
    expect: [
      { kind: "identifier", text: "a", value: "a", line: 1, col: 1 },
      { kind: "identifier", text: "b", value: "b", line: 2, col: 2 },
    ],
  },

  // ---------------------------------------------------------------
  // error_recovery
  // ---------------------------------------------------------------
  {
    group: "error_recovery",
    name: "bad_number resyncs after the alnum run, including trailing letters",
    // "007 x": run "007" (leading zero, len>1) is invalid -> error at
    // col1, resumes at col4 (space), then identifier x at col5.
    // "12ab,": run "12ab" (digits+letters) is invalid -> error at
    // col1, resumes at col5 (,), then punct , at col5.
    run: (m: any) => [m.tokenize("007 x"), m.tokenize("12ab,")],
    expect: [
      [
        { kind: "error", reason: "bad_number", line: 1, col: 1 },
        { kind: "identifier", text: "x", value: "x", line: 1, col: 5 },
      ],
      [
        { kind: "error", reason: "bad_number", line: 1, col: 1 },
        { kind: "punct", text: ",", value: ",", line: 1, col: 5 },
      ],
    ],
  },
  {
    group: "error_recovery",
    name: "bad_escape abandons the string and resumes right after the escape char",
    // "a\qb" y (8 raw chars): \q is invalid at col3-4; error at the
    // string's start (col1); resumes at col5. "b" becomes an
    // ordinary identifier. The " at col6 then starts a fresh string
    // attempt that runs through " y" to EOF without closing:
    // unterminated_string at col6.
    run: (m: any) => m.tokenize(String.raw`"a\qb" y`),
    expect: [
      { kind: "error", reason: "bad_escape", line: 1, col: 1 },
      { kind: "identifier", text: "b", value: "b", line: 1, col: 5 },
      { kind: "error", reason: "unterminated_string", line: 1, col: 6 },
    ],
  },
  {
    group: "error_recovery",
    name: "unterminated double-quoted string resyncs at the newline, not past it",
    // "abc (cols1-4), then a real newline before any closing " ->
    // unterminated_string at col1; the newline itself is not
    // consumed, so line 2 starts fresh with def at col1.
    run: (m: any) => m.tokenize('"abc\ndef'),
    expect: [
      { kind: "error", reason: "unterminated_string", line: 1, col: 1 },
      { kind: "identifier", text: "def", value: "def", line: 2, col: 1 },
    ],
  },

  // ---------------------------------------------------------------
  // eof_edges
  // ---------------------------------------------------------------
  {
    group: "eof_edges",
    name: "empty input produces no tokens",
    run: (m: any) => m.tokenize(""),
    expect: [],
  },
  {
    group: "eof_edges",
    name: "a leading BOM is skipped without affecting line or col",
    run: (m: any) => m.tokenize("\uFEFFx"),
    expect: [{ kind: "identifier", text: "x", value: "x", line: 1, col: 1 }],
  },
  {
    group: "eof_edges",
    name: "double-quoted string unterminated at EOF (no newline in between)",
    run: (m: any) => m.tokenize('"abc'),
    expect: [{ kind: "error", reason: "unterminated_string", line: 1, col: 1 }],
  },
  {
    group: "eof_edges",
    name: "block comment unterminated at EOF",
    run: (m: any) => m.tokenize("/* abc"),
    expect: [{ kind: "error", reason: "unterminated_comment", line: 1, col: 1 }],
  },
];
