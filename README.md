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
| `status` | `ok`: the parse reproduces the filer's stated count and value total. Other values: `count_mismatch`, `value_mismatch`, `empty`, `unchecked` (no summary page), `error: …` |

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

## Validation

The SEC does not publish a parsed version of pre-2013 filings to compare against. Every filing
does carry its own entry count and value total on the summary page, though, so each parse is
checked against those (`status`). I ran two independent random samples, stratified at 5–6 filings
per quarter from 2006Q1 to 2026Q3: 913 filings and 204,000 holding rows.

| era | filings | parse matches the stated count and value | count within 2% | no summary page |
|---|---|---|---|---|
| XML (2013Q3+) | 584 | 94.5% | 98.5% | 0 |
| text (≤2013Q2) | 329 | 81.6% of checkable | 93.0% of checkable | 30 |

**XML era.** Every mismatch I inspected was the filer's own summary page disagreeing with its
table (for example a total stated in different units, or an entry count that doesn't match the
rows filed). The parser reads every `infoTable` row.

**Text era.** The remaining mismatches are mostly:

1. Filer errors in the stated totals.
2. Unusual layouts. One table printed column by column (all names, then all CUSIPs) can't be
   read this way.
3. Mistyped or truncated CUSIPs on single rows.

Run `python -m thirteenf report` after a full build to see the same breakdown for every year. Use
`status` to filter, for example keep `status = 'ok'` only, or relax that to a near match on
`n_holdings` against `entry_total`.

Tests: `python -m unittest discover tests`

## Layout

```
thirteenf/edgar.py     EDGAR index + rate-limited, resumable downloader
thirteenf/parse.py     one submission file -> (filing, holdings); XML and text parsers
thirteenf/db.py        SQLite schema, parallel loader, quality report
thirteenf/__main__.py  command line
tests/                 unit tests with inline example filings
```
