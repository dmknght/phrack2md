#!/usr/bin/env python3
"""
phrack2md - convert extracted Phrack issue text files to Markdown.

The Phrack archive tarballs extract to one text file per *phile* (article),
named ``1.txt``, ``2.txt`` ... inside a per-issue directory (``phrackNN/``).
This tool walks an issues directory, reads the philes of each issue *in
numeric order* (not the alphabetical order the old join step used, which put
``10.txt`` before ``2.txt``), and writes one Markdown file per issue to an
output directory.

Design priority: never lose content. Recognised structural markup is turned
into Markdown; anything ambiguous is preserved verbatim, and monospace-looking
material (ASCII art, code, tables) is wrapped in fenced code blocks so it keeps
its layout instead of being mangled by Markdown reflow.

Usage:
    python3 phrack2md.py --issues ISSUES_DIR --out MARKDOWNS_DIR
    python3 phrack2md.py --issues ../phrack_mag/issues --out ../markdowns
    python3 phrack2md.py --issues ../phrack_mag/issues --out ../markdowns --only 55 62

Pure standard library; runs on any Python 3.8+.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #

def read_text(path: str) -> str:
    """Read a phile as text, tolerating the messy encodings in the archive.

    ~97% of files are ASCII, ~3% UTF-8, and a handful carry stray control
    bytes (Ctrl-Z end markers, ESC, form-feeds). We decode as UTF-8 with
    replacement, normalise line endings, and strip control characters that
    would otherwise show up as garbage in the Markdown.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Some files lost the high bit on a word-initial capital, leaving a Ctrl
    # letter (Ctrl-A..Ctrl-Z = 0x01..0x1a) right before lowercase letters, e.g.
    # "\x13et up" for "Set up". Restore those to the uppercase letter.
    text = re.sub(
        r"[\x01-\x08\x0b\x0c\x0e-\x1a](?=[a-z])",
        lambda m: chr(ord(m.group()) + 0x40),
        text,
    )
    # Drop the remaining control chars (tab/newline kept). \x1a (DOS EOF),
    # \x0c (form feed), ESC sequences etc. are just junk.
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text


def natural_part_key(filename: str) -> int:
    """Sort key so 2.txt < 10.txt (the whole point of this rewrite)."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    return int(stem) if stem.isdigit() else 1 << 30


def find_issues(issues_dir: str):
    """Yield (issue_number, issue_dir_path, [part_paths...]) in issue order."""
    entries = []
    for name in os.listdir(issues_dir):
        m = re.fullmatch(r"phrack(\d+)", name)
        path = os.path.join(issues_dir, name)
        if not m or not os.path.isdir(path):
            continue
        parts = [
            os.path.join(path, f)
            for f in os.listdir(path)
            if re.fullmatch(r"\d+\.txt", f)  # only numeric philes; skip joined phrackNN.txt
        ]
        parts.sort(key=natural_part_key)
        if parts:
            entries.append((int(m.group(1)), path, parts))
    entries.sort(key=lambda e: e[0])
    return entries


# --------------------------------------------------------------------------- #
# Front matter
# --------------------------------------------------------------------------- #

@dataclass
class Phile:
    number: int
    title: str = ""
    author: str = ""
    body: str = ""


def split_front_matter(text: str):
    """Split leading ``---\\n...\\n---\\n`` YAML-ish block from the body.

    Every phile in the corpus has a two-line (title/author) front matter, but
    we parse defensively and fall back to no-front-matter if it is missing.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            meta = {}
            for l in lines[1:i]:
                if ":" in l:
                    k, v = l.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
            return meta, "\n".join(lines[i + 1:])
    return {}, text  # unterminated: treat all as body


# --------------------------------------------------------------------------- #
# Line classification patterns
# --------------------------------------------------------------------------- #

# Section headings:  ----[ 2.1 - Title ]----   or   --[ Intro
RE_DASH_HEAD = re.compile(r"^(-{2,})\[\s*(.*?)\s*(?:\]-*)?\s*$")
# Modern banner box lines:  |=----=[ text ]=----=|   and   |=--------=|
RE_PIPE_BANNER = re.compile(r"^\|=[-=]*(?:\[\s*(?P<inner>.*?)\s*\]=[-=]*)?\|?\s*$")
RE_PIPE_HAS_TEXT = re.compile(r"^\|=.*\[\s*(?P<inner>.+?)\s*\]=.*$")
# Numbered heading text like "2.1.3 - Foo" pulled from inside brackets
RE_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\s*[-.):]?\s*(.*)$")
# Extract markers used from issue ~41 onward. The rest of the open line is the
# path plus an optional ``!checksum`` produced by Phrack's extract utility; we
# keep all of it so nothing is lost.
RE_EXTRACT_OPEN = re.compile(r"^<\+\+>\s*(.*)$")
RE_EXTRACT_CLOSE = re.compile(r"^<-->\s*$")
# uuencode / base64 blocks
RE_UU_BEGIN = re.compile(r"^begin(?:-base64)?\s+[0-7]{3}\s+(\S+)")
RE_UU_END = re.compile(r"^(?:end|====)\s*$")
# Underline runs (setext-style headings and rules)
RE_UNDERLINE = re.compile(r"^\s*([-=~])\1{3,}\s*$")
# Author-written Markdown code fences (modern issues carry these already).
# A real fence line is exactly three backticks plus at most a language token and
# nothing else. Anything else after the backticks (`` ```'  -;;;!'' ``) is
# decorative ASCII art that merely happens to start with backticks, not a fence.
RE_FENCE = re.compile(r"^\s*`{3}(?!`)[ \t]*[A-Za-z0-9_+#.-]*[ \t]*$")
RE_FENCE_CLOSE = re.compile(r"^\s*`{3}\s*$")
# A divider made of 4+ backticks (old-issue ASCII art).
RE_BACKTICK_RULE = re.compile(r"^\s*`{4,}\s*$")

EOF_MARKERS = re.compile(
    r"(\|=\[\s*EOF\s*\]=)|(\bEND OF (THE )?FILE\b)|(^-+\[\s*EOF\b)", re.I
)

# Ordered-list items:  "1. text"  "2) text"  "(3) text"  "(4> text"  "5 - text"
# "6  text"  "#7  text".  The punctuation marker must be followed by whitespace
# so a decimal section number such as "3.1" is not mistaken for an item.
RE_LIST_ITEM = re.compile(
    r"^(?P<indent> *)[(#]?(?P<num>\d{1,3})(?:[.)>](?=\s)|\s+-\s+|\s{2,})\s*(?P<text>\S.*)$"
)

# "1: Index written by X" -- a colon-numbered list. Hex dumps ("0a: 804e08ac") and
# line-numbered listings ("175: code") use the same shape, so this form is only
# accepted when the text opens with a plain word (handle_list also requires the
# list to start at 0 or 1).
RE_LIST_COLON = re.compile(
    r"^(?P<indent> *)(?P<num>\d{1,3}):\s+(?P<text>[A-Za-z][a-z'-]+.*)$"
)


def list_item(line):
    return RE_LIST_ITEM.match(line) or RE_LIST_COLON.match(line)


# A line that begins with a source-line number:  "577   if (x) {"  "576"  "40. foo"
RE_LINENO = re.compile(r"^(\s*)(\d{1,6})(?:[.)]|\s|$)")

# A line that begins with a known Unix command or shell keyword (lowercase only,
# so capitalised prose like "Make it." does not match).
RE_SHELL_CMD = re.compile(
    r"^\s*@?(?:mkdir|rmdir|cd|ls|cat|rm|cp|mv|chmod|chown|chgrp|source|cc|gcc|ld|"
    r"make|echo|printf|grep|egrep|fgrep|sed|awk|perl|touch|ln|tar|gzip|gunzip|"
    r"compress|uncompress|zcat|find|sort|uniq|head|tail|wc|cut|tr|tee|xargs|"
    r"kill|killall|ps|umask|export|setenv|unsetenv|alias|while|until|for|if|"
    r"then|fi|elif|else|case|esac|do|done|exec|eval|chdir|mount|umount|dd|"
    r"strings|od|nm|strip|crontab|su|sudo|passwd|finger|who|w|mail|write|wall|"
    r"telnet|ftp|rsh|rlogin|rcp|ssh|scp|ping|netstat|ifconfig|rmmod|insmod|lsmod|"
    r"modprobe|strace|ltrace|gdb|objdump|readelf|ldd|ar|ranlib|as|nasm|ld|dmesg|"
    r"mount|umount|fdisk|dd|hexdump|xxd|patch|diff|wget|curl|git|svn|make)\b"
)
# Shell-specific punctuation / keywords that confirm a line really is shell.
RE_SHELL_STRONG = re.compile(
    r"[/><|;$`&]|(?:^|\s)-\w|\b(?:while|do|done|if|then|fi|for|else|elif|case|esac|until)\b"
)
# A clear command *argument* (redirect/pipe, backgrounding &, -flag, /path or
# ./path, octal mode); stricter than RE_SHELL_STRONG (no bare "/") so prose like
# "and/or" is excluded.
RE_SHELL_ARGSIG = re.compile(r"[<>|]|\s&\s*$|\s--?[A-Za-z]|\s\.?/\w|\s~/|\s\d{3,4}\s")
# Commands that are NOT also common English words -- so a bare "rmmod foo" or
# "mv a b" in a known-command context is safely treated as a command, while
# "make sure", "do it", "source of" are not.
RE_SHELL_STRICT = re.compile(
    r"^\s*@?(?:mkdir|rmdir|chmod|chown|chgrp|rmmod|insmod|lsmod|modprobe|"
    r"gcc|cc|ld|objdump|readelf|strace|ltrace|gdb|ldd|ranlib|nasm|dmesg|umount|"
    r"fdisk|hexdump|xxd|telnet|ftp|rsh|rlogin|scp|ssh|netstat|ifconfig|killall|"
    r"crontab|sudo|passwd|finger|umask|setenv|unsetenv|grep|egrep|fgrep|sed|awk|"
    r"perl|gzip|gunzip|compress|uncompress|zcat|chdir|strings|rm|cp|mv|ln)\b"
)

# A shell compound-command opener that ends its own line: ``case X in`` /
# ``if ...; then`` / ``for|while|until ...; do``. The matching closers are
# ``esac`` / ``fi`` / ``done``. Used to fence such a construct as one block even
# when its body is full of ``echo "an English sentence."`` lines that would
# otherwise each read as prose.
RE_SH_OPEN = re.compile(
    r"^\s*(?:case\b.*\bin|(?:if|elif)\b.*;?\s*then|(?:for|while|until)\b.*;?\s*do)\s*$"
)
RE_SH_CLOSE = re.compile(r"^\s*(?:esac|fi|done)\b\s*$")

