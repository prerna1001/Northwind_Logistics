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

### History

Use `History` to filter past submissions by:

- employee
- date
- status

### Assistant

- `Policy library` answers only from the policy corpus and declines unsupported questions.
- `Current case` answers about the selected submission using receipt facts plus policy evidence.

## Architecture

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

Chosen:

- routed retrieval over policy families
- vector similarity
- keyword overlap
- topic-tag signals

Why:

- the policy library contains realistic noise
- category-first routing is much safer than searching every policy equally

Tradeoff:

- better precision
- more orchestration and routing logic

### Chunking strategy

Chosen:

- split bundled PDFs into individual policies first
- chunk by section / subsection structure

Why:

- policy clauses are easier to retrieve and cite faithfully
- updates can be versioned cleanly

Tradeoff:

- more ingestion complexity than naive token chunking

### Model tier selection

Chosen:

- extraction: PaddleOCR / deterministic parsing
- retrieval: embeddings + heuristics
- adjudication: Llama-compatible model

Why:

- keep expensive reasoning focused on the final decision layer

Tradeoff:

- more pipeline stages
- better controllability and lower cost

### When to use a vision model

Chosen:

- PaddleOCR is the default document-vision layer
- text-like files bypass OCR
- weak extraction routes toward review instead of pretending confidence

Tradeoff:

- free and practical
- less robust than premium managed extraction

### Confidence handling

Chosen:

- three-part confidence:
  - extraction
  - retrieval
  - decision
- conservative routing when upstream evidence is weak

Why:

- avoids a single misleading confidence number
- weak evidence should reduce automation aggressiveness

Tradeoff:

- more nuanced
- requires explicit calibration work

### Flag vs reject vs human review

Chosen:

- `compliant`
- `flagged`
- `rejected`
- `needs_human_review`

Why:

- finance review is not cleanly binary
- explicit uncertainty is better than false certainty

Tradeoff:

- more workflow complexity
- much safer reviewer experience

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

## Evaluation harness

The harness lives at:

- [scripts/eval_harness.py](scripts/eval_harness.py)

Example input spec:

- [docs/eval-spec.example.json](docs/eval-spec.example.json)

Run it from the project root:

```bash
python3 scripts/eval_harness.py docs/eval-spec.example.json --json-out data/eval-report.json
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

## What I would do next

- add a formal evaluation harness that accepts expected-outcome JSON and reports the metrics above
- add reranking on top of initial retrieval
- improve policy-family routing calibration
- strengthen receipt extraction for noisy image receipts
- tighten confidence calibration with reviewer-labeled outcomes
- deploy the stack publicly for browser-based review
