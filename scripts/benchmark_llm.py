"""Benchmark armory's LLM classify task across model configs.

Dataset: real tacswap listings (site-structured price as ground truth) plus
hand-labeled synthetic positives/negatives for rules and scams. Scores each
config on latency, tokens, extraction accuracy, rule precision/recall, scam
detection. Usage: .venv/bin/python scripts/benchmark_llm.py [config ...]
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from armory.adapters.tacswap import _flight_listing_to_model, parse_flight_listings  # noqa: E402
from armory.llm import SYSTEM_PROMPT, extract_json  # noqa: E402
from armory.models import Listing  # noqa: E402
from armory.secrets import load_env, secret  # noqa: E402

FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "tacswap_search_flight.txt"
BASE = "https://api.z.ai/api/anthropic"

RULE_X300 = "SureFire X300 Ultra B weapon light — the B model specifically (not the A, not X300 Turbo)"
RULE_GLOCK = "Glock pistols under $600"
RULE_HOLDSUN = "Holosun red dots under $300"
RULES = [RULE_X300, RULE_GLOCK, RULE_HOLDSUN]


def synthetic(id_, title, body, price_usd, **kw):
    return Listing(
        source="tacswap", external_id=id_, url=f"https://tacswap.com/post/{id_}",
        title=title, body=body, price=f"${price_usd:,}", **kw,
    )


def build_dataset() -> tuple[list[Listing], dict]:
    """Returns (dataset, expectations)."""
    objs = {o["id"]: o for o in parse_flight_listings(FIXTURE.read_text())}
    real = []
    for o in objs.values():
        if o.get("itemValue") is None:
            continue
        real.append(_flight_listing_to_model(o))
        if len(real) >= 16:
            break

    syn = [
        synthetic("syn-x300b-yes", "SureFire X300 Ultra B weapon light w/ box",
                  "1000 lumens, like new, includes keys and box. $235 shipped.", 235),
        synthetic("syn-g19-yes", "Glock 19 Gen 5 with night sights",
                  "Round count ~500, includes 3 mags and case. FFL transfer required.", 550),
        synthetic("syn-507c-yes", "Holosun 507C X2 red dot",
                  "Green reticle, like new,comes with killmount.", 240),
        synthetic("syn-g17-no", "Glock 17 Gen 4",
                  "Clean pistol, two mags.", 700),
        synthetic("syn-scam1", "Brand NEW Trijicon ACOG TA31 - $250!!",
                  "First come first served DM me. No pics yet, cashapp only, shipping from overseas.", 250),
        synthetic("syn-scam2", "Aimpoint T2 brand new $180",
                  "Selling for my cousin, friends and family payment only, no returns.", 180),
    ]
    # real listing that must NOT match the X300 Ultra B rule (Turbo variant)
    x300t = next((l for l in real if "X300T" in l.title.upper()), None)
    # real listing that must NOT match the Glock pistol rule (accessory)
    glock_acc = next((l for l in real if "GLOCK" in l.title.upper() and "SIGHT" in l.title.upper()), None)

    dataset = real + syn
    expect = {
        "price": {o["id"]: float(o["itemValue"]) for o in objs.values() if o.get("itemValue") is not None},
        "brand": {},  # hand labels, filled below for unambiguous cases
        "rules": {
            "syn-x300b-yes": [RULE_X300],
            "syn-g19-yes": [RULE_GLOCK],
            "syn-507c-yes": [RULE_HOLDSUN],
            "syn-g17-no": [],
            "syn-scam1": [],
            "syn-scam2": [],
        },
        "scam_high": ["syn-scam1", "syn-scam2"],
        "scam_low": [],  # filled: every real listing + non-scam synthetics
    }
    for l in dataset:
        if l.external_id not in expect["scam_high"]:
            expect["scam_low"].append(l.external_id)
        expect["rules"].setdefault(l.external_id, [])
    for l in real:
        t = l.title.upper()
        if "HOLOSUN" in t:
            expect["brand"][l.external_id] = "Holosun"
        elif "SUREFIRE" in t or "SURE FIRE" in t:
            expect["brand"][l.external_id] = "SureFire"
    if x300t is not None:
        expect["rules"][x300t.external_id] = []
    if glock_acc is not None:
        expect["rules"][glock_acc.external_id] = []
    expect["brand"]["syn-x300b-yes"] = "SureFire"
    expect["brand"]["syn-g19-yes"] = "Glock"
    expect["brand"]["syn-507c-yes"] = "Holosun"
    print(f"dataset: {len(dataset)} listings ({len(real)} real, {len(syn)} synthetic), "
          f"{sum(1 for v in expect['rules'].values() if v)} rule positives, brand labels={len(expect['brand'])}")
    return dataset, expect


def to_payload(listings: list[Listing]) -> str:
    return json.dumps(
        {
            "rules": RULES,
            "listings": [
                {
                    "id": l.external_id,
                    "source": l.source,
                    "title": l.title,
                    "price": l.price,
                    "location": l.location,
                    "category": l.category,
                    "seller": l.author,
                    "description": (l.body or "")[:900],
                }
                for l in listings
            ],
        }
    )


def call_once(model: str, thinking: dict | None, listings: list[Listing]) -> tuple[dict, dict]:
    body = {
        "model": model,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": to_payload(listings)}],
    }
    if thinking:
        body["thinking"] = thinking
    last_error = None
    for attempt in range(3):  # z.ai throws occasional transient 500s
        t0 = time.time()
        r = httpx.post(
            f"{BASE}/v1/messages",
            headers={"x-api-key": secret("LLM_API_KEY"), "anthropic-version": "2023-06-01"},
            json=body,
            timeout=240,
        )
        dt = time.time() - t0
        if r.status_code == 200:
            data = r.json()
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            results = {str(x["id"]): x for x in extract_json(text).get("results", [])}
            usage = data.get("usage", {})
            return {"seconds": dt, "results": results, "usage": usage}
        last_error = f"HTTP {r.status_code}: {r.text[:200]}"
        time.sleep(2)
    raise RuntimeError(last_error)


def _norm(s: str) -> str:
    import re as _re

    return _re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def _map_rule(returned: str) -> str | None:
    """Map a (possibly truncated/paraphrased) rule echo back to a real rule."""
    from difflib import SequenceMatcher

    r = _norm(returned)
    best_rule, best_ratio = None, 0.0
    for rule in RULES:
        ratio = SequenceMatcher(None, r, _norm(rule)).ratio()
        if ratio > best_ratio:
            best_rule, best_ratio = rule, ratio
    return best_rule if best_ratio >= 0.55 else None


def score(runs: list[dict], dataset: list[Listing], expect: dict) -> dict:
    # merge results across all calls (each run covers a different batch)
    results: dict[str, dict] = {}
    for run in runs:
        for rid, res in run["results"].items():
            results.setdefault(rid, res)
    price_ok = price_n = 0
    for rid, expected_price in expect["price"].items():
        got = results.get(rid, {}).get("price_usd")
        if got is None:
            continue
        price_n += 1
        if abs(float(got) - expected_price) < 1.01:
            price_ok += 1
    brand_ok = brand_n = 0
    for rid, expected_brand in expect["brand"].items():
        got = (results.get(rid, {}).get("brand") or "").lower()
        if got:
            brand_n += 1
            if expected_brand.lower() in got:
                brand_ok += 1
    tp = fp = fn = 0
    for rid, expected_rules in expect["rules"].items():
        got = {_map_rule(r) for r in (results.get(rid, {}).get("matched_rules") or [])}
        got.discard(None)
        exp = set(expected_rules)
        tp += len(got & exp)
        fp += len(got - exp)
        fn += len(exp - got)
    scam_hits = sum(1 for rid in expect["scam_high"] if results.get(rid, {}).get("scam_risk") == "high")
    scam_fp = sum(1 for rid in expect["scam_low"] if results.get(rid, {}).get("scam_risk") == "high")
    return {
        "price": f"{price_ok}/{price_n}",
        "brand": f"{brand_ok}/{brand_n}",
        "rules_tp/fp/fn": f"{tp}/{fp}/{fn}",
        "scam": f"{scam_hits}/{len(expect['scam_high'])} caught, {scam_fp} false-high",
        "median_s": round(statistics.median(r["seconds"] for r in runs), 1),
        "avg_in_tok": int(statistics.mean(r["usage"].get("input_tokens", 0) for r in runs)),
        "avg_out_tok": int(statistics.mean(r["usage"].get("output_tokens", 0) for r in runs)),
    }


CONFIGS = {
    "turbo-default": ("glm-5-turbo", None),
    "5.2-noreason": ("glm-5.2", {"type": "disabled"}),
    "5.2-lowreason": ("glm-5.2", {"type": "enabled", "budget_tokens": 1024}),
    "5.3flash-default": ("glm-5.3-flash", None),
    "5.3flash-noreason": ("glm-5.3-flash", {"type": "disabled"}),
    "5.3-noreason": ("glm-5.3", {"type": "disabled"}),
}

if __name__ == "__main__":
    load_env()
    dataset, expect = build_dataset()
    batches = [dataset[i : i + 10] for i in range(0, len(dataset), 10)]
    names = sys.argv[1:] or list(CONFIGS)
    report: dict[str, dict] = {}
    for name in names:
        model, thinking = CONFIGS[name]
        print(f"\n=== {name} ({model}, thinking={thinking}) ===", flush=True)
        runs = []
        failures = 0
        for rep in range(2):
            for batch in batches:
                try:
                    run = call_once(model, thinking, batch)
                except Exception as exc:  # noqa: BLE001 — transient API errors skip the call
                    failures += 1
                    print(f"  call failed ({str(exc)[:120]})", flush=True)
                    continue
                runs.append(run)
                print(f"  call {len(runs)}: {run['seconds']:.1f}s "
                      f"in={run['usage'].get('input_tokens')} out={run['usage'].get('output_tokens')}", flush=True)
        if runs:
            report[name] = score(runs, dataset, expect)
            if failures:
                report[name]["failed_calls"] = failures
        else:
            report[name] = {"error": "all calls failed"}
    print("\n==== SUMMARY ====")
    for name, metrics in report.items():
        print(f"\n{name}:")
        for k, v in metrics.items():
            print(f"  {k:16} {v}")