# WebAssembly-text / Lisp S-expression code: a line opening with ``(func`` /
# ``(call`` / ``(local`` ... or carrying a stack instruction like ``global.get``
# or ``i32.const``. Such lines are mostly letters, so the prose test would keep
# them out of a code block; these cues put them back in one.
RE_SEXPR = re.compile(
    r"^\s*\(\s*(?:func|param|result|local|global|module|memory|table|export|"
    r"import|call|call_indirect|type|data|elem|block|loop|if|then|else|end|br|"
    r"br_if|br_table|return|drop|select|nop|unreachable)\b"
)
RE_WASM_INSTR = re.compile(r"\b(?:global|local|i32|i64|f32|f64|memory|table|ref|v128)\.\w+")

# Flush-left signals that a line is source code / a hex dump rather than prose.
RE_CPP = re.compile(r"^\s*#\s*(include|define|ifn?def|endif|else|elif|if|pragma|undef|error)\b")
# A bare URL line or a "[N] ..." reference citation -- plain text, never code, so
# a References section renders as text (with clickable URLs), not a fenced block.
RE_URLLINE = re.compile(r"^\s*(?:https?|ftp)://\S+\s*$", re.I)
RE_CITATION = re.compile(r"^\s*\[\d{1,3}\]\s+\S")


def _textish_ref(line: str) -> bool:
    """A reference citation / URL line that should stay plain text. Excludes a
    ``[636] | ... | ...`` table row ('|' columns) and a numbered *code* listing
    (``[1] for (p=msg; ...){``) -- a real citation carries a title/author, no code."""
    if RE_URLLINE.match(line):
        return True
    if RE_CITATION.match(line) and "|" not in line \
            and not RE_IDA.match(line) \
            and not re.search(r"[{}=]|\w\(|->", line):
        return True
    return False
RE_SHEBANG = re.compile(r"^\s*#!")  # #!/bin/sh , #! /bin/csh -f
# VMS DCL script ("$open/write io f", "$sfile = """, "!!!! comment") and a Cisco
# IOS prompt ("reeb(config)#enable secret x", "reeb#sh run")
RE_DCL = re.compile(r"^\s*(?:\$[a-z][\w.$/]*(?:[\s=:,]|$)|!{4,}(?=\s|$))")
RE_CISCO = re.compile(r"^(?:[A-Za-z][\w-]{2,}\((?:config|conf)[\w-]*\)#\s*\S|[a-z][a-z0-9-]{2,15}#(?=[a-z]))")


# IDA Pro listing ("8019A0E6 arg_0 = dword ptr 8", "8019A0E6 SeAccessCheck proc near")
# and elfsh table rows ("[01] PT_INTERP .interp", "[029] 0x08049888 aw----- .bss")
RE_IDA = re.compile(
    r"^[0-9A-F]{8}(?:[ \t]+(?:;|\w+[ \t]+=[ \t]|\w+[ \t]+(?:proc|endp|db|dd|dw)\b|public\b"
    r"|(?:push|pop|mov|lea|call|jmp|jz|jnz|retn?|cmp|test|add|sub|xor|and|or|nop|int)\b))"
    r"|^\s*\[\d{2,3}\][ \t]+(?:PT_\w+|0x[0-9A-Fa-f]{6,})"
    r"|^[0-9A-F]{4}[ 	]+[0-9A-F]{4}:[0-9A-F]{8}[ 	]+params="
)


def _dcl_line(line: str) -> bool:
    """A DCL script line -- but not a sentence that merely starts with ``$var``."""
    if not RE_DCL.match(line):
        return False
    ws = line.split()
    return not (len(ws) >= 8 and len(RE_ENGLISH_WORD.findall(line)) >= 0.6 * len(ws))
RE_HEXDUMP = re.compile(r"^\s*[0-9A-Fa-f]{3,}:?\s+([0-9A-Fa-f]{2}[\s-]+){3,}")
RE_CODE_TOKENS = re.compile(
    r"""([=)\]}][^;]*;\s*$)         # statement ending ;  (after a code-ish char)
      | ([{}]\s*$) | (^\s*[{}])     # a brace owns the line
      | (\b0x[0-9A-Fa-f]+)          # hex literal
      | (->|::|:=|\+\+|<<|>>|==|!=|&&|\|\|)   # C/C++/DCL operators (note: no bare --)
      | (^\s*[$%]\s+\S)              # a shell / csh / DCL ($ or %) command prompt
      | (^\s*[A-Za-z_]\w*=(?:\S|$))  # a shell / makefile assignment  VAR=value
      | (/\*|\*/)                   # block comments
      | (\b(?:sizeof|malloc|calloc|realloc|free|memcpy|memmove|memset|strcpy|
             strncpy|strcat|strlen|strcmp|strncmp|printf|fprintf|sprintf|snprintf|
             scanf|fopen|fclose|fread|fwrite|kmalloc|kfree|va_start|va_arg|
             mmap|execve|fork|waitpid)\s*\()   # a known C call, needs "("
      | (^\s*(?:void|int|char|short|long|unsigned|signed|float|double|struct|union|
             enum|typedef|static|const|volatile|extern|register|u_int|u_char|
             u_long|u_short|size_t|ssize_t|caddr_t|pid_t|FILE|DIR|va_list|jmp_buf|
             time_t|off_t|mode_t|uid_t|gid_t|socklen_t|u_int32_t|u_int16_t|
             u_int8_t)\b[^.!?]*[(;{])  # a declaration
    """,
    re.X,
)
# An assembly instruction: mnemonic (+ optional AT&T size suffix) followed by an
# operand that is unmistakably asm ($imm, %reg, 0x.., a register, [mem], a size
# keyword). Requiring an asm-shaped operand keeps English words that happen to be
# mnemonics ("and", "or", "not", "call", "in") from matching prose.
_ASM_REG = r"(?:e|r)?(?:ax|bx|cx|dx|si|di|bp|sp|ip|[abcd][lh]|r[0-9]+[bwd]?)\b"
RE_ASM = re.compile(
    r"""^\s*(?:
      # Unambiguous mnemonics -- accept any asm-shaped operand, bare immediates
      # included, because these words never start English prose.
      (?:mov|movz?x|movs?x|lea|push|pusha|pop|popa|sbb|adc|xor|cmp|test|shl|shr|
         sal|sar|rol|ror|imul|idiv|neg|nop|leave|enter|xchg|cdq|cbw|cwd|syscall|
         sysenter|stos|lods|movs|scas|cmps|bswap|bt|bts|btr|setz|setnz|sete|setne|
         jmp|je|jz|jne|jnz|jg|jge|jl|jle|ja|jae|jb|jbe|jc|jnc|js|jns|jo|jno|retn)
        [bwlq]?\s+
        (?:\$|%|\*|0x[0-9A-Fa-f]|-?[0-9]|\[|
           (?:byte|word|dword|qword|tbyte|ptr|offset|near|far|short)\b|""" + _ASM_REG + r""")
    |
      # Mnemonics that are also English words -- demand a clearly-asm operand
      # (%reg, register, [mem], hex or $imm) so prose like "and 29 channels" or
      # "call the function" is not mistaken for assembly.
      (?:and|or|not|add|sub|inc|dec|int|call|ret|div|mul|loop|cld|std|cli|sti|hlt)
        [bwlq]?\s+
        (?:%|\[|0x[0-9A-Fa-f]|\$-?[0-9]|""" + _ASM_REG + r""")
    )""",
    re.X | re.I,
)
# ASCII-art / box-drawing / rule glyph runs that must keep their monospace
# layout. (No bare "...." rule: prose ellipses would match it; genuine dot-leader
# art is caught by the symbol-density test in is_art instead.)
RE_ART = re.compile(r"[│─┌┐└┘├┤┬┴┼╔╗╚╝║═]|[|+/\\_=~*#]{4,}")
# Markers that identify the recurring Phrack header/banner at the top of a phile.
RE_BANNER_MARK = re.compile(
    r"(==\s*Phrack\s*Inc\.?\s*==)|(\bVolume\b.{0,40}\bIssue\b)|(^\s*\|=)"
    r"|(::>)|(\.oO)|(Phrack\s+Magazine)|(Issue\b.{0,30}\b(Phile|File)\b)",
    re.I,
)


def dash_head_level(dashes: int, numbered: str | None) -> int:
    """Choose a Markdown heading depth for a ``----[ ... ]`` line.

    Prefer the depth implied by an explicit section number (``2.1.1`` -> 3);
    otherwise bucket by the number of leading dashes, which loosely tracks
    nesting in most authors' conventions. Clamped to H3..H6 so it always sits
    under the issue (H1) and phile (H2) headings.
    """
    # Section headings sit at H3 directly under the phile (H2); an explicit
    # section number nests deeper (2 -> H3, 2.1 -> H4, 2.1.1 -> H5). The raw dash
    # count is not a reliable nesting signal, so unnumbered headings stay at H3.
    # A decorative trailing ".0" ("1.0", "2.0") does not add a level.
    if numbered:
        numbered = re.sub(r"(?:\.0)+$", "", numbered)
    depth = 3 + (numbered.count(".") if numbered else 0)
    return max(3, min(6, depth))


# --------------------------------------------------------------------------- #
# Line classifiers
# --------------------------------------------------------------------------- #

def looks_indented(line: str) -> bool:
    """A tab or a 4+ space left margin (the Markdown code-indent threshold)."""
    return line.startswith("\t") or bool(re.match(r"^ {4,}\S", line))


def is_spaced_rule(line: str) -> bool:
    """A divider / underline made of a single glyph with optional spaces between,
    e.g. ``~~~~ ~~~~``, ``- - - - -``, ``=== ===``. (A continuous run is handled
    by RE_UNDERLINE; this adds the spaced variants.)"""
    s = line.strip()
    glyphs = [c for c in s if not c.isspace()]
    return len(glyphs) >= 4 and len(set(glyphs)) == 1 and glyphs[0] in "-=~*_"


