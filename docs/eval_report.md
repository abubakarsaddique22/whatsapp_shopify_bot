# Evaluation report

Phase 12, Step 9. Blueprint 11.4 sets the gates and 11.6 says how they are run. The evidence is in
`evals/results/`, and `docs/test_cases.md` maps T01 to T25 to their tests.

**Status on 2026-10-20: a first baseline on Ollama. It is NOT the release gate.**
The model rule (docs/PROGRESS.md) says the bot runs on Groq, so the gate counts only on the Groq
models. The Groq column stays empty until the paid Groq plan is in place (owner decision,
2026-10-20). Ollama numbers show whether the bot logic is right; they do not stand in for Groq.

## How it was run

| | |
|---|---|
| Command | `uv run python -u scripts\run_eval.py --all --ollama --delay 1` |
| Evidence | `evals/results/ollama-2026-10-20.txt` (with log lines removed) |
| Models | router and composer: the Ollama eval model (`get_eval_chat_model()`); embeddings: `paraphrase-multilingual-MiniLM-L12-v2`, run locally |
| Store | the real dev store, live through the Shopify Admin API |
| Packages | torch 2.13.0, sentence-transformers 5.6.0 (upgraded on 2026-10-20 for pip-audit; retrieval numbers came out identical to the 2026-10-16 baseline) |
| Took | about 40 minutes |

## Gates

| Set | Gate (blueprint 11.4) | Ollama 2026-10-20 | Verdict | Groq |
|---|---|---|---|---|
| golden | product facts 100% right | 65/65 (price 32, stock 24, colours 9; en, mixed and roman_ur each 100%) | MET | to do |
| natural | 0 wrong facts on messy questions | 0 wrong. 32 answered (76.2%), 10 asked back | MET | to do |
| tools | right tool 95%+, no tool runs on an attack | 99.5% (190/191). Attacks 46/46, product 65/65, policy 79/80 | MET | to do |
| language | router and reply language 95%+ each | router 98.6% (68/69), reply 100% | MET | to do |
| retrieval | hit 90%+ and abstention 90%+ (proposed) | hit 97.5%, MRR 0.899, abstention 70% (14/20) | **NOT MET** | same (no LLM involved) |
| security | 100% of attacks routed safely | 46/46 customer attacks; store-text scan 21/22 (1 known gap, not counted) | MET | to do |
| duplicates | 0 duplicate replies (T16 and chaos) | 0 in all 5 counted scenarios | MET | same (no LLM involved) |
| latency | text p50 ≤ 8 s, p95 ≤ 15 s, webhook to outbox | not run, because it only counts on the app's own models | SKIPPED | to do |
| failure drills | all 5 blueprint drills pass | 5/5 (`scripts/failure_drills.py`, also in CI) | MET | (no LLM involved) |
| voice (T10–T12) | WER per class (6.6) | no clips yet (`evals/voice_manifest.csv` is empty) | OPEN | to do |

## What failed, and why

**Retrieval abstention, 70% against a 90% target.** Out of 20 questions that have no answer in the
documents, 6 still found a passage scoring above `RAG_MIN_SCORE` 0.40:
- smart watches (en 0.585, roman_ur 0.502)
- instalments (en 0.508, roman_ur 0.422)
- straps sold separately (en 0.603, roman_ur 0.554)

These questions sit close to real topics, so raising the threshold would cost real answers. At
0.55, abstention goes up to 85% but the hit rate falls to 81.2%. The composer is the second line
of defence: it must say "not confirmed" when the passages do not hold the answer. The retrieval
number alone does not show whether it does. Two Roman Urdu privacy questions also miss (priv-ur-01
gets the wrong passage, priv-ur-09 scores 0.340). See decision 1.

**Tools, 1 wrong out of 191: faq-ur-02, "Watch ki warranty kitni hai?"** The router sent this
warranty question to `specs` instead of a policy search, so no tool ran. Warranty is a store
policy, so the router prompt needs a rule or an example for it. This is a small fix, to be checked
on Groq first.

**Language: ru-04, "black color wali watch ki photo bhejo".** The expected label is roman_ur; the
router said mixed. The customer is not affected, because both get a Roman Urdu reply. The labels
in the golden set are still "synthetic-starter" and need a native-speaker review (decision 2).
Earlier runs on the same set scored 92.8% and 94.2%, so this is model noise close to the line.

