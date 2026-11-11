"""Stage 3: find what each company says about its SAP / ERP systems.

For every sentence that names a system (S/4HANA, SAP, ERP, Oracle ERP, Workday), look at that
sentence plus the one before and the two after for signals: delays, overruns, impairments, exceptional
costs, disruption, go-live, programme stage, the 2027 deadline, and money amounts.

Rules-based on purpose: every finding comes with the exact quote and page, so anyone can check it.

Outputs:
  output/companies.csv  one row per company, ranked (best leads first)
  output/findings.csv   every matching passage with page number
  output/summary.md     headline numbers

Run:  python -m erpmap.scan
"""

import csv
import json
import re
from collections import Counter

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
OUT = ROOT / "output"

SYSTEMS = {
    "S/4HANA": re.compile(r"\bS\s?/\s?4\s?HANA\b|\bS4\s?/?\s?HANA\b|\bS/4\b|\bRISE with SAP\b", re.I),
    "SAP ECC": re.compile(r"\bSAP\s+(?:ECC|R/3|Business Suite)\b|\bECC\s?6", re.I),
    "SAP": re.compile(r"\bSAP\b"),
    "ERP": re.compile(r"\bERP\b|enterprise resource planning", re.I),
    "Oracle ERP": re.compile(r"\bOracle\s+(?:Fusion|Cloud\s+ERP|ERP|E-Business|NetSuite)", re.I),
    "Microsoft D365": re.compile(r"\bD365\b|Dynamics\s?365", re.I),
}
ANY_SYSTEM = re.compile("|".join(rx.pattern for rx in SYSTEMS.values()), re.I)

SIGNALS = {
    "delay": r"\bdelay(?:s|ed)?\b|\bpostpone(?:d|ment)?\b|\bslipp(?:ed|age)\b|\blater than (?:planned|expected|anticipated)\b|\brephas|\bpaused\b",
    "overrun": r"\bover-?runs?\b|\bover budget\b|\bhigher than (?:originally |initially )?(?:planned|expected|anticipated|budget(?:ed)?)\b|\bcost increases?\b|\bincreased costs?\b|\bconsultancy costs\b",
    "impairment": r"\bimpair(?:ed|ment)s?\b|\bwrit(?:e|ten)[- ]?(?:off|down)\b|\baccelerat\w+ (?:of )?(?:amortisation|depreciation)",
    "exceptional_cost": r"\bexceptional\b|\badjusting items?\b|\bseparately disclosed\b|\bnon-underlying\b|\bspecific (?:adjusting )?items\b",
    "disruption": r"\bdisruption\b|\bdisrupted\b|\bdifficulties\b|\bstabilis|\bstabiliz|\bteething\b|\bservice (?:challenges|issues)\b|\bchallenges to delivery\b",
    "legacy_or_unsupported": r"\bunsupported\b|\bend of (?:its )?(?:supported |useful )?life\b|\bend of (?:mainstream )?(?:support|maintenance)\b|\blegacy\b|\bage profile\b|\boutdated\b|\b2027\b",
    "go_live": r"\bgo[- ]live\b|\bwent live\b|\bgone live\b|\bcut-?over\b|\bsuccessfully (?:implemented|deployed|launched|migrated|completed)\b",
    "in_progress": r"\bprogramme\b|\bprogram\b|\bimplementation\b|\bimplementing\b|\bmigrat|\broll-?out\b|\btransformation\b|\bupgrade\b|\breplac|\bmoderni[sz]",
    "external_review": r"\bexternal review\b|\bassurance (?:partner|provider)s?\b|\bindependent review\b|\bprincipal risk\b",
}
SIGNALS = {k: re.compile(v, re.I) for k, v in SIGNALS.items()}
STRONG = {"delay", "overrun", "impairment", "exceptional_cost", "disruption"}

