# UK ERP Disclosures

What do the UK's largest companies tell their shareholders about their SAP and ERP programmes?

SAP stops mainstream maintenance for ECC 6.0 at the end of 2027, so most large SAP users are planning, running or recovering from a move to S/4HANA. Surveys say most of these projects run late or over budget. This project looks at the evidence companies have actually filed: their audited annual reports.

For each company it finds every passage that mentions SAP, S/4HANA, ERP, Oracle ERP or Workday, then flags nearby signals: delays, overruns, impairments, exceptional costs, disruption, go-live and the 2027 deadline. Every finding links back to the exact quote and page.

## Output

- `output/companies.csv`: one row per company, ranked, with a stage guess, signals, amounts and the strongest quote
- `output/findings.csv`: every matching passage with its page number
- `output/summary.md`: headline numbers

## How it works

1. **Reports** (`erpmap/fetch.py`): UK main-market companies have to file their annual report digitally (ESEF / Inline XBRL). filings.xbrl.org collects these. The script lists every GB filing since mid-2024, keeps the latest one per company, skips investment trusts and funds, downloads the report and extracts the text page by page. Companies House isn't used here because large companies' accounts there are stored as scanned images with no text.
2. **Scan** (`erpmap/scan.py`): the scan is rules-based on purpose, with no black box. A system mention has to be in the sentence itself. Problem signals (delay, overrun, impairment, exceptional cost) only count if they appear in that sentence or the next two, so an unrelated goodwill impairment elsewhere isn't pinned on the ERP programme. Mentions of SAP in director bios are ignored.

## Limitations

- Only covers what companies choose to disclose. A silent annual report doesn't mean there's no programme.
- Stage guesses come from keywords, so read the quote before relying on a row.
- Scanned PDFs with no text layer are skipped and counted in the summary.
- Only covers companies whose digital report is on filings.xbrl.org, which is mainly the UK main market. AIM companies and private groups aren't covered.
- Uses the latest accounts only, not the full history.

## Run it

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m erpmap.fetch --limit 10   # test
python -m erpmap.fetch              # everything
python -m erpmap.scan
```

Data: annual reports via filings.xbrl.org (XBRL International).