def is_art(line: str) -> bool:
    """ASCII art / box-drawing / rule glyphs that must keep monospace layout."""
    s = line.strip()
    if not s:
        return False
    if RE_ART.search(line):
        return True
    # a picture drawn from one repeated digit ("55 555 55 5555 ...")
    if len(s) >= 20 and re.fullmatch(r"(\d)(?:\1| )*", s) and s.count(" ") >= 4:
        return True
    # A box border drawn with | = + (e.g. "| text |", "= (c) 1986 ... ="), or a
    # column-separated row, is preformatted, not Markdown table / heading syntax.
    if len(s) >= 3 and s[0] in "|=+" and s[-1] in "|=+":
        return True
    if s.startswith("|") or s.endswith("|") or line.count("|") >= 2:
        return True
    # Symbol-dense lines are ASCII art (logos drawn from $ ` ' ? . ; etc.) that
    # no fixed glyph list would catch; keep them monospaced.
    nonspace = [c for c in s if not c.isspace()]
    if len(nonspace) >= 12:
        symbols = sum(1 for c in nonspace if not c.isalnum())
        if symbols / len(nonspace) >= 0.5:
            return True
    return False


def esc(s: str) -> str:
    """Escape Markdown that prose would trigger by accident:
    * a ``<`` that could open an HTML tag (``<stdio.h>``, ``<filename>``,
      ``<user@host>``) -- escaping just the ``<`` is enough, so a bare ``1>`` in
      prose is left intact, and
    * a leading ``#`` (used in Phrack as a "number" sign or comment marker) that
      would otherwise become an ATX heading.
    Any leading blockquote markers are preserved."""
    m = re.match(r"^(\s*(?:>\s?)*)(.*)$", s, re.S)
    prefix, rest = m.group(1), m.group(2)
    rest = rest.replace("<", "&lt;")
    # Only a real ATX heading form (#..# followed by space or end) needs escaping;
    # "#1", "#define", "#!/bin" etc. are not headings, so leave them intact.
    if re.match(r"#{1,6}(\s|$)", rest):
        rest = "\\" + rest
    return prefix + rest


def struct_count(line: str) -> int:
    """Number of code-structure characters -- high in scripts of any language
    (shell, DCL/VMS, Perl, PL/I ...), low in ordinary prose."""
    return len(re.findall(r"[(){}\[\];=$|&*/\\]", line))


def strong_code(line: str) -> bool:
    """A stricter code test than ``has_code_signal`` (ignores a bare trailing
    ``;`` so prose enumerations do not count) -- used to spot a numbered code
    listing masquerading as an ordered list."""
    return bool(
        RE_CPP.match(line)
        or RE_HEXDUMP.match(line)
        or RE_ASM.match(line)
        or _dcl_line(line)
        or RE_IDA.match(line)
        or RE_CISCO.match(line)
        or re.search(r"[{}]|=|0x[0-9A-Fa-f]|->|\w\(|\)\s*;", line)
    )


def is_tabular(line: str) -> bool:
    """Aligned columns: a gap of 3+ spaces not following sentence or label
    punctuation (so spacing after '.', '!', '?' or a ':' label in prose, e.g.
    'b:   ftp ...', is not mistaken for a column)."""
    # The padding after a list marker "(3)   text" / "a)   text" is not a column,
    # and neither is one stray justified-text gap inside a long English sentence.
    body = re.sub(r"^\s*(?:\(\w{1,3}\)|\w{1,3}[.)])(?= {2,}\S)", "", line)
    gaps = list(re.finditer(r"[^\s.!?:]( {3,})(?=\S)", body))
    if not gaps:
        return False
    if len(gaps) == 1 and len(gaps[0].group(1)) == 3:
        g = gaps[0]
        mid_sentence = body[g.start()].isalpha() and body[g.end()].islower()
        if (mid_sentence and re.match(r"\s*[\"'(]?[A-Za-z][a-z]+\b", body)
                and len(body.split()) >= 8 and len(RE_ENGLISH_WORD.findall(body)) >= 7):
            return False
    return True


def has_code_signal(line: str) -> bool:
    """Does the line carry an unambiguous source-code / hex-dump / asm marker?"""
    return bool(
        RE_CPP.match(line)
        or RE_SHEBANG.match(line)
        or RE_HEXDUMP.match(line)
        or RE_ASM.match(line)
        or _dcl_line(line)
        or RE_IDA.match(line)
        or RE_CISCO.match(line)
        or RE_CODE_TOKENS.search(line)
    )


def is_prose_line(line: str) -> bool:
    """A natural-language line: mostly letters, real words, no code markers.

    This is what lets us de-indent the indented prose that older issues use for
    their whole body (and the hanging indents in timelines) instead of wrapping
    it in code fences.
    """
    s = line.strip()
    if not s:
        return False
    if has_code_signal(line) or is_art(line) or is_tabular(line):
        return False
    # A shell command with a real argument (mv ./x /y, chmod 4755 /z) is code.
    if RE_SHELL_CMD.match(line) and RE_SHELL_ARGSIG.search(line):
        return False
    # Ratio of letters among non-space characters (so an address/abbreviation
    # line like "P.O. Box 93" counts as prose, not code).
    nonspace = sum(1 for c in s if not c.isspace())
    letters = sum(c.isalpha() for c in s)
    if nonspace == 0 or letters < 0.5 * nonspace:
        return False
    return re.search(r"[A-Za-z]{3,}", s) is not None


def is_sentence(line: str) -> bool:
    """A natural-language sentence (not merely a word-y code line): prose that is
    long (7+ words) or ends with sentence punctuation."""
    if not is_prose_line(line):
        return False
    if re.search(r"[;{}]\s*$", line):
        return False  # a statement terminator: code
    s = line.strip()
    return len(s.split()) >= 7 or s.endswith((".", "!", "?"))


RE_ENGLISH_WORD = re.compile(r"\b[A-Za-z]{2,}\b")


def reads_as_sentence(line: str) -> bool:
    """A flush-left English sentence that merely *embeds* a code token.

    A line such as ``To wrap the input value (0x42) in an array for use with
    Object.groupBy(),`` carries a hex literal and a method call, so the raw code
    signals fire, yet it is plainly prose: many real words, a capitalised start,
    no statement terminator. Such a line must not open a code block. Tables, art
    and true statements (ending ``;``/``{``, or a C declaration) are excluded so
    genuine code is unaffected.
    """
    s = line.strip()
    if not s or not s[:1].isalnum():
        return False
    if is_tabular(line) or is_art(line) or RE_CPP.match(line):
        return False
    if re.search(r"[;{]\s*$", s):
        return False
    if re.search(r"\)\s*;|\{[^}]*\}\s*$", s):
        return False  # a statement end, or a trailing {brace comment}
    words = len(s.split())
    eng = len(RE_ENGLISH_WORD.findall(s))
    if re.match(r"(?:unsigned|signed|const|struct|static|extern|void|char|short|int|long|size_t|ssize_t)\b", s) and re.search(r"[(,)]", s):
        return False  # a C prototype / parameter list, not a sentence
    lettered = sum(1 for w in s.split() if re.search(r"[A-Za-z]", w))
    if not s[:1].isupper() and (re.search(r"[{}%#\t]|\s=\s", line) or len(re.findall(r"\b[A-Za-z]+\b", s)) < 0.75 * lettered):
        # A wrapped continuation ("... the loop[2] of the frontlink() macro while")
        # starts lowercase and is nearly all words; statements, assignments and
        # commented assembly never read as one.
        return False
    return words >= 8 and eng >= 7


def starts_code(lines, i: int) -> bool:
    """Should a preformatted (code/art/table) block begin at line i?"""
    l = lines[i]
    if not l.strip():
        return False
    if _textish_ref(l):
        return False  # a URL / "[N]" citation is plain text (References section)
    if reads_as_sentence(l):
        return False  # an English sentence that merely embeds a code token
    if has_code_signal(l) or is_art(l) or is_tabular(l):
        return True
    if l.startswith("\t") or re.match(r"^ {4,}\S", l):
        # Indented (tab or 4+ spaces): code unless it reads as natural-language
        # prose -- old issues indent whole paragraphs and greets with tabs.
        return not is_prose_line(l)
    # Flush-left, symbol-dense and not natural language: a line of some scripting
    # language (shell, DCL, Perl, PL/I) that our C/asm signals do not name.
    return (not is_prose_line(l)) and struct_count(l) >= 3


def is_block_start(lines, i: int) -> bool:
    """Would line i begin some non-prose block (used to end a paragraph)?"""
    l = lines[i]
    s = l.strip()
    if RE_FENCE.match(l):
        return True
    if s.startswith("<++>") or RE_UU_BEGIN.match(s):
        return True
    dm = RE_DASH_HEAD.match(l)
    if dm and dm.group(2):
        return True
    if RE_PIPE_BANNER.match(l):
        return True
    if RE_UNDERLINE.match(l) or RE_BACKTICK_RULE.match(l) or is_spaced_rule(l):
        return True
    if RE_LIST_ITEM.match(l):
        return True
    # a shell command with a real argument ends a paragraph so it can be fenced
    if RE_SHELL_CMD.match(l) and RE_SHELL_ARGSIG.search(l):
        return True
    # an "o  " bullet (o + 2 spaces) starts a list
    mo = RE_O_BULLET.match(l)
    if mo and len(mo.group("sp")) >= 2:
        return True
    return False


def dedent(lines):
    """Strip the common leading whitespace from a block, keeping alignment."""
    indents = [len(l) - len(l.lstrip(" \t")) for l in lines if l.strip()]
    if not indents:
        return list(lines)
    cut = min(indents)
    return [l[cut:] if l.strip() else "" for l in lines]


# --------------------------------------------------------------------------- #
# Block handlers -- each consumes lines[i:] and returns (emitted, next_i),
# or None when it does not apply. Ordering in convert_body encodes priority.
# --------------------------------------------------------------------------- #

def consume_banner(lines):
    """Grab the recurring Phrack header at the very top of a phile.

    Returned as a single verbatim code block so the ASCII logo / ==Phrack Inc.==
    / volume / title banner is preserved intact and never split across several
    Markdown blocks. Returns (block_lines, next_i) or None when there is no
    recognisable banner.
    """
    n = len(lines)
    i = 0
    while i < n and not lines[i].strip():
        i += 1
    if i >= n:
        return None
    start = i
    j = i
    saw_marker = False
    limit = min(n, i + 70)
    while j < limit:
        l = lines[j]
        s = l.strip()
        if not s:
            j += 1
            continue
        if is_art(l) or RE_UNDERLINE.match(l):
            j += 1
            continue
        dm = RE_DASH_HEAD.match(l)
        if (dm and dm.group(2)) or RE_FENCE.match(l):
            break  # a real section heading / fence ends the banner
        if j > start and (setext_heading(lines, j) or column_table(lines, j)):
            break  # an underlined heading (e.g. a PWN news headline) starts content
        if len(s) >= 45 and len(s.split()) >= 8:
            break  # a substantial prose sentence (checked before the marker test,
            # so prose that merely mentions "Phrack Magazine" does not extend the banner)
        if RE_BANNER_MARK.search(l):
            saw_marker = True
            j += 1
            continue
        j += 1  # a short, centred line (title / author / date): part of banner
    if not saw_marker:
        return None
    block = lines[start:j]
    while block and not block[-1].strip():
        block.pop()
    if not block:
        return None
    return block, j


