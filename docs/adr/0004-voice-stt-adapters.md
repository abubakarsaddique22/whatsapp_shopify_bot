# ADR 0004: Voice STT adapters
Date: 2026-10-15  Status: accepted

Context: Voice notes need transcription; budget is limited.
Decision: Only the Groq adapter (Whisper large-v3 or turbo, chosen by the bake-off) is written.
Other providers (OpenAI etc.) are NOT written until a bake-off shows a need; adding one needs a
new ADR. Audio is deleted after each message; only the transcript is stored. Low-confidence
transcripts go through the confidence gate and get a clarification, not a guess.
Consequences: Small code surface. Thresholds and Punjabi handling are set in Phase 6 Step 6.
