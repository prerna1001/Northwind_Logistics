import { useEffect, useState } from "react";

const emptyEmployee = {
  employee_id: "",
  name: "",
  grade: 5,
  title: "",
  department: "",
  manager_id: "",
  home_base: "",
  trip_purpose: "",
  trip_dates: "",
};

const emptyHistoryFilters = {
  employee: "",
  status: "",
  date_from: "",
  date_to: "",
};

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail || "Request failed";
    const friendly =
      typeof detail === "string" && detail.includes("truth value of an array with more than one element is ambiguous")
        ? "The analysis service hit an internal comparison error. Please restart the app and try again."
        : detail;
    throw new Error(friendly);
  }
  return response.json();
}

function formatMoney(value) {
  if (typeof value !== "number") return "Not available";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
  }).format(value);
}

function SubmissionCard({ submission, selected, onSelect }) {
  return (
    <button
      type="button"
      className={`submission-card ${selected ? "selected" : ""}`}
      onClick={() => onSelect(submission)}
    >
      <div>
        <h4>{submission.employee.name}</h4>
        <p>{submission.trip_purpose || "Trip context pending"}</p>
      </div>
      <div className="submission-meta">
        <span>{submission.source === "sample" ? "Provided sample" : "Manual case"}</span>
        <span>{submission.receipts.length} receipts</span>
        <span className={`status-badge verdict-${submission.status}`}>{submission.status}</span>
      </div>
    </button>
  );
}

function CitationList({ policyFindings }) {
  if (!policyFindings?.length) return <p className="muted-copy">No policy citations yet.</p>;
  return (
    <div className="citation-list">
      {policyFindings.map((finding, index) => (
        <article className="citation-card" key={`${finding.document_code}-${index}`}>
          <strong>{finding.policy_title || finding.document_code}</strong>
          <span>{finding.section_key || "Section not tagged"}</span>
          <p>{finding.quote || "No quote stored"}</p>
        </article>
      ))}
    </div>
  );
}

