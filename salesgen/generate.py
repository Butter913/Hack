"""Sales-call dataset generator: planned personas, ElevenLabs simulations, challenge-format output.

    python salesgen/generate.py plan     --run runs/demo --calls 200 --lang pl
    python salesgen/generate.py simulate --run runs/demo            # needs ELEVENLABS_API_KEY; --mock for offline
    python salesgen/generate.py build    --run runs/demo
    python salesgen/generate.py audio    --run runs/demo --seller-voice <id> --customer-voice <id> --limit 10

Each step reads the previous step's files from ``--run`` and can be rerun: ``simulate`` skips calls already in
transcripts.jsonl. ``build`` writes ``dataset/`` in the challenge format (histories, questions, gold) plus
calls_gold.jsonl with the planted label of every call.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenarios as sc  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def opaque(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


# ---- plan ---------------------------------------------------------------------------------------------------

def cmd_plan(args) -> None:
    run = Path(args.run)
    if (run / "transcripts.jsonl").exists() and not args.force:
        sys.exit(f"{run} already has simulated calls, which a new plan would not match: use another --run "
                 f"(or --force to replan and start the transcripts over)")
    if args.force:
        (run / "transcripts.jsonl").unlink(missing_ok=True)
    calls = sc.plan_campaign(args.calls, lang=args.lang, seed=args.seed, start=args.start, days=args.days)
    write_jsonl(run / "plan.jsonl", calls)
    (run / "config.json").write_text(json.dumps(vars(args), indent=2), encoding="utf-8")
    patterns = collections.Counter(c["pattern"] for c in calls)
    reasons = collections.Counter(c["primary_reason"] for c in calls if c["outcome"] == "no_sale")
    print(f"{len(calls)} calls, {len({c['customer_id'] for c in calls})} customers -> {run / 'plan.jsonl'}")
    print("patterns:", dict(patterns.most_common()))
    print("planted primary reasons (no_sale):", dict(reasons.most_common()))
    print("\nexample persona:\n" + sc.scenario_prompt(next(c for c in calls if c["outcome"] == "no_sale")))


# ---- simulate -----------------------------------------------------------------------------------------------

def cmd_simulate(args) -> None:
    run = Path(args.run)
    plan = read_jsonl(run / "plan.jsonl")
    done = {t["call_id"] for t in read_jsonl(run / "transcripts.jsonl")}
    todo = [c for c in plan if c["call_id"] not in done][: args.limit or None]
    if not todo:
        print("nothing to simulate")
        return
    lang = plan[0]["lang"]
    out = (run / "transcripts.jsonl").open("a", encoding="utf-8")

    if args.mock:
        for call in todo:
            out.write(json.dumps(mock_transcript(call), ensure_ascii=False) + "\n")
        print(f"{len(todo)} mock transcripts -> {run / 'transcripts.jsonl'}")
        return

    from elevenlabs_api import ElevenLabs, ElevenLabsError
    api = ElevenLabs()
    state_path = run / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    prompt = sc.SELLER_PROMPT.format(language=sc.LANGS[lang])
    version = opaque("", prompt, sc.SELLER_FIRST_MESSAGE[lang])
    if args.agent_id:
        state.update(agent_id=args.agent_id, agent_version=version)
    if state.get("agent_version") != version:  # no agent yet, or one made from an older prompt
        state["agent_id"] = api.create_agent(
            name=f"salesgen seller ({lang})", prompt=prompt, first_message=sc.SELLER_FIRST_MESSAGE[lang],
            language=lang, llm=args.agent_llm,
            placeholders={"customer_name": "Jan Kowalski", "product": "oferta", "price": "100 zł", "term": "rok",
                          "seller_style": sc.GOOD_SELLER})
        state["agent_version"] = version
        state_path.write_text(json.dumps(state, indent=2))
        print("seller agent:", state["agent_id"])

    for i in range(0, len(todo), args.batch):
        batch = todo[i: i + args.batch]
        tests = {}
        try:
            for call in batch:
                tests[api.create_simulation_test(
                    name=f"salesgen {call['call_id']}", scenario=sc.scenario_prompt(call),
                    conditions=sc.success_conditions(call), max_turns=args.max_turns, user_llm=args.user_llm,
                    dynamic_variables={**{k: call[k] for k in ("customer_name", "product", "price", "term")},
                                       "seller_style": sc.seller_style(call)})] = call
            inv = api.wait_invocation(api.run_tests(state["agent_id"], list(tests), f"salesgen batch {i}"),
                                      timeout=args.timeout)
        except ElevenLabsError as e:
            sys.exit(f"stopped at batch {i // args.batch}: {e}\n(rerun to continue: finished calls are kept)")
        for r in inv.get("test_runs", []):
            call = tests.get(r.get("test_id"))
            if call is None:
                continue
            turns = [{"role": "user" if m.get("role") == "user" else "assistant", "content": m["message"]}
                     for m in r.get("agent_responses") or [] if m.get("message")]
            cond = r.get("condition_result") or {}
            out.write(json.dumps({"call_id": call["call_id"], "status": r.get("status"), "turns": turns,
                                  "condition": cond.get("result"), "rationale": cond.get("rationale"),
                                  "credits": r.get("credits_used"), "test_run_id": r.get("test_run_id")},
                                 ensure_ascii=False) + "\n")
        out.flush()
        if not args.keep_tests:
            for t in tests:
                try:
                    api.delete_test(t)
                except ElevenLabsError:
                    pass
        statuses = collections.Counter(r.get("status") for r in inv.get("test_runs", []))
        print(f"batch {i // args.batch + 1}/{-(-len(todo) // args.batch)}: {dict(statuses)}")


def mock_transcript(call: dict) -> dict:
    """A short scripted call that follows the plan, for testing the later steps offline. Not realistic text."""

    pl = call["lang"] == "pl"
    turns = [("assistant", sc.SELLER_FIRST_MESSAGE[call["lang"]].replace("{{customer_name}}", call["customer_name"])),
             ("user", "Tak, słucham." if pl else "Yes, speaking."),
             ("assistant", (f"Dzwonię z ofertą: {call['product']}, {call['price']}, {call['term']}." if pl else
                            f"I'm calling about {call['product']}, {call['price']}, {call['term']}.")),
             ("user", "W weekend jedziemy do teściów, ale mów pani dalej." if pl else
              "We're visiting my in-laws this weekend, but go on.")]
    if call["pattern"] == "agent_wrong_guess":
        turns.append(("assistant", "Czy chodzi o cenę?" if pl else "Is it the price?"))
        turns.append(("user", "Nie, cena jest w porządku." if pl else "No, the price is fine."))
    if call["primary_detail"]:
        turns.append(("assistant" if call["fault"] == "seller" else "user",
                      f"({call['primary_reason']}) {call['primary_detail']}"))
    if call["secondary_detail"]:
        turns.append(("user", f"({call['secondary_reason']}) {call['secondary_detail']}."))
    end = {"sale": "Dobrze, biorę." if pl else "OK, I'll take it.",
           "follow_up": "Proszę zadzwonić w przyszłym miesiącu." if pl else "Call me next month.",
           "no_sale": "Muszę to przemyśleć." if pl else "I need to think about it."}[call["outcome"]]
    if call["pattern"] == "price_grumble_sale":
        turns.append(("user", "Trochę drogo..." if pl else "That's a bit pricey..."))
    turns.append(("user", end))
    return {"call_id": call["call_id"], "status": "passed", "condition": "success", "mock": True,
            "turns": [{"role": r, "content": c} for r, c in turns]}


# ---- build --------------------------------------------------------------------------------------------------

Q = {
    "pl": {
        "agg": "Jaki jest najczęstszy powód, dla którego klienci nie kupili oferowanego produktu lub usługi?",
        "agg_product": "Jaki jest najczęstszy powód odmowy zakupu oferty „{product}”?",
        "count": "Ilu klientów odmówiło zakupu głównie z powodu: {label}?",
        "seller_top": "Jaki błąd sprzedawcy najczęściej powodował, że rozmowa nie kończyła się sprzedażą?",
        "fault_split": "W ilu nieudanych rozmowach winny był sprzedawca, a w ilu powód leżał po stronie klienta?",
        "why": "Dlaczego {name} nie {verb} oferty „{product}”?",
        "why_now": "Jaki jest najbardziej aktualny powód, dla którego {name} nie {verb} oferty „{product}”?",
    },
    "en": {
        "agg": "What is the most common reason customers did not buy the product or service they were offered?",
        "agg_product": "What is the most common reason customers declined the \"{product}\" offer?",
        "count": "How many customers declined mainly because of: {label}?",
        "seller_top": "Which salesperson mistake most often cost the sale?",
        "fault_split": "In how many failed calls was the salesperson at fault, and in how many was the reason on the customer's side?",
        "why": "Why didn't {name} buy the \"{product}\" offer?",
        "why_now": "What is the latest reason {name} gave for not buying the \"{product}\" offer?",
    },
}


def clean(text: str) -> str:
    """Drops the voice-expression tags ElevenLabs agents put in their text ("[happy]", "[slow]")."""

    return re.sub(r"\s{2,}", " ", re.sub(r"\[[a-z][a-z ]{0,20}\]\s*", "", text)).strip()


def cmd_build(args) -> None:
    run = Path(args.run)
    plan = {c["call_id"]: c for c in read_jsonl(run / "plan.jsonl")}
    transcripts = read_jsonl(run / "transcripts.jsonl")
    lang = next(iter(plan.values()))["lang"]
    rng = random.Random(args.seed)

    kept, dropped = [], collections.Counter()
    for t in transcripts:
        ok = t.get("status") == "passed" and any(x["role"] == "user" for x in t["turns"])
        if ok or args.keep_failed:
            kept.append((plan[t["call_id"]], t))
        else:
            dropped[t.get("status") or "empty"] += 1
    kept.sort(key=lambda ct: ct[0]["date"])

    history_id = opaque("h_", args.run, "campaign")
    sessions, calls_gold, sid = [], [], {}
    for call, t in kept:
        sid[call["call_id"]] = opaque("s_", call["call_id"], args.run)
        sessions.append({"session_id": sid[call["call_id"]], "date": call["date"],
                         "meta": {"call_id": call["call_id"], "customer_id": call["customer_id"],
                                  "customer_name": call["customer_name"], "product_id": call["product_id"]},
                         "turns": [{"turn_id": opaque("t_", sid[call["call_id"]], str(i)), "role": turn["role"],
                                    "content": clean(turn["content"])} for i, turn in enumerate(t["turns"])]})
        calls_gold.append({"session_id": sid[call["call_id"]], **{k: call[k] for k in (
            "call_id", "customer_id", "customer_name", "product_id", "pattern", "outcome", "fault", "primary_reason",
            "secondary_reason", "primary_detail", "secondary_detail", "prior_call")}, "simulation_status": t.get("status")})

    questions, gold = [], []
    last_date = max(c["date"] for c, _ in kept) if kept else "2026/01/01 (Thu) 00:00"
    T = Q[lang]

    def add(qtype, question, answer, evidence, abstain=False, aliases=()):
        qid = opaque("q_", history_id, question)
        questions.append({"question_id": qid, "history_id": history_id, "question": question,
                          "question_date": last_date})
        gold.append({"question_id": qid, "question_type": qtype, "abstain": abstain, "answer": answer,
                     "aliases": list(aliases), "evidence": evidence if not abstain else []})

    label = lambda r: sc.label(r, lang)
    # Aggregates count customers, each by their latest call: a customer declined twice counts once, with the
    # latest reason; the evidence is every call of theirs that gave that reason.
    per_customer = collections.defaultdict(list)
    for c, _ in kept:
        per_customer[c["customer_id"]].append(c)
    declined = [cs[-1] for cs in per_customer.values() if cs[-1]["outcome"] == "no_sale"]
    by_reason = collections.defaultdict(list)
    for c in declined:
        by_reason[c["primary_reason"]] += [sid[x["call_id"]] for x in per_customer[c["customer_id"]]
                                           if x["primary_reason"] == c["primary_reason"]]
    n_customers = collections.Counter(c["primary_reason"] for c in declined)
    if by_reason:
        (top, n_top), *_ = n_customers.most_common()
        dist = ", ".join(f"{label(r)}: {n}" for r, n in n_customers.most_common())
        add("aggregate", T["agg"], f"{label(top)} ({n_top}/{len(declined)}; {dist})", by_reason[top],
            aliases=[label(top), sc.label(top, "en")])
        for r, ids in by_reason.items():
            add("aggregate-count", T["count"].format(label=label(r)), str(n_customers[r]), ids)
        for pid in sorted({c["product_id"] for c in declined}):
            per = collections.Counter(c["primary_reason"] for c in declined if c["product_id"] == pid)
            if sum(per.values()) >= args.min_per_product:
                r, n = per.most_common(1)[0]
                product = next(c["product"] for c in declined if c["product_id"] == pid)
                add("aggregate-product", T["agg_product"].format(product=product), label(r),
                    [sid[c["call_id"]] for c in declined if c["product_id"] == pid and c["primary_reason"] == r],
                    aliases=[sc.label(r, "en")])
    seller = collections.Counter(c["primary_reason"] for c in declined if c["fault"] == "seller")
    if seller:
        (top, n_top), *_ = seller.most_common()
        add("aggregate-seller", T["seller_top"], f"{label(top)} ({n_top}/{sum(seller.values())})", by_reason[top],
            aliases=[label(top), sc.label(top, "en")])
        n_cust = len(declined) - sum(seller.values())
        add("aggregate-fault", T["fault_split"],
            (f"sprzedawca: {sum(seller.values())}, klient: {n_cust}" if lang == "pl" else
             f"salesperson: {sum(seller.values())}, customer: {n_cust}"),
            [sid[c["call_id"]] for c in declined if c["fault"] == "seller"])

    def verb(name):
        return ("kupiła" if sc.is_female(name, lang) else "kupił") if lang == "pl" else ""

    single, updated, bought = [], [], []
    for cs in per_customer.values():
        last = cs[-1]
        if len(cs) == 1 and last["outcome"] == "no_sale":
            single.append(last)
        elif len(cs) > 1 and last["outcome"] == "no_sale" and last["prior_call"] != last["primary_reason"]:
            updated.append(cs)
        if all(c["outcome"] == "sale" for c in cs):
            bought.append(last)
    for fault, qtype in (("customer", "customer-reason/{pattern}"), ("seller", "seller-fault/{primary_reason}")):
        pool = [c for c in single if c["fault"] == fault]
        for c in rng.sample(pool, min(args.per_type, len(pool))):
            prefix = ("błąd sprzedawcy - " if lang == "pl" else "salesperson's mistake - ") if fault == "seller" else ""
            add(qtype.format(**c),
                T["why"].format(name=c["customer_name"], verb=verb(c["customer_name"]), product=c["product"]),
                f"{prefix}{label(c['primary_reason'])}: {c['primary_detail']}", [sid[c["call_id"]]],
                aliases=[label(c["primary_reason"])])
    for cs in rng.sample(updated, min(args.per_type, len(updated))):
        c = cs[-1]
        add("knowledge-update",
            T["why_now"].format(name=c["customer_name"], verb=verb(c["customer_name"]), product=c["product"]),
            f"{label(c['primary_reason'])}: {c['primary_detail']}", [sid[x["call_id"]] for x in cs],
            aliases=[label(c["primary_reason"])])
    # Abstention: customers who bought (price grumblers first, the near misses), and names never called.
    bought.sort(key=lambda c: c["pattern"] != "price_grumble_sale")
    for c in bought[: args.per_type // 2]:
        add("abstain/bought", T["why"].format(name=c["customer_name"], verb=verb(c["customer_name"]),
                                             product=c["product"]),
            "Klient kupił ofertę." if lang == "pl" else "The customer bought the offer.", [], abstain=True)
    called = {c["customer_name"] for c, _ in kept}
    ghosts = [n for n in (sc._full_name(f, l, lang) for f in sc.FIRST[lang] for l in sc.LAST[lang]) if n not in called]
    for name in rng.sample(ghosts, min(args.per_type - args.per_type // 2, len(ghosts))):
        add("abstain/unknown", T["why"].format(name=name, verb=verb(name), product=sc.PRODUCTS[0][lang]),
            "Brak rozmowy z tym klientem." if lang == "pl" else "There was no call with this customer.", [],
            abstain=True)

    ds = run / "dataset"
    write_jsonl(ds / "histories.jsonl", [{"history_id": history_id, "sessions": sessions}])
    write_jsonl(ds / "questions.jsonl", questions)
    write_jsonl(ds / "gold.jsonl", gold)
    write_jsonl(ds / "calls_gold.jsonl", calls_gold)
    types = collections.Counter(g["question_type"].split("/")[0] for g in gold)
    print(f"{len(sessions)} calls kept, dropped {dict(dropped) or 0}; {len(questions)} questions {dict(types)} -> {ds}")


# ---- audio --------------------------------------------------------------------------------------------------

def cmd_audio(args) -> None:
    """Voices each kept call turn by turn and joins the MP3 parts (MP3 frames concatenate into one playable file)."""

    from elevenlabs_api import ElevenLabs
    api = ElevenLabs()
    run = Path(args.run)
    histories = read_jsonl(run / "dataset" / "histories.jsonl")
    sessions = [s for h in histories for s in h["sessions"]][: args.limit]
    (run / "audio").mkdir(exist_ok=True)
    for s in sessions:
        path = run / "audio" / f"{s['meta']['call_id']}.mp3"
        if path.exists():
            continue
        parts = [api.tts(args.customer_voice if t["role"] == "user" else args.seller_voice, t["content"], args.tts_model)
                 for t in s["turns"]]
        path.write_bytes(b"".join(parts))
        print(path)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("plan", help="plan calls with planted outcomes and reasons")
    a.add_argument("--run", required=True)
    a.add_argument("--calls", type=int, default=200)
    a.add_argument("--lang", choices=sorted(sc.LANGS), default="pl")
    a.add_argument("--seed", type=int, default=0)
    a.add_argument("--start", default="2026-09-01", help="first possible call date (YYYY-MM-DD)")
    a.add_argument("--days", type=int, default=60, help="length of the campaign in days")
    a.add_argument("--force", action="store_true", help="replan a run that has transcripts (deletes them)")

    a = sub.add_parser("simulate", help="run the planned calls as ElevenLabs simulations")
    a.add_argument("--run", required=True)
    a.add_argument("--mock", action="store_true", help="scripted transcripts, no API calls (offline testing)")
    a.add_argument("--limit", type=int, default=0, help="simulate at most this many calls now (0: all)")
    a.add_argument("--batch", type=int, default=10, help="tests per run-tests invocation")
    a.add_argument("--max-turns", type=int, default=16)
    a.add_argument("--agent-id", help="reuse an existing seller agent instead of creating one")
    a.add_argument("--agent-llm", default="gemini-2.5-flash", help="LLM of the seller agent")
    a.add_argument("--user-llm", default=None, help="LLM of the simulated customer (default: ElevenLabs')")
    a.add_argument("--timeout", type=float, default=900, help="seconds to wait for one batch")
    a.add_argument("--keep-tests", action="store_true", help="leave the created tests in the ElevenLabs workspace")

    a = sub.add_parser("build", help="write the dataset in the challenge format")
    a.add_argument("--run", required=True)
    a.add_argument("--keep-failed", action="store_true", help="keep simulations that missed their success conditions")
    a.add_argument("--per-type", type=int, default=10, help="questions per customer-level question type")
    a.add_argument("--min-per-product", type=int, default=5)
    a.add_argument("--seed", type=int, default=0)

    a = sub.add_parser("audio", help="voice kept calls with ElevenLabs text to speech")
    a.add_argument("--run", required=True)
    a.add_argument("--seller-voice", required=True)
    a.add_argument("--customer-voice", required=True)
    a.add_argument("--tts-model", default="eleven_multilingual_v2")
    a.add_argument("--limit", type=int, default=10)

    args = p.parse_args()
    {"plan": cmd_plan, "simulate": cmd_simulate, "build": cmd_build, "audio": cmd_audio}[args.cmd](args)


if __name__ == "__main__":
    main()