def handle_fence(lines, i):
    """Pass an author's own ``` fenced block (modern issues) through verbatim."""
    if not RE_FENCE.match(lines[i]):
        return None
    n = len(lines)
    out = [lines[i].rstrip()]
    j = i + 1
    while j < n:
        out.append(lines[j])
        if RE_FENCE_CLOSE.match(lines[j]):
            j += 1
            break
        j += 1
    out.append("")
    return out, j


def handle_backtick_fence(lines, i):
    """A line of 20+ backticks opening a block closed by another such line is
    the author's own code fence (phrack72 writes ```` rules in place of ```)."""
    if not (RE_BACKTICK_RULE.match(lines[i]) and len(lines[i].strip()) >= 20):
        return None
    j = i + 1
    while j < len(lines) and not (RE_BACKTICK_RULE.match(lines[j]) and len(lines[j].strip()) >= 20):
        j += 1
    if j >= len(lines):
        return None
    block = lines[i + 1:j]
    while block and not block[-1].strip():
        block.pop()
    while block and not block[0].strip():
        block.pop(0)
    return ["```"] + block + ["```", ""], j + 1


def handle_extract(lines, i):
    """Phrack extract block:  <++> path !checksum ... <-->"""
    m = RE_EXTRACT_OPEN.match(lines[i].strip())
    if not m or not lines[i].strip().startswith("<++>"):
        return None
    n = len(lines)
    label = (m.group(1) or "").strip()
    j = i + 1
    buf = []
    while j < n and not RE_EXTRACT_CLOSE.match(lines[j].strip()):
        buf.append(lines[j])
        j += 1
    header = f"**Extracted file:** `{label}`" if label else "**Extracted file:**"
    return [header, "", "```"] + buf + ["```", ""], j + 1


def handle_uu(lines, i):
    """uuencode / base64 block:  begin 644 name ... end"""
    m = RE_UU_BEGIN.match(lines[i].strip())
    if not m:
        return None
    n = len(lines)
    j = i + 1
    buf = [lines[i]]
    while j < n and not RE_UU_END.match(lines[j].strip()):
        buf.append(lines[j])
        j += 1
    if j < n:
        buf.append(lines[j])
        j += 1
    return [f"**Encoded file:** `{m.group(1)}`", "", "```"] + buf + ["```", ""], j


def handle_dash_heading(lines, i):
    """Section heading:  ----[ 2.1 - Title ]----  /  --[ Intro"""
    m = RE_DASH_HEAD.match(lines[i])
    if not m or not m.group(2):
        # bracket-less "-- 2.1 - Title" (phrack70-72)
        b = re.match(r"^-- (\d+(?:\.\d+)*) - (\S.{0,100})$", lines[i].rstrip())
        if b and (i == 0 or not lines[i - 1].strip()) and (i + 1 >= len(lines) or not lines[i + 1].strip()):
            num = b.group(1)
            return ["", "#" * dash_head_level(2, num) + " " + esc(f"{num} - {b.group(2).strip()}"), ""], i + 1
        return None
    inner = m.group(2).strip()
    # A "--[" line carrying a long hex address (0x80495e4) is program output, not
    # a section title (headings use short section numbers like 0x1); let it fall
    # through to the code handler. Short 0xN section numbers stay headings.
    if re.search(r"0x[0-9A-Fa-f]{4,}", inner):
        return None
    # Strip nested bracket-dash noise from odd markup like  ---[ 1 ---[ TITLE ]---
    inner = re.sub(r"-{2,}\[", " ", inner)
    inner = re.sub(r"\]-{2,}", " ", inner)
    inner = " ".join(inner.split())
    if not inner:
        return ["", "---", ""], i + 1
    if EOF_MARKERS.search(lines[i]):
        # An end-of-file marker: a divider, but keep its label so no text is lost.
        return ["", "---", "", f"*{inner}*", ""], i + 1
    num_m = RE_NUMBERED.match(inner)
    num = num_m.group(1) if num_m and num_m.group(2) else None
    level = dash_head_level(len(m.group(1)), num)
    return ["", "#" * level + " " + esc(inner), ""], i + 1


# Decorative one-line section titles: "*** Title ***", "]Title[", ">>> Title <<<"
_DECO_PAIRS = [
    (r"\*{2,}", r"\*{2,}", True),
    (r"={2,}", r"={2,}", True),
    (r"~{2,}", r"~{2,}", True),
    (r">{2,}", r"<{2,}", False),
    (r"::>", r"<::", False),
    (r"<:[=-]+", r"[=-]+:>", False),
    (r"\]", r"\[", False),
    (r"-=+", r"=+-", False),
]
RE_DECO_HEADING = [
    re.compile(r"^\s*" + op + (r"\s+(?P<t>\S.*?\S)\s+" if sp else r"\s*(?P<t>\S.*?\S)\s*") + cl + r"\s*$")
    for op, cl, sp in _DECO_PAIRS
]


def handle_deco_heading(lines, i):
    """A one-line section title wrapped in matching decoration -> heading."""
    s = lines[i]
    if len(s.strip()) > 75:
        return None
    for rx in RE_DECO_HEADING:
        m = rx.match(s)
        if m and re.search(r"[A-Za-z]{2,}", m.group("t")):
            return ["", "### " + esc(m.group("t").strip()), ""], i + 1
    return None


RE_NUMBERED_TITLE = re.compile(
    r"^ {0,3}(?P<num>\d{1,2}(?:\.\d{1,3}){1,3})\.?[ \t]{1,4}(?P<t>[A-Z][A-Za-z0-9].{2,70}?)[ \t]*$"
)


def handle_numbered_title(lines, i):
    """A lone dotted-number section title ("2.1  Basic Structure") -> heading.

    It must stand alone between blank lines: consecutive ones are a table of
    contents and stay as text, and a wrapped sentence never has blank lines on
    both sides.
    """
    m = RE_NUMBERED_TITLE.match(lines[i])
    if not m:
        return None
    n = len(lines)
    if i > 0 and lines[i - 1].strip():
        return None
    if i + 1 < n and lines[i + 1].strip():
        return None
    t = " ".join(m.group("t").split())
    if t.endswith((".", ",", ";", ":", "?", "!")) or is_tabular(t) or "\t" in m.group("t"):
        return None
    if reads_as_sentence(t) or is_sentence(t) or has_code_signal(t):
        return None
    num = m.group("num")
    level = dash_head_level(0, num)
    return ["", "#" * level + " " + num + " " + esc(t), ""], i + 1


def handle_pipe_banner(lines, i):
    """A mid-body |=---=[ text ]=---=| box -> blockquote of its inner text."""
    if not RE_PIPE_BANNER.match(lines[i]):
        return None
    n = len(lines)
    j = i
    texts = []
    while j < n and RE_PIPE_BANNER.match(lines[j]):
        # A single line may hold several bracketed segments, e.g. an author
        # list  |=[ a ]=[ b ]=[ c ]=|  or  |=[ 0x02 ]=[ Title ]=| . Keep them
        # all (joined) so no name / title is dropped.
        segs = [s.strip() for s in re.findall(r"\[\s*(.*?)\s*\]", lines[j]) if s.strip()]
        if segs:
            texts.append(" · ".join(segs))
        j += 1
    if j - i < 2:
        return None  # a single stray pipe line: let another handler take it
    out = [f"> {esc(t)}" for t in texts if not EOF_MARKERS.search(t)]
    if out:
        out.append("")
    return out, j


RE_COL_UNDERLINE = re.compile(r"^\s*[-=~_]{2,}(?:\s+[-=~_]{2,})+\s*$")
TABLE_MAX_ROWS = 60
TABLE_MAX_COLS = 8


def column_table(lines, i):
    """A plain-text table: a header row, a column underline (``---- --------``,
    one dash group per column) and the data rows below it.

    Returns ``(header, spans, rows, end, clean)`` -- header and rows tab-expanded,
    ``end`` the index after the last row -- or None. The header text must sit over
    the underline groups. ``clean`` is true when every row also keeps each cell out
    of the next column (so it can become a Markdown table); a title underlined
    word by word, or prose, fails that. A header that is plainly multi-column
    (2+ spaces or a tab between its cells) whose rows are ragged is still returned,
    with ``clean`` false, so it can be fenced instead of mistaken for a title.
    """
    n = len(lines)
    if i + 1 >= n or not RE_COL_UNDERLINE.match(lines[i + 1].expandtabs(8)):
        return None
    head = lines[i].expandtabs(8).rstrip()
    if not head.strip() or "|" in head or RE_COL_UNDERLINE.match(head):
        return None
    u = lines[i + 1].expandtabs(8)
    sp = [(m.start(), m.end()) for m in re.finditer(r"[-=~_]{2,}", u)]
    if not 2 <= len(sp) <= TABLE_MAX_COLS:
        return None
    for a, b in sp:
        if not head[max(0, a - 1):b + 1].strip():
            return None
    for m in re.finditer(r"\S", head):
        if not any(a - 1 <= m.start() < b + 1 for a, b in sp):
            return None
    rows = []
    j = i + 2
    while j < n and lines[j].strip():
        r = lines[j].expandtabs(8).rstrip()
        if RE_UNDERLINE.match(r) or is_spaced_rule(r) or RE_FENCE.match(r):
            break
        rows.append(r)
        j += 1
    # Word-by-word underlined titles have 1-space gaps; demand more evidence there.
    tight = any(sp[k + 1][0] - sp[k][1] < 2 for k in range(len(sp) - 1))
    clean = len(rows) >= (3 if tight else 2)
    if clean:
        for r in rows:
            padded = r.ljust(sp[-1][1])
            if padded[:max(0, sp[0][0] - 1)].strip():
                clean = False
                break
            # a cell may run past its underline, but never into the next column
            if any(padded[a - 1].strip() for a, _ in sp[1:]):
                clean = False
                break
    if clean:
        return head, sp, rows, j, True
    if re.search(r"\S(?: {2,}|\t)\S", lines[i].strip()):
        keep = 0
        while keep < len(rows) and not is_sentence(rows[keep]):
            keep += 1
        return head, sp, rows[:keep], i + 2 + keep, False
    return None


