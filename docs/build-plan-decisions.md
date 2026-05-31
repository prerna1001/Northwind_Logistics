# Build Plan Decisions

## Locked Build Step 1: Data And Storage Foundation

Status: approved

Decision:

- Use three storage layers from the start:
  - S3 for raw artifacts
  - PostgreSQL for application state and persistence
  - pgvector inside PostgreSQL for policy chunk embeddings and semantic retrieval
- Use S3 folder structure for both sample and manual cases:
  - `cases/sample/<case_id>/employee_info.json`
  - `cases/sample/<case_id>/receipts/...`
  - `cases/manual/<employee_id>/<submission_id>/employee_info.json`
  - `cases/manual/<employee_id>/<submission_id>/receipts/...`
  - `policies/raw/<bundle_name>.pdf`
- Use PostgreSQL as the system of record for:
  - employees
  - submissions
  - receipts
  - receipt extractions
  - policy documents
  - policy chunks
  - policy rules
  - verdicts
  - review overrides
- Enable `pgvector` in the same PostgreSQL database.
- Store embeddings only for `policy_chunks` in the MVP.
- Keep explicit source markers where relevant:
  - `sample`
  - `manual`
- Keep policy versioning fields from the start:
  - version
  - effective date
  - active/inactive status

Why:

- Raw files, application state, and retrieval vectors have different responsibilities and should not be conflated.
- The policy corpus is small enough that `pgvector` is a strong fit and avoids introducing a separate vector database.
- Early source markers and policy versioning make later ingestion, replay, and evaluation cleaner.

Tradeoffs:

- We chose S3 + PostgreSQL + pgvector instead of collapsing everything into one storage system.
  - Benefit: clean separation of raw artifacts, relational state, and retrieval vectors.
  - Cost: more infrastructure coordination.
- We chose `pgvector` instead of a separate vector database.
  - Benefit: simpler stack, easier metadata filtering, easier joins, and better fit for a small policy corpus.
  - Cost: less specialized than a dedicated vector store if the corpus becomes much larger later.
- We chose separate processing/result tables instead of stuffing all derived state into base receipt rows.
  - Benefit: cleaner processing history and easier extensibility.
  - Cost: more schema complexity.

Locked shape:

`S3 for raw case and policy artifacts + PostgreSQL for application state + pgvector inside PostgreSQL for policy chunk embeddings and semantic retrieval`

## Locked Build Step 2: Policy Ingestion Pipeline

Status: approved

Decision:

- Treat the 8 provided files in `policies/` as bundled policy PDFs, not direct retrieval units.
- Upload the raw bundled policy PDFs to S3 first.
- Parse each bundled PDF into individual policy documents.
- Create one versioned `policy_document` record per actual policy inside each bundle.
- For each individual policy document:
  - chunk by numbered section and subsection structure
  - preserve section identity
  - split oversized sections carefully only when needed
- Tag each chunk during ingestion with topic tags relevant to later routing and retrieval.
- Generate embeddings for active policy chunks and store them in `pgvector`.
- Keep active/inactive policy versioning so re-ingestion does not overwrite blindly.

Why:

- The bundled PDFs are packaging artifacts, while retrieval and policy logic should operate on actual policy documents.
- Splitting into policy documents before chunking gives cleaner retrieval boundaries and stronger citations.
- Topic-tagging and embeddings make the later routed retrieval system much more precise.
- Version-aware ingestion keeps policy updates safe and traceable.

Tradeoffs:

- We chose bundle parsing before chunking.
  - Benefit: retrieval units match real policy documents rather than arbitrary source bundles.
  - Cost: bundle-splitting logic adds ingestion complexity.
- We chose topic-tagging during ingestion.
  - Benefit: stronger routing-aware retrieval later.
  - Cost: tagging quality becomes another ingestion dependency.
- We chose to embed only policy chunks in the MVP.
  - Benefit: simpler retrieval scope and lower indexing complexity.
  - Cost: less flexibility for future retrieval over other entity types.
