# phrack2md

Convert the extracted Phrack issue text files into Markdown.

## What it does

- Walks an **issues directory** that contains one subdirectory per issue
  (`phrack1/`, `phrack2/`, ... `phrack72/`).
- Inside each issue it reads the individual *phile* files (`1.txt`, `2.txt`, ...)
  **in numeric order** — not the alphabetical order (`1, 10, 11, 2, ...`) that a
  plain `cat *.txt` produced, which silently reordered the philes. The combined
  `phrackNN.txt` file, if present, is ignored.
- Writes **one Markdown file per issue** to the output directory:
  `markdowns/phrackNN.md`.

The converter's guiding rule is **never lose content**. Anything it does not
positively recognise as structural markup is preserved verbatim, and every
run of the tool is checked so that each issue's Markdown contains at least as
many alphanumeric characters as its sources.

## Usage

Requires Python 3.8+ (standard library only — no third-party packages).

```sh
python3 phrack2md.py --issues ../phrack_mag/issues --out ../markdowns
```

Convert only some issues:

```sh
python3 phrack2md.py --issues ../phrack_mag/issues --out ../markdowns --only 55 62 70
```

On Windows the project's own toolchain lives in WSL, so run it there:

```sh
wsl python3 phrack2md.py --issues ../phrack_mag/issues --out ../markdowns
```

## How the Phrack format is mapped

Phrack's layout changed across its 40-year run; the converter handles all eras.

| Phrack source                                             | Markdown output |
|----------------------------------------------------------|-----------------|
| `--- title: ... author: ... ---` front matter            | `## NN Title` heading + author/phile metadata line |
| Leading banner (ASCII logo, `==Phrack Inc.==`, volume line, `\|=--=[ Title ]=--=\|` box) | one fenced code block, kept verbatim so it never splits |
| `----[ 2.1 - Section ]----` / `--[ Intro`                | `###` section heading; an explicit section number nests it (`2` → H3, `2.1` → H4, `2.1.1` → H5; a decorative `.0` is ignored) |
| Underlined title (`Title` then `====` / `----` / `~~~~`) | `###` / `####` heading |
| A header row over per-column underlines (`Command   License` / `-------   -------` / rows) | Markdown table when it is clean (2-8 columns, up to 60 rows, header text over its underline, no code-like rows); otherwise a verbatim fenced block |
| A lone dotted-number title between blank lines (`2.1  Basic Structure`) | heading nested by its number (`2.1` -> H4, `2.1.1` -> H5); consecutive ones (a table of contents) stay text |
| `====` / `----` / `~~~~` / ```` divider on its own line  | `---` thematic break |
| Ordered lists (`1. x`, `2) x`, `3 - x`, `4  x`) with increasing numbers | Markdown ordered list; original numbers kept, sub-items indented |
| `<++> path !checksum ... <-->` extract blocks            | `**Extracted file:**` label + fenced code |
| `begin 644 name ... end` uuencode / base64               | `**Encoded file:**` label + fenced code |
| Source code, hex/disassembly dumps, ASCII art, aligned tables, `\|`-bordered boxes | fenced code block (layout preserved) |
| Assembly (incl. AT&T-suffixed `movl`/`pushl` and `%reg`/`$imm` operands) | fenced, even flush-left or embedded inside a list item |
| A numbered *code* listing (`1. u_int off = 0;` ...)       | one fenced block, line numbers kept (not turned into a prose list) |
| A BASIC program listed with climbing line numbers (`10 print` / `20 &"..."` / `80 z$=sys(...)`), wrapped continuation lines and all | one fenced block, not split at the `print`/`echo` lines |
| A shell compound command (`case ... in` ... `esac`, `if ... then` ... `fi`, `for`/`while`/`until ... do` ... `done`) | one fenced block, kept together even when its body is `echo "an English sentence."` lines |
| A WebAssembly-text / Lisp S-expression listing (`(func $chr ...` / `global.get` / `;;` comments) | one fenced block |
| Modern issues' own ```` ``` ```` fenced blocks           | passed through verbatim |
| `\|=--=[ text ]=--=\|` boxes in the body (any number of `[ ]` segments) | blockquote of the inner text, nothing dropped |
| Everything else                                          | prose paragraphs, de-indented so old issues' indented body text flows normally instead of turning into code |

