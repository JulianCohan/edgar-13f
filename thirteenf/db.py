"""Load parsed filings into SQLite."""
import gzip
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .parse import parse_filing

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    accession      TEXT PRIMARY KEY,
    cik            INTEGER,
    manager_name   TEXT,
    form_type      TEXT,     -- 13F-HR or 13F-HR/A
    period         TEXT,     -- quarter end the holdings are as of (YYYY-MM-DD)
    date_filed     TEXT,
    amendment_type TEXT,     -- RESTATEMENT / NEW HOLDINGS for amendments
    report_type    TEXT,     -- XML era only (HOLDINGS / COMBINATION REPORT)
    format         TEXT,     -- xml or text
    entry_total    INTEGER,  -- as stated on the filer's summary page
    value_total    REAL,     -- as stated on the filer's summary page
    n_holdings     INTEGER,  -- rows we parsed
    value_sum      REAL,     -- sum of parsed values
    status         TEXT      -- ok / near / count_mismatch / value_mismatch / empty / unchecked / error
);
CREATE TABLE IF NOT EXISTS holdings (
    accession      TEXT REFERENCES filings(accession),
    row            INTEGER,
    name_of_issuer TEXT,
    title_of_class TEXT,
    cusip          TEXT,
    value          REAL,     -- as reported: $1000s for filings before 2023-01-03, dollars after
    shares         REAL,     -- shares or principal amount
    sh_prn         TEXT,
    put_call       TEXT,
    discretion     TEXT,
    other_managers TEXT,
    vote_sole      REAL,     -- voting columns: XML era only
    vote_shared    REAL,
    vote_none      REAL,
    PRIMARY KEY (accession, row)
);
CREATE INDEX IF NOT EXISTS holdings_cusip ON holdings(cusip);
CREATE INDEX IF NOT EXISTS filings_cik_period ON filings(cik, period);
-- value in dollars, using the SEC's 2023-01-03 switch from $1000s. Some filers ignored the switch.
CREATE VIEW IF NOT EXISTS holdings_usd AS
SELECT h.*, f.cik, f.period, f.date_filed, f.form_type,
       CASE WHEN f.date_filed < '2023-01-03' THEN h.value * 1000 ELSE h.value END AS value_usd
FROM holdings h JOIN filings f USING (accession);
"""

HOLDING_COLS = ["name_of_issuer", "title_of_class", "cusip", "value", "shares", "sh_prn", "put_call",
                "discretion", "other_managers", "vote_sole", "vote_shared", "vote_none"]
FILING_COLS = ["accession", "cik", "manager_name", "form_type", "period", "date_filed", "amendment_type",
               "report_type", "format", "entry_total", "value_total", "n_holdings", "value_sum", "status"]


def connect(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def _parse_file(path):
    accession = Path(path).name.removesuffix(".txt.gz")
    try:
        text = gzip.decompress(Path(path).read_bytes()).decode("utf-8", errors="replace")
        filing, rows = parse_filing(text)
        filing["accession"] = filing["accession"] or accession
        return filing, rows
    except Exception as e:  # one bad filing must not stop a 400k-file run
        return dict(accession=accession, status=f"error: {type(e).__name__}: {e}"[:200]), []


def load(data, db, reparse=False, workers=None):
    con = connect(db)
    done = set() if reparse else {a for (a,) in con.execute("SELECT accession FROM filings")}
    files = [p for p in sorted((Path(data) / "filings").glob("*/*.txt.gz"))
             if p.name.removesuffix(".txt.gz") not in done]
    print(f"{len(files)} filings to parse", flush=True)
    with ProcessPoolExecutor(workers) as pool:
        for i, (filing, rows) in enumerate(pool.map(_parse_file, files, chunksize=16), 1):
            acc = filing["accession"]
            con.execute("DELETE FROM holdings WHERE accession = ?", (acc,))
            con.execute(f"INSERT OR REPLACE INTO filings VALUES ({','.join('?' * len(FILING_COLS))})",
                        [filing.get(c) for c in FILING_COLS])
            con.executemany(f"INSERT INTO holdings VALUES ({','.join('?' * (len(HOLDING_COLS) + 2))})",
                            [[acc, n] + [r[c] for c in HOLDING_COLS] for n, r in enumerate(rows)])
            if i % 1000 == 0:
                con.commit()
                print(f"{i}/{len(files)}", flush=True)
    con.commit()
    return con


def report(con):
    """Parse quality by filing year: share of filings whose parse matches the filer's own totals
    (ok) or nearly matches them (near), and the share of holding rows in such filings."""
    q = """SELECT substr(date_filed,1,4) AS yr, format, COUNT(*),
                  ROUND(100.0 * SUM(status = 'ok') / COUNT(*), 1),
                  ROUND(100.0 * SUM(status IN ('ok', 'near')) / COUNT(*), 1),
                  ROUND(100.0 * SUM(CASE WHEN status IN ('ok', 'near') THEN n_holdings END) / SUM(n_holdings), 1),
                  SUM(status = 'count_mismatch'), SUM(status = 'value_mismatch'),
                  SUM(status = 'empty'), SUM(status = 'unchecked'), SUM(status LIKE 'error%')
           FROM filings GROUP BY yr, format ORDER BY yr, format"""
    cols = ["year", "fmt", "filings", "%ok", "%ok+near", "%rows", "count", "value", "empty", "unchk", "error"]
    print("".join(f"{c:>9}" for c in cols))
    for r in con.execute(q):
        print("".join(f"{'-' if x is None else x:>9}" for x in r))
