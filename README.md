# edgar-13f

Bulk-download SEC Form 13F-HR filings from EDGAR and parse them into a local SQLite database of
institutional holdings (manager × security × quarter). Covers both eras:

- **2013Q3 onward:** the information table is XML, so every field is parsed exactly.
- **Before that:** the table is free-form text or HTML, laid out differently by each filer. The
  parser finds each holding row by its CUSIP and reads value and shares from the columns after it.

Pure Python standard library, no dependencies. Needs Python 3.9 or newer.

## Quick start

```bash
export SEC_USER_AGENT="Your Name your.email@tulane.edu"   # the SEC requires a name and email

# 1. Download raw filings (resumable: re-running skips files already on disk)
python -m thirteenf download --start 2005Q1 --end 2025Q4 --data data

# 2. Parse everything into SQLite (incremental: only new filings get parsed)
python -m thirteenf parse --data data --db 13f.sqlite

# 3. Parse-quality report by year
python -m thirteenf report --db 13f.sqlite
```

To try it on a small random sample first: `download --sample 5` grabs 5 random filings per quarter.

Quarterly indexes are cached. If you download the current quarter before it has ended, rerun
later with `--refresh-index` to pick up the filings made since.

### Full-run sizing

There are about 424,000 13F-HR and 13F-HR/A filings from 2006 through 2026Q3. The downloader stays
under the SEC's limit of 10 requests per second, so a full run takes about 15–25 hours. Raw files
are stored gzipped, roughly 6 GB in total. The SQLite database comes to roughly 25 GB, about 170
million holding rows. Downloading and parsing are separate steps, so after a parser fix you can
re-parse (`parse --reparse`) without downloading again.

## Database

`filings`: one row per filing.

| column | meaning |
|---|---|
| `accession` | EDGAR accession number (primary key) |
| `cik`, `manager_name` | the filing manager |
| `form_type` | `13F-HR` or `13F-HR/A` (amendment) |
| `period` | quarter-end date the holdings are as of |
| `date_filed` | date the filing was made |
| `amendment_type` | `RESTATEMENT` (replaces the original) or `NEW HOLDINGS` (adds to it) |
| `entry_total`, `value_total` | totals the filer states on its own summary page |
| `n_holdings`, `value_sum` | what we parsed |
| `status` | Parse vs. the filer's own totals. `ok`: entry count matches exactly and value within 1%. `near`: count within 2% (or one row) and value within 2%. Otherwise `count_mismatch` or `value_mismatch`. Also `empty` (no table), `unchecked` (no summary page), `error: …` |

`holdings`: one row per line of the information table. Columns: `name_of_issuer`,
`title_of_class`, `cusip`, `value`, `shares` (shares, or principal amount for bonds), `sh_prn`,
`put_call`, `discretion`, `other_managers`, `vote_sole`, `vote_shared`, `vote_none`.

`holdings_usd`: a view that joins in the filing fields and adds `value_usd`.

```sql
-- Apple holders, 2020Q4
SELECT manager_name, shares, value_usd
FROM holdings_usd JOIN filings USING (accession)
WHERE cusip = '037833100' AND period = '2020-12-31' AND form_type = '13F-HR'
ORDER BY shares DESC;
```

### Things to know before using the data

- **Value units.** Filings made before 2023-01-03 report value in thousands of dollars; later ones
  report dollars. `value_usd` applies that rule. Some filers ignored the rule in both directions,
  so check implied price (`value_usd / shares`) when it matters.
- **Amendments.** A `RESTATEMENT` amendment replaces the original filing for that period. A
  `NEW HOLDINGS` amendment adds rows to it. Build holdings per manager-quarter accordingly.
- **Duplicate rows are real.** One security can appear on several rows, split by discretion type
  or by other manager. Sum `shares` by `(accession, cusip)` to get a manager's position.
- **Pre-2013 text fields.** Voting authority and other-manager columns are left NULL: they sit in
  positional columns that are often blank, so reading them would mean guessing. Option rows
  carry the filer's option "CUSIP", which may not pass the check digit. A few filers give SEDOLs
  instead of CUSIPs, and some rows have no identifier at all (`cusip` NULL, about 0.1% of rows).
- **CUSIPs are cleaned.** When a filer's spreadsheet dropped leading zeros (`37833100` for Apple)
  and the check digit confirms it, the zeros are restored (`037833100`), in both eras. The SEC's
  own data sets keep the raw value.
- **Unreadable values are left empty.** Some text tables print the value and shares columns
  with no gap between them. The parser splits them when the share count can be confirmed from
  the voting columns. Otherwise it stores `value` and `shares` as NULL instead of a wrong number.

## Validation

Two independent checks.

**1. Against the SEC's own parsed data (2013 onward).** The SEC publishes its own parse of every
XML-era filing as the quarterly [Form 13F data sets](https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets).
I took 70 random filings from each of 2014Q1, 2019Q3 and 2024Q3. All 206 that appear in the data
sets match row for row on all nine fields (CUSIP, value, shares, SH/PRN, put/call, discretion,
three voting columns): 61,058 of 61,058 rows. The two left over were filed in September 2024,
after that data set's window ends. To repeat the check on any quarter:

```bash
python tools/compare_sec.py 13f.sqlite 2019q3_form13f.zip
```

**2. Against each filing's own totals (all years).** No parsed version of pre-2013 filings
exists to compare against. Every filing does state its own entry count and value total on the
summary page, so each parse is checked against those (`status`). I ran three independent random
samples, stratified by quarter, 2001Q1–2026Q3: 1,625 filings and 680,000 holding rows.

| era | filings | `ok` | `ok` or `near` | holding rows in `ok`/`near` filings | no summary page |
|---|---|---|---|---|---|
| XML (2013Q3+) | 796 | 95.0% | 96.7% | 97.8% | 0 |
| text (≤2013Q2) | 829 | 81.8% | 88.7% | 91.0% | 92 |

Percentages are of filings that have a summary page to check against.

Large filers are less often an exact `ok` than small ones: a 3,000-row table is more likely to
have a mistyped row, and its stated totals are more often slightly off. So filter on
`status IN ('ok', 'near')` rather than `ok` alone, or you lose many of the biggest managers.

**XML era.** The mismatches are the filer's own summary page disagreeing with its table, for
example a total stated in different units. Check 1 shows the parser reads every row as filed.

**Text era.** The remaining mismatches are mostly:

1. Filer errors in the stated totals. Some state total shares as the entry count, or put the
   price per share in the value column.
2. Unusual layouts that can't be read line by line: tables printed column by column, CSV
   exports with unlabeled columns, CUSIPs written in scientific notation by Excel.
3. Mistyped, truncated or missing CUSIPs on individual rows.

Problems found and fixed during validation, all covered by `tests/`:

- A second CUSIP-like code in the issuer text ("SEE CUSIP 69713P107", "WT EX030402") was taken as
  the CUSIP. The real CUSIP was then read as the value, inflating some large filers' totals by up
  to a trillion times.
- Value and shares columns printed with no gap between them.
- Filers who state the number of distinct securities as the entry count, rather than the number
  of rows.

Run `python -m thirteenf report` after a full build to see the same breakdown for every year.

Tests: `python -m unittest discover tests`

## Layout

```
thirteenf/edgar.py     EDGAR index + rate-limited, resumable downloader
thirteenf/parse.py     one submission file -> (filing, holdings); XML and text parsers
thirteenf/db.py        SQLite schema, parallel loader, quality report
thirteenf/__main__.py  command line
tools/compare_sec.py   check XML-era output against the SEC's Form 13F data sets
tests/                 unit tests with inline example filings
```