Prose is escaped so it does not trigger Markdown by accident: a `<` that could
open an HTML tag (`<filename>`, `#include <stdio.h>` mentioned in text,
`<user@host>`) becomes `&lt;` so it is not swallowed -- escaping just the `<` is
enough, so a bare `1>` or `-->` in prose stays intact -- and a leading `#` (used
in Phrack as a "number" sign or comment marker) is escaped so it does not become
a heading. Text inside code fences is left untouched. `*emphasis*` written by
the author is deliberately left as-is so it renders as intended.

Setext headings (a title underlined by `====`/`----`/`~~~~`) are only accepted
when the underline is about as wide as the title and the title does not read like
a sentence, so a prose line sitting above a full-width divider stays prose.

Some files lost the high bit on a word-initial capital, leaving a control byte
(Ctrl-A..Ctrl-Z) before lowercase letters, e.g. `\x13et up` for `Set up`; these
are restored to the capital letter before other control bytes are dropped.

A line that wraps a sentence onto a number (`... Dark Side of the Moon of` /
`818.  He gives credit ...`) is not split into a false list item: a `N.` line
only starts a list when the previous line actually ended a sentence or clause.

Line breaks the author made on purpose are kept: Markdown would otherwise join
consecutive lines into one paragraph, so Pro-Phile records (`Handle: ...` /
`Call him: ...`), addresses, signatures and dot-leader tables of contents get a
trailing two-space hard break. A line is treated as deliberately ended when the
next line's first word would still have fitted on it (i.e. it was not wrapped),
when both lines are `Label: value` fields, or when it is a dot-leader row.
Ordinary wrapped prose still reflows.

A code block split by a blank line keeps going when the lines after the gap are
short code-like lines (C parameter lists, `label: RET` assembly, shell), and an
indented line inside a code block only ends it if it is a real sentence (7+
words or sentence punctuation). `VAR=value` assignments count as code.

Older philes often have no blank lines at all: a paragraph starts with an
indented first line, and literal values or typed commands sit on their own
lines with a different indentation right after a line ending in `:`
(`Common passwords are:` / ` SYSLIB` / ` SYSGEN`). Such an indented first line
starts a new paragraph, and the run of short literals becomes a fenced block
(an assembly label like `.copier:` introducing the run is kept inside it).
Bullet items (`- ...`) are left as Markdown lists. Values typed or shown after a
"type:" / "you will see:" cue are fenced too (`Type:` / `log`, `cp ipl cms`).

Bullet lists that use `o` as the marker (`o  Intoxicated Pedestrian` / `o  ...`,
with wrapped lines folded in) become Markdown `-` bullets. (The `o` marker letter
is replaced by `-`, the one place a structural marker is dropped.) A two-column
table of contents drawn with `o` bullets (`o  Background         o  Logical
Names`) is split into one bullet per cell, read down the left column and then the
right. Author emphasis written with underscores (`_Hack_`) already renders as
italic, so it is left as-is.

Plain-text tables whose header is underlined column by column become real Markdown
tables: the header words must sit over their dash groups (+/-1), every row must keep
its cells inside those columns, and word-by-word underlined titles need 3+ rows so a
title is not mistaken for a table. Wrapped continuation rows are merged into the cell
above, `|` inside a cell is escaped, and anything ragged, code-like or too large is
fenced verbatim instead. A colon-numbered list (`1: Index written by X` / `2: ...`)
is a list only when it starts at 0 or 1 and its text opens with a word, so hex dumps
(`0a: 804e08ac`) and line-numbered listings (`175: code`) stay code.

