# Policy Document Ingestion & RAG Storage Technical Report

## Executive Summary

This report details the architectural design, implementation, and verification of the document ingestion pipeline for **SprintFlow**. The pipeline parses unstructured policy files (`.pdf`, `.docx`, `.md`), segments text using sliding-window chunking, generates dense vector embeddings, and stores indexed vectors in PostgreSQL using `pgvector`. Role-based access control (RBAC) is enforced at the chunk layer via audience tagging (`learner` vs. `internal_operator`) to guarantee strict security boundaries before context is provided to the LLM.

---

## 1. Document Loading (`document_loading`)

The ingestion pipeline supports multi-format parsing to extract raw text and structural metadata across diverse policy document types:

- **PDF Documents (`.pdf`)**: Processed using `pypdf`. Pages are extracted sequentially while preserving page numbers and heading headers. Used for `_ACC FAQs Presentation (editable).pdf`[cite: 1].
- **Word Documents (`.docx`)**: Parsed using `python-docx`. Paragraphs are mapped by heading styles (`Heading 1`, `Heading 2`) to maintain logical section context. Used for `Ops Circle Chatbot Scripts.docx`[cite: 2].
- **Markdown Files (`.md`)**: Parsed line-by-line or via AST header parsing to maintain clear section boundaries.

The loader (`app.services.document_ingestion.loaders`) outputs standardized document models containing raw text, document identifiers, section headers, and page references before passing data to the chunking engine.

---

## 2. Chunking Strategy (`chunking_strategy`)

To preserve semantic continuity without exceeding model context limits, text segmentation employs a recursive sliding-window strategy:

- **Target Chunk Size (`POLICY_CHUNK_SIZE`)**: `500` characters / tokens.
- **Overlap (`POLICY_CHUNK_OVERLAP`)**: `50` characters / tokens.
- **Splitting Logic**: Text is recursively split along structural boundaries (`\n\n`, `\n`, `. `, ` `) to avoid severing sentences mid-clause.
- **Header Context Retention**: Each chunk inherits the active section header or topic title to ensure standalone semantic completeness when embedded.

---

## 3. Embedding Choice (`embedding_choice`)

- **Model**: OpenAI `text-embedding-3-small` (routed via LiteLLM proxy).
- **Dimensions**: `1536` vector dimensions.
- **Normalization**: Embeddings are L2-normalized upon generation to enable efficient inner-product / cosine similarity search directly within PostgreSQL.

---

## 4. Vector Storage & Audience Separation (`storage_and_separation`)

Vector storage is integrated into the core PostgreSQL database via the `pgvector` extension and managed through Alembic migration `0003_add_policy_document_chunks.py`.

### Schema Definition (`policy_document_chunks`):

- `id` (UUID, Primary Key)
- `document_id` (VARCHAR / String)
- `content` (TEXT)
- `embedding` (VECTOR(1536))
- `metadata` (JSONB - storing `section_title`, `page_number`, `audience`, `content_hash`)
- `audience` (VARCHAR - indexed: `'learner'` or `'internal_operator'`)
- `created_at` (TIMESTAMP WITH TIMEZONE)

### Security & Audience Separation

To prevent unauthorized access to internal operational playbooks:

- Chunks are explicitly tagged during ingestion with `audience="learner"` or `audience="internal_operator"`.
- Downstream queries apply mandatory database filtering (`WHERE audience = :user_role`) during vector retrieval, ensuring internal operator details are pruned before context reaches the LLM context window.

---

## 5. Citation Metadata (`metadata_for_citation`)

Every stored chunk preserves granular provenance attributes within its JSONB metadata payload to support auditability and precise user-facing inline citations:

- `document_name`: Source file name (e.g., `_ACC FAQs Presentation (editable).pdf`)[cite: 1].
- `source_file_path`: Source path retained from document loading for complete provenance.
- `section_title`: Section or slide heading (e.g., `"What are the fees for the program?"`)[cite: 1].
- `page_number`: Exact page or slide index (e.g., Page `5`).
- `audience`: Targeted access scope (`learner` vs `internal_operator`).

---

## 6. Repeatability & Idempotency (`repeatability`)

To prevent duplicate records and redundant embedding API calls upon repeated script runs:

