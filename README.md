# Northwind Expense Review Workbench

Northwind Expense Review Workbench is an AI-assisted finance pre-review tool for employee expense receipts. It ingests seeded sample cases and new reviewer-created submissions, extracts receipt details, retrieves relevant policy evidence, produces a pre-review verdict, and preserves reviewer overrides and submission history.

## What the app does

- Seeds the five provided employees and sample submissions on startup.
- Lets a reviewer start a new submission by:
  - picking an existing seeded employee, or
  - creating a new employee with trip context.
- Accepts mixed-format receipts:
  - PDF
  - image (`jpg`, `jpeg`, `png`, `webp`, `tiff`)
  - text-like files (`txt`, lightweight `rtf`)
- Runs receipt extraction, categorization, rule checks, policy retrieval, and adjudication.
- Shows, for each receipt:
  - extracted category
  - verdict
  - reasoning
  - quoted policy evidence
  - confidence breakdown
- Allows reviewer overrides with comments and stores the original system verdict separately.
- Lets reviewers move manually created cases into a trash view and restore them later without losing the case record.
- Preserves searchable history by employee, date, and status.
- Supports two assistant modes:
  - policy-grounded chat
  - case-grounded chat

## Local run

### 1. Configure environment

Create `.env` in the project root from `.env.example`.

Minimum required values:

```env
POSTGRES_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/northwind_expense
RAW_BUCKET_NAME=northwind-expense-raw
RAW_BUCKET_PREFIX=
R2_REGION=auto
R2_ENDPOINT_URL=https://<cloudflare_account_id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=<cloudflare_r2_access_key_id>
R2_SECRET_ACCESS_KEY=<cloudflare_r2_secret_access_key>

LLAMA_API_URL=
LLAMA_API_TOKEN=<cloudflare_workers_ai_token>
CLOUDFLARE_ACCOUNT_ID=<cloudflare_account_id>
LLAMA_MODEL=@cf/meta/llama-3.1-8b-instruct
LLAMA_TIMEOUT_SECONDS=20
```

Notes:

- Cloudflare R2 is used for raw policy and receipt artifact storage.
- Cloudflare Workers AI is the intended hosted Llama endpoint.
- If `LLAMA_API_TOKEN` is omitted, the backend falls back to heuristic adjudication.

Frontend-only deployment note:

- when deploying the UI separately on Vercel, set `VITE_API_BASE_URL` to the public backend base URL, for example `https://your-backend.example.com`

### 2. Start the stack

From the project root:

```bash
docker compose up --build
```

If you need a clean reseed of policies and sample data:

```bash
docker compose down -v
docker compose up --build
```

### 3. Open the app