Aligned-column detection (`is_tabular`) ignores two things that are not columns: the
padding after a list marker (`(3)   Pac Tel ...`, `a)   text`) and a single 3-space
justification gap in the middle of a long English sentence (`The   company has ...`,
gap followed by a lowercase word). Wrapped prose inside a list item that merely
mentions a hex value or `func()` is kept as text rather than fenced, and a typed-value
cue (`label:` + short literal) is not applied to a centered all-caps heading with its
subtitle or to a quoted English phrase.

Parenthesised markers (`handle_paren_list`) -- `(1) ... (2) ...` with lettered
sub-items `(a) (b)` (letters keep counting across the numbers: `(a)(b)`, `(3)`, `(c)(d)`) --
become an ordered list with nested `- (a)` bullets. The run must start at `(1)` or
`(a)`, each kind must increase, items may be blank-separated, and wrapped lines belong
to the item until a table/code row or a new paragraph (a full stop followed by a change
of indent). An out-of-sequence `(A)` line inside an item becomes a sub-bullet.

Ordered lists numbered with a `#` ("#1 Index / #2 Loopback") or with Roman
numerals in sequence (`I.  Do not ...` / `II.  ...`, `i) ... / ii) ...`) are
rendered as Markdown ordered lists; a lone "I," pronoun or an "L:" dialogue label
is not, because those do not form an increasing sequence.

A one-line section title wrapped in matching decoration becomes a heading:
`*** Gaining Entry ***`, `]Entering Text[`, `>>> News <<<`, `::> Title <::`,
`<:=- Part B : Programming VAX/VMS -=:>`. A multi-line decoration *box* (its
border rows carry no letters) stays as art, so only the one-line titles convert.

An ASCII diagram or table stays in one fenced block even when it contains
horizontal rules drawn inside it (`   = = = = = =`, a tab-indented `-------`): a
`====`/`----`/`~~~~`/spaced rule ends a code block only when it is truly
flush-left (a real full-width divider), never when it is indented or tab-indented
as part of the drawing. A line containing `|` is never turned into a heading (it
is a table row), and a `[636] | ... | ...` table row is kept as data, not mistaken
for a `[N]` reference citation.

A title underlined on the next line becomes a heading, including the Phrack
World News form `Headline            May 10, 1986` whose `----` underlines just
the headline -- rendered as `### Headline — May 10, 1986`. A long underlined line
that reads as a sentence (an auxiliary verb, or ending in `.`/`,`) is a divider
under prose, not a title, and stays prose.

The banner ends at the first real prose sentence even when that sentence happens
to mention "Phrack Magazine" ("For 20 years PHRACK magazine has been ..."), so
the article's opening paragraph is never swallowed into the header block.

Raw uuencoded data without a `begin 644` header (length-prefixed lines in the
uuencode charset) is kept as one fenced block.

A References section stays plain text: a bare URL line or a `[N] "Title"` citation
is never treated as code, and each keeps its own line, so the URLs render as
clickable links instead of being locked inside a code fence. A citation that ends
with a `;` (`[6] Chow, Jin. "...emulation";`) is still a reference; only a `[N]`
line that also carries real code structure (`{`, `}`, `=`, a `word(` call, `->`)
is a code listing (`[1] for ( p=msg; ... ) {`) and gets fenced.

Heading levels form a clean hierarchy: `#` issue, `## NN` phile (chapter), `###`
section, with deeper levels only for numbered subsections. Setext and decorated
titles all sit at `###`, so sections never jump straight from the phile to H4.

C block comments (`/* ... * ... */`), tables with interior `|`, line-numbered
source listings (`575  code` / `576` / `577  code`), and runs of shell commands
(`mkdir subdir` / `cd subdir` / `source /path`) are each kept together as one
fenced block. A `--[` line carrying a long hex address (program output) is
fenced rather than treated as a section heading, while short `0xN` section
numbers stay headings.

