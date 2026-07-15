# Security and privacy

EchoVault is local-first. Canonical Markdown lives under the effective
`memory_home` (normally `~/.memory/vault/`) and the SQLite index stays beside
it. Cursor and Gemini MCP definitions contain an executable command and bound
arguments, never an API key or copied credential.

## Prompt, transcript, and hook handling

Curated policy explicitly forbids storing complete prompts, responses,
credentials, or **transcripts**. Gemini's `BeforeAgent` parser ignores the
transcript field and never opens the transcript path. It uses the validated
prompt only as an in-memory retrieval query, does not retain that prompt, and
writes only a short-lived digest claim for exactly-once injection. Hook errors
are fail-open and sanitized on stderr; public MCP errors omit internal paths.

## Embeddings and remote opt-in

Automatic context queries use local lexical search and local embeddings by
default. A configured remote provider is not sufficient to send the query.
The explicit configuration switch is:

```yaml
context:
  allow_remote_query_embeddings: true
```

With `allow_remote_query_embeddings` enabled, the provider receives only a
**redacted copy** of the retrieval query. Redaction is applied again at the
service boundary even if a caller supplies a query embedding override. Keep
the option false for fully local automatic retrieval and use Ollama when local
semantic search is desired.

## Filesystem and ownership boundaries

Client setup validates target roots and refuses symlink escapes. EchoVault uses
atomic writes, per-target locks, ownership manifests, file/entry/marked-block
hashes, and journaled tree swaps. Update and uninstall preserve unrelated user
configuration. `--force-managed` applies only to artifacts already claimed by
the EchoVault manifest; custom or unowned same-named content remains a hard
conflict.

Native Gemini installation deliberately retains the client's normal trust and
consent prompts. CI validates extension structure without accounts, API keys,
install, update, or uninstall operations. Authenticated client observations are
reported separately and must never include raw prompts, model output, paths,
tokens, environment values, or credentials.
