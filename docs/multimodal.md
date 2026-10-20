# Images, documents and audio

Wherever you pass a prompt, you can pass a list that mixes text and `Attachment` objects. An `Attachment` is a file given to the model: either inline bytes (sent base64-encoded) or a URL the provider downloads itself. Which kinds of file a model accepts depends on the provider. A file the provider cannot take is rejected with a clear error before any request is sent.

## A prompt with an attachment

```python
from nexura_agents import Agent, Attachment, FakeModel

png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 16  # stand-in for real image bytes

model = FakeModel(responses=["It is a picture."])
agent = Agent(name="Vision", instructions="Describe images.", model=model)
result = agent.run(
    ["What is in this image?", Attachment.from_bytes(png, "image/png", name="pic.png")]
)
assert result.output == "It is a picture."

# The model received one user message holding the text and the attachment.
user = [m for m in model.calls[0]["messages"] if m.role == "user"][0]
assert user.content == "What is in this image?"
assert user.attachments[0].kind == "image"
assert user.attachments[0].mime_type == "image/png"
```

In a list prompt, strings become the message text (several strings are joined with a blank line) and attachments ride along on the same message. A list holds either text and attachments, or `Message` objects, not both. Attachments are saved with sessions and paused runs.

## Creating attachments

There are three constructors.

```python
from pathlib import Path

from nexura_agents import Attachment

# From a file. The MIME type is guessed from the extension.
Path("invoice.pdf").write_bytes(b"%PDF-1.4 fake")
pdf = Attachment.from_path("invoice.pdf")
assert pdf.mime_type == "application/pdf" and pdf.kind == "document"
assert pdf.name == "invoice.pdf"

# From bytes you already have. The MIME type is required; name is optional.
note = Attachment.from_bytes(b"hello", "text/plain", name="note.txt")
assert note.kind == "text" and note.size_bytes == 5

# From a URL the provider fetches itself: https://, http:// or gs://.
photo = Attachment.from_url("https://example.com/cat.jpg")
assert photo.mime_type == "image/jpeg" and photo.size_bytes is None
audio = Attachment.from_url("gs://bucket/call", mime_type="audio/wav")
assert audio.kind == "audio"
```

`from_path` also knows `.webp`, `.md` and `.heic`. When the type cannot be guessed (an unknown extension), pass `mime_type=` explicitly; otherwise `SDKValidationError` is raised. `from_url` guesses from the URL path, ignoring the query string, and needs `mime_type=` for URLs without a recognisable extension. `kind` is `"image"`, `"audio"`, `"video"` or `"text"` for those MIME majors and `"document"` for anything else (such as PDF).

An `Attachment` is immutable. `to_dict()` and `Attachment.from_dict()` convert to and from a plain dict, which is how attachments are stored in sessions.

## Size limits

Inline attachments (`from_path` and `from_bytes`) are limited to 20 MiB, because the base64 data is sent in every request and providers reject larger bodies. `from_path` checks the file size before it reads the file, and refuses anything that is not a regular file. For larger files, use `Attachment.from_url`.

```python
from nexura_agents import Attachment
from nexura_agents.errors import SDKValidationError

too_big = b"\x00" * (20 * 1024 * 1024 + 1)
try:
    Attachment.from_bytes(too_big, "application/pdf", name="big.pdf")
except SDKValidationError as exc:
    assert "limited to" in str(exc)
else:
    raise AssertionError("expected a size error")

try:
    Attachment.from_url("ftp://example.com/file.pdf")
except SDKValidationError:
    pass
else:
    raise AssertionError("expected a scheme error")
```

Budgets reserve extra tokens for attachments (an estimate: 1,600 tokens per image; for other types about a quarter of the inline size in bytes, at least 1,000, or 4,000 for a URL), so a [budget](budgets-and-usage.md) accounts for them before the request is sent.

## URLs are redacted

Presigned URLs carry secrets in the query string or in `user:password@host`. `repr()` of an attachment drops the user info, query and fragment, and never prints base64 data:

```python
from nexura_agents import Attachment

a = Attachment.from_url("https://user:pw@files.example.com/a/report.pdf?X-Signature=abc123")
text = repr(a)
assert "abc123" not in text and "pw" not in text
assert "files.example.com/a/report.pdf?..." in text

b = Attachment.from_bytes(b"x" * 100, "image/png")
assert "100 bytes inline" in repr(b)
```

The real URL is still sent to the provider; only the printed form is redacted. Treat `to_dict()` output and saved sessions as sensitive if your URLs are presigned.

## What each provider accepts

| Provider | Accepts |
|---|---|
| OpenAI | JPEG, PNG, GIF and WebP images (inline or URL); PDF (inline only); WAV and MP3 audio (inline only); text files (inline, sent as text). Anything else is rejected. |
| Anthropic | JPEG, PNG, GIF and WebP images and PDFs (inline or URL); text files (inline). Anything else is rejected. |
| Gemini and Vertex AI | Any MIME type inline. URLs are sent as file references: `gs://` and HTTPS on Vertex AI, File API URIs on the Gemini API. The service itself decides what it can read. |
| Sarvam | Text only. Any attachment is rejected. |

