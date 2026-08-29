"""Route-level tests for POST /assets/{id}/chat (app/routers/dashboard.py's
asset_chat), the app's only async route handler and its only file-upload
surface. Focus is the attachment validation ladder (lines ~1465-1494 at time
of writing): file count, declared size, actual size, cumulative total,
content-type allowlist, then a magic-byte check -- six sequential rejection
branches that were entirely untested before this file existed.

assistant.run_chat_turn is monkeypatched in every test: it's a real, billable
Anthropic API call (see app/assistant.py), the same way
tests/test_dashboard_helpers.py stubs assistant.guess_model_number. A spy
records whether/how it was called, so a rejected-attachment test can assert
the API was never reached, not just that an error string appeared.

Starlette's TestClient drives this async route synchronously -- no
pytest-asyncio or async test function needed.
"""

from conftest import make_asset


def _stub_run_chat_turn(monkeypatch, calls, error=None):
    # is_configured() gates whether _chat_panel_context even shows an `error`
    # string in the rendered template (see app/templates/_chat_panel.html's
    # `{% if not configured %}` branch) -- forced True so the validation-
    # ladder rejection text in these tests is actually visible to assert on,
    # the same way tests/test_dashboard_helpers.py stubs it.
    monkeypatch.setattr("app.assistant.is_configured", lambda: True)

    def _fake(session, asset, message, attachments=()):
        calls.append({"message": message, "attachments": list(attachments)})
        return error

    monkeypatch.setattr("app.assistant.run_chat_turn", _fake)


