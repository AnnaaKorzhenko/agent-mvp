# Requests to the local-model track

These come from the MCP server track. Nothing here changes your code. The MCP server keeps working on a fallback until each request is decided.

| # | Request | Why | MCP fallback until then |
|---|---|---|---|
| 1 | A stable, general entry point such as `complete(messages, system=None, max_tokens, temperature) -> {text, usage, model}` in your client module | The MCP adapter currently calls `summarizer.client.chat.completions.create(model=summarizer.MODEL, …)` directly, so it depends on internals | `brain/integrations/local_models.py` wraps `summarizer.client` and `summarizer.MODEL`, with the assumptions marked in the code |
| 2 | Embeddings plan: will the pipeline compute embeddings (`fastembed` is in `requirements-pipeline.txt`)? If so: which model, how many dimensions, and where they're stored (`posts.embedding`?) | Semantic search (phase 5). Private text must be embedded locally. | Text index search only |
| 3 | Store `published_at` as a BSON date (or confirm it will always be an ISO-8601 UTC string in one format) | Date filters on strings depend on the format never changing | String comparison on ISO strings, validated by the contract test |
| 4 | Write `sensitivity` on new posts and digests (`public` / `internal` / `confidential` / `restricted`) once private sources arrive | Filtering for cloud clients is enforced from this label | A missing label is treated as `internal` |
| 5 | Tell us before renaming or removing a field listed in `docs/DATA_CONTRACT.md` | The contract test will flag it, but knowing early avoids breakage | The contract test reports the change |
| 6 | Where will the local runtime live (the always-on machine?) and what is its base URL and auth? | `LocalPipelineBackend` configuration | `LLM_BASE_URL` as read by your module |