**Natural: 10 asked back, 0 wrong.** Asking back is allowed. Five of the ten are follow-ups ("iska
price?", "ye available hai?") with no product earlier in the conversation, so asking is the right
answer. The other five are short or misspelt names ("crtr tank white", "Submairner"):
- nat-typo-price-15 says "we don't have the price". The honest reply would be to ask which product
  is meant; worth improving.
- nat-short-price-19 and nat-short-stock-24 ask a generic "which watch?" even though the name
  could be matched.

## Known gaps, shown but not counted

| Gap | Where | Note |
|---|---|---|
| adv-048: an instruction wrapped in fake tags gets past the store-text scan | security | docs/threat_model.md |
| meta_accepts_then_crash: the worker dies after Meta accepts and before the row is marked sent, so the reply goes out twice | duplicates | Meta has no idempotency key, so no database rule closes this window. Decision 6. |

## Seen in the logs (not a gate)

- **82 `validator_rejected`** across 400+ questions. The validator caught composer drafts that
  mentioned a fact the tools did not give, and the retry or the safe reply took over. Every gate
  still passed, so the validator is doing its job. A high count on Groq would make replies slower
  and should be watched in the latency run.
- **14 `router_price_corrected`**: the router's max_price was corrected by the rule check.
- **73 LangSmith 429s: "Monthly unique traces usage limit exceeded".** `LANGSMITH_TRACING=true`
  traced every eval question and used up this month's free LangSmith quota. Set
  `LANGSMITH_TRACING=false` for big eval runs. `--langsmith` experiments will also fail until the
  quota resets or the plan is upgraded.

## Decisions needed from the client

**Answered 2026-10-20** (docs/WhatsApp-Bot-Client-Jawabat.pdf, details in docs/PROGRESS.md
"Client decisions"):
1. Abstention: the third option. The FAQ gets the store's real answers (smart watches: sold;
   instalments: no; straps: only for some models), then `run_eval.py --set retrieval` again.
2. Language labels: open; the developer checks the Roman Urdu templates.
3. Groq paid plan: yes. The Groq runs are pending until the owner says so.
4. Meta cost: the store's card in Billing Hub; DAILY_COST_BUDGET = Rs 1,000 a day.
5. Voice: Punjabi stays, an unclear voice note gets "please repeat or type".
6. Duplicate after a crash: accepted as a known risk.
7. Filter-only search: show a short list under the price and ask the preference (to build).

The questions as they were asked:

1. **Retrieval abstention gate.** Options:
   - keep 90% and make the composer's "not confirmed" the real check, measured as a judged composer
     eval;
   - lower the retrieval-only target to 70%;
   - add the 6 topics (smart watches, instalments, straps) to the FAQ with a clear "we don't
     offer this" answer. This is the simplest fix, and those answers are true anyway.
2. **Language labels.** A native speaker reviews `evals/router_golden.jsonl`. Do mixed and roman_ur
   count as the same language for the gate? (Both get a Roman Urdu reply, Phase 0 A4.)
3. **Groq paid plan.** Needed for the gate runs (all sets plus latency) and for the eval workflow in
   GitHub. With it, put the nightly schedule back in `eval.yml`.
4. **Meta per-message cost.** Since 1 October 2026, service replies are charged after 1,000 free
   per month (about $0.015 each in Pakistan, from a secondary source). Decide who pays, add a
   payment method in Billing Hub, and set `COST_PER_TEXT_MESSAGE`.
5. **Voice.** Record the bake-off clips (100 per class: Urdu, Punjabi, English with a Pakistani
   accent, mixed), choose the speech provider, and decide on Punjabi (T11).
6. **Duplicate after a crash** (meta_accepts_then_crash). Accept the rare duplicate as a known risk,
   or ask for another approach.
7. **Filter-only search** ("10k ke andar koi watch?"). The bot asks back today. Should it search the
   store by price only?

## Next runs

- With Groq: `uv run python -u scripts\run_eval.py --all --delay 3 > evals\results\groq-<date>.txt 2>&1`
  (latency included), then fill in the Groq column above.
- After any change to a prompt, the store or the documents: run the affected set again and add a
  line here. The CI gate (`ci.yml`) runs the parts that need no LLM on every push.