def _split_cells(row, sp):
    cuts = [0] + [a for a, _ in sp[1:]] + [len(row) + len(sp) + 1]
    row = row.ljust(sp[-1][1])
    return [row[cuts[k]:cuts[k + 1]].strip() for k in range(len(sp))]


def handle_column_table(lines, i):
    """Render a column-underlined text table as a Markdown table, or -- when it is
    long, wide, or carries code -- as one verbatim fenced block."""
    res = column_table(lines, i)
    if not res:
        return None
    head, sp, rows, end, clean = res
    as_code = (
        not clean
        or len(rows) > TABLE_MAX_ROWS
        or sum(1 for r in rows if has_code_signal(r)) * 3 >= len(rows)
    )
    if as_code:
        raw = [lines[k].rstrip() for k in range(i, end)]
        return ["```"] + raw + ["```", ""], end
    table = [_split_cells(head, sp)]
    for r in rows:
        cells = _split_cells(r, sp)
        if not cells[0] and len(table) > 1:
            for k, c in enumerate(cells):      # a wrapped continuation of the row above
                if c:
                    table[-1][k] = (table[-1][k] + " " + c).strip()
        else:
            table.append(cells)

    def cell(c):
        return esc(c).replace("|", "\\|") if c else " "

    out = [""]
    out.append("| " + " | ".join(cell(c) for c in table[0]) + " |")
    out.append("|" + "|".join(" --- " for _ in sp) + "|")
    for cells in table[1:]:
        out.append("| " + " | ".join(cell(c) for c in cells) + " |")
    out.append("")
    return out, end


def setext_heading(lines, i):
    """If lines[i] is a title underlined on the next line, return (text, level).

    Real setext underlines span roughly the title's width (a full-width rule under
    a prose line is a divider, not an underline). Also handles the news-item form
    ``Headline            May 10, 1986`` whose underline matches just the headline.
    """
    n = len(lines)
    if i + 1 >= n:
        return None
    line = lines[i]
    s = line.strip()
    if not s or "|" in s:
        return None  # a '|' means a table row, not a title
    if column_table(lines, i):
        return None
    if not (RE_UNDERLINE.match(lines[i + 1]) or is_spaced_rule(lines[i + 1])):
        return None
    if RE_UNDERLINE.match(line) or is_spaced_rule(line):
        return None
    under = len(lines[i + 1].strip())
    level = 3  # a setext title is a top-level section, directly under the phile
    # News headline + right-aligned date:  "Headline        Date"  (the full line
    # can exceed 78 because of the padded date; the headline itself does not).
    m = re.match(r"(.*?\S)\s{3,}(\S.*)$", s)
    if m:
        head, rest = m.group(1), m.group(2)
        if (len(head) <= 78 and len(head) * 0.5 <= under <= len(head) + 3
                and not head.rstrip().endswith((".", ","))):
            return (head + "  —  " + rest, level)
        return None
    # Plain title.
    if len(s) > 78 or starts_code(lines, i):
        return None
    if not (len(s) * 0.5 <= under <= len(s) + 3):
        return None
    if s.rstrip().endswith((".", ",")):
        return None
    # A long line containing an auxiliary/verb reads as a sentence above a
    # divider, not a title ("This file has been brought to you by ...").
    if len(s.split()) >= 8 and re.search(
        r"\b(?:is|was|are|were|has|have|had|been|will|would|could|should|does|did)\b",
        s, re.I,
    ):
        return None
    return (s, level)


def handle_setext_heading(lines, i):
    """A title line directly underlined by ==== / ---- / ~~~~ (see setext_heading)."""
    res = setext_heading(lines, i)
    if not res:
        return None
    text, level = res
    return ["", "#" * level + " " + esc(text), ""], i + 2


def handle_rule(lines, i):
    """A standalone ==== / ---- / ~~~~ / ```` / spaced ~ ~ ~ divider -> break."""
    if not (RE_UNDERLINE.match(lines[i]) or RE_BACKTICK_RULE.match(lines[i])
            or is_spaced_rule(lines[i])):
        return None
    return ["", "---", ""], i + 1


def handle_shell_construct(lines, i):
    """A shell compound command -- ``case/esac``, ``if/fi``,
    ``for``|``while``|``until``/``done`` -- fenced verbatim from its opener to the
    matching closer keyword.

    The body typically mixes commands with ``echo "a full English sentence."``
    lines that each read as prose, which would otherwise split the construct into
    fragments. Fencing the whole span keeps it as one code block. If the matching
    closer is never found (so the opener was really prose such as a sentence that
    happened to end in ``then``), nothing is consumed.
    """
    if not RE_SH_OPEN.match(lines[i]):
        return None
    n = len(lines)
    depth = 1
    rows = [lines[i].rstrip()]
    blanks = 0
    j = i + 1
    while j < n and depth > 0 and j - i < 600:
        s = lines[j]
        if not s.strip():
            blanks += 1
            if blanks > 1:          # a paragraph gap: not a single tight script
                return None
            rows.append("")
            j += 1
            continue
        blanks = 0
        rows.append(s.rstrip())
        if RE_SH_CLOSE.match(s):
            depth -= 1
        elif RE_SH_OPEN.match(s):
            depth += 1
        j += 1
    if depth != 0:
        return None                 # never closed -> not a real shell construct
    while rows and not rows[-1].strip():
        rows.pop()
    return ["```"] + rows + ["```", ""], j


def _wat_line(s: str) -> bool:
    st = s.strip()
    return bool(
        RE_SEXPR.match(s)
        or RE_WASM_INSTR.search(s)
        or (st.startswith("(") and "$" in st)
        or re.match(r"^[()]+$", st)
        or st.startswith(";;")
    )


def handle_sexpr_block(lines, i):
    """A WebAssembly-text / Lisp S-expression listing, fenced verbatim.

    The lines are mostly letters (``(func $chr (param $c i32) ...``) so they read
    as prose; this groups the run -- S-expressions, ``;;`` comments and lone
    paren lines -- into one code block. A ``$`` identifier or a ``global.get`` /
    ``i32.const`` stack instruction must appear, so a stray parenthetical prose
    line is never swept in.
    """
    first = lines[i]
    if RE_SEXPR.match(first) or RE_WASM_INSTR.search(first) \
            or (first.strip().startswith("(") and "$" in first):
        pass
    elif first.strip().startswith(";;"):
        # A leading ``;;`` comment only opens the block if an S-expression follows.
        k = i + 1
        while k < len(lines) and not lines[k].strip():
            k += 1
        if k >= len(lines) or not RE_SEXPR.match(lines[k]):
            return None
    else:
        return None
    n = len(lines)
    rows = []
    anchor = False
    blanks = 0
    j = i
    while j < n:
        s = lines[j]
        if not s.strip():
            blanks += 1
            if blanks > 1:
                break
            rows.append("")
            j += 1
            continue
        if not _wat_line(s):
            break
        blanks = 0
        if "$" in s or RE_WASM_INSTR.search(s):
            anchor = True
        rows.append(s.rstrip())
        j += 1
    while rows and not rows[-1].strip():
        rows.pop()
    if not anchor or sum(1 for r in rows if r.strip()) < 2:
        return None
    return ["```"] + rows + ["```", ""], j


def handle_shell_block(lines, i):
    """A run of >=2 consecutive shell-command lines -> fenced block.

    Catches flush-left command tutorials (``mkdir subdir`` / ``cd subdir`` /
    ``source /path``) that carry no C-style code signal. At least one line must
    show shell punctuation or a shell keyword so coincidental prose that merely
    starts with a command word is not swept in.
    """
    if not RE_SHELL_CMD.match(lines[i]):
        return None
    n = len(lines)
    rows = []
    strong = 0
    j = i
    while j < n:
        s = lines[j]
        if not s.strip() or not RE_SHELL_CMD.match(s):
            break
        # A long line without any shell punctuation is a prose sentence.
        if len(s.strip()) > 70 and not RE_SHELL_STRONG.search(s):
            break
        rows.append(s.rstrip())
        if RE_SHELL_STRONG.search(s):
            strong += 1
        j += 1
    if not rows:
        return None
    if len(rows) == 1:
        # A lone command still fences if it carries a clear argument signal --
        # a redirect/pipe, a -flag, a /path or ./path, or an octal mode -- and is
        # short (not an English sentence that merely starts with a command word).
        r = rows[0].strip()
        if len(r) > 60 or r.endswith((".", ",", ":")):
            return None
        if not RE_SHELL_ARGSIG.search(r):
            return None
    elif strong < 1:
        return None
    return ["```"] + rows + ["```", ""], j


# BASIC-dialect keywords and sigils, used to recognise a line-numbered program
# (``10 print`` / ``20 &"..."`` / ``80 z$=sys(chr$(3%))``) whose statements carry
# no C-style ``;{}`` signal. Deliberately strong tokens only, so prose that merely
# starts with a number is not mistaken for code.
RE_BASIC_CUE = re.compile(
    r"(?i)\b(?:goto|gosub|resume|extend|erl)\b|\w[%$]|&\"",
)


def handle_numbered_code(lines, i):
    """A source listing printed with line numbers.

    Two shapes are handled: a C/debug dump with consecutive numbers
    (``574`` / ``575  code`` ...) and a BASIC program whose numbers climb in
    steps (``10 print`` / ``20 &"..."`` / ``30 print`` ...), including statements
    that wrap onto an un-numbered continuation line. The run is fenced verbatim so
    the numbering and layout survive. A prose numbered list (``1. First point``)
    is excluded: its delimiter is ``.``/``)`` and its items are not code, and the
    run stops before a real prose sentence so following paragraphs are never
    swallowed.
    """
    m = RE_LINENO.match(lines[i])
    if not m:
        return None
    # Only a whitespace/end delimiter marks a code line number; ``1.``/``1)`` are
    # prose list items, handled elsewhere.
    if lines[i][m.end(2):m.end()][:1] in (".", ")"):
        return None
    n = len(lines)
    rows = []
    numbered = []          # the rows that actually carry an (ascending) line number
    code_hits = 0
    last_num = -1
    j = i
    while j < n:
        line = lines[j]
        if not line.strip():
            break
        mm = RE_LINENO.match(line)
        is_num = bool(mm) and line[mm.end(2):mm.end()][:1] not in (".", ")") \
            and int(mm.group(2)) > last_num
        if is_num:
            last_num = int(mm.group(2))
            rest = line[mm.end():]
            numbered.append(line)
            if rest.strip() and (has_code_signal(rest) or RE_BASIC_CUE.search(rest)):
                code_hits += 1
        elif rows and not is_sentence(line):
            pass            # a wrapped continuation of the previous numbered line
        else:
            break
        rows.append(line)
        j += 1
    # Require a genuine listing: several ascending line numbers and real code in
    # >= half of them, so a prose numbered list is never swallowed.
    if len(numbered) < 3 or code_hits * 2 < len(numbered):
        return None
    return ["```"] + rows + ["```", ""], j


