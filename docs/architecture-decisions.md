# Architecture Decisions

## Locked Decision 1: Retrieval Approach

Status: approved

Decision:

- Use a routed retrieval pipeline, not blind RAG over all policy PDFs.
- For each receipt, first determine:
  - primary category
  - secondary signals
- Use those signals to route into the relevant policy families.
- Only then run retrieval inside that filtered subset.
- Retrieval inside the filtered subset should be hybrid:
  - metadata filtering
  - vector retrieval
  - exact-term boosting
- Return a small evidence package for the decision layer:
  - top 3-5 chunks
  - from 1-3 most relevant policies
  - with policy name, section, quoted clause, and retrieval rationale

Why:

- The policy set contains meaningful noise outside travel and expense.
- Blind search across all PDFs increases irrelevant matches and weaker citations.
- Routing first improves precision, faithfulness, and downstream confidence.

Tradeoffs:

- We chose routed retrieval over blind search across all policies.
  - Benefit: better precision, less noise, stronger citations.
  - Cost: requires an upfront category-routing layer and policy-family mapping.
- We chose hybrid retrieval over pure vector similarity.
  - Benefit: exact policy phrases like `solo travel`, `itemized receipt`, and `included meals` are easier to recover reliably.
  - Cost: more ingestion metadata and slightly more retrieval logic.
- We chose to send only a small evidence package into the decision layer.
  - Benefit: reduces prompt noise and hallucination risk.
  - Cost: if routing is wrong, good evidence may be filtered out too early, so routing quality matters a lot.

Locked shape:

`Receipt/category routing -> policy family filtering -> hybrid retrieval within filtered policies -> top evidence package for decision layer`

## Locked Decision 2: Chunking Strategy

Status: approved

Decision:

- First split each bundled PDF into individual policy documents.
- Do not treat `policy1.pdf`, `policy2.pdf`, and similar bundles as single retrievable policy units.
- Within each individual policy document, chunk by structure:
  - section headings
  - numbered clauses
  - subsections like `2.1`, `3.4`, `5.1`
- Use one clause or one short subsection as the default chunk unit.
- If a section is too long:
  - split by paragraph or bullet-group boundaries
  - preserve the same section identity with a sub-part index
- Store strong metadata on every chunk:
  - bundle PDF
  - policy title
  - document code
  - section key
  - part index
  - page range
  - topic tags
  - chunk text
  - version
  - effective date
  - active/inactive status
- Make chunk storage version-aware.
  - New policy versions create new document and chunk records.
  - Older versions are marked inactive rather than overwritten.
- Re-ingestion should be first-class.
  - parse updated bundled PDF
  - split into policy documents
  - re-chunk changed policies
  - re-embed new chunks
  - switch active version

Why:

- The final system needs precise citations, so policy structure is more valuable than generic token windows.
- Bundled PDFs are packaging artifacts, not logical policies.
- Version-aware storage keeps policy updates safe and traceable.
- Section-aware chunks are easier to quote faithfully and easier to link back to policy logic.

Tradeoffs:

- We chose structure-aware chunking over generic token-window chunking.
  - Benefit: stronger citations, clearer section references, less semantic mixing.
  - Cost: ingestion is more complex because we must parse policy structure reliably.
- We chose to split bundled PDFs into individual policy documents before chunking.
  - Benefit: retrieval units match the real policy boundaries, not arbitrary source files.
  - Cost: requires an additional document-segmentation step during ingestion.
- We chose version-aware chunk storage instead of overwrite-in-place updates.
  - Benefit: safer policy evolution, traceability, and auditability.
  - Cost: more schema complexity and the need to manage active/inactive versions.
- We chose topic-tagged chunks.
  - Benefit: better routing-aware retrieval and easier filtering.
  - Cost: tagging quality becomes another ingestion dependency that must stay consistent.

Locked shape:

`bundle PDF -> individual policy documents -> versioned section-aware chunks -> metadata + topic tags + active/inactive versioning`

## Locked Decision 3: Model Tier Selection

Status: approved

Decision:

- Use different tools and model tiers for different jobs instead of one model for the entire pipeline.
- Extraction layer:
  - Use PaddleOCR as the primary free OCR and document parsing layer for PDF and image receipts.
  - Use direct parsing for `.txt` receipts.
  - Keep receipt normalization in backend code after OCR.
- Categorization layer:
  - Use non-LLM heuristics first for receipt category and routing.
  - Use Llama only as a fallback for ambiguous categorization cases.
- Embedding layer:
  - Use a dedicated embedding model for policy chunk retrieval.
- Adjudication layer:
  - Use Llama for the current MVP and test-project reasoning layer.
  - For production, recommend a stronger reasoning model such as Claude Sonnet.
- Vision strategy:
  - Treat PaddleOCR as the current document-vision/OCR layer.
  - Do not use a separate general multimodal vision model by default.

Why:

