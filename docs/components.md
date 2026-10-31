# Offline builder components

These 30 versioned modules run locally without models, credentials or network access.
Importing `nexura_runtime.modules` registers them on `default_registry`. All have version
`1.0.0`, kind `data_transform`, effect policy `read`, empty config and display examples.
Input options belong in the node input mapping; config is `{}`.

## Run a component

```python
import asyncio
from nexura_agents.contracts import ExecutionContext
from nexura_runtime.modules import default_registry

async def main():
    module = default_registry.get("nexura.text.clean", "1.0.0")
    result = await module.execute(
        ExecutionContext(run_id="offline-example", node_id="clean"),
        {"text": "  Hello   world  "},
        {},
    )
    assert result.outputs == {"text": "Hello world"}

asyncio.run(main())
```

## Catalog

Each example is the input object for `module.execute(context, inputs, {})`.
Fields in parentheses are optional. Output fields are always present.
The API descriptors expose full schemas and the same executable examples.

### Text

| Module id | Behavior | Inputs | Outputs | Example input |
| --- | --- | --- | --- | --- |
| `nexura.text.split` | Split by separator, line, sentence or overlapping fixed character windows. | text, (mode), (separator), (size), (overlap) | items | `{"text":"a,b","separator":","}` |
| `nexura.text.join` | Combine text items with a separator. | items, (separator) | text | `{"items":["hello","world"],"separator":" "}` |
| `nexura.text.replace` | Replace at most max_replacements occurrences; regex replacements support groups. | text, pattern, replacement, (mode), (max_replacements) | text | `{"text":"hello world","pattern":"world","replacement":"Nexura"}` |
| `nexura.text.regex_extract` | Extract whole matches or named group objects (unmatched groups are null). | text, pattern, (mode), (max_matches) | matches | `{"text":"order 42","pattern":"[0-9]+"}` |
| `nexura.text.case` | Convert text case, splitting camel case and acronym boundaries for identifiers. | text, mode | text | `{"text":"Hello World","mode":"snake"}` |
| `nexura.text.clean` | NFC normalization, control removal (preserving tabs/newlines), whitespace collapse and trim. | text, (trim), (collapse_whitespace), (strip_controls), (normalize_unicode) | text | `{"text":"  Hello   world  "}` |
| `nexura.text.truncate` | Keep limit characters or words and append ellipsis only if truncated. | text, (mode), limit, (ellipsis) | text, truncated | `{"text":"hello world","limit":5}` |
| `nexura.text.count` | Count Unicode characters, whitespace words, splitlines and ceil(chars/4) approximate tokens. | text | chars, words, lines, approximate_tokens | `{"text":"hello world"}` |
| `nexura.text.html_to_text` | Extract text and decode entities; drop script/style contents and separate blocks. | text | text | `{"text":"<p>Hello <b>world</b></p>"}` |
| `nexura.text.markdown_to_text` | Remove common Markdown markers; keep link labels, image alt text and code contents. | text | text | `{"text":"# Hello **world**"}` |

### Data

| Module id | Behavior | Inputs | Outputs | Example input |
| --- | --- | --- | --- | --- |
| `nexura.data.json_parse` | Parse strict JSON text to a JSON value; reject NaN and Infinity. | text | value | `{"text":"{\"answer\": 42}"}` |
| `nexura.data.json_stringify` | Serialize Unicode JSON compactly or with two-space indentation. | value, (pretty), (sort_keys) | text | `{"value":{"answer":42},"pretty":true}` |
| `nexura.data.csv_parse` | Parse a unique nonempty header and equal-width string rows; reject overflow. | text, (delimiter), (max_rows) | items | `{"text":"name,score\nAda,10\n"}` |
| `nexura.data.csv_build` | Build CSV with supplied columns or sorted union of row keys; missing cells are empty. | items, (delimiter), (fields) | text | `{"items":[{"name":"Ada","score":10}]}` |
| `nexura.data.filter` | Keep matching items in order; omit field to compare each whole item. | items, (field), operator, value | items | `{"items":[{"score":2},{"score":7}],"field":"score","operator":"gte","value":5}` |
| `nexura.data.sort` | Stable ascending/descending sort on scalar values or dotted fields; require comparable keys. | items, (field), (order) | items | `{"items":[{"score":7},{"score":2}],"field":"score"}` |
| `nexura.data.pick` | Keep dotted fields as a nested object; missing fields become null. | data, fields | data | `{"data":{"user":{"name":"Ada","age":20}},"fields":["user.name"]}` |
| `nexura.data.set_field` | Copy an object and set a dotted path, creating intermediate objects. | data, field, value | data | `{"data":{},"field":"user.name","value":"Ada"}` |
| `nexura.data.flatten` | Flatten nested object keys to dotted paths or unflatten; arrays stay intact. | data, (mode) | data | `{"data":{"user":{"name":"Ada"}}}` |
| `nexura.data.dedupe` | Keep first item per field (or whole value); compare canonical JSON including value types. | items, (field) | items | `{"items":[{"id":1,"name":"Ada"},{"id":1,"name":"Other"}],"field":"id"}` |
| `nexura.data.group_by` | Group by a dotted field, in first-seen order; emit typed keys, lists and counts. | items, field | groups | `{"items":[{"team":"A"},{"team":"A"}],"field":"team"}` |