RE_ROMAN_ITEM = re.compile(
    r"^(?P<indent> *)\(?(?P<num>M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3}))"
    r"(?:[.)>]|\s+-\s+)\s+(?P<text>\S.*)$",
    re.I,
)


def roman_to_int(s):
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    s = s.lower()
    total = 0
    for k, ch in enumerate(s):
        v = vals[ch]
        total += -v if k + 1 < len(s) and vals[s[k + 1]] > v else v
    return total


RE_O_BULLET = re.compile(r"^(?P<indent> *)o(?P<sp> +)(?P<text>\S.*)$")
# Two 'o' bullets side by side on one line (a two-column table of contents):
# ``o  Background                     o  Logical Names``.
RE_O_TWOCOL = re.compile(r"^(?P<indent> *)o {2,}(?P<left>\S.*?) {3,}o {2,}(?P<right>\S.*)$")


# A full uuencode line is the length byte 'M' followed by a dense run of encoded
# bytes (no spaces for the first several chars); that dense start is what sets it
# apart from all-caps prose like " MY LIFE JUST BURST ..." (a space comes early).
RE_UU_DATA = re.compile(r"^M[!-`]{8}[ -`]{20,}$")


def _uu_dataish(line):
    return bool(RE_UU_DATA.match(line)) and not re.search(r"[a-z]", line) and \
        sum(c.isdigit() or (not c.isalnum() and c != " ") for c in line) >= 3


def handle_uu_data(lines, i):
    """A block of raw uuencoded data (no ``begin 644`` header) -> one fenced block.
    Such lines are length-prefixed, all in the uuencode charset, no lowercase."""
    if not _uu_dataish(lines[i]):
        return None
    n = len(lines)
    j = i
    buf = []
    while j < n and lines[j].strip() and _uu_dataish(lines[j]):
        buf.append(lines[j])
        j += 1
    if len(buf) < 3:
        return None
    # include a trailing short final line / "end" marker if present
    if j < n and lines[j].strip() in ("end", "`") or (j < n and re.match(r"^[ -`]{1,20}$", lines[j]) and lines[j].strip()):
        buf.append(lines[j]); j += 1
    return ["```"] + buf + ["```", ""], j


def handle_o_bullet(lines, i):
    """A bullet list that uses 'o' as the marker -> Markdown '-' bullets. The
    marker must be ``o`` + 2 spaces (unambiguous; prose never starts "o  word"),
    and wrapped continuation lines (flush-left) are folded into the item."""
    m = RE_O_BULLET.match(lines[i])
    if not m or len(m.group("sp")) < 2:
        return None
    base = len(m.group("indent"))
    n = len(lines)
    items = []          # list[list[str]]: left column text + wrapped continuations
    right = []          # list[str|None]: the right column cell of a two-column row
    twocol = False
    cur = None
    j = i

    def ind_of(s):
        return len(s) - len(s.lstrip(" "))

    while j < n:
        l = lines[j]
        s = l.strip()
        mm = RE_O_BULLET.match(l)
        if mm and ind_of(l) == base and len(mm.group("sp")) >= 2:
            two = RE_O_TWOCOL.match(l)
            if two:
                twocol = True
                items.append([two.group("left").strip()])
                right.append(two.group("right").strip())
                cur = None  # a two-column row never has wrapped continuations
            else:
                cur = [mm.group("text").strip()]
                items.append(cur)
                right.append(None)
            j += 1
            continue
        if cur is not None and s and not is_block_start(lines, j) and not starts_code(lines, j):
            cur.append(s)  # wrapped continuation of the current bullet
            j += 1
            continue
        if not s:  # a blank stays in the list only if another bullet follows
            k = j + 1
            while k < n and not lines[k].strip():
                k += 1
            mk = RE_O_BULLET.match(lines[k]) if k < n else None
            if mk and ind_of(lines[k]) == base and len(mk.group("sp")) >= 2:
                j = k
                continue
        break
    if not items:
        return None
    out = [""]
    # A two-column table of contents is read down the left column, then down the
    # right, so the whole left column is emitted first and the right column after.
    for texts in items:
        out.append("- " + " ".join(esc(t) for t in texts))
    if twocol:
        for r in right:
            if r:
                out.append("- " + esc(r))
    out.append("")
    return out, j


def handle_roman_list(lines, i):
    """An ordered list numbered with Roman numerals (I. II. III. / i) ii) )."""
    m = RE_ROMAN_ITEM.match(lines[i])
    if not m or not m.group("num"):
        return None
    base = len(m.group("indent"))
    upper = m.group("num").isupper()
    expect = roman_to_int(m.group("num"))
    if expect != 1:  # a real list starts at I
        return None
    n = len(lines)
    items = []
    j = i
    cur = None
    while j < n:
        mm = RE_ROMAN_ITEM.match(lines[j])
        ind = len(lines[j]) - len(lines[j].lstrip(" "))
        s = lines[j].strip()
        if (mm and mm.group("num") and ind <= base + 1
                and mm.group("num").isupper() == upper
                and roman_to_int(mm.group("num")) == expect
                and not is_tabular(mm.group("text"))):
            cur = [mm.group("text").strip()]
            items.append((mm.group("num"), cur))
            expect += 1
            j += 1
            continue
        if cur is not None and s and ind > base:
            cur.append(s)
            j += 1
            continue
        break
    if len(items) < 2:
        return None
    # Keep the original Roman numerals (Markdown cannot renumber them, and they
    # are part of the text); a trailing hard break keeps each item on its line.
    out = [""]
    for num, texts in items:
        out.append(f"{num}. " + " ".join(esc(t) for t in texts) + "  ")
    out.append("")
    return out, j


def handle_list(lines, i):
    """A run of >=2 consecutive, strictly incrementing ordered-list items.

    Requiring the numbers to increase (1,2,3 ...) keeps prose that merely starts
    with a number -- phone numbers, dollar amounts, wrapped sentences -- out of
    the list. Continuation lines and blank separators between items are allowed.
    """
    first = list_item(lines[i])
    # An aligned numeric table (3+ space columns *within the item text*) is not a
    # list -- leave it to the code/table handler so its columns are preserved.
    # Wide padding after the marker itself (``(1>     text``) is fine.
    if not first or is_tabular(first.group("text")):
        return None
    if first.re is RE_LIST_COLON and int(first.group("num")) > 1:
        return None
    n = len(lines)
    base = len(first.group("indent"))
    expect = int(first.group("num"))
    items = []  # each: (original_number, [text line, continuation lines...])
    j = i
    cur = None

    def indent_of(s):
        return len(s) - len(s.lstrip(" "))

    while j < n:
        l = lines[j]
        s = l.strip()
        m = list_item(l)
        ind = indent_of(l)
        if m and ind <= base + 1 and int(m.group("num")) == expect and not is_tabular(m.group("text")):
            cur = [m.group("text").strip()]
            items.append((m.group("num"), cur))
            expect += 1
            j += 1
            continue
        if cur is not None and s and ind > base:
            cur.append(s)  # a deeper line: wrapped text or a sub-item
            j += 1
            continue
        if not s:
            # Look past blank line(s): the list continues if the next non-blank
            # line is either the next same-level item or a deeper (sub) line.
            k = j + 1
            while k < n and not lines[k].strip():
                k += 1
            if k < n:
                mk = list_item(lines[k])
                indk = indent_of(lines[k])
                if mk and indk <= base + 1 and int(mk.group("num")) == expect and not is_tabular(mk.group("text")):
                    j = k
                    continue
                if cur is not None and indk > base:
                    j = k
                    continue
        break
    if len(items) < 2:
        return None
    # A numbered *code* listing ("1. u_int off = 0;") is not a prose list; hand
    # it back so the code handler fences it as one block (numbers included).
    code_items = sum(1 for _, t in items if strong_code(t[0]))
    if code_items * 2 >= len(items):
        return None

    def is_pre(line):
        # wrapped prose that merely mentions a hex value / code token
        ws = line.split()
        if (len(ws) >= 6 and not line.rstrip().endswith("..") and not is_tabular(line)
                and not re.search(r"[;{}=<>]", line)
                and len(RE_ENGLISH_WORD.findall(line)) >= 0.7 * len(ws)
                and (line.rstrip().endswith((".", ",")) or line[:1].islower())):
            return False
        if has_code_signal(line) or is_art(line) or is_tabular(line):
            return True
        if RE_SHELL_CMD.match(line) and RE_SHELL_ARGSIG.search(line):
            return True
        # C/C++ control flow or a function call: "if (__builtin_expect (...", etc.
        if re.match(r"^\s*(?:if|for|while|else|switch|return|do|case)\b.*[({]", line) and not reads_as_sentence(line):
            return True
        if re.search(r"\b\w+\s*\([^)]*\)", line) and not is_prose_line(line):
            return True
        # inside a list step, a short bare non-English command (rmmod foo, mv a b)
        # is almost certainly a command -- but a long "ftp is used to ..." sentence
        # that merely starts with a command name is not.
        s = line.strip()
        if (
            RE_SHELL_STRICT.match(line)
            and len(s) <= 40
            and len(s.split()) <= 5
            and not s.endswith((".", ",", ":"))
        ):
            return True
        return False

    # Keep each item's ORIGINAL number (no renumbering) so no digit is lost;
    # continuation / sub lines are indented 3 spaces to stay inside the item,
    # and any run of code/table among them is fenced so it keeps a code anchor.
    out = [""]
    for num, texts in items:
        out.append(f"{num}. {esc(texts[0])}")
        conts = texts[1:]
        k = 0
        while k < len(conts):
            if is_pre(conts[k]):
                out.append("   ```")
                while k < len(conts) and is_pre(conts[k]):
                    out.append("   " + conts[k])
                    k += 1
                out.append("   ```")
            else:
                out.append("   " + esc(conts[k]))
                k += 1
    out.append("")
    return out, j


RE_PAREN_ITEM = re.compile(r"^(?P<indent> *)\((?P<mark>\d{1,2}|[a-z])\)\s+(?P<text>[A-Za-z0-9\"'].*)$")


def _paren_next(mark, nd, nl):
    """Is ``mark`` the next number (nd) or the next letter (nl) of the run?"""
    if mark.isdigit():
        return int(mark) == nd
    return ord(mark) == nl