- The project needs a free test-project OCR path, so AWS Textract is not a good fit for the current constraints.
- PaddleOCR is open source and designed for OCR and document parsing, which fits receipt-heavy workflows better than a bare OCR engine.
- Llama is currently available for low-cost reasoning, while a stronger paid model can be recommended honestly for production.
- A tiered design keeps expensive reasoning away from simpler extraction and classification tasks.

Tradeoffs:

- We chose PaddleOCR over AWS Textract.
  - Benefit: open-source and free to use as software, which fits the test-project constraint.
  - Cost: more local setup, less turnkey than Textract, and quality may vary more depending on receipt quality and local runtime setup.
- We chose PaddleOCR over Tesseract as the primary OCR layer.
  - Benefit: better suited for document parsing and structured extraction than plain OCR alone.
  - Cost: a heavier dependency and more setup complexity than Tesseract.
- We chose heuristics-first categorization with Llama fallback.
  - Benefit: cheaper and easier to control than routing every receipt through a model.
  - Cost: ambiguous edge cases require a fallback threshold and extra handling logic.
- We chose Llama for adjudication in the MVP.
  - Benefit: accessible and practical under current constraints.
  - Cost: weaker reasoning and citation discipline than stronger paid frontier models.
- We chose to explicitly recommend Claude Sonnet for production.
  - Benefit: honest distinction between current implementation constraints and production-grade architecture.
  - Cost: production would introduce a paid model dependency.

Locked shape:

`PaddleOCR/direct parsing for extraction -> heuristic routing with Llama fallback -> embeddings for retrieval -> Llama for adjudication -> stronger model recommendation for production`

## Locked Decision 5: Confidence Handling

Status: approved

Decision:

- Confidence should not be hardcoded case by case.
- Use three confidence dimensions:
  - extraction confidence
  - retrieval confidence
  - decision confidence
- Base confidence on general evidence-quality signals rather than brittle scenario-specific scoring.
- Confidence architecture is fixed, but confidence answers should remain data- and evidence-driven.
- Final confidence should be conservative:
  - weak extraction or weak retrieval should cap final decision confidence
- Current version:
  - use conservative signal-based confidence
- Stronger future version:
  - use a calibrated confidence model over pipeline signals
  - logistic regression first
  - boosted trees if later needed
  - only after enough eval or reviewer-labeled data exists

Why:

- Confidence should reflect evidence quality, not policy-specific hardcoded heuristics.
- The system needs honest uncertainty handling without maintaining fragile rule-by-rule confidence logic.
- A future calibrated meta-model is a stronger mathematical approach, but only becomes worthwhile once enough labeled outcomes exist.

Tradeoffs:

- We chose multi-part confidence instead of a single raw score.
  - Benefit: easier to debug and more faithful to the pipeline’s real uncertainty points.
  - Cost: more backend logic and more fields to store.
- We chose signal-based confidence now instead of a trained confidence model immediately.
  - Benefit: faster to ship, simpler, and does not require labeled training data.
  - Cost: less statistically grounded than a calibrated learned confidence model.
- We chose to design toward a calibrated confidence model later.
  - Benefit: stronger long-term reliability and better mapping between score and actual correctness.
  - Cost: needs eval infrastructure and labeled outcomes before it is justified.

Locked shape:

`non-brittle confidence framework based on extraction quality, retrieval quality, and decision agreement now -> calibrated meta-model over pipeline signals later`

## Locked Decision 6: Verdict Boundary

Status: approved

Decision:

- Use four verdict states:
  - compliant
  - flagged
  - rejected
  - needs_human_review
- `compliant`:
  - clearly okay
  - evidence is sufficient
  - no meaningful issue found
- `flagged`:
  - likely issue exists
  - reviewer action or context still matters
  - partial reimbursement or nuanced handling may apply
- `rejected`:
  - only for explicit high-confidence non-reimbursable cases
  - strong facts
  - strong policy support
  - low ambiguity
- `needs_human_review`:
  - weak extraction
  - weak retrieval
  - missing context
  - unresolved ambiguity
  - conflicting or insufficient evidence
- Bias the system toward:
  - flagged over rejected
  - needs_human_review over false certainty

Why:

- The brief rewards grounded uncertainty and reviewer support more than aggressive automation.
- Expense review is often nuanced, and many cases need reviewer attention without deserving a hard denial.
- Separating `flagged` from `rejected` creates a more realistic finance-review workflow.

Tradeoffs:

- We chose four verdict states instead of a binary approve/reject flow.
  - Benefit: better reflects real reviewer action and ambiguity.
  - Cost: more UI and backend complexity.
- We chose a conservative rejection threshold.
  - Benefit: reduces overconfident incorrect denials.
  - Cost: more cases may remain flagged instead of being auto-rejected.
- We preserved a dedicated human-review state.
  - Benefit: keeps the system honest when evidence is weak.
  - Cost: lowers full automation coverage.

Locked shape:

`compliant for clearly okay -> flagged for likely issues needing reviewer action -> rejected only for explicit high-confidence non-reimbursable cases -> needs_human_review when evidence is weak or ambiguity remains`
