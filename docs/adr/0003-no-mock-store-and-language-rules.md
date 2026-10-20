# ADR 0003: No mock store; language rules
Date: 2026-10-15  Status: accepted; the language part is superseded by ADR 0005 (2026-10-18)

Context: Tests must reflect real data; replies must feel natural for Pakistani customers.
Decision:
- No fake Shopify gateway or fake catalog data. Tests run against the real dev store
  (`RUN_LIVE=1`) or the Postgres mirror. Only failure cases (e.g. Shopify down) are simulated.
- Reply language switches only after 2 consecutive messages in another language.
- Urdu-script text and Urdu/Punjabi voice get Roman Urdu replies (client confirmation A4 pending).
Consequences: Slower, honest tests; language policy documented for the client questions (Phase 0).