# Director/executive bios mention past employers ("previously CMO at SAP"); skip those passages
BIO = re.compile(r"\b(previously|formerly|former|non-executive director|career|joined [A-Z]|served as|experience at|positions at|worked at|held (?:a number of|several|various)|chief \w+ officer at|brings over)\b|SAP[’']s\b", re.I)
# "SAP" also abbreviates non-software things in annual reports
OTHER_SAP = re.compile(r"standard assessment procedure|SAP (?:rating|score|10|2012)|\bEPC\b|share award plan|action plan|agricultural principles|\bSAP\s+(?:awards?|vesting)", re.I)
SAP_SOFTWARE = re.compile(r"S\s?/\s?4|HANA|\bECC\b|SAP[- ]based|SAP (?:ERP|system|platform|implementation|programme|estate|environment|application|software|modules?|Ariba|SuccessFactors|BW|Concur|Analytics)|ERP|enterprise resource planning|IT controls|finance system", re.I)
MONEY = re.compile(r"(?:£|€|\$|GBP|EUR|USD)\s?\d[\d,]*(?:\.\d+)?\s?(?:bn|billion|m|million|k)\b", re.I)

QUOTE_BEFORE, QUOTE_AFTER = 250, 350     # characters shown around the system mention
STRONG_BEFORE, STRONG_AFTER = 200, 300   # problem signals must sit this close to the mention

WEIGHTS = {"delay": 4, "overrun": 4, "impairment": 3, "exceptional_cost": 3, "disruption": 4,
           "legacy_or_unsupported": 3, "external_review": 2, "go_live": 1, "in_progress": 1}
SYSTEM_WEIGHT = {"S/4HANA": 5, "SAP ECC": 5, "SAP": 3, "ERP": 1, "Oracle ERP": 1, "Microsoft D365": 1}
SAP_FAMILY = {"S/4HANA", "SAP ECC", "SAP"}


def clean(page_text):
    text = re.sub(r"-\s*\n\s*", "", page_text)  # re-join hyphenated line breaks
    return re.sub(r"\s+", " ", text)


def doc_sap_is_other(full_text):
    """True if this report defines SAP as something other than the software company."""
    return bool(OTHER_SAP.search(full_text))


