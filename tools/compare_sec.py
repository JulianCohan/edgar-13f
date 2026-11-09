"""Check parsed XML-era holdings against the SEC's own Form 13F data sets.

Download one or more quarterly zips from
https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets
then run:

    python tools/compare_sec.py 13f.sqlite 2019q3_form13f.zip [more.zip ...]

For every filing that is in both, holdings are compared as multisets of
(cusip, value, shares, sh/prn, put/call, discretion, voting sole/shared/none).
"""
import collections
import csv
import io
import sqlite3
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from thirteenf.parse import fix_cusip  # noqa: E402  (same CUSIP clean-up on both sides)


def main(db, zips):
    ours = collections.defaultdict(list)
    q = """SELECT h.accession, cusip, value, shares, sh_prn, put_call, discretion, vote_sole, vote_shared, vote_none
           FROM holdings h JOIN filings f USING (accession) WHERE f.format = 'xml'"""
    for acc, cusip, *rest in sqlite3.connect(db).execute(q):
        rest[3] = (rest[3] or "").upper()
        ours[acc].append((cusip, *rest))

    theirs = collections.defaultdict(list)
    csv.field_size_limit(sys.maxsize)
    for z in zips:
        with zipfile.ZipFile(z) as zf, zf.open("INFOTABLE.tsv") as f:
            rows = csv.DictReader(io.TextIOWrapper(f, "utf-8", errors="replace"), delimiter="\t",
                                  quoting=csv.QUOTE_NONE)
            for r in rows:
                if r["ACCESSION_NUMBER"] not in ours:
                    continue
                n = lambda k: float(r[k]) if r[k] else None  # noqa: E731
                theirs[r["ACCESSION_NUMBER"]].append((
                    fix_cusip(r["CUSIP"]), n("VALUE"), n("SSHPRNAMT"), r["SSHPRNAMTTYPE"] or None,
                    (r["PUTCALL"] or "").upper(), r["INVESTMENTDISCRETION"] or None,
                    n("VOTING_AUTH_SOLE"), n("VOTING_AUTH_SHARED"), n("VOTING_AUTH_NONE")))

    common = [a for a in ours if a in theirs]
    if not common:
        sys.exit("No filings in common: download the data-set zips for the quarters in your database.")
    sec_rows = matched = identical = 0
    for a in common:
        o, t = collections.Counter(ours[a]), collections.Counter(theirs[a])
        sec_rows += len(theirs[a])
        matched += sum((o & t).values())
        if o == t:
            identical += 1
        else:
            print(f"DIFF {a}: ours {len(ours[a])} rows, SEC {len(theirs[a])} | "
                  f"only ours {list((o - t).elements())[:2]} | only SEC {list((t - o).elements())[:2]}")
    print(f"filings in both: {len(common)}, identical: {identical} ({100 * identical / len(common):.2f}%)")
    print(f"SEC rows: {sec_rows}, matched on all fields: {matched} ({100 * matched / sec_rows:.3f}%)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2:])