def test_missing_asset_redirects_without_calling_the_assistant(admin_client, monkeypatch):
    calls = []
    _stub_run_chat_turn(monkeypatch, calls)

    resp = admin_client.post("/assets/999999/chat", data={"message": "hello"}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/assets"
    assert calls == []


def test_message_only_reaches_the_assistant(admin_client, session, monkeypatch):
    asset = make_asset(session)
    calls = []
    _stub_run_chat_turn(monkeypatch, calls)

    resp = admin_client.post(f"/assets/{asset.id}/chat", data={"message": "what is this?"})

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["message"] == "what is this?"
    assert calls[0]["attachments"] == []


def test_an_untouched_file_picker_does_not_block_a_message_only_send(admin_client, session, monkeypatch):
    # A browser form with a file <input> the user never touched still posts
    # one part for it, with an empty filename -- `files = [f for f in files
    # if f.filename]` is what filters that out before the count/size/type
    # ladder ever sees it.
    #
    # Built as a raw multipart body, not httpx's `files=` kwarg: httpx
    # silently drops an empty-string filename down to a plain form field
    # rather than a file part (confirmed by hand -- the handler then sees a
    # `str` where it expects an `UploadFile` and 422s), so it can't produce
    # the shape a real browser sends for an untouched <input type="file">.
    asset = make_asset(session)
    calls = []
    _stub_run_chat_turn(monkeypatch, calls)

    body = (
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="message"\r\n\r\n'
        b'no attachment really\r\n'
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="files"; filename=""\r\n'
        b'Content-Type: application/octet-stream\r\n\r\n'
        b'\r\n'
        b'--BOUNDARY--\r\n'
    )
    resp = admin_client.post(
        f"/assets/{asset.id}/chat",
        content=body,
        headers={"content-type": "multipart/form-data; boundary=BOUNDARY"},
    )

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["attachments"] == []


def test_blank_message_and_no_attachments_never_calls_the_assistant(admin_client, session, monkeypatch):
    asset = make_asset(session)
    calls = []
    _stub_run_chat_turn(monkeypatch, calls)

    resp = admin_client.post(f"/assets/{asset.id}/chat", data={"message": "   "})

    assert resp.status_code == 200
    assert calls == []


def test_too_many_files_is_rejected_before_reading_any_of_them(admin_client, session, monkeypatch):
    asset = make_asset(session)
    calls = []
    _stub_run_chat_turn(monkeypatch, calls)

    files = [
        ("files", (f"receipt{i}.png", b"\x89PNG\r\n\x1a\nrest", "image/png"))
        for i in range(6)  # over _CHAT_ATTACHMENT_MAX_FILES (5)
    ]
    resp = admin_client.post(f"/assets/{asset.id}/chat", data={"message": "many files"}, files=files)

    assert resp.status_code == 200
    assert "Attach at most 5 files at once." in resp.text
    assert calls == []


def test_oversized_file_is_rejected_on_actual_byte_count(admin_client, session, monkeypatch):
    # Lowers the module-level cap rather than uploading a real 15MB file --
    # the len(data) check is the authoritative backstop regardless of what
    # threshold it's compared against.
    import app.routers.dashboard as dashboard_module

    monkeypatch.setattr(dashboard_module, "_CHAT_ATTACHMENT_MAX_BYTES", 10)
    asset = make_asset(session)
    calls = []
    _stub_run_chat_turn(monkeypatch, calls)

    resp = admin_client.post(
        f"/assets/{asset.id}/chat",
        data={"message": "big file"},
        files=[("files", ("big.png", b"\x89PNG\r\n\x1a\n" + b"x" * 20, "image/png"))],
    )

    assert resp.status_code == 200
    assert "larger than" in resp.text
    assert calls == []


def test_cumulative_total_across_files_is_rejected(admin_client, session, monkeypatch):
    import app.routers.dashboard as dashboard_module

    # Each file individually under the cap, but two together over it.
    monkeypatch.setattr(dashboard_module, "_CHAT_ATTACHMENT_MAX_BYTES", 12)
    asset = make_asset(session)
    calls = []
    _stub_run_chat_turn(monkeypatch, calls)

    one = b"\x89PNG\r\n\x1a\n"  # 8 bytes, valid PNG magic, under the 12-byte cap alone
    resp = admin_client.post(
        f"/assets/{asset.id}/chat",
        data={"message": "two files"},
        files=[
            ("files", ("a.png", one, "image/png")),
            ("files", ("b.png", one, "image/png")),
        ],
    )

    assert resp.status_code == 200
    assert "total more than" in resp.text
    assert calls == []


def test_disallowed_content_type_is_rejected(admin_client, session, monkeypatch):
    asset = make_asset(session)
    calls = []
    _stub_run_chat_turn(monkeypatch, calls)

    resp = admin_client.post(
        f"/assets/{asset.id}/chat",
        data={"message": "a zip file"},
        files=[("files", ("archive.zip", b"PK\x03\x04rest", "application/zip"))],
    )

    assert resp.status_code == 200
    assert "only JPEG/PNG/WebP/GIF images and PDFs are accepted" in resp.text
    assert calls == []


def test_content_type_not_matching_magic_bytes_is_rejected(admin_client, session, monkeypatch):
    # Claims image/png in the multipart part but the bytes aren't a PNG --
    # the _CHAT_ATTACHMENT_MAGIC check exists precisely so the browser-
    # supplied content_type alone is never trusted.
    asset = make_asset(session)
    calls = []
    _stub_run_chat_turn(monkeypatch, calls)

    resp = admin_client.post(
        f"/assets/{asset.id}/chat",
        data={"message": "fake png"},
        files=[("files", ("fake.png", b"not actually a png", "image/png"))],
    )

    assert resp.status_code == 200
    # Jinja autoescapes by default, so the apostrophes come back as &#39;.
    assert "doesn&#39;t look like a valid image/png file" in resp.text
    assert calls == []


def test_valid_attachment_reaches_the_assistant_with_its_bytes_intact(admin_client, session, monkeypatch):
    asset = make_asset(session)
    calls = []
    _stub_run_chat_turn(monkeypatch, calls)

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"rest-of-file"
    resp = admin_client.post(
        f"/assets/{asset.id}/chat",
        data={"message": "here's a photo"},
        files=[("files", ("photo.png", png_bytes, "image/png"))],
    )

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["message"] == "here's a photo"
    assert calls[0]["attachments"] == [("photo.png", "image/png", png_bytes)]


def test_an_error_returned_by_the_assistant_is_rendered(admin_client, session, monkeypatch):
    asset = make_asset(session)
    calls = []
    _stub_run_chat_turn(monkeypatch, calls, error="The assistant is not configured.")

    resp = admin_client.post(f"/assets/{asset.id}/chat", data={"message": "hello"})

    assert resp.status_code == 200
    assert "The assistant is not configured." in resp.text
    assert len(calls) == 1