- We chose version-aware re-ingestion instead of overwrite-in-place updates.
  - Benefit: safer policy evolution and auditability.
  - Cost: more schema and ingestion workflow complexity.

Locked shape:

`Upload raw bundled policy PDFs to S3 -> split each bundle into individual versioned policy documents -> chunk them by section structure -> tag chunks -> embed chunks into pgvector -> activate latest policy versions`

## Locked Build Step 3: Sample-Case Ingestion Pipeline

Status: approved

Decision:

- Treat each provided folder in `submissions/` as one complete sample submission.
- For each sample folder:
  - upload `employee_info.json` to S3
  - upload all receipt files to S3
  - preserve the folder structure in S3
- Create or upsert the employee record in PostgreSQL with `source = sample`.
- Create one sample submission row per folder with:
  - `source = sample`
  - stable `sample_case_id`
  - trip context from `employee_info.json`
- Create one receipt row per sample receipt and attach it to the sample submission.
- Make the whole seed step idempotent:
  - reruns should not duplicate employees
  - reruns should not duplicate submissions
  - reruns should not duplicate receipts

Why:

- The provided folders are not just employee seeds; they are complete review-ready sample cases.
- The reviewer should be able to open the app and immediately inspect these sample cases without uploading anything.
- Preserving the local folder shape in S3 keeps the raw case structure easy to debug and replay.

Tradeoffs:

- We chose to ingest the provided folders as full sample submissions instead of only seeding employees.
  - Benefit: matches the intended reviewer workflow exactly.
  - Cost: more ingestion logic up front.
- We chose to mirror the local folder layout in S3.
  - Benefit: easier reasoning, debugging, and replayability.
  - Cost: not the most abstract object-store layout possible.
- We chose idempotent seeding.
  - Benefit: safe repeated development runs.
  - Cost: requires stable case identity logic and deduplication checks.

Locked shape:

`For each provided sample folder: upload raw JSON and receipts to S3 -> create or upsert the employee -> create one sample submission -> attach receipt rows -> mark everything as source=sample -> keep it idempotent`

## Locked Build Step 4: Receipt Extraction Pipeline

Status: approved

Decision:

- Build a shared extraction pipeline for both sample and manual receipts.
- Branch first by file type:
  - direct parsing for `.txt`
  - PaddleOCR pipeline for PDFs
  - PaddleOCR pipeline for images
- Use PaddleOCR as the default document-vision/OCR layer for PDF and image receipts.
- After OCR, run a deterministic normalization pass to extract common receipt fields.
- Keep raw OCR text separate from normalized extracted JSON.
- Store extraction records with:
  - OCR text
  - normalized fields
  - extraction status
  - extraction confidence
  - parser version
  - retry or incompleteness markers
- Make extraction rerunnable so later OCR/parser improvements can reprocess the same receipts safely.

Why:

- OCR output alone is not enough; the rest of the system needs normalized receipt JSON.
- Separating raw OCR from normalized extraction makes debugging and future improvement easier.
- Rerunnable extraction is important because OCR and parsing quality often improve during development.

Tradeoffs:

- We chose PaddleOCR plus normalization instead of sending receipts directly to an LLM.
  - Benefit: cheaper, more controllable, and more auditable.
  - Cost: more engineering effort in parsing and normalization.
- We chose to store raw OCR separately from normalized fields.
  - Benefit: replayability and easier future improvement.
  - Cost: more storage and more schema complexity.
- We chose extraction rerun support.
  - Benefit: easier iteration as OCR/parser quality improves.
  - Cost: requires versioning discipline and processing bookkeeping.

Locked shape:

`Detect file type -> run PaddleOCR or direct text parsing -> normalize into receipt JSON -> store raw OCR + normalized extraction + extraction confidence + parser version -> keep rerunnable`

## Locked Build Step 5: Receipt Categorization And Routing

Status: approved

Decision:

- Use normalized receipt data plus employee and trip context to determine:
  - one primary receipt category
  - secondary routing signals
  - relevant policy families