### Logic

| Module id | Behavior | Inputs | Outputs | Example input |
| --- | --- | --- | --- | --- |
| `nexura.logic.if_else` | Evaluate a condition and select the true or false workflow branch. | left, right, operator | result, route | `{"left":5,"right":3,"operator":"gt"}` |
| `nexura.logic.switch` | Choose first matching case (whole data or dotted field), otherwise default_route. | data, cases, (default_route) | route | `{"data":5,"cases":[{"operator":"gt","value":3,"route":"large"}]}` |
| `nexura.logic.compare` | Compare left and right with a classify operator, including guarded regex. | left, right, operator | result | `{"left":5,"right":3,"operator":"gt"}` |

### Utilities

| Module id | Behavior | Inputs | Outputs | Example input |
| --- | --- | --- | --- | --- |
| `nexura.utils.math` | Interpret numeric arithmetic and comparisons with min/max/round/abs; exponent cap 16. | expression, (variables) | value | `{"expression":"round(price * 1.2, 2)","variables":{"price":10}}` |
| `nexura.utils.datetime` | Parse/format ISO dates, apply durations and convert zones; now requires explicit or context ISO time. | operation, (text), (format), (timezone), (duration) | text, iso | `{"operation":"add","text":"2026-01-01T00:00:00Z","duration":{"days":1}}` |
| `nexura.utils.hash` | Compute a SHA-256, SHA-1 or MD5 hexadecimal fingerprint (not password hashing). | text, (algorithm) | digest | `{"text":"hello"}` |
| `nexura.utils.base64` | Encode UTF-8 text or strictly decode padded Base64 to UTF-8; optional URL-safe alphabet. | text, operation, (url_safe) | text | `{"text":"hello","operation":"encode"}` |
| `nexura.utils.url` | Parse URL parts or build with encoded query pairs, preserving duplicate keys and blanks. | operation, (url), (scheme), (netloc), (path), (query), (fragment) | url, parts | `{"operation":"parse","url":"https://example.test/search?q=hello+world"}` |
| `nexura.utils.uuid` | Derive UUID5 from a namespace UUID (or dns/url/oid/x500) and a UTF-8 name. | namespace, name | uuid | `{"namespace":"dns","name":"example.test"}` |

## Options and semantics

- Text splitting defaults to separator mode with `,`. Line splitting uses `splitlines()`;
  sentence splitting is a punctuation/whitespace heuristic. Fixed windows default to
  size 1000 and overlap 0; overlap must be smaller than size.
- Join defaults to an empty separator. Replace defaults to literal mode and at most 1024
  replacements; set `mode="regex"` for patterns. Regex replacements accept `\1`,
  `\g<name>` / `\g<0>`, `\n`, `\r`, `\t` and `\\`. Invalid escapes fail.
  Extraction defaults to all whole matches, with a maximum of 1024; named mode emits
  objects with null for unmatched groups. Exceeding the match cap fails rather than silently dropping data.
- Case supports lower, upper, title, snake, kebab and camel. Identifier modes split ASCII
  camel/acronym boundaries and Unicode word boundaries. Clean enables all four options
  by default: NFC, remove control characters except tabs/newlines, collapse whitespace, trim.
- Truncate defaults to character mode and appends `…` outside the retained limit only when
  truncation occurs; word mode joins retained words with spaces. Counts use Unicode code
  points, whitespace words and `splitlines()`. Approximate tokens are `ceil(chars/4)`,
  independent of any model tokenizer.
- HTML parsing drops script/style contents, decodes entities and separates common block tags.
  Markdown extraction supports headings, lists, blockquotes, inline emphasis/code, fenced
  code, inline links and images, then extracts HTML text. It is a text extraction heuristic,
  not a complete CommonMark renderer or an HTML sanitizer.
- JSON parsing rejects nonstandard NaN/Infinity; duplicate keys follow Python JSON behavior
  (last wins). Stringify defaults to compact, unsorted Unicode JSON; pretty uses two spaces.
