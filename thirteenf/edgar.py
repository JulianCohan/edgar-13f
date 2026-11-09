"""Download 13F-HR filings from SEC EDGAR.

Layout on disk:
    <data>/index/<YYYY>Q<n>.idx                  quarterly master index (cached)
    <data>/filings/<cik>/<accession>.txt.gz      full submission text, gzipped
"""
import gzip
import os
import random
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://www.sec.gov/Archives/"
FORMS = {"13F-HR", "13F-HR/A"}
MAX_RPS = 8  # SEC fair-access limit is 10 requests/second per user


class RateLimiter:
    def __init__(self, rps):
        self.interval, self.next, self.lock = 1.0 / rps, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(self.next, now)
            self.next = slot + self.interval
        time.sleep(slot - now)


_limiter = RateLimiter(MAX_RPS)


def user_agent():
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise SystemExit('Set SEC_USER_AGENT, e.g. export SEC_USER_AGENT="Jane Doe jdoe@tulane.edu" '
                         "(the SEC requires a name and contact email).")
    return ua


def fetch(url, tries=6):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip"})
    for attempt in range(tries):
        _limiter.wait()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                return gzip.decompress(body) if r.headers.get("Content-Encoding") == "gzip" else body
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            if attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == tries - 1:
                raise
        time.sleep(2 ** attempt + random.random())  # 403/429/5xx: back off; SEC throttles bursts


def quarters(start, end):
    """Inclusive (year, q) pairs; start/end like '2005Q1' or '2005'."""
    def parse(s, last):
        return (int(s[:4]), int(s[5]) if len(s) > 4 else (4 if last else 1))
    (y, q), stop = parse(start, False), parse(end, True)
    while (y, q) <= stop:
        yield y, q
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)


def index_rows(data, year, q, refresh=False):
    """13F-HR(/A) rows from a quarter's master index: dicts with cik, name, form, date_filed, path."""
    path = Path(data) / "index" / f"{year}Q{q}.idx"
    if refresh or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        body = gzip.decompress(fetch(f"{BASE}edgar/full-index/{year}/QTR{q}/master.gz")).decode("latin-1")
        keep = [line for line in body.splitlines() if line.count("|") == 4 and line.split("|")[2] in FORMS]
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(keep) + "\n", encoding="latin-1")  # cache only the 13F rows
        tmp.replace(path)
    rows = []
    for line in path.read_text(encoding="latin-1").splitlines():
        parts = line.split("|")
        if len(parts) == 5:
            rows.append(dict(cik=parts[0], name=parts[1], form=parts[2], date_filed=parts[3], path=parts[4]))
    return rows


def filing_path(data, row):
    accession = row["path"].rsplit("/", 1)[1].removesuffix(".txt")
    return Path(data) / "filings" / row["cik"] / f"{accession}.txt.gz"


def download_one(data, row):
    out = filing_path(data, row)
    if out.exists():
        return "skip"
    try:
        body = fetch(BASE + row["path"])
    except urllib.error.HTTPError as e:
        return f"error {e.code} {row['path']}"
    except Exception as e:
        return f"error {type(e).__name__} {row['path']}"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(body, 6))
    tmp.replace(out)  # atomic: an interrupted run never leaves a truncated file behind
    return "ok"


def download(data, start, end, sample=None, seed=0, workers=8, refresh_index=False):
    rows, rng = [], random.Random(seed)
    for y, q in quarters(start, end):
        qrows = index_rows(data, y, q, refresh=refresh_index)
        if sample:  # per-quarter sample, so early and late years are equally represented
            qrows = rng.sample(qrows, min(sample, len(qrows)))
        rows += qrows
        print(f"{y}Q{q}: {len(qrows)} filings", flush=True)
    counts = {}
    with ThreadPoolExecutor(workers) as pool:
        for i, result in enumerate(pool.map(lambda r: download_one(data, r), rows), 1):
            key = result.split()[0]
            counts[key] = counts.get(key, 0) + 1
            if key == "error":
                print(result, flush=True)
            if i % 500 == 0 or i == len(rows):
                print(f"{i}/{len(rows)} {counts}", flush=True)
    return counts