### Distinguishing code from indented prose

Older issues indent their whole body, and timelines use hanging indents, so a
"4+ spaces means code" rule would wrap most of the text in code fences. Instead
a run is treated as code only when it carries a real code signal (`#include`,
`#define`, a hex dump, `0x...`, a `;`/`{`/`}` in code context, C-call or
declaration syntax, an assembly instruction), is ASCII art or an aligned table,
or is flush-left/indented **and** does not read as natural-language prose but is
symbol-dense (catching shell, DCL, Perl and other scripting dialects). The C
keyword and `;` tests are deliberately narrow so ordinary words like "free." or
a prose sentence ending in ";" are not mistaken for code.

A flush-left line that merely *embeds* a code token stays prose: a real English
sentence (eight or more words, seven of them real words, a capitalised start, no
trailing `;`/`{`) that happens to mention `(0x42)` or `Object.groupBy()` is not
turned into the start of a code block. Tables, ASCII art and genuine statements
are still fenced. A wrapped continuation line that starts lowercase
(`of this fake chunk is set to 0 in order to trick free(3) into leaving`) counts
as prose too when it is almost all words; C prototypes, statements ending `);`,
`{brace comments}`, `%`/`#` assembly and ` = ` assignments never do.

Inside a code block, the text lines of an unclosed `/* ... */` comment stay code,
and an indented caption after a table and a blank line (`Industry norms based on
469 firms,`) ends the block instead of being swallowed by it.

## Layout

`phrack2md.py` is organised as small single-purpose pieces:

- **I/O & front matter:** `read_text`, `natural_part_key`, `find_issues`,
  `split_front_matter`.
- **Line classifiers:** `looks_indented`, `is_art`, `is_tabular`,
  `has_code_signal`, `is_prose_line`, `is_sentence`, `reads_as_sentence`,
  `starts_code`, `is_block_start`, `dedent`, `esc` (angle-bracket escaping),
  `strong_code` / `struct_count` (listing & script detection).
- **Block handlers** (each consumes lines and returns `(emitted, next_i)` or
  `None`): `consume_banner`, `handle_fence`, `handle_extract`, `handle_uu`,
  `handle_dash_heading`, `handle_deco_heading`, `handle_numbered_title`, `handle_pipe_banner`,
  `handle_column_table`, `handle_setext_heading`,
  `handle_rule`, `handle_numbered_code` (incl. BASIC listings),
  `handle_sexpr_block` (WebAssembly-text / Lisp), `handle_shell_construct`
  (`case`/`if`/`for`/`while`), `handle_shell_block`, `handle_list`,
  `handle_code`, `handle_paragraph`.
- **Drivers:** `convert_body` (runs the handlers in priority order),
  `convert_issue`, `main`.

## Later additions

- **Backtick-rule fences (phrack72):** when a phile has an even number (>= 6) of
  rule lines made of 20+ backticks, `handle_backtick_fence` is tried first and
  treats each pair as an open/close fence for a code block. `_code_continuation`
  never swallows such a rule line, so the pairing cannot shift.
- **Bare dash headings:** `-- 1.2 - Title` standing alone between blank lines
  becomes a heading (level from the number depth).
- **Extra code signals:** VMS/DCL lines (`$ command`, `!!!!` banners via
  `_dcl_line`, with an English-prose guard), Cisco prompts (`RE_CISCO`), IDA
  disassembly addresses and elfsh `[NN] PT_...` listings (`RE_IDA`). Inside an
  IDA listing, bare address-only lines and the flush-left `====` rule stay in
  the same fence.
- **Hex dumps:** `RE_HEXDUMP` accepts `offset: xx xx xx ...` (3+ hex digits) with or without the
  colon.
- **Art:** a long run of one repeated digit separated by spaces (phrack67) counts
  as art.
- **`handle_code`:** a blank-separated line that starts with a capital letter and
  reads as a >= 7-word sentence ends the block when everything before it was
  indented code.