- Use heuristics first for primary categorization.
- Use Llama only as a fallback when categorization is ambiguous.
- Do not route supporting policies through brittle hardcoded branching.
- Instead:
  - use the primary category as the seed policy family
  - score additional supporting policy families using:
    - embeddings
    - keyword overlap
    - tag overlap
    - factual signals such as payment method, trip type, and anomaly signals
- Include only the high-scoring supporting policy families for downstream retrieval and checks.

Why:

- The system should avoid both blind retrieval over all policies and brittle hand-built routing trees.
- Primary-category routing gives precision, while semantic expansion catches adjacent relevant policies like alcohol, corporate card, or conduct when truly relevant.
- A scoring approach scales better as policies evolve.

Tradeoffs:

- We chose heuristics-first categorization instead of default LLM classification.
  - Benefit: cheaper, faster, and more controllable.
  - Cost: requires good vendor and text-pattern coverage.
- We chose secondary routing signals in addition to one category.
  - Benefit: more precise policy routing.
  - Cost: more intermediate logic and data fields.
- We chose supporting policy-family scoring instead of brittle hardcoded branches.
  - Benefit: more scalable and less fragile as policies expand.
  - Cost: requires embeddings, tags, and a ranking layer.
- We chose Llama fallback only for ambiguous categorization.
  - Benefit: preserves a low-cost path for common cases.
  - Cost: requires an ambiguity threshold.

Locked shape:

`Take normalized receipt data + context -> assign one primary category -> compute secondary routing signals -> use heuristics first and Llama only for ambiguous cases -> seed base policy families -> score supporting policy families using embeddings/keywords/tags/factual signals -> output routed policy families`

## Locked Build Step 6: Deterministic Checks Layer

Status: approved

Decision:

- Build a generic rule engine that runs on normalized facts, not raw PDFs.
- Inputs:
  - normalized receipt extraction JSON
  - employee JSON
  - trip context
  - receipt category
  - routing signals
  - scoped policy families
  - structured policy rules from PostgreSQL
- Do not scatter policy-specific logic across code.
- Keep policy-specific thresholds and conditions in structured `policy_rules`.
- Start with a limited set of generic rule types:
  - amount cap
  - prohibited item
  - required itemization
  - required receipt field
  - included meal conflict
  - lodging cap
  - approval required
  - amount mismatch
- Emit structured deterministic findings, not final verdicts.

Why:

- Explicit rule checks improve consistency and reduce hallucination risk before LLM adjudication.
- A generic engine plus structured rules is easier to maintain than scattered hardcoded policy logic.
- Deterministic findings are useful evidence for retrieval, confidence, and final adjudication.

Tradeoffs:

- We chose a data-driven rule engine over scattered hardcoded checks.
  - Benefit: cleaner maintenance and better policy evolution support.
  - Cost: more up-front rule modeling and schema design.
- We chose a small generic rule-type set.
  - Benefit: keeps the engine maintainable.
  - Cost: not every policy nuance can be captured deterministically.
- We chose deterministic findings as intermediate outputs instead of final verdicts.
  - Benefit: better traceability and cleaner downstream reasoning.
  - Cost: adds another explicit pipeline layer.

Locked shape:

`Use normalized receipt facts + employee/trip context + routed policy scope -> load relevant structured policy rules -> evaluate them through a generic rule engine -> emit structured deterministic findings for retrieval, confidence, and adjudication`

## Locked Build Step 7: Retrieval Layer

Status: approved

Decision:

- Build retrieval only after categorization, routing, and deterministic findings already exist.
- Use normalized receipt facts, employee/trip context, and deterministic findings to construct a compact retrieval summary.
- Do not retrieve from raw OCR text dumps directly.
- Restrict search to active routed policy families before retrieval starts.
- Use hybrid retrieval inside that filtered subset:
  - pgvector similarity
  - keyword overlap
  - tag overlap
  - section specificity