- **Chunk Hashing**: A deterministic SHA-256 hash is generated from the combined string `f"{document_id}:{content}"`.
- **Upsert Logic**: Ingestion checks existing hashes in `policy_document_chunks`. New items are inserted, updated content is refreshed, and identical existing chunks are skipped automatically.

---

## 7. Handling Document Removals (`handling_removals`)

To ensure stale or deleted policies do not persist in vector search results:

- **Pruning Function**: `delete_document(document_id: str)` purges all associated chunk records from `policy_document_chunks`.
- **Re-ingestion Workflow**: When a policy file is replaced, old records under that `document_id` are purged before new chunks are embedded and inserted.

---

## 8. Verification & Test Execution (`verification`)

The pipeline and schema integrity are verified via `scripts/verify_ingestion.py` executed within the `ai-core` container.

### Verification Execution Command:

```powershell
docker compose run --rm -e PYTHONPATH=/app --entrypoint /app/.venv/bin/python ai-core /app/scripts/verify_ingestion.py
```

The verification run completed successfully inside the container and passed all seven assertions:

1. **pgvector extension active** — the `vector` extension is installed and available.
2. **Ingestion table created** — `policy_document_chunks` exists in PostgreSQL.
3. **Audience isolation** — a semantic search with `audience="learner"` returns learner content only and filters out `internal_operator` chunks.
4. **Idempotent re-run** — running ingestion consecutively produces zero chunk growth; the total remains unchanged.
5. **Configured chunking** — `PolicyChunker` reads `POLICY_CHUNK_SIZE` and `POLICY_CHUNK_OVERLAP` from `app/core/config.py`.
6. **Source provenance** — every stored chunk retains `source_file_path` alongside `page_number` and `section_title`.
7. **Stale chunk pruning** — deleting a document removes its old chunks, after which the document can be re-ingested cleanly.

Representative output:

```text
✓ pgvector extension is active.
✓ policy_document_chunks table exists.
✓ chunker uses configured POLICY_CHUNK_SIZE and POLICY_CHUNK_OVERLAP.
✓ consecutive ingestion is idempotent (28 chunks).
✓ source file paths are stored with page and section provenance.
✓ learner semantic search excludes internal_operator chunks.
✓ document pruning removed stale chunks and restored the document.
All ingestion verification assertions passed!
```

## 9. Trade-Off Analysis (`trade_offs`)

The chunker uses a 50-character overlap with the default 500-character chunk size. This overlap preserves context across boundaries, which improves retrieval when a sentence or procedure spans two adjacent chunks. The cost is controlled duplication: for long sections, approximately 10% of the chunk text is repeated, increasing the number of embedding tokens and PostgreSQL rows compared with non-overlapping chunks.

The overlap is intentionally character-based because the current chunker operates on extracted text before tokenization. It is a practical approximation of token overlap, not an exact token budget. Increasing overlap improves boundary continuity but increases embedding API payload size, latency, and spend; decreasing it reduces those costs but can weaken retrieval for boundary-spanning answers. The 500/50 defaults balance citation granularity, embedding request size, and response latency. Changes should be validated against chunk counts, retrieval quality, and embedding API timings.

## 10. Downstream Retrieval Interface Contract (`retrieval_contract`)

Downstream RAG consumers provide the following explicit inputs:

- `query`: The user’s natural-language search query. The orchestration layer converts it to an embedding before vector search.
- `audience`: Required access scope, currently `learner` or `internal_operator`; results must never cross this boundary.
- `top_k`: Maximum number of results to return.
- `similarity_threshold`: Minimum accepted similarity score; results below this threshold are discarded.

Each returned result has this schema:

```json
{
  "content": "Retrieved chunk text",
  "document_id": "acc_faqs",
  "audience": "learner",
  "section_title": "What are the fees for the program?",
  "page_number": 5,
  "source_file_path": "/app/data/sample_policies/_ACC FAQs Presentation (editable).pdf",
  "similarity_score": 0.87
}
```

`source_file_path`, `page_number`, and `section_title` are retained in each chunk's metadata so downstream responses can produce complete, auditable citations. `similarity_score` is calculated by the vector retrieval adapter from the selected distance metric and is returned with the result rather than inferred by the caller.