- Frontend: [http://localhost:5173](http://localhost:5173)
- Backend health: [http://localhost:8000/api/health](http://localhost:8000/api/health)
- Bootstrap status: [http://localhost:8000/api/bootstrap-status](http://localhost:8000/api/bootstrap-status)

## Vercel deployment note

This repository is Vercel-ready for the frontend build. The frontend reads `VITE_API_BASE_URL`, so a Vercel deployment can point at a separately hosted backend without relying on same-origin `/api` routing.

## Browser flows

### Sample cases

1. Open `Sample Cases`
2. Select one of the seeded submissions
3. Click `Do Analysis`
4. Review receipt-level verdicts, evidence, confidence, and overrides

### New submission

1. Open `New Submission`
2. Either:
   - choose an existing employee and start a new submission, or
   - create a new employee and submission
3. Upload receipts
4. Run `Do Analysis`

### Trash and restore

1. Open a manual case
2. Click `Move to trash`
3. Open the `Trash` tab to review removed cases
4. Click `Restore case` to move a case back into the active manual list

### History

Use `History` to filter past submissions by:

- employee
- date
- status

### Assistant

- `Policy library` answers only from the policy corpus and declines unsupported questions.
- `Current case` answers about the selected submission using receipt facts plus policy evidence.

## Architecture

Open the architecture flow sketch here:

- [architecture-diagram.html](architecture-diagram.html)

### Storage

- PostgreSQL stores:
  - employees
  - submissions
  - receipts
  - extractions
  - deterministic findings
  - verdicts
  - overrides
  - policy documents
  - policy chunks
  - policy rules
- `pgvector` stores policy chunk embeddings inside PostgreSQL.
- Cloudflare R2 stores raw policy PDFs and raw receipt artifacts.

### Policy pipeline

1. Upload bundled policy PDFs
2. Split each bundle into individual policy documents
3. Chunk by policy structure
4. Tag chunks by topic
5. Embed chunks
6. Seed deterministic rule definitions from supported policy clauses

### Receipt pipeline

1. Detect file type
2. Extract text
   - PaddleOCR for images / image-based receipts when available
   - direct text parsing for text-like inputs
3. Normalize receipt fields
4. Score extraction confidence
5. Categorize and route to policy families
6. Run deterministic checks
7. Retrieve policy evidence
8. Adjudicate verdict + reasoning
9. Apply conservative confidence routing

### UI shape

- `Sample Cases`
- `New Submission`
- `History`
- `Assistant`

## Design choices and tradeoffs

### Retrieval approach

I chose routed retrieval over policy families, then combined vector similarity, keyword overlap, and topic-tag signals inside that smaller search space. The main reason was that the policy library intentionally contains realistic noise, including unrelated corporate documents, so a blind semantic search over everything would look flexible but would actually make citation faithfulness much worse. Routing first by receipt category and supporting signals gives the system a narrower and more defensible search area.

The tradeoff is orchestration complexity. This design is less elegant than “embed everything and ask top-k,” because it needs categorization, family scoring, and stricter retrieval rules before generation. I accepted that cost because this case study rewards groundedness and honest uncertainty more than retrieval simplicity. In this domain, a slightly more complex retrieval layer is preferable to a simpler one that regularly cites the wrong policy family.

### Chunking strategy

I split bundled PDFs into individual policy documents first and then chunked by section and subsection structure instead of using naive token windows. That choice was driven by the need for faithful citations. Finance reviewers need to see the actual clause the system relied on, and that is much easier when chunks line up with real policy boundaries such as `2.1`, `4.3`, or a named subsection.

The tradeoff is a more involved ingestion pipeline. Bundle splitting, section detection, and metadata preservation are more fragile than generic token chunking, especially when PDFs are messy. I still chose the structure-aware route because policy updates, versioning, and citation quality matter more here than ingestion simplicity. A cleanly versioned section chunk is much easier to replace, cite, and debug than an arbitrary text window.

### Model tier selection

I did not use one model for the whole system. Extraction uses PaddleOCR or deterministic parsing, retrieval uses embeddings plus heuristics, and the final verdict comes from a Llama-compatible reasoning layer. The reason for this split is cost and controllability: most receipts do not need expensive reasoning at every stage, and the system is easier to audit when each stage has a narrow job.

The tradeoff is that the pipeline has more moving parts and more handoff points. A one-model design would be easier to explain at a high level, but it would also blur extraction errors, retrieval mistakes, and reasoning mistakes together. I preferred the layered design because it makes failure modes easier to inspect and keeps the stronger reasoning step focused where it is actually valuable: the adjudication layer.

### When to use a vision model

I treated PaddleOCR as the default document-vision layer for scanned or image-based receipts, while allowing text-like files to bypass OCR entirely. That choice keeps the system practical and affordable for a case-study build while still supporting the mixed receipt formats described in the brief.

The tradeoff is robustness. A managed commercial extraction stack could likely be stronger on messy receipt images, but it would increase external dependency cost and make the project feel more like a vendor integration than a designed system. I chose the lighter vision path because the brief values design reasoning, failure handling, and end-to-end architecture, not just buying the strongest extractor available. When OCR is weak, the system is expected to surface uncertainty rather than mask it.

### Confidence handling

I separated confidence into extraction confidence, retrieval confidence, and decision confidence instead of collapsing everything into one score. That design reflects how this system can fail: a receipt may be read clearly but matched to weak policy evidence, or policy retrieval may be strong while extraction is incomplete. A single score would hide those differences and make the final verdict look more certain than it really is.

The tradeoff is that confidence becomes harder to calibrate and explain internally. It requires explicit scoring logic and, ideally, later tuning against reviewer outcomes. I still chose the multi-part approach because this brief explicitly rewards honest “I don’t know” behavior. In a reviewer-assist system, nuanced confidence is worth the added calibration effort because it is what allows the system to route weak cases to human review instead of bluffing.

### Flag vs reject vs human review

I used four states: `compliant`, `flagged`, `rejected`, and `needs_human_review`. I chose that structure because finance review is not a clean pass/fail workflow. Some expenses are clearly acceptable, some clearly violate policy, some have a likely issue but still need reviewer judgment, and some simply do not have enough trustworthy evidence for the system to act confidently.

The tradeoff is added workflow complexity in both the backend and the UI. A binary approve/reject flow would be simpler to build and easier to summarize, but it would push ambiguity into the wrong places and encourage overconfident automation. I preferred the richer state model because it matches the actual reviewer task more closely and lets the system distinguish between “likely wrong” and “not safe to decide automatically,” which is a critical difference for trust.

## Cost per submission

Rough MVP cost shape:

- PostgreSQL + app hosting: mostly fixed infrastructure cost
- R2 storage: low variable cost for policy/receipt artifacts
- Workers AI / hosted Llama: main variable inference cost
- OCR: zero direct API cost if run with local PaddleOCR

Approximate per-submission intuition:

- clean small submission: low single-digit cents equivalent if using hosted inference sparingly
- heavier or ambiguous submission: grows with number of receipts and adjudication calls

The cost driver is not storage. It is model inference and any future reranking layer.

## Scaling to 10,000 submissions/day

Main steps:

- move receipt extraction and adjudication into background workers
- queue ingestion and analysis jobs
- cache policy embeddings and family profiles in memory
- batch or pool embedding and reranking work
- separate reviewer reads from analysis writes
- add observability around:
  - retrieval quality
  - override rate
  - latency
  - fallback rate

Likely architecture at that scale:

- app API
- worker queue
- PostgreSQL with read replicas if needed
- object storage
- horizontally scaled analysis workers

## Evaluation methodology

The intended evaluation dimensions for this project are:

- verdict accuracy
- retrieval quality
- citation faithfulness
- refusal / decline behavior
- confidence usefulness

Key retrieval metrics:

- context relevance
- hit rate / top-k family accuracy
- context recall

Key answer metrics:

- faithfulness / groundedness
- answer relevance

Key operational metrics:

- latency
- fallback rate
- override rate

Why these metrics:

- `verdict accuracy` checks whether the reviewer-facing outcome is directionally right.
- `retrieval quality` checks whether the system pulled the right policy family and supporting clauses.
- `citation correctness` checks faithfulness: quoted policy text must actually support the claim.
- `answer relevance` checks that policy or case chat stays on the user’s question instead of drifting.
- `refusal rate` checks whether the system declines unsupported questions instead of fabricating.
- `confidence usefulness` is indirectly tested through the separation of strong, weak, and fallback outcomes.

## Evaluation harness

The harness lives at:

- [scripts/eval_harness.py](scripts/eval_harness.py)

Example input spec:

- [eval-spec.sample.json](eval-spec.sample.json)

Run it from the project root:

```bash
python3 scripts/eval_harness.py eval-spec.sample.json --json-out data/eval-report.json
```

What it supports:

- `receipt_review`
  - against a seeded sample case via `sample_case_id`
  - against a specific DB submission via `submission_id`
  - against a drop-in held-out folder via `submission_dir`
- `policy_chat`
- `case_chat`

Supported expectation fields include:

- `verdict`
- `category`
- `human_review_needed`
- `grounded`
- `document_codes_any`
- `policy_title_contains_any`
- `quote_terms_any`
- `answer_terms_any`
- `refusal_expected`

The harness runs the real backend analysis path and reports:

- case pass rate
- verdict accuracy
- category accuracy
- retrieval quality
- citation correctness
- grounded accuracy
- answer relevance
- refusal rate

This means a grader can drop in a held-out JSON spec after submission and get back both:

- high-level summary metrics
- per-case detail showing what matched, what failed, and what the system actually produced

## What I would do next

- add reranking on top of initial retrieval
- improve policy-family routing calibration
- strengthen receipt extraction for noisy image receipts
- tighten confidence calibration with reviewer-labeled outcomes
- deploy the stack publicly for browser-based review
