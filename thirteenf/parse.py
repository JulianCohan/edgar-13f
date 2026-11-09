"""Parse one EDGAR 13F-HR full-submission text file into a filing record and its holdings.

Two eras:
  * XML (mandatory from 2013Q3): the INFORMATION TABLE document is XML; exact field parsing.
  * Text (before that): the information table is a free-form text or HTML table; we find each
    holding line by its CUSIP (check digit validated) and read value and shares after it.

Every filing is checked against the totals the filer reports on its own summary page
(entry count and value total); `status` records whether the parse reproduces them.
"""
import html
import re
import xml.etree.ElementTree as ET

# --- SEC header ---------------------------------------------------------------------------

HEADER_FIELDS = {
    "accession": "ACCESSION NUMBER",
    "form_type": "CONFORMED SUBMISSION TYPE",
    "period": "CONFORMED PERIOD OF REPORT",
    "date_filed": "FILED AS OF DATE",
    "manager_name": "COMPANY CONFORMED NAME",
    "cik": "CENTRAL INDEX KEY",
}


def parse_header(text):
    head = text[: text.find("<DOCUMENT>")] if "<DOCUMENT>" in text else text[:5000]
    out = {}
    for key, label in HEADER_FIELDS.items():
        m = re.search(rf"^\s*{label}:\s*(.+?)\s*$", head, re.M)
        out[key] = m.group(1) if m else None
    for k in ("period", "date_filed"):
        v = out[k]
        out[k] = f"{v[:4]}-{v[4:6]}-{v[6:8]}" if v and len(v) == 8 else v
    if out["manager_name"]:
        out["manager_name"] = " ".join(out["manager_name"].split())
    if out["cik"]:
        out["cik"] = int(out["cik"])
    return out


def documents(text):
    """(type, body) for each <DOCUMENT> in the submission."""
    for doc in re.findall(r"<DOCUMENT>(.*?)</DOCUMENT>", text, re.S):
        m = re.search(r"<TYPE>([^\n<]+)", doc)
        body = re.search(r"<TEXT>(.*?)(?:</TEXT>|\Z)", doc, re.S)
        yield (m.group(1).strip() if m else ""), (body.group(1) if body else doc)


# --- numbers ------------------------------------------------------------------------------