- Return a small chunk-level evidence package, not a large noisy context set.
- Emit retrieval-quality and retrieval-confidence signals for downstream confidence handling.
- Do not let Step 7 make the final reviewer verdict.

Why:

- Retrieval should gather evidence, not decide the case.
- Routed retrieval sharply reduces policy noise and improves citation quality.
- Query representations built from normalized facts are more precise than raw OCR text.
- Retrieval-quality signals are important inputs to later confidence and human-review routing.

Tradeoffs:

- We chose routed hybrid retrieval over blind semantic search.
  - Benefit: much better precision and less irrelevant policy noise.
  - Cost: more orchestration and metadata dependency.
- We chose retrieval summaries over raw OCR dump queries.
  - Benefit: cleaner and more policy-relevant search behavior.
  - Cost: requires a query-construction step.
- We chose a small evidence package.
  - Benefit: less prompt noise and stronger downstream adjudication.
  - Cost: weak ranking could omit useful evidence if retrieval quality is poor.
- We chose to emit retrieval-confidence signals without making the final verdict here.
  - Benefit: keeps layer responsibilities clean.
  - Cost: requires downstream logic to consume and act on low-confidence retrieval properly.

Locked shape:

`Use normalized receipt facts + context + deterministic findings to build a retrieval summary -> restrict search to active routed policy families -> run hybrid retrieval over policy chunks using pgvector plus keyword/tag signals -> return a small chunk-level evidence package plus retrieval-confidence signals for adjudication and review routing`

## Locked Build Step 8: Decision And Adjudication Layer

Status: approved

Decision:

- Use the adjudication layer only after extraction, categorization, routing, deterministic checks, and retrieval are complete.
- Use Llama as the current MVP adjudication model.
- Structure the adjudication prompt so the model:
  - only uses provided receipt facts and policy evidence
  - does not invent policies or citations
  - respects deterministic findings
  - respects weak retrieval and low-confidence evidence
  - returns strict structured JSON
- The adjudication output should include:
  - verdict
  - reasoning summary
  - policy findings
  - quoted citations
  - confidence explanation
  - human-review recommendation
  - recommended action
- This layer consumes evidence; it does not perform retrieval itself.
- Persist the adjudication result as the first reviewer-ready system answer.

Why:

- The final reviewer-facing answer needs structured reasoning, grounded citations, and clear uncertainty handling.
- Constrained JSON output is easier to persist, render, and audit than free-form model text.
- Adjudication should synthesize the evidence, not rebuild earlier pipeline steps.

Tradeoffs:

- We chose constrained structured adjudication over free-form LLM responses.
  - Benefit: cleaner persistence, less hallucination risk, and easier UI rendering.
  - Cost: more prompt and schema discipline.
- We chose to require citation-backed reasoning.
  - Benefit: stronger trust and auditability.
  - Cost: weak retrieval leads to more surfaced uncertainty.
- We chose to make the model consume deterministic findings instead of replacing them.
  - Benefit: better consistency and stronger reasoning scaffolding.
  - Cost: more orchestration between layers.

Locked shape:

`Send structured receipt facts + employee/trip context + deterministic findings + retrieved policy evidence into a constrained Llama adjudication prompt -> require strict JSON output with verdict, reasoning, citations, and human-review recommendation -> persist the adjudication result`

## Locked Build Step 9: Reviewer UI

Status: approved

Decision:

- Build the reviewer UI around two primary tabs:
  - Sample Cases
  - New Submission
- Sample Cases tab:
  - shows the 5 seeded sample submissions
  - allows direct review with no upload flow
  - reviewer can click a sample case and then explicitly click `Do Analysis`
  - analysis results are then shown for that selected sample case
- New Submission tab:
  - collects new employee and trip context
  - is the only place where manual receipt upload happens
- Build submission detail views that show:
  - employee summary
  - trip context
  - receipt list
  - receipt-level review cards
- Each receipt review card should show:
  - category
  - vendor/date/total
  - verdict
  - reasoning summary
  - exact policy citation
  - confidence
  - human-review indicator when needed
  - override action