def trim(text, start, end):
    a, b = max(0, start), min(len(text), end)
    if a > 0:
        a = text.find(" ", a) + 1 or a
    if b < len(text):
        b = text.rfind(" ", a, b) or b
    return ("…" if a > 0 else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def scan_company(doc):
    pages = [clean(p) for p in doc["pages"]]
    sap_ambiguous = doc_sap_is_other(" ".join(pages))
    findings, seen = [], set()
    for page_no, text in enumerate(pages, 1):
        last_end = -10**9
        for m in ANY_SYSTEM.finditer(text):
            if m.start() < last_end:  # already covered by the previous quote
                continue
            q0, q1 = m.start() - QUOTE_BEFORE, m.end() + QUOTE_AFTER
            quote_raw = text[max(0, q0): q1]
            local = text[max(0, m.start() - 60): m.end() + 60]
            systems = [n for n, rx in SYSTEMS.items() if rx.search(quote_raw)]
            if systems and set(systems) <= {"SAP"}:
                if OTHER_SAP.search(local) or (sap_ambiguous and not SAP_SOFTWARE.search(quote_raw)):
                    continue
            if not systems or BIO.search(text[max(0, m.start() - 200): m.end() + 120]):
                continue
            last_end = q1
            key = re.sub(r"\W", "", quote_raw.lower())[:150]
            if key in seen:
                continue
            seen.add(key)
            near = text[max(0, m.start() - STRONG_BEFORE): m.end() + STRONG_AFTER]
            signals = [n for n, rx in SIGNALS.items() if rx.search(near if n in STRONG else quote_raw)]
            amounts = sorted(set(a.group(0) for a in MONEY.finditer(near)))
            findings.append({
                "company_name": doc["company_name"],
                "lei": doc["company_number"],
                "period_end": doc["period_end"],
                "page": page_no if len(pages) > 1 else "",
                "systems": "; ".join(systems),
                "signals": "; ".join(signals),
                "amounts": "; ".join(amounts),
                "passage": trim(text, q0, q1),
                "_score": max(SYSTEM_WEIGHT[x] for x in systems) + sum(WEIGHTS[x] for x in signals) + (1 if amounts else 0),
            })
    return findings


def stage_guess(signals):
    if signals & {"delay", "overrun", "disruption"}:
        return "programme with reported problems"
    if "legacy_or_unsupported" in signals:
        return "legacy / unsupported system"
    if signals & {"impairment", "exceptional_cost"}:
        return "programme with one-off costs"
    if "go_live" in signals:
        return "recently went live"
    if "in_progress" in signals:
        return "programme in progress"
    return "mentions system only"


def main():
    OUT.mkdir(exist_ok=True)
    docs = sorted((DATA / "text").glob("*.json"))
    if not docs:
        raise SystemExit("No text found. Run python -m erpmap.fetch first.")

    all_findings, companies, status = [], [], Counter()
    for path in docs:
        doc = json.loads(path.read_text())
        status[doc.get("status", "")] += 1
        if doc.get("status") != "ok":
            continue
        found = scan_company(doc)
        if not found:
            continue
        all_findings.extend(found)
        found.sort(key=lambda f: f["_score"], reverse=True)
        signals = set(s for f in found for s in f["signals"].split("; ") if s)
        systems = set(s for f in found for s in f["systems"].split("; ") if s)
        amounts = []
        for f in found:
            for a in f["amounts"].split("; "):
                if a and a not in amounts:
                    amounts.append(a)
        best = found[0]
        companies.append({
            "company_name": doc["company_name"],
            "lei": doc["company_number"],
            "period_end": doc["period_end"],
            "stage_guess": stage_guess(signals),
            "systems": "; ".join(sorted(systems)),
            "signals": "; ".join(sorted(signals)),
            "amounts_mentioned": "; ".join(amounts[:6]),
            "passages": len(found),
            "score": sum(f["_score"] for f in found[:5]),
            "best_page": best["page"],
            "best_passage": best["passage"],
            "report_link": doc.get("report_link", ""),
        })

    companies.sort(key=lambda c: c["score"], reverse=True)
    for rank, c in enumerate(companies, 1):
        c["rank"] = rank

    with (OUT / "companies.csv").open("w", newline="") as f:
        cols = ["rank", "company_name", "lei", "period_end", "stage_guess", "systems", "signals",
                "amounts_mentioned", "passages", "score", "best_page", "best_passage", "report_link"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(companies)
    all_findings.sort(key=lambda f: (f["company_name"], -f["_score"]))
    with (OUT / "findings.csv").open("w", newline="") as f:
        cols = ["company_name", "lei", "period_end", "page", "systems", "signals", "amounts", "passage"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_findings)

    sap = [c for c in companies if SAP_FAMILY & set(c["systems"].split("; "))]
    s4 = [c for c in companies if "S/4HANA" in c["systems"]]
    problems = [c for c in companies if c["stage_guess"].startswith("programme with")]
    summary = f"""# UK ERP disclosures: summary

- Annual reports downloaded: {sum(status.values())}
- Readable (text layer): {status['ok']}  |  scanned images, skipped: {status['no_text_layer']}
- Reports that mention SAP, ERP, Oracle ERP or Microsoft D365: {len(companies)}
- Mention SAP (any product) as their own system: {len(sap)}
- Mention S/4HANA or RISE with SAP: {len(s4)}
- Describe a programme with delays, overruns, impairments, exceptional costs or disruption: {len(problems)}

Stage guesses are keyword-based. Check the quote and page in companies.csv before relying on any row.

## Top 15
""" + "\n".join(f"{c['rank']}. {c['company_name']}: {c['stage_guess']} ({c['systems']})" for c in companies[:15]) + "\n"
    (OUT / "summary.md").write_text(summary)
    print(summary)
    print("Files: output/companies.csv, output/findings.csv, output/summary.md")


if __name__ == "__main__":
    main()
