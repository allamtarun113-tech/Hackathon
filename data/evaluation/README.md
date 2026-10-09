# Workflow evaluation dataset

`workflow_cases.jsonl`: 365 test cases that evaluate the **LangGraph workflow itself** (routing,
guardrails, evidence validation, safety, resilience and persistence), separately from answer quality.

| Dataset | Evaluates | Command |
|---------|-----------|---------|
| `production/guardrail_tests.json` (200) | Guardrail functions in isolation | `pagermind-eval guardrails` |
| `evaluation/workflow_cases.jsonl` (365) | The end-to-end workflow: API → graph → guardrails → Postgres | `pagermind-eval workflow run` |
| `demo_scenarios.json` (100 held-out) | Answer quality with vs without memory | `pagermind-eval run` |

## How a case works

Each line is one conversation of one or more turns. Each turn has:

- **Inputs:** a message and/or attachments. Attachment scenarios: owned, another conversation's file, another
  engineer's file, tampered after upload, or a bad file type.
- **`simulation`:** what the external services do in offline mode. This covers what Groq extracts, what Hindsight
  reflect answers and retrieves, and which dependency is down.
- **`expect`:** required behaviour with scripted services, for example the outcome, match strength, exact matched
  incidents, guardrail reason codes recorded in `guardrail_results`, strings that must never appear, approval
  flags, escalation team, deployments that must or must not be surfaced, and upload status.
- **`expect_live`:** model-agnostic expectations for runs against real Groq and Hindsight. Cases without it are
  scripted-only (for example, "the model cites an invented incident").

Every turn is also checked against **invariants**:

- the stream terminates
- every cited incident exists and was retrieved in this request
- there are no execution claims
- every destructive command requires approval
- no recognisable secret appears in the stream or in stored messages

## Categories

| Category | Cases | What it proves |
|----------|------:|----------------|
| routing.answered | 30 | Valid reports are answered with a strong, cited match and open a live incident |
| routing.off_topic | 30 | Non-incident requests are refused and recorded |
| routing.clarification | 20 | Missing context gets one question; a second vague turn proceeds |
| input.secrets | 36 | Secrets in messages or logs never reach any output, even if the model echoes them |
| input.pii | 16 | Customer contact details are redacted everywhere |
| input.injection | 25 | Instructions inside logs, stack traces or the message are neutralised as data |
| attachments.* | 40 | Ownership, checksums and file-type validation for uploads |
| evidence.* | 65 | Invented or unretrieved citations are removed; unearned strong matches are downgraded; novel problems abstain |
| safety.* | 46 | Destructive commands are gated and escalated; execution claims are stripped; contradictions are removed |
| resilience.* | 45 | Outages and malformed or over-long model output degrade gracefully and with disclosure |
| context.recent_changes | 12 | Deployments inside the 24 h window are surfaced; older ones are not |

## Commands

```bash
pagermind-eval workflow generate            # rebuild deterministically (seeded) from the catalog + incident history
pagermind-eval workflow generate --check    # CI: fail if this file is stale
WORKFLOW_EVAL_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
  pagermind-eval workflow run               # offline; creates and drops a scratch database
pagermind-eval workflow run --category safety --verbose
pagermind-eval workflow run --mode live --bank-id pagermind-eval-xxxx   # real models, live expectations only
```

Reports are written to `backend/evals/reports/workflow-<mode>-<timestamp>.{md,json}`. They contain:

- the pass rate overall and per category
- accuracy for each kind of check
- guardrail detection recall
- a routing confusion matrix
- leak and invariant counts
- p50/p95 latency
- every failure with its reason

## Rules for this dataset

- It is evaluation data. Case IDs (`WF-0001`…) are blocked by the memory guard, so they can never be retained into
  a Hindsight bank.
- Do not hand-edit the JSONL. Change `backend/evals/workflow/generate.py` and regenerate. The generator asserts
  each case's premise against the guardrail primitives: an off-topic prompt really is off-topic, and a planted
  secret really is detectable.
- Live mode must use a disposable evaluation bank. The CLI refuses the production bank.