def num(s):
    if s is None:
        return None
    s = s.strip().replace(",", "").replace("$", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = float(s.strip("()"))
    except ValueError:
        return None
    v = -v if neg else v
    return int(v) if v.is_integer() else v


# --- XML era ------------------------------------------------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _xml_root(body):
    m = re.search(r"<XML>\s*(.*?)\s*</XML>", body, re.S | re.I)
    src = (m.group(1) if m else body).strip()
    return ET.fromstring(src.encode("utf-8"))


def fix_cusip(c):
    """Uppercase, and restore leading zeros a spreadsheet dropped ("37833100" -> "037833100")
    when the check digit confirms it."""
    c = (c or "").strip().upper()
    return c.zfill(9) if _zeros_dropped(c) else (c or None)


def _zeros_dropped(c, sedol_too=False):
    # 7-8 digits whose zero-padded form passes the CUSIP check. In XML a 7-digit code that is
    # also a valid SEDOL is left alone; in text tables (sedol_too) it is far more often a CUSIP.
    return c.isdigit() and len(c) in (7, 8) and cusip_ok(c.zfill(9)) and (sedol_too or not sedol_ok(c))


def parse_xml_table(body):
    rows = []
    for el in _xml_root(body).iter():
        if _local(el.tag) != "infoTable":
            continue
        f = {}
        for child in el.iter():
            name = _local(child.tag)
            if child is not el and name not in f:
                f[name] = (child.text or "").strip()
        rows.append(dict(
            name_of_issuer=f.get("nameOfIssuer"), title_of_class=f.get("titleOfClass"),
            cusip=fix_cusip(f.get("cusip")), value=num(f.get("value")),
            shares=num(f.get("sshPrnamt")), sh_prn=f.get("sshPrnamtType") or None,
            put_call=f.get("putCall") or None, discretion=f.get("investmentDiscretion") or None,
            other_managers=f.get("otherManager") or None,
            vote_sole=num(f.get("Sole")), vote_shared=num(f.get("Shared")), vote_none=num(f.get("None")),
        ))
    return rows


def parse_xml_cover(body):
    f = {}
    for el in _xml_root(body).iter():
        f.setdefault(_local(el.tag), (el.text or "").strip())
    return dict(entry_total=num(f.get("tableEntryTotal")), value_total=num(f.get("tableValueTotal")),
                amendment_type=f.get("amendmentType") or None, report_type=f.get("reportType") or None)


# --- text era -----------------------------------------------------------------------------

_CUSIP_VALUES = {**{str(d): d for d in range(10)}, **{chr(65 + i): 10 + i for i in range(26)},
                 "*": 36, "@": 37, "#": 38}


def cusip_ok(c):
    if len(c) != 9 or not c[8].isdigit() or sum(ch.isdigit() for ch in c) < 3:
        return False
    total = 0
    for i, ch in enumerate(c[:8]):
        v = _CUSIP_VALUES.get(ch)
        if v is None:
            return False
        v *= 2 if i % 2 else 1
        total += v // 10 + v % 10
    return (10 - total % 10) % 10 == int(c[8])


# 9-char CUSIP, optionally written "037833 10 0" / "037833-10-0", or 7-8 chars (leading zeros
# lost in spreadsheet exports, or a SEDOL in place of a CUSIP), or 10 chars (a stray prefix).
_CUSIP_RE = re.compile(r"(?<![\w.,$])([0-9A-Za-z*@#]{6}[ -]?[0-9A-Za-z*@#]{2}[ -]?[0-9A-Za-z]|[0-9A-Za-z]{7,8}|[0-9A-Za-z]{10})(?![\w.,])")
# Fixed-width tables where the class text overflows into the CUSIP column: "COM NEW373200302".
_GLUED_RE = re.compile(r"(?<=[A-Za-z$&./])(\d[0-9A-Z]{7}\d)(?![\w.,])")
_NUM_RE = re.compile(r"\(?\d[\d,]*(?:\.\d+)?\)?|\(?\.\d+\)?")
_SHPRN_RE = re.compile(r"(?<![A-Za-z])(SH|SHS|SHRS|PRN|PRIN)\b", re.I)
_PUTCALL_RE = re.compile(r"\b(PUT|CALL)S?\b", re.I)
_DISC_RE = re.compile(r"\b(SOLE|SHARED|DEFINED|DFND|DEF|OTHER|OTR|SHR|SHD)\b", re.I)


def sedol_ok(c):
    """7-char SEDOL (some filers give these for foreign-listed stocks instead of a CUSIP)."""
    # SEDOLs are all digits, or "B" + 5 non-vowel characters + a check digit.
    if len(c) != 7 or not (c.isdigit() or (c[0] == "B" and not set(c) & set("AEIOU"))) or not c[6].isdigit():
        return False
    total = sum(_CUSIP_VALUES[ch] * w for ch, w in zip(c, (1, 3, 1, 7, 3, 9)))
    return (10 - total % 10) % 10 == int(c[6])


def cusip_candidates(line):
    """(cusip, start, end, check_digit_ok) for each CUSIP-like token, left to right."""
    glued = [m for m in _GLUED_RE.finditer(line) if cusip_ok(m.group(1))]
    for m in sorted([*_CUSIP_RE.finditer(line), *glued], key=lambda m: m.start()):
        raw = m.group(1).upper()
        c = raw.replace(" ", "").replace("-", "")
        if sum(ch.isdigit() for ch in c) < 4 and not sedol_ok(c):
            continue
        if raw == c and _zeros_dropped(c, sedol_too=True):
            c = c.zfill(9)
        elif len(c) == 10 and cusip_ok(c[1:]):
            c = c[1:]
        yield c, m.start(), m.end(), cusip_ok(c)


def find_cusip(line):
    return next(((c, s, e) for c, s, e, ok in cusip_candidates(line) if ok), None)


def html_to_text(body):
    """Flatten HTML tables to one line per <tr>, cells separated by 3 spaces."""
    body = re.sub(r"\s+", " ", body)
    body = re.sub(r"<\s*(tr|br|p|div)\b[^>]*>", "\n", body, flags=re.I)
    body = re.sub(r"<\s*/\s*t[dh]\s*>", "   ", body, flags=re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    return html.unescape(body).replace("\xa0", " ")


# Summary rows ("GRAND TOTALS 728458 ...", "Total: $1,234") but not issuers like "TOTAL S A".
_TOTAL_RE = re.compile(r"\b(grand|column|page|sub|report)\s*totals?\b|\btotals\b|\btotal\s*[:$]|\btotal\s+\d", re.I)


def _split_glued(token, votes):
    """Value and shares columns printed with no gap ("63312190480SH"): split the digit run where
    its tail equals the share count, which reappears in the voting columns after SH/PRN.
    Returns (value, shares); either may be None if the run can't be split."""
    digits = token.replace(",", "").split(".")[0]
    tries = [sum(votes), sum(votes[1:]), sum(votes[2:])] + votes  # [1:], [2:] skip manager codes
    for t in (round(t) for t in tries if t >= 100):
        if digits.isdigit() and digits.endswith(str(t)) and len(digits) > len(str(t)):
            return int(digits[: -len(str(t))]), t
    # One number and it equals the share count: the value column was left blank.
    return (None, num(token)) if any(round(num(token) or -1) == round(t) for t in tries) else (None, None)


def parse_text_line(line, prev_text):
    if _TOTAL_RE.search(line):
        return None
    best = None
    for cand in cusip_candidates(line):
        cusip, start, end, valid = cand
        tail = line[end:].replace("$", " ")
        nums = list(_NUM_RE.finditer(tail))
        if len(nums) < 2:
            continue
        shprn = _SHPRN_RE.search(tail, nums[0].end())
        # Only one number before SH/PRN (or before the discretion, in tables with no SH column)
        # means the value and shares columns were printed with no gap between them.
        kw = shprn or re.search(r"(?i)(SOLE|SHARED|DEFINED|OTHER)", tail[nums[0].end():])
        glued = bool(kw) and (shprn.start() if shprn else nums[0].end() + kw.start()) < nums[1].start()
        disc = _DISC_RE.search(tail, (shprn or nums[1]).end())
        putcall = _PUTCALL_RE.search(tail, nums[0].end())
        # Option rows use made-up CUSIPs and some filers mistype them: accept a token that fails
        # the check digit only when the rest of the line looks like a holding row.
        if not (valid or shprn or putcall or disc):
            continue
        # Filers sometimes put a second code in the issuer/class text ("SEE CUSIP 69713P107",
        # "WT EX030402"). Prefer the candidate the value column directly follows, then valid ones.
        score = (bool(re.match(r"[\s|]*\(?\d", tail)), valid)
        if best is None or score > best[0]:
            best = score, cand, nums, shprn, glued, disc, putcall
    if best is None:
        return None
    _, (cusip, start, end, valid), nums, shprn, glued, disc, putcall = best
    if glued:
        votes = [num(m.group()) or 0 for m in nums[1:]]
        value, shares = _split_glued(nums[0].group(), votes)
    else:
        value, shares = num(nums[0].group()), num(nums[1].group())
    head = line[:start].strip(" |\t") or prev_text
    parts = [p for p in re.split(r"\s{2,}|\t|\|", head) if p.strip()]
    return dict(
        name_of_issuer=parts[0].strip() if parts else None,
        title_of_class=" ".join(p.strip() for p in parts[1:]) or None,
        cusip=cusip, value=value, shares=shares,
        sh_prn=shprn.group(1).upper() if shprn else None,
        put_call=putcall.group(1).capitalize() if putcall else None,
        discretion=disc.group(1).upper() if disc else None,
        # shortcut: voting/other-manager columns are positional and often blank in text tables,
        # so they are left NULL for pre-XML filings; parse by column offsets if they are needed.
        other_managers=None, vote_sole=None, vote_shared=None, vote_none=None,
    )


_LOOSE_RE = re.compile(r"^(?P<head>.*?)\s+\$?\s*(?P<value>\(?\d[\d,]*(?:\.\d+)?\)?)\s+(?P<shares>\d[\d,]*(?:\.\d+)?)"
                       r"\s*(?P<shprn>SH|SHS|SHRS|PRN|PRIN)\b(?P<rest>.*)$", re.I)


def parse_loose_line(line, prev_row):
    """A holding row with no usable CUSIP: either left blank because it repeats the security of
    the row above (split by manager or discretion), or missing ("Not Avail", private stock)."""
    m = _LOOSE_RE.match(line)
    if not m or _TOTAL_RE.search(line):
        return None
    head, rest = m["head"].strip(" |\t"), m["rest"]
    disc, putcall = _DISC_RE.search(rest), _PUTCALL_RE.search(rest)
    if head:
        parts = [p.strip() for p in re.split(r"\s{2,}|\t|\|", head) if p.strip()]
        if not disc or not re.match(r"[A-Za-z0-9]", parts[0]) or not re.search(r"[A-Za-z]{2}", parts[0]):
            return None
        name, cls, cusip = parts[0], " ".join(parts[1:]) or None, None
    elif prev_row:
        name, cls, cusip = prev_row["name_of_issuer"], prev_row["title_of_class"], prev_row["cusip"]
    else:
        return None
    return dict(name_of_issuer=name, title_of_class=cls, cusip=cusip,
                value=num(m["value"]), shares=num(m["shares"]), sh_prn=m["shprn"].upper(),
                put_call=putcall.group(1).capitalize() if putcall else None,
                discretion=disc.group(1).upper() if disc else None,
                other_managers=None, vote_sole=None, vote_shared=None, vote_none=None)


_FILLER_TOK = re.compile(r"[\d,.$()%-]+|SH|SHS|PRN|SOLE|NONE|DEFINED|DFND|SHARED|OTHER|OTR|X|PUT|CALL|N/?A", re.I)


_CONTINUATION_RE = re.compile(r"\s+\$?\s*\d[\d,.]*\s+\d[\d,.]*\s*(SH|PRN)\b", re.I)


def _filler(line):
    """A line holding only trailing columns (numbers, SH, SOLE...) of the row above it."""
    return not _CONTINUATION_RE.match(line) and all(_FILLER_TOK.fullmatch(t) for t in line.split())


def _prev_text(pending):
    """Issuer (and class) for a row whose CUSIP line has nothing before it."""
    if len(pending) >= 2 and len(pending[-1]) <= 12 and not _filler(pending[-2]):
        return pending[-2] + "   " + pending[-1]
    return pending[-1] if pending else ""


def parse_text_table(body):
    if re.search(r"<\s*t[dr]\b", body, re.I):
        body = html_to_text(body)
    else:
        body = re.sub(r"<[^>\n]{0,40}>", " ", body)  # SGML layout tags like <S> <C> <PAGE>
    lines = body.splitlines()
    rows, pending, i = [], [], 0
    while i < len(lines):
        line, j = lines[i], i + 1
        row = parse_text_line(line, _prev_text(pending))
        if row is None and find_cusip(line):
            # One-field-per-line layouts (PDF/Word exports): glue on the following lines.
            while row is None and j < min(i + 8, len(lines)) and not find_cusip(lines[j]):
                line += "   " + lines[j].strip()
                j += 1
                row = parse_text_line(line, _prev_text(pending))
        if row is None:
            row = parse_loose_line(line, rows[-1] if rows else None)
        if row is None:
            if line.strip():
                pending.append(line.strip())
            i += 1
            continue
        start = j
        while j < len(lines) and _filler(lines[j]) and j - start < 8:
            j += 1
        if j > start:  # re-read with the trailing columns so SH/SOLE are picked up
            row = parse_text_line(line + "   " + "   ".join(l.strip() for l in lines[start:j]),
                                  _prev_text(pending)) or row
        rows.append(row)
        pending, i = [], j
    return rows


def parse_text_cover(text):
    def grab(pattern):
        m = re.search(pattern, text, re.I)
        return num(m.group(1)) if m else None
    amend = None
    if re.search(r"\[\s*[xX]\s*\]\s*is\s+a\s+restatement", text):
        amend = "RESTATEMENT"
    elif re.search(r"\[\s*[xX]\s*\]\s*adds\s+new", text):
        amend = "NEW HOLDINGS"
    # Some filing agents print "REPORT SUMMARY  313 DATA RECORDS  743603" instead of a summary page.
    agent = re.search(r"REPORT SUMMARY\s+([\d,]+)\s+DATA RECORDS\s+\$?([\d,]+)", text)
    return dict(entry_total=grab(r"Entry\s*(?:Total)?\s*:?[\s$]*(\d[\d,]*)") or (num(agent[1]) if agent else None),
                value_total=grab(r"Value\s+(?:Total|Entry)\s*:?[\s$]*(\d[\d,]*(?:\.\d+)?)") or (num(agent[2]) if agent else None),
                amendment_type=amend, report_type=None)


# --- whole filing -------------------------------------------------------------------------

def check(entry_total, value_total, rows):
    """Compare the parse with the filer's own summary-page totals.
    ok:   entry count matches exactly and value total within 1%.
    near: entry count within 2% (or 1 row) and value total within 2%. Typical of large filers
          with a few mistyped rows, or summary totals that are themselves slightly off.
    Otherwise count_mismatch / value_mismatch."""
    if not rows:
        return "ok" if entry_total == 0 else "empty"
    if entry_total is None and value_total is None:
        return "unchecked"
    # Many filers state the number of distinct securities rather than the number of table rows.
    count_gap = 0 if entry_total is None else min(abs(c - entry_total) for c in (len(rows), len({r["cusip"] for r in rows})))
    v = sum(r["value"] or 0 for r in rows)
    # Filers often state the total in dollars while the table is in thousands (or vice versa).
    value_gap = 0 if not value_total else min(abs(v * k - value_total) for k in (1, 1000, 0.001)) / value_total
    value_gap = 0 if value_total is not None and value_total == 0 == v else value_gap
    if count_gap == 0 and value_gap <= 0.01:
        return "ok"
    if count_gap <= max(1, 0.02 * (entry_total or 0)) and value_gap <= 0.02:
        return "near"
    return "count_mismatch" if count_gap else "value_mismatch"


def parse_filing(text):
    filing = parse_header(text)
    docs = list(documents(text))
    xml_tables = [b for t, b in docs if re.search(r"<(\w+:)?informationTable\b", b)]
    cover = None
    if xml_tables:
        filing["format"] = "xml"
        rows = [r for b in xml_tables for r in parse_xml_table(b)]
        for _, b in docs:
            if re.search(r"<(\w+:)?edgarSubmission\b", b):
                cover = parse_xml_cover(b)
                break
    else:
        filing["format"] = "text"
        rows = [r for _, b in docs for r in parse_text_table(b)]
    # drop "nothing to report" placeholder rows (CUSIP 000000000, zero value and shares)
    rows = [r for r in rows if (r["cusip"] or "").strip("0") or r["value"] or r["shares"]]
    cover = cover or parse_text_cover("\n".join(b for _, b in docs))
    filing.update(cover)
    filing["n_holdings"] = len(rows)
    filing["value_sum"] = sum(r["value"] or 0 for r in rows)
    filing["status"] = check(filing["entry_total"], filing["value_total"], rows)
    return filing, rows
