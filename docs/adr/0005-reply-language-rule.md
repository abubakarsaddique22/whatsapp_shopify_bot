# ADR 0005: Reply language rule (supersedes the language part of ADR 0003)
Date: 2026-10-18  Status: accepted (owner decision; client confirmation A4 still pending)

Context: In the live 10-message chat on 2026-10-18, a Roman Urdu question got an English reply,
because the language only switched after 2 messages. The language mix of real customers also
showed that 'mixed' and 'roman_ur' need the same reply.
Decision:
- Each clear message sets the conversation language at once (`SWITCH_AFTER = 1` in
  `language/policy.py`). An UNKNOWN message (pic, ok, an emoji, Hindi) keeps the previous one.
- English conversation -> English reply. Everything else (Roman Urdu, mixed, Urdu script,
  Punjabi, Devanagari, unknown, a doubtful voice transcript) -> Roman Urdu. `reply_language` in
  `language/policy.py` is the only place this rule lives. The setting URDU_SCRIPT_REPLIES can
  turn on Urdu-script replies for Urdu.
- An explicit request ('English mein bataein', 'Urdu mein likhein') always wins. A message that
  only asks for a language gets the `language_switched` template and does not count as unclear
  (added 2026-10-20).
Alternatives: switch after 2 messages (ADR 0003: one stray message kept the wrong language);
reply in Urdu script (most customers type Roman Urdu); a separate 'mixed' reply style (the
customers saw no difference, so it only made testing harder).
Consequences: The router's roman_ur vs mixed label no longer changes what the customer gets, so
the language gate (95%) measures the router, not the reply. The client must still confirm A4
(Phase 0).