def paren_run(lines, i):
    """Items of a ``(1) ... (a) ...`` run starting at i, or None.

    The run starts at (1) or (a); numbers and letters each keep their own
    increasing sequence (letters may continue across numbers: (a)(b) (c)(d)),
    items may be separated by blank lines, and an item's wrapped lines run to the
    next blank line or marker. Needs 2+ items and mostly non-code text.
    """
    first = RE_PAREN_ITEM.match(lines[i])
    if not first or first.group("mark") not in ("1", "a"):
        return None
    base = len(first.group("indent"))
    n = len(lines)
    nd, nl = 1, ord("a")
    items = []  # (mark, [text, wrapped...])
    cont_ind = None
    j = i
    while j < n:
        m = RE_PAREN_ITEM.match(lines[j])
        if m and abs(len(m.group("indent")) - base) <= 1 and _paren_next(m.group("mark"), nd, nl):
            mark = m.group("mark")
            if mark.isdigit():
                nd += 1
            else:
                nl += 1
            cur = [m.group("text").strip()]
            items.append((mark, cur))
            j += 1
            while j < n and lines[j].strip() and not RE_PAREN_ITEM.match(lines[j]):
                l = lines[j]
                ind = len(l) - len(l.lstrip(" "))
                if is_tabular(l.strip()) or strong_code(l.strip()):
                    break  # a table row / code line right after the item is not its text
                if cont_ind is None:
                    cont_ind = ind
                elif ind != cont_ind and cur[-1].rstrip().endswith((".", "!", "?", ":")):
                    break  # back at another indent after a full stop: a new paragraph
                cur.append(l.strip())
                j += 1
            k = j
            while k < n and not lines[k].strip():
                k += 1
            if k < n and k - j <= 2:
                mk = RE_PAREN_ITEM.match(lines[k])
                if mk and abs(len(mk.group("indent")) - base) <= 1 and _paren_next(mk.group("mark"), nd, nl):
                    j = k
                    continue
            break
        break
    if len(items) < 2:
        return None
    texts = [t for _, ts in items for t in ts]
    if sum(1 for t in texts if strong_code(t)) * 3 >= len(texts) or any(is_tabular(t) for t in texts):
        return None
    return items, j


def handle_paren_list(lines, i):
    """``(1) text`` / ``(a) text`` -> ordered list with lettered sub-bullets."""
    run = paren_run(lines, i)
    if run is None:
        return None
    items, j = run
    out = [""]
    nested = False
    for mark, texts in items:
        if mark.isdigit():
            out.append(f"{mark}. {esc(texts[0])}")
            pad = "   "
            nested = True
        else:
            out.append(("   " if nested else "") + f"- ({mark}) {esc(texts[0])}")
            pad = "      " if nested else "  "
        for t in texts[1:]:
            # an out-of-sequence "(A) ..." line inside an item is a sub-item
            sub = "- " if re.match(r"\(\w{1,4}\)\s", t) else ""
            out.append(pad + sub + esc(t))
    out.append("")
    return out, j


def handle_code(lines, i, out):
    """A run of code / hex dump / ASCII art / aligned table -> fenced block.

    ``out`` is the document so far, so a code run that lands immediately after a
    previous one (separated only by blank lines) is merged into it rather than
    opening a second adjacent fence.
    """
    if not starts_code(lines, i):
        return None
    n = len(lines)
    j = i
    buf = []
    blanks = 0
    open_comment = False  # inside an unclosed /* ... */ C comment
    while j < n:
        cur = lines[j]
        if not cur.strip():
            blanks += 1
            if blanks >= 2:
                break
            buf.append(cur)
            j += 1
            continue
        gap, blanks = blanks, 0
        # A backtick divider / fence always ends the run (it would break the
        # fence). A ==== / ---- / ~~~~ / spaced rule ends it only when flush-left:
        # an *indented* one is a horizontal line of an ASCII diagram (e.g.
        # "   = = = = = =" inside a box), which must stay in the block.
        if RE_BACKTICK_RULE.match(cur) or RE_FENCE.match(cur):
            break
        if _textish_ref(cur):
            break  # a bare URL / "[N]" citation is text, not code
        ida_run = any(RE_IDA.match(b) for b in buf[-3:])  # IDA listing: its ==== is part of it
        if (RE_UNDERLINE.match(cur) or is_spaced_rule(cur)) and cur[:1] not in (" ", "\t") and not ida_run:
            break  # a flush-left divider ends it; an indented one is diagram art
        # A title underlined on the next line is a heading, not code content.
        if j > i and (setext_heading(lines, j) or column_table(lines, j)):
            break
        # After a blank line, an indented plain-prose line of 4+ words that ends
        # with punctuation ("Industry norms based on 469 firms,") is the table's
        # caption/footer, not another row.
        if (gap and j > i and looks_indented(cur) and is_prose_line(cur) and len(cur.split()) >= 4
                and re.match(r"\s*[A-Za-z]", cur)
                and any(is_tabular(b) for b in buf[-4:])
                and cur.rstrip().endswith((",", ".", ";", ":", "!", "?"))
                and not re.search(r"[;{}]\s*$", cur)
                and not (has_code_signal(cur) or is_art(cur) or is_tabular(cur))):
            break
        # A flush-left capitalised sentence after a blank line, following
        # indented code, is prose that merely mentions a token ("... s->len").
        if (gap and j > i and not open_comment and cur[:1].isupper()
                and len(cur.split()) >= 7 and reads_as_sentence(cur)
                and [b for b in buf if b.strip()] and all(_indent(b) >= 2 for b in buf if b.strip())):
            break
        member = (
            has_code_signal(cur)
            or (ida_run and (re.fullmatch(r"[0-9A-F]{8}\s*", cur) or RE_UNDERLINE.match(cur)))
            or is_art(cur)
            or is_tabular(cur)
            # an indented line continues the block unless it is a real sentence
            # (old issues indent wrapped prose right after a table/code); short
            # word-y lines like "if read Line; then" are still code.
            or (looks_indented(cur) and not is_sentence(cur))
            or (not is_prose_line(cur) and struct_count(cur) >= 2)  # keep exotic code together
            # a C block-comment continuation line ( * text) while inside /* ... */
            or open_comment  # inside an unclosed /* ... */: its text lines stay code
            # shell/script (# ...) or C++ (// ...) comment lines once we are in code
            or cur.lstrip().startswith("#")
            or cur.lstrip().startswith("//")
        )
        if not member:
            break
        buf.append(cur)
        if "/*" in cur or "*/" in cur:
            open_comment = cur.rfind("/*") > cur.rfind("*/") and (
                cur.lstrip().startswith("/*") or bool(re.search(r"[;{})]\s*/\*", cur))
            )
        j += 1
    while buf and not buf[-1].strip():
        buf.pop()
    if not any(l.strip() for l in buf):
        return [], max(j, i + 1)
    emitted = []
    k = len(out) - 1
    while k >= 0 and out[k] == "":
        k -= 1
    if k >= 0 and out[k] == "```":
        del out[k:]          # reopen the previous fenced block ...
        emitted.append("")   # ... keeping the blank line between the two runs
    else:
        emitted.append("```")
    emitted.extend(dedent(buf))
    emitted.append("```")
    emitted.append("")
    return emitted, max(j, i + 1)


# Dot leader of a table of contents: "Title . . . . . ..012 Apple Sectors"
RE_DOT_LEADER = re.compile(r"(?:\.\s){3,}\.|\.{2,}\s*\d+\s*\w*\s*\w*$")
# A "Label: value" field line, as in Pro-Phile records ("Handle origin: ...")
RE_FIELD = re.compile(r"^\s*[A-Z][\w'./&-]*(?: [\w'./&()-]+){0,4}:\s+\S")


def wrap_width(lines) -> int:
    """Estimate the column a phile's prose was wrapped at (90th percentile of
    line lengths, clamped to 60..80)."""
    lens = sorted(len(l.rstrip()) for l in lines if l.strip() and len(l.rstrip()) <= 80)
    if not lens:
        return 76
    return max(60, min(80, lens[int(len(lens) * 0.9)]))


def _hard_break(cur: str, nxt: str, width: int) -> bool:
    """Did the author end ``cur`` on purpose (rather than wrapping it)?

    True for a table-of-contents dot-leader row, or when the next line's first
    word would still have fitted on this line -- i.e. the text was not reflowed,
    as in "Handle: Broadway Hacker" / "Call him: Mike" or an address block.
    """
    if RE_DOT_LEADER.search(cur):
        return True
    # one "Label: value" record field after another
    if RE_FIELD.match(cur) and RE_FIELD.match(nxt):
        return True
    # a reference citation or a URL keeps its own line
    if _textish_ref(cur) or _textish_ref(nxt):
        return True
    first = nxt.split()[0] if nxt.split() else ""
    return len(cur.rstrip()) + 1 + len(first) <= width


def _code_continuation(line: str) -> bool:
    """A flush-left line that reads like code rather than a sentence: short,
    not ending like prose, and carrying code punctuation or few words."""
    s = line.strip()
    if not s or _textish_ref(line) or RE_BACKTICK_RULE.match(line) or RE_FENCE.match(line):
        return False  # a URL / "[N]" citation, or a fence line, is not a code continuation
    if s.endswith((".", "!", "?")) and len(s.split()) > 3:
        return False
    if re.search(r"[A-Za-z]{2,}[^.!?]*\s[a-z]+\s[a-z]+\s[a-z]+\s[a-z]+", s) and not re.search(r"[=(){};$\[\]*]", s):
        return False  # four+ lowercase words in a row: a sentence
    return bool(re.search(r"[=(){},:;$*\[\]]", s)) or len(s.split()) <= 3


def handle_code_tail(lines, i, out):
    """Lines right after a code block that continue it (split by a blank line but
    lacking a strong code signal on their own) are merged back into the block."""
    k = len(out) - 1
    while k >= 0 and out[k] == "":
        k -= 1
    if k < 0 or out[k] != "```":
        return None
    n = len(lines)
    j = i
    buf = []
    while j < n and lines[j].strip() and _code_continuation(lines[j]):
        buf.append(lines[j])
        j += 1
    # must consume the whole paragraph, and look like code (not 1 stray word)
    if not buf or (j < n and lines[j].strip()):
        return None
    strong = [
        b for b in buf
        if re.search(r"[=(){};$\[\]]", b) or re.match(r"^\s*\w+:\s+[A-Z]{2,}\b", b)
    ]
    if not strong:
        return None
    del out[k:]
    return [""] + dedent(buf) + ["```", ""], j


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _short_literal(line: str) -> bool:
    """A short typed-in value / command (``SYSLIB``, ``SY``, ``DIR (*,*)``)."""
    s = line.strip()
    if not s or len(s) > 40 or len(s.split()) > 5:
        return False
    if s.endswith((".", ",", "!")):
        return False
    if s.endswith("?") and re.search(r"[A-Za-z]{2,}", s):
        return False
    if RE_LIST_ITEM.match(line) or is_block_start([line], 0):
        return False
    if re.match(r"^\s*[-*+o]\s+\S", line):
        return False  # a bullet list item, rendered as a Markdown list instead
    # A sentence fragment ("then press X and Y") is prose, not a literal value.
    if re.match(r"^\s*(?:then|and|or|but|so|if|when|to|for|with|the|a|an|is|are|was)\b\s+[a-z]", s):
        return False
    return True