function ReceiptCard({ receipt, onOverride }) {
  const [overrideVerdict, setOverrideVerdict] = useState(receipt.effective_verdict || "flagged");
  const [comment, setComment] = useState("");
  const normalized = receipt.normalized_data || {};
  const systemVerdict = receipt.system_verdict;
  const confidence = systemVerdict?.confidence;

  return (
    <article className="receipt-card">
      <div className="receipt-head">
        <div>
          <h4>{receipt.original_filename}</h4>
          <p>
            {receipt.category || "Uncategorized"} · extraction {Math.round((receipt.extraction_confidence || 0) * 100)}%
          </p>
          <p>{receipt.storage_backend === "s3" ? "Stored in S3" : "Stored in the S3-shaped local mirror"}</p>
        </div>
        <div className="receipt-verdicts">
          <span className={`status-badge verdict-${receipt.effective_verdict || "draft"}`}>
            {receipt.effective_verdict || "not_analyzed"}
          </span>
          {systemVerdict ? (
            <small>
              system: <strong>{systemVerdict.verdict}</strong>
            </small>
          ) : null}
        </div>
      </div>

      <dl className="receipt-grid">
        <div>
          <dt>Vendor</dt>
          <dd>{normalized.vendor || "Not extracted"}</dd>
        </div>
        <div>
          <dt>Date</dt>
          <dd>{normalized.date || "Unknown"}</dd>
        </div>
        <div>
          <dt>Total</dt>
          <dd>{formatMoney(normalized.total)}</dd>
        </div>
        <div>
          <dt>Payment</dt>
          <dd>{normalized.payment_method || "Unknown"}</dd>
        </div>
      </dl>

      {normalized.line_items?.length ? (
        <div className="line-items">
          <p>Extracted receipt details</p>
          <ul>
            {normalized.line_items.slice(0, 8).map((item, index) => (
              <li key={`${item.description}-${index}`}>
                <span>{item.description}</span>
                <strong>{formatMoney(item.amount)}</strong>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {receipt.deterministic_findings?.length ? (
        <div className="finding-list">
          <p>Deterministic findings</p>
          {receipt.deterministic_findings.map((finding) => (
            <article key={finding.rule_id} className="finding-card">
              <strong>{finding.rule_id}</strong>
              <span className={`status-badge verdict-${finding.severity_hint}`}>{finding.severity_hint}</span>
              <p>{finding.summary}</p>
            </article>
          ))}
        </div>
      ) : null}

      <div className="analysis-block">
        <div>
          <h5>System reasoning</h5>
          <p className="reasoning-copy">{systemVerdict?.reasoning_summary || "Run analysis to generate a policy-backed decision."}</p>
        </div>
        <div>
          <h5>Confidence</h5>
          {confidence ? (
            <div className="confidence-card">
              <strong>{confidence.band} confidence</strong>
              <span>Extraction {Math.round(confidence.extraction * 100)}%</span>
              <span>Retrieval {Math.round(confidence.retrieval * 100)}%</span>
              <span>Decision {Math.round(confidence.decision * 100)}%</span>
            </div>
          ) : (
            <p className="muted-copy">No confidence breakdown yet.</p>
          )}
        </div>
      </div>

      <div className="analysis-block">
        <div className="full-width">
          <h5>Policy evidence</h5>
          <CitationList policyFindings={systemVerdict?.policy_findings} />
        </div>
      </div>

      <div className="override-panel">
        <div>
          <h5>Reviewer override</h5>
          {receipt.latest_override ? (
            <p className="override-summary">
              {receipt.latest_override.override_verdict} by {receipt.latest_override.reviewer_name || "reviewer"}:{" "}
              {receipt.latest_override.reviewer_comment}
            </p>
          ) : (
            <p className="muted-copy">No override recorded yet.</p>
          )}
        </div>
        <div className="override-form">
          <select value={overrideVerdict} onChange={(event) => setOverrideVerdict(event.target.value)}>
            <option value="compliant">compliant</option>
            <option value="flagged">flagged</option>
            <option value="rejected">rejected</option>
            <option value="needs_human_review">needs_human_review</option>
          </select>
          <input
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            placeholder="Reviewer note"
          />
          <button
            type="button"
            className="secondary-button"
            onClick={() => {
              if (!comment.trim()) return;
              onOverride(receipt.id, overrideVerdict, comment);
              setComment("");
            }}
          >
            Save override
          </button>
        </div>
      </div>
    </article>
  );
}

function SubmissionDetail({ submission, onAnalyze, onUpload, onOverride, analyzing }) {
  if (!submission) {
    return (
      <section className="panel detail-panel">
        <h2>Submission detail</h2>
        <p>Select a sample case or manual submission to inspect its receipts.</p>
      </section>
    );
  }

  const canUpload = submission.source === "manual";

  return (
    <section className="panel detail-panel">
      <div className="panel-head">
        <div>
          <h2>{submission.employee.name}</h2>
          <p>{submission.trip_purpose}</p>
          <p className="muted-copy">
            {submission.trip_dates} · {submission.employee.title} · {submission.employee.department}
          </p>
        </div>
        <div className="detail-actions">
          <span className={`status-badge verdict-${submission.status}`}>{submission.status}</span>
          <button type="button" className="primary-button" onClick={() => onAnalyze(submission.id)} disabled={analyzing}>
            {analyzing ? "Running analysis…" : "Do Analysis"}
          </button>
        </div>
      </div>

      {canUpload ? (
        <label className="upload-dropzone">
          <input
            type="file"
            multiple
            onChange={(event) => {
              if (event.target.files?.length) {
                onUpload(submission.id, event.target.files);
                event.target.value = "";
              }
            }}
          />
          <span>Upload receipts for this new employee case</span>
          <small>PDF, image, or text receipts are accepted.</small>
        </label>
      ) : (
        <div className="sample-note">
          <strong>Provided sample case</strong>
          <span>Receipts are already attached. Use “Do Analysis” to run the policy review flow.</span>
        </div>
      )}

      <div className="receipts-grid">
        {submission.receipts.map((receipt) => (
          <ReceiptCard key={receipt.id} receipt={receipt} onOverride={onOverride} />
        ))}
      </div>
    </section>
  );
}

function ChatPanel({ selectedSubmission, onAsk }) {
  const [scope, setScope] = useState("policy");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState(null);
  const [asking, setAsking] = useState(false);

  async function submitQuestion(event) {
    event.preventDefault();
    if (!question.trim()) return;
    setAsking(true);
    try {
      const nextAnswer = await onAsk(scope, question, selectedSubmission?.id);
      setAnswer(nextAnswer);
      setQuestion("");
    } catch (error) {
      setAnswer({
        grounded: false,
        answer: error.message || "The assistant could not answer that right now.",
        citations: [],
      });
    } finally {
      setAsking(false);
    }
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Ask The Assistant</h2>
          <p>Switch between policy-grounded chat and case-grounded chat.</p>
        </div>
      </div>
      <form className="chat-form" onSubmit={submitQuestion}>
        <select value={scope} onChange={(event) => setScope(event.target.value)}>
          <option value="policy">Policy library</option>
          <option value="case">Current case</option>
        </select>
        <input
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder={
            scope === "policy"
              ? "Ask a policy question and get cited answers."
              : "Ask about the selected submission."
          }
        />
        <button type="submit" className="primary-button" disabled={asking}>
          {asking ? "Thinking…" : "Ask"}
        </button>
      </form>
      {scope === "case" && !selectedSubmission ? (
        <p className="muted-copy">Select a submission first to use case-grounded chat.</p>
      ) : null}
      {answer ? (
        <div className="chat-answer">
          <span className={`status-badge ${answer.grounded ? "verdict-compliant" : "verdict-needs_human_review"}`}>
            {answer.grounded ? "grounded" : "declined"}
          </span>
          <p>{answer.answer}</p>
          <CitationList policyFindings={answer.citations} />
        </div>
      ) : null}
    </section>
  );
}

export default function App() {
  const [bootstrap, setBootstrap] = useState(null);
  const [submissions, setSubmissions] = useState([]);
  const [employees, setEmployees] = useState([]);
  const [selectedSubmission, setSelectedSubmission] = useState(null);
  const [tab, setTab] = useState("sample");
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [analyzingId, setAnalyzingId] = useState(null);
  const [newEmployee, setNewEmployee] = useState(emptyEmployee);
  const [existingEmployeeId, setExistingEmployeeId] = useState("");
  const [historyFilters, setHistoryFilters] = useState(emptyHistoryFilters);
  const [historyRows, setHistoryRows] = useState([]);

  async function refreshSubmissions(params = "") {
    const [status, rows, employeeRows] = await Promise.all([
      fetchJson("/api/bootstrap-status"),
      fetchJson(`/api/submissions${params}`),
      fetchJson("/api/employees"),
    ]);
    setBootstrap(status);
    setSubmissions(rows);
    setEmployees(employeeRows);
    if (!existingEmployeeId && employeeRows.length) {
      setExistingEmployeeId(employeeRows[0].employee_id);
    }
    if (!selectedSubmission && rows.length) {
      const defaultSample = rows.find((row) => row.source === "sample");
      setSelectedSubmission(defaultSample || rows[0]);
    } else if (selectedSubmission) {
      const refreshed = rows.find((row) => row.id === selectedSubmission.id);
      if (refreshed) {
        setSelectedSubmission(refreshed);
      } else {
        const defaultSample = rows.find((row) => row.source === "sample");
        setSelectedSubmission(defaultSample || rows[0] || null);
      }
    }
    return rows;
  }

  useEffect(() => {
    refreshSubmissions()
      .catch((error) => setMessage(error.message))
      .finally(() => setLoading(false));
  }, []);

  async function handleCreateEmployee(event) {
    event.preventDefault();
    try {
      await fetchJson("/api/employees", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...newEmployee,
          grade: Number(newEmployee.grade),
          manager_id: newEmployee.manager_id || null,
          home_base: newEmployee.home_base || null,
          trip_purpose: newEmployee.trip_purpose || null,
          trip_dates: newEmployee.trip_dates || null,
        }),
      });
      const submission = await fetchJson("/api/submissions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          employee_id: newEmployee.employee_id,
          trip_purpose: newEmployee.trip_purpose || null,
          trip_dates: newEmployee.trip_dates || null,
        }),
      });
      setMessage(`Created a new manual submission for ${newEmployee.name}.`);
      setNewEmployee(emptyEmployee);
      await refreshSubmissions();
      setSelectedSubmission(submission);
      setTab("manual");
    } catch (error) {
      setMessage(error.message);
    }
  }

  async function handleCreateSubmissionForExisting(event) {
    event.preventDefault();
    if (!existingEmployeeId) return;
    try {
      const employee = employees.find((row) => row.employee_id === existingEmployeeId);
      const submission = await fetchJson("/api/submissions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          employee_id: existingEmployeeId,
          trip_purpose: employee?.trip_purpose || null,
          trip_dates: employee?.trip_dates || null,
        }),
      });
      await refreshSubmissions();
      setSelectedSubmission(submission);
      setTab("manual");
      setMessage(`Started a new submission for ${employee?.name || existingEmployeeId}.`);
    } catch (error) {
      setMessage(error.message);
    }
  }

  async function handleUpload(submissionId, files) {
    const formData = new FormData();
    Array.from(files).forEach((file) => formData.append("files", file));
    try {
      const updated = await fetchJson(`/api/submissions/${submissionId}/receipts`, {
        method: "POST",
        body: formData,
      });
      setSelectedSubmission(updated);
      await refreshSubmissions();
      setMessage(`${files.length} receipt${files.length > 1 ? "s" : ""} uploaded.`);
    } catch (error) {
      setMessage(error.message);
    }
  }

  async function handleAnalyze(submissionId) {
    setAnalyzingId(submissionId);
    try {
      let updated;
      try {
        updated = await fetchJson(`/api/submissions/${submissionId}/analyze`, {
          method: "POST",
        });
      } catch (error) {
        const shouldRetry = error.message?.toLowerCase().includes("submission not found");
        if (!shouldRetry) {
          throw error;
        }

        const previousSelection =
          submissions.find((submission) => submission.id === submissionId) || selectedSubmission;
        const latestRows = await refreshSubmissions();
        const replacement = latestRows.find((row) => {
          if (previousSelection?.sample_case_id) {
            return row.sample_case_id === previousSelection.sample_case_id;
          }
          if (previousSelection?.employee_id && previousSelection?.source === "manual") {
            return row.employee_id === previousSelection.employee_id && row.source === "manual";
          }
          return false;
        });

        if (!replacement) {
          throw error;
        }

        updated = await fetchJson(`/api/submissions/${replacement.id}/analyze`, {
          method: "POST",
        });
      }
      setSelectedSubmission(updated);
      await refreshSubmissions();
      setMessage(`Analysis completed for ${updated.employee.name}.`);
    } catch (error) {
      setMessage(error.message);
    } finally {
      setAnalyzingId(null);
    }
  }

  async function handleOverride(receiptId, overrideVerdict, reviewerComment) {
    try {
      const updated = await fetchJson(`/api/receipts/${receiptId}/override`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ override_verdict: overrideVerdict, reviewer_comment: reviewerComment }),
      });
      setSelectedSubmission(updated);
      await refreshSubmissions();
      setMessage("Override saved.");
    } catch (error) {
      setMessage(error.message);
    }
  }

  async function runHistorySearch(event) {
    event.preventDefault();
    const params = new URLSearchParams();
    Object.entries(historyFilters).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    try {
      const rows = await fetchJson(`/api/submissions?${params.toString()}`);
      setHistoryRows(rows);
      if (rows.length) setSelectedSubmission(rows[0]);
      setMessage(`Loaded ${rows.length} historical submission${rows.length === 1 ? "" : "s"}.`);
    } catch (error) {
      setMessage(error.message);
    }
  }

  async function askAssistant(scope, question, submissionId) {
    return fetchJson("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope, question, submission_id: submissionId }),
    });
  }

  const sampleSubmissions = submissions.filter((submission) => submission.source === "sample");
  const manualSubmissions = submissions.filter((submission) => submission.source === "manual");

  if (loading) {
    return <main className="shell"><p>Loading the expense review workspace…</p></main>;
  }

  return (
    <main className="shell">
      <section className="hero">
        <div>
          <p className="eyebrow">Northwind Logistics</p>
          <h1>Expense Review Workbench</h1>
          <p className="hero-copy">
            Review employee expense receipts against company policy, inspect grounded findings, and manage overrides across
            sample cases and new submissions in one place.
          </p>
        </div>
      </section>

      {message ? <div className="banner">{message}</div> : null}

      <nav className="tab-bar">
        {[
          ["sample", "Sample Cases"],
          ["manual", "New Submission"],
          ["history", "History"],
          ["chat", "Assistant"],
        ].map(([value, label]) => (
          <button
            key={value}
            type="button"
            className={`tab-button ${tab === value ? "active" : ""}`}
            onClick={() => setTab(value)}
          >
            {label}
          </button>
        ))}
      </nav>

      {tab === "sample" ? (
        <section className="grid-two">
          <div className="panel">
            <h2>Choose a provided sample case</h2>
            <p>Open one of the seeded folders, then click “Do Analysis” to run the review pipeline.</p>
            <div className="submission-list">
              {sampleSubmissions.map((submission) => (
                <SubmissionCard
                  key={submission.id}
                  submission={submission}
                  selected={selectedSubmission?.id === submission.id}
                  onSelect={setSelectedSubmission}
                />
              ))}
            </div>
          </div>
          <SubmissionDetail
            submission={selectedSubmission?.source === "sample" ? selectedSubmission : sampleSubmissions[0]}
            onAnalyze={handleAnalyze}
            onUpload={handleUpload}
            onOverride={handleOverride}
            analyzing={analyzingId === selectedSubmission?.id}
          />
        </section>
      ) : null}

      {tab === "manual" ? (
        <section className="grid-two">
          <div className="panel">
            <h2>Create a new employee submission</h2>
            <p>Choose an existing employee or add a new one, then upload receipts into the created submission.</p>
            <div className="subsection">
              <h3>Start from an existing employee</h3>
              <form className="employee-form" onSubmit={handleCreateSubmissionForExisting}>
                <label>
                  employee
                  <select value={existingEmployeeId} onChange={(event) => setExistingEmployeeId(event.target.value)}>
                    {employees.map((employee) => (
                      <option key={employee.employee_id} value={employee.employee_id}>
                        {employee.name} · {employee.department} · {employee.trip_purpose || "Trip context on file"}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="form-action-row">
                  <button type="submit" className="primary-button" disabled={!existingEmployeeId}>
                    Start submission
                  </button>
                </div>
              </form>
            </div>

            <div className="subsection">
              <h3>Create a new employee</h3>
            <form className="employee-form" onSubmit={handleCreateEmployee}>
              {Object.entries(newEmployee).map(([key, value]) => (
                <label key={key}>
                  {key.replaceAll("_", " ")}
                  <input
                    value={value}
                    onChange={(event) =>
                      setNewEmployee((current) => ({ ...current, [key]: event.target.value }))
                    }
                  />
                </label>
              ))}
              <button type="submit" className="primary-button">Create submission</button>
            </form>
            </div>

            <div className="subsection">
              <h3>Manual cases</h3>
              <div className="submission-list">
                {manualSubmissions.length ? (
                  manualSubmissions.map((submission) => (
                    <SubmissionCard
                      key={submission.id}
                      submission={submission}
                      selected={selectedSubmission?.id === submission.id}
                      onSelect={setSelectedSubmission}
                    />
                  ))
                ) : (
                  <p className="muted-copy">No manual submissions yet.</p>
                )}
              </div>
            </div>
          </div>

          <SubmissionDetail
            submission={selectedSubmission?.source === "manual" ? selectedSubmission : manualSubmissions[0]}
            onAnalyze={handleAnalyze}
            onUpload={handleUpload}
            onOverride={handleOverride}
            analyzing={analyzingId === selectedSubmission?.id}
          />
        </section>
      ) : null}

      {tab === "history" ? (
        <section className="grid-two">
          <div className="panel">
            <h2>Submission history</h2>
            <p>Filter by employee, date, and status exactly as requested in the brief.</p>
            <form className="history-form" onSubmit={runHistorySearch}>
              {Object.entries(historyFilters).map(([key, value]) => (
                <label key={key}>
                  {key.replaceAll("_", " ")}
                  <input
                    type={key.includes("date") ? "date" : "text"}
                    value={value}
                    onChange={(event) =>
                      setHistoryFilters((current) => ({ ...current, [key]: event.target.value }))
                    }
                  />
                </label>
              ))}
              <button type="submit" className="primary-button">Apply filters</button>
            </form>
            <div className="submission-list">
              {(historyRows.length ? historyRows : submissions).map((submission) => (
                <SubmissionCard
                  key={submission.id}
                  submission={submission}
                  selected={selectedSubmission?.id === submission.id}
                  onSelect={setSelectedSubmission}
                />
              ))}
            </div>
          </div>

          <SubmissionDetail
            submission={selectedSubmission}
            onAnalyze={handleAnalyze}
            onUpload={handleUpload}
            onOverride={handleOverride}
            analyzing={analyzingId === selectedSubmission?.id}
          />
        </section>
      ) : null}

      {tab === "chat" ? <ChatPanel selectedSubmission={selectedSubmission} onAsk={askAssistant} /> : null}
    </main>
  );
}