This follows `providers/multimodal.py` in the SDK. The rejection happens in the adapter, so you can see it without a network call by using the helper functions directly:

```python
from nexura_agents import Attachment
from nexura_agents.providers.errors import CapabilityError
from nexura_agents.providers.multimodal import anthropic_blocks, gemini_parts, openai_content_parts

pdf = Attachment.from_bytes(b"%PDF-1.4", "application/pdf", name="a.pdf")
wav_url = Attachment.from_url("https://example.com/a.wav")

# Anthropic sends a PDF as a base64 document block.
assert anthropic_blocks([pdf])[0]["type"] == "document"
# Gemini passes inline data through and URLs as fileData.
assert "inlineData" in gemini_parts([pdf])[0]
assert "fileData" in gemini_parts([wav_url])[0]

# OpenAI only takes audio inline, so a URL is refused before any request.
try:
    openai_content_parts("Transcribe", [wav_url])
except CapabilityError as exc:
    assert "OpenAI cannot accept" in str(exc)
else:
    raise AssertionError("expected a CapabilityError")
```

These helpers are lower-level than the rest of the SDK; in normal use you only build attachments and the provider adapter calls them.

## Tools that return images and files

A tool can hand the model a file: a chart it drew, a page it rendered, a document it
fetched. Return a `ToolOutput(text, attachments)`, an `Attachment`, or a list that mixes
text and attachments:

```python
from nexura_agents import Agent, Attachment, FakeModel, Runner, ToolCall, ToolOutput, tool

chart_png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 16  # stand-in for real image bytes


@tool
def plot_sales(region: str) -> ToolOutput:
    """Plot monthly sales for a region."""
    chart = Attachment.from_bytes(chart_png, "image/png", name="sales.png")
    return ToolOutput(f"Sales in {region} rose 12% in Q3.", [chart])


model = FakeModel(
    responses=[[ToolCall(id="c1", name="plot_sales", arguments={"region": "West"})], "Up 12%."]
)
agent = Agent(name="Analyst", instructions="Explain charts.", model=model, tools=[plot_sales])
Runner().run(agent, "How did the West do?")

sent = model.calls[1]["messages"]
assert sent[-2].role == "tool" and sent[-2].content == "Sales in West rose 12% in Q3."
assert sent[-1].role == "user" and sent[-1].attachments[0].name == "sales.png"
```

- The text becomes the tool result. If there is no text, it is a short note naming the
  files.
- The files follow in one user message after the whole batch of tool results. Every
  provider accepts text tool results, and multimodal models read the files like any
  other attachment, with the same per-provider rules as above. With parallel calls the
  tool results stay together and all files come in that one message.
- A model whose `capabilities.can_multimodal` is `False` (for example Sarvam) gets a
  short description of the files instead, so the run continues.
- The follow-up message counts as part of the current turn for history processors.
  Cached calls (`@tool(cache=True)`) show their files again.
- Files are sent only after the tool's output guardrails pass. If a guardrail rejects or
  rewrites the result, its files are withheld too.
- `SchemaTool` handlers (MCP, OpenAPI, your own) can return files the same way.

## A real call

Using a provider needs credentials, so this block is not run by the docs tests.

```python notest
from nexura_agents import Agent, Attachment

agent = Agent(name="Claims", instructions="Check claims.", model="vertex:gemini-2.5-flash")
result = agent.run(
    [
        "Does this receipt match the claim form? Answer yes or no and why.",
        Attachment.from_path("receipt.jpg"),
        Attachment.from_path("claim-form.pdf"),
        Attachment.from_url("gs://claims-bucket/call-recording.wav"),
    ]
)
print(result.output)
```

## See also

- [Agents](agents.md)
- [Models and providers](providers.md)
- [Budgets and usage](budgets-and-usage.md)
- [Testing your agents](testing.md)
- [Troubleshooting](troubleshooting.md)

## Images, documents and audio

Pass a list mixing text and attachments wherever you'd pass a prompt:

```python notest
from nexura_agents import Agent, Attachment

agent = Agent(name="Claims", instructions="Check claims.", model="vertex:gemini-2.5-flash")
result = agent.run(
    [
        "Does this receipt match the claim form? Answer yes or no and why.",
        Attachment.from_path("receipt.jpg"),
        Attachment.from_path("claim-form.pdf"),
        Attachment.from_url("gs://claims-bucket/call-recording.wav"),
    ]
)
```

| Provider | Accepts |
|---|---|
| Gemini, Vertex AI | Images, PDFs, audio, video, text — inline, or `gs://` URLs on Vertex |
| OpenAI | JPEG/PNG/GIF/WebP images (inline or URL), PDF and WAV/MP3 (inline), text files |
| Anthropic | JPEG/PNG/GIF/WebP images and PDFs (inline or URL), text files |
| Sarvam | Text only |

Anything a provider can't take fails with a clear error before a request is sent. Inline
files are capped at 20 MB; use `Attachment.from_url` for bigger ones. Attachments are
saved with conversations and paused runs, and budgets reserve extra tokens for them.