- Visually distinguish verdict states clearly.
- Keep policy Q&A separate from the main receipt review workflow.

Why:

- The reviewer experience should mirror the two real workflows:
  - review provided sample cases
  - create and review new manual submissions
- An explicit `Do Analysis` action on sample cases makes the review flow feel intentional instead of automatic or hidden.
- Receipt-level transparency is critical because the brief expects reviewers to understand, trust, and override individual decisions.
- Clear visual verdict distinction makes flagged and non-compliant items easy to act on quickly.

Tradeoffs:

- We chose two primary tabs instead of one blended workflow.
  - Benefit: clearer reviewer mental model.
  - Cost: slightly more UI structure.
- We chose an explicit `Do Analysis` action for sample cases instead of silently auto-running analysis on open.
  - Benefit: clearer reviewer control and a more deliberate demo flow.
  - Cost: one extra click in the sample-case workflow.
- We chose receipt-level review cards instead of only submission-level summaries.
  - Benefit: better actionability and transparency.
  - Cost: more UI detail to build.
- We chose a separate override flow with audit visibility.
  - Benefit: aligns with the brief and improves trust.
  - Cost: more persistence and UI state handling.

Locked shape:

`Build a reviewer UI with two primary tabs (Sample Cases and New Submission), let reviewers open a sample case and click Do Analysis, use submission detail views with receipt-level verdict cards, clear verdict styling, citations, confidence, and persistent override actions`

## Locked Build Step 10: Override, Audit Trail, And History

Status: approved

Decision:

- Never destroy the original system verdict when a reviewer overrides a decision.
- Store reviewer overrides separately from system verdicts.
- Each override should persist:
  - original system verdict
  - overridden verdict
  - reviewer comment
  - timestamp
  - reviewer identity if available
- Maintain a durable audit trail that preserves both system actions and reviewer actions.
- History must survive server restarts.
- Each receipt should expose:
  - system verdict
  - override verdict if any
  - effective verdict
- Build a history view that allows reopening past submissions and inspecting:
  - employee and trip context
  - receipt list
  - original reasoning and citations
  - overrides and comments
- Add explicit history filters for:
  - employee
  - date
  - status

Why:

- The brief explicitly requires persistent overrides and auditability.
- Review software must preserve both what the system said and what the human changed.
- Non-destructive history is essential for trust and traceability.
- The brief explicitly calls for browsing history by employee, date, and status, so those filters should be part of the first real history design.

Tradeoffs:

- We chose non-destructive overrides instead of overwriting system verdicts.
  - Benefit: proper auditability and trust.
  - Cost: more schema and UI complexity.
- We chose persistent history instead of lightweight session-only state.
  - Benefit: aligns with the brief and real reviewer workflow.
  - Cost: more backend persistence work.
- We chose to expose both system and effective verdicts.
  - Benefit: clearer review history.
  - Cost: more detail in the UI.
- We chose to include employee/date/status filters in the history view.
  - Benefit: directly matches the case-study browsing requirement.
  - Cost: adds some query and UI-state complexity.

Locked shape:

`Store original system verdicts separately from reviewer overrides -> persist override comments and timestamps -> compute an effective verdict without losing original reasoning -> provide a durable submission history view with employee/date/status filters that survives restarts`

## Locked Build Step 11: Confidence And Review Routing

Status: approved

Decision:

- Use confidence as an operational safety layer, not just a UI display.
- Inputs to this layer:
  - extraction confidence
  - retrieval confidence
  - adjudication confidence
  - deterministic finding strength
  - adjudication output
- Apply conservative routing behavior:
  - preserve strong explicit decisions when evidence is sufficient
  - soften or downgrade weak-evidence cases to `needs_human_review`
- Allow confidence to influence effective verdict severity.
- Emit:
  - overall confidence band
  - effective review routing decision
  - explanation for the routing outcome
- Current version:
  - conservative signal-based routing
- Future stronger version:
  - calibrated confidence model over pipeline signals

Why:

- Confidence should actively protect the system from overclaiming, not just decorate the UI.
- The brief explicitly rewards honest uncertainty and safe handling of weak evidence.
- A review-routing layer helps separate “model opinion” from “system-safe final action.”

Tradeoffs:

- We chose to let confidence influence final routing behavior, not just UI display.
  - Benefit: safer and more honest system behavior.
  - Cost: some automation decisions may be softened into human review.
- We chose a conservative routing strategy.
  - Benefit: reduces overconfident incorrect answers.
  - Cost: more cases may require reviewer attention.
- We chose a future path toward calibrated confidence models.
  - Benefit: stronger long-term mathematical grounding.
  - Cost: requires labeled eval outcomes before full implementation is worthwhile.

Locked shape:

`Take extraction, retrieval, and adjudication confidence signals -> apply conservative routing logic -> preserve strong explicit decisions when evidence is sufficient -> downgrade weak-evidence cases to needs_human_review -> emit final confidence band and effective review routing`

## Locked Build Step 12: Eval Harness

Status: approved

Decision:

- Build an end-to-end evaluation harness that accepts held-out JSON test cases.
- Run the real pipeline, not a mocked shortcut.
- Report:
  - verdict accuracy
  - retrieval quality
  - citation faithfulness
  - confidence usefulness
  - human-review and refusal behavior
- Preserve enough trace detail for failure analysis:
  - retrieved chunks
  - verdict output
  - confidence state
  - routing behavior
  - citation behavior

Why:

- The case study explicitly asks for an evaluation harness and expects more than one headline metric.
- The harness is how we validate retrieval, reasoning, and uncertainty handling together.

Tradeoffs:

- We chose end-to-end evaluation instead of only component-level evaluation.
  - Benefit: measures real product behavior.
  - Cost: slower and more complex runs.
- We chose multiple metrics instead of one accuracy number.
  - Benefit: more honest measurement of groundedness and uncertainty.
  - Cost: more analysis work.

Locked shape:

`Build an end-to-end eval harness that accepts held-out JSON test cases, runs the real pipeline, and reports verdict accuracy, retrieval quality, citation faithfulness, and confidence/human-review behavior with enough trace data for failure analysis`

## Locked Build Step 13: Chatbot Q&A

Status: approved

Decision:

- Add a chatbot experience powered by Llama, but keep two explicit scopes:
  - policy-grounded chat
  - case-grounded chat
- Policy-grounded chat:
  - retrieves only from active policy chunks
  - answers only from policy evidence
  - cites clauses and sections
  - refuses questions outside the policy library or weakly supported by it
- Case-grounded chat:
  - uses current submission context from the database
  - may use employee info, receipt extraction, deterministic findings, verdicts, confidence, and policy evidence
  - answers questions about the current case while still grounding policy claims in retrieved policy chunks
- Do not build one unconstrained “knows everything” chatbot.
- Keep the refusal boundary explicit:
  - policy chat must decline out-of-policy questions instead of fabricating

Why:

- The brief explicitly requires policy-library Q&A with grounded refusal.
- A single unconstrained chatbot would blur policy answers with case reasoning and make refusal behavior weaker.
- Two scopes let us support both policy exploration and case explanation without losing grounding discipline.

Tradeoffs:

- We chose one chat experience with two scopes instead of a single freeform assistant.
  - Benefit: cleaner grounding and safer refusal behavior.
  - Cost: slightly more product structure and UX work.
- We chose policy-only refusal behavior in the policy scope.
  - Benefit: directly aligns with the case-study requirement.
  - Cost: the bot may feel more conservative.
- We chose case-grounded chat to use database context plus policy evidence.
  - Benefit: stronger reviewer copilot experience.
  - Cost: more context assembly logic.

Locked shape:

`Use Llama for chatbot Q&A, but split it into policy-grounded chat and case-grounded chat -> policy chat answers only from policy evidence and refuses out-of-scope questions -> case chat uses current submission context plus policy retrieval for grounded explanation`
