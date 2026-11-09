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
    status         TEXT      -- ok / count_mismatch / value_mismatch / empty / unchecked / error
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
    """Parse quality by filing year. %ok: parse reproduces the filer's stated entry count and value
    total. %~ok: also counts filings whose entry count is within 2% (or 1 row) of the stated one."""
    q = """SELECT substr(date_filed,1,4) AS yr, format, COUNT(*) AS n,
                  ROUND(100.0 * SUM(status = 'ok') / COUNT(*), 1) AS pct_ok,
                  ROUND(100.0 * SUM(status = 'ok' OR abs(n_holdings - entry_total) <= max(1, 0.02 * entry_total))
                        / COUNT(*), 1) AS pct_close,
                  SUM(status = 'count_mismatch'), SUM(status = 'value_mismatch'),
                  SUM(status = 'empty'), SUM(status = 'unchecked'), SUM(status LIKE 'error%')
           FROM filings GROUP BY yr, format ORDER BY yr, format"""
    print(f"{'year':6}{'fmt':6}{'filings':>8}{'%ok':>7}{'%~ok':>7}{'count':>7}{'value':>7}{'empty':>7}{'unchk':>7}{'error':>7}")
    for r in con.execute(q):
        print(f"{r[0] or '?':6}{r[1] or '?':6}{r[2]:>8}{r[3]:>7}{r[4]:>7}" + "".join(f"{x:>7}" for x in r[5:]))
