# OpenAI Evals fixtures (synthetic)

TEST FIXTURES ONLY. Small, hand-written, synthetic files in the shapes the OpenAI Evals
documentation describes; they are not exports of any real eval and hold no real data. Used by
`tests/test_p30_openai_import.py` and mirrored in
`studio-web/tests/unit/p30_openai_evals_import.test.tsx`.

| File | Shape |
| --- | --- |
| `tickets.jsonl` | `{"item": {...}}` per line (the documented upload format, `purpose="evals"`) |
| `tickets_eval.json` | an eval object with `data_source_config` and `testing_criteria` |
| `registry_samples.jsonl` | bare `{"input": [chat messages], "ideal": ...}` rows (open-source `openai/evals` registry) |
| `run_content.json` | an inline run source `{"type": "file_content", "content": [{"item", "sample"}]}` |
| `custom_fields.jsonl` | items with custom field names and a nested object |
| `malformed.jsonl` | lines 2, 3 and 4 are broken on purpose (bad JSON, not an object, no input) |
