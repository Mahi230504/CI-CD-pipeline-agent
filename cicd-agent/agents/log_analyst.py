"""
Log analyst agent — diagnoses why a CI job failed.

Flow:
1. Calls log_fetcher.slice_log() to get the relevant log window
2. Sends sliced log + LOG_ANALYST_PROMPT to Gemini via gemini_client
3. Parses response through response_parser.parse_diagnosis()
4. Returns Diagnosis dataclass

Confidence gate: if diagnosis.confidence < 0.6, returns the Diagnosis
with a flag indicating the orchestrator should escalate to human rather
than attempting a patch. A low-confidence diagnosis patched blindly makes
things worse.

Uses PRIMARY_MODEL (gemini-2.5-flash).
"""