- CSV defaults to comma delimiters and a 1024-row cap. Headers must be unique and nonempty,
  rows must match header width. Build uses supplied fields or their sorted union, fills
  absent cells with empty text, ignores columns outside supplied fields and rejects nested
  cells. Empty input lists produce empty CSV unless fields are supplied. CSV is a data format;
  text values are preserved verbatim, including spreadsheet formula prefixes.
- Filter, compare, if_else and switch share classify operators: `eq`, `ne`, `gt`, `gte`,
  `lt`, `lte`, `in`, `contains`, `regex`. Ordered comparisons require compatible types.
  Missing fields compare as null; operators preserve existing classify equality semantics
  (including Python numeric/bool equality). Regex comparisons require a string pattern.
- Sort defaults to ascending and is stable in either direction. Sort keys must be
  comparable non-null scalar values. Pick reconstructs nested objects and supplies null
  for absent fields. Set field copies inputs and refuses to descend through scalars.
- Dotted paths navigate dictionaries, not array indexes, and require 1–24 nonempty
  segments and at most 256 characters. Flatten leaves arrays and empty objects intact;
  nested keys containing dots or empty keys are rejected. Unflatten rejects colliding
  parent/child paths. Dedupe keeps the first item using typed canonical JSON equality;
  group_by returns first-seen groups as `{key, items, count}` to avoid string-key collisions.
- Math permits numeric literals/variables, unary signs, `+ - * / // % **`, numeric
  comparisons and `min/max/round/abs`. It interprets AST nodes and never executes Python.
  Expressions are at most 1024 characters / 128 AST nodes, exponents have magnitude at most
  16, intermediate magnitudes are at most `1e100`, and round digits range from -16 to 16.
  Booleans are permitted as comparison results, not arithmetic operands.
- Datetime treats naive ISO times as UTC. Add/subtract accepts days/hours/minutes/seconds
  (each within ±36600), then optionally converts to an IANA timezone through `zoneinfo`.
  Zone rules must be installed on the host; no arbitrary files are accepted. Format permits
  only `%Y %m %d %H %M %S %f %z %j %w %u %V %G %%` (no locale-dependent directives).
  `ExecutionContext` has no wall-clock API: `now` requires explicit ISO `text` or the
  caller-injected ISO string `context.attributes["now"]`. Missing clock input fails;
  the component never reads the current time. Pin host timezone data for replay across hosts.
- Hash defaults to SHA-256; SHA-1/MD5 are fingerprints, not password or authentication hashes.
  Base64 operates on UTF-8 text; decoding requires valid padded Base64 and valid UTF-8.
  URL-safe encoding uses `-`/`_`. URL queries use ordered `{key, value}` pairs, preserving
  duplicates and blanks; build percent-encodes query values. URL parsing performs no requests.
- UUID always uses UUID5. Namespaces are UUID strings or `dns`, `url`, `oid`, `x500`.

## Routing in workflows

`if_else` reports `outputs["route"]` as `"true"` or `"false"` and sets
`ModuleResult.branch` to that same value. `switch` sets both to its first matching case route
or `default_route` (default `"default"`). This preserves classify-style inspectable route
output while using the engine branch contract: the workflow engine reads `result.branch`
(or the legacy `_branch` output), not an ordinary `route` output.
Wire those branch names through `WorkflowNode.transitions`, and use `output_mapping` to
save only the desired fields. A switch case may omit `field` to compare the whole data value.
Compare returns a boolean and does not branch.

## Resource limits and failures

Every direct invocation and registry execution validates input and config. Inputs are
JSON-only, finite, valid UTF-8, at most 131072 serialized bytes (also subject to a conservative
preflight size check), 24 levels deep and 16384 visited values. Lists and objects have at
most 1024 entries; text fields have at most 65536 characters. Cycles and integers above
4096 bits fail. Outputs are checked against 262144 bytes and the same structural bounds;
individual text outputs are capped at 65536 characters. Output expansion fails explicitly.

Modules declare a 2-second timeout and check cancellation/deadlines through the execution
context before and after processing. Work is bounded by the limits above; the synchronous
non-regex operations do not offer preemptive interruption mid-operation. Regex patterns
are capped at 512 characters, with an interruptible 50ms matching budget per operation
(shared across collection comparisons). Invalid patterns and timeouts raise
`SchemaValidationError`. Syntax, missing operation-dependent inputs, malformed encodings,
unrepresentable results and resource overflows also raise clear schema errors.

See [Workflows](workflows.md) for engine wiring and [Service and Studio](studio-and-service.md)
for the module descriptor API and palette. These checks establish offline deterministic
behavior; they do not validate any live model or claim production readiness.