# A cue line that introduces something the reader types or sees on screen.
RE_TYPE_CUE = re.compile(
    r"\b(?:type|typing|enter|see|get|respond(?:s| with)?|reply|prompt|"
    r"like this|do this|you(?:'ll| will) (?:see|get))\b"
    r"[^.!?]*:\s*$",
    re.I,
)


def _starts_literal_run(lines, j) -> bool:
    """lines[j] begins a run of literals introduced by a label line above:
    either a 'label:' set off by a different indentation (``Common passwords
    are:``), or a 'type:' / 'you will see:' cue regardless of indentation."""
    if j == 0 or not _short_literal(lines[j]):
        return False
    p = j - 1
    while p >= 0 and not lines[p].strip():  # a blank may sit between cue and value
        p -= 1
    if p < 0:
        return False
    prev = lines[p]
    if not prev.rstrip().endswith(":"):
        return False
    # A centered all-caps heading ("GAMES UPDATE:") with its subtitle, or a
    # quoted English phrase, is prose rather than a typed value.
    if _indent(prev) >= 10 and prev.strip().isupper() and len(prev.split()) >= 2:
        return False
    if (not RE_TYPE_CUE.search(prev) and re.match(r"\s*[\"'][A-Z]", lines[j])) and len(RE_ENGLISH_WORD.findall(lines[j])) >= 4:
        return False
    # Across a blank line, only a typed cue ("you will see:") introduces a value.
    if p != j - 1:
        return RE_TYPE_CUE.search(prev) is not None
    return _indent(lines[j]) != _indent(prev) or RE_TYPE_CUE.search(prev) is not None


RE_CODE_LABEL = re.compile(r"^\s*[._$@a-z][\w.$@]*:\s*$")  # .copier:  _reset:


def _label_before_literals(lines, j) -> bool:
    """An assembly/code label line whose body (the literal run) follows it."""
    return bool(RE_CODE_LABEL.match(lines[j])) and j + 1 < len(lines) and _starts_literal_run(lines, j + 1)


def handle_literal_run(lines, i):
    """Typed values or commands listed right after 'Common passwords are:' or
    'type:' -> a fenced block, rather than words lost inside a paragraph. A code
    label introducing the run (``.copier:``) is kept inside the block."""
    label = []
    if _label_before_literals(lines, i):
        label = [lines[i]]
        i += 1
    elif not _starts_literal_run(lines, i):
        return None
    ind = _indent(lines[i])
    n = len(lines)
    j = i
    buf = list(label)
    while j < n and lines[j].strip() and _short_literal(lines[j]) and _indent(lines[j]) == ind:
        buf.append(lines[j])
        j += 1
    return ["", "```"] + dedent(buf) + ["```", ""], j


def _new_indented_paragraph(lines, j) -> bool:
    """First-line-indent paragraph style: an indented line between two
    flush-left lines starts a new paragraph (the source has no blank lines)."""
    n = len(lines)
    if j == 0 or j + 1 >= n:
        return False
    return (
        _indent(lines[j]) >= 3
        and _indent(lines[j - 1]) <= 1
        and lines[j + 1].strip() != ""
        and _indent(lines[j + 1]) <= 1
        and is_prose_line(lines[j])
    )


def handle_paragraph(lines, i, width=76):
    """Fallback: a natural-language paragraph, de-indented to flow as prose.

    Wrapped lines are joined by Markdown as usual; lines the author ended on
    purpose (key: value lists, addresses, signatures, dot-leader tables) keep
    their break via a trailing two-space hard line break.
    """
    n = len(lines)
    para = []
    raw = []
    j = i
    while j < n:
        s = lines[j].strip()
        if not s:
            break
        if para:
            if starts_code(lines, j):
                break
            if RE_PAREN_ITEM.match(lines[j]) and paren_run(lines, j):
                break
            if (_starts_literal_run(lines, j) or _new_indented_paragraph(lines, j)
                    or _label_before_literals(lines, j)):
                break
            if is_block_start(lines, j):
                # A line like "818.  He gives..." looks like a list item but is
                # really a sentence wrapping onto a number; only break here if the
                # previous line actually ended a sentence/clause.
                only_list = (
                    RE_LIST_ITEM.match(lines[j])
                    and not RE_FENCE.match(lines[j])
                    and not (RE_DASH_HEAD.match(lines[j]) and RE_DASH_HEAD.match(lines[j]).group(2))
                    and not RE_PIPE_BANNER.match(lines[j])
                    and not RE_UNDERLINE.match(lines[j])
                    and not RE_BACKTICK_RULE.match(lines[j])
                )
                if not only_list or para[-1].rstrip().endswith((".", "!", "?", ":", ")")):
                    break
        para.append(s)
        raw.append(lines[j])
        j += 1
    if not para:
        return [esc(lines[i].rstrip())], i + 1
    out = []
    for k, p in enumerate(para):
        line = esc(p)
        if k + 1 < len(para) and _hard_break(raw[k], raw[k + 1], width):
            line += "  "
        out.append(line)
    return out + [""], j


def convert_body(body: str) -> str:
    """Convert one phile body to Markdown, preserving all content."""
    lines = body.split("\n")
    out: list[str] = []
    n = len(lines)
    width = wrap_width(lines)

    # The leading Phrack banner is consumed as one verbatim block.
    i = 0
    banner = consume_banner(lines)
    if banner:
        block, i = banner
        out.extend(["```"] + list(block) + ["```", ""])

    handlers = (
        handle_fence,
        handle_extract,
        handle_uu,
        handle_dash_heading,
        handle_deco_heading,
        handle_numbered_title,
        handle_pipe_banner,
        handle_column_table,
        handle_setext_heading,
        handle_rule,
        handle_numbered_code,
        handle_sexpr_block,
        handle_shell_construct,
        handle_shell_block,
        handle_literal_run,
        handle_uu_data,
        handle_o_bullet,
        handle_roman_list,
        handle_list,
        handle_paren_list,
    )
    bt_rules = sum(1 for l in lines if RE_BACKTICK_RULE.match(l) and len(l.strip()) >= 20)
    if bt_rules >= 6 and bt_rules % 2 == 0:
        handlers = (handle_backtick_fence,) + handlers

    while i < n:
        for handler in handlers:
            res = handler(lines, i)
            if res is not None:
                emitted, i = res
                out.extend(emitted)
                break
        else:
            res = handle_code(lines, i, out)  # may mutate out in place to merge
            if res is not None:
                emitted, i = res
                out.extend(emitted)
            else:
                res = handle_code_tail(lines, i, out)  # may mutate out to merge
                if res is None:
                    res = handle_paragraph(lines, i, width)
                emitted, i = res
                out.extend(emitted)

    out = _tidy_rules(out)
    # collapse trailing and excess blank lines
    while len(out) >= 2 and out[-1] == "" and out[-2] == "":
        out.pop()
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return text.strip("\n") + "\n"


def _tidy_rules(lines):
    """Drop a thematic break (``---``) that would be redundant: one right after a
    heading, or a second one in a row (a title's decorative underline often lands
    just below the heading we already made from it)."""
    res = []
    for s in lines:
        if s.strip() == "---":
            k = len(res) - 1
            while k >= 0 and res[k] == "":
                k -= 1
            if k >= 0 and (res[k].strip() == "---" or re.match(r"^#{1,6}\s", res[k])):
                continue
        res.append(s)
    return res


# --------------------------------------------------------------------------- #
# Assemble an issue
# --------------------------------------------------------------------------- #

def convert_issue(number: int, part_paths) -> str:
    philes = []
    for p in part_paths:
        meta, body = split_front_matter(read_text(p))
        philes.append(
            Phile(
                number=natural_part_key(p),
                title=meta.get("title", "").strip(),
                author=meta.get("author", "").strip(),
                body=body,
            )
        )

    total = len(philes)
    doc = [f"# Phrack Issue {number}", ""]

    # Table of contents
    doc.append("## Contents")
    doc.append("")
    for ph in philes:
        title = ph.title or "(untitled)"
        anchor = _anchor(f"{ph.number:02d} {title}")
        doc.append(f"{ph.number}. [{esc(title)}](#{anchor})" + (f" — *{esc(ph.author)}*" if ph.author else ""))
    doc.append("")

    for ph in philes:
        title = ph.title or "(untitled)"
        doc.append("")
        doc.append(f"## {ph.number:02d} {esc(title)}")
        doc.append("")
        meta_bits = []
        if ph.author:
            meta_bits.append(f"**Author:** {esc(ph.author)}")
        meta_bits.append(f"**Phile:** {ph.number} of {total}")
        doc.append("  ·  ".join(meta_bits))
        doc.append("")
        doc.append(convert_body(ph.body))
        doc.append("")

    return "\n".join(doc).rstrip("\n") + "\n"


def _anchor(text: str) -> str:
    """GitHub-style heading anchor."""
    a = text.lower()
    a = re.sub(r"[^\w\s-]", "", a)
    a = re.sub(r"\s+", "-", a.strip())
    return a


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert Phrack issue text to Markdown.")
    ap.add_argument("--issues", required=True, help="directory containing phrackNN/ subdirs")
    ap.add_argument("--out", required=True, help="output directory for phrackNN.md files")
    ap.add_argument("--only", nargs="*", type=int, help="convert only these issue numbers")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.issues):
        sys.exit(f"issues dir not found: {args.issues}")
    os.makedirs(args.out, exist_ok=True)

    issues = find_issues(args.issues)
    if args.only:
        want = set(args.only)
        issues = [e for e in issues if e[0] in want]

    if not issues:
        sys.exit("no issues found to convert")

    for number, _dir, parts in issues:
        md = convert_issue(number, parts)
        dest = os.path.join(args.out, f"phrack{number}.md")
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(md)
        print(f"phrack{number}: {len(parts)} philes -> {dest}")

    print(f"done: {len(issues)} issue(s) written to {args.out}")


if __name__ == "__main__":
    main()
