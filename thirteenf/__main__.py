"""python -m thirteenf {download,parse,report} ..."""
import argparse

from . import db, edgar


def main():
    p = argparse.ArgumentParser(prog="thirteenf", description="Bulk download and parse SEC 13F-HR filings.")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="download 13F-HR(/A) filings for a range of quarters")
    d.add_argument("--start", required=True, help="first quarter, e.g. 2005Q1 or 2005")
    d.add_argument("--end", required=True, help="last quarter, e.g. 2025Q2 or 2025")
    d.add_argument("--data", default="data", help="directory for raw filings (default: data)")
    d.add_argument("--sample", type=int, help="download only N random filings per quarter")
    d.add_argument("--seed", type=int, default=0)
    d.add_argument("--workers", type=int, default=8)
    d.add_argument("--refresh-index", action="store_true", help="re-fetch cached quarterly indexes")

    s = sub.add_parser("parse", help="parse downloaded filings into SQLite")
    s.add_argument("--data", default="data")
    s.add_argument("--db", default="13f.sqlite")
    s.add_argument("--reparse", action="store_true", help="re-parse filings already in the database")
    s.add_argument("--workers", type=int)

    r = sub.add_parser("report", help="parse-quality summary by year")
    r.add_argument("--db", default="13f.sqlite")

    a = p.parse_args()
    if a.cmd == "download":
        edgar.download(a.data, a.start, a.end, a.sample, a.seed, a.workers, a.refresh_index)
    elif a.cmd == "parse":
        db.report(db.load(a.data, a.db, a.reparse, a.workers))
    else:
        db.report(db.connect(a.db))


if __name__ == "__main__":
    main()
