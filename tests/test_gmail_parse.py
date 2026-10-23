import base64

from autopilot.ingest.gmail_client import GmailClient, parse_message, safe_filename, search_label


def b64(s: bytes | str) -> str:
    if isinstance(s, str):
        s = s.encode()
    return base64.urlsafe_b64encode(s).decode().rstrip("=")


SAMPLE = {
    "id": "18f00aa",
    "internalDate": "1792700000000",
    "labelIds": ["INBOX", "Label_1"],
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": [{"name": "Subject", "value": "Acme SWE intern"}, {"name": "From", "value": "Me <me@gmail.com>"}],
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": b64("Look: https://jobs.lever.co/acme/123\n\nSent from my iPhone")},
                    },
                    {
                        "mimeType": "text/html",
                        "body": {"data": b64('<p>Look: <a href="https://jobs.lever.co/acme/123">here</a></p>')},
                    },
                ],
            },
            {"mimeType": "image/png", "filename": "story.png", "body": {"attachmentId": "ATT1", "size": 1000}},
            {"mimeType": "image/jpeg", "filename": "", "body": {"data": b64(b"\xff\xd8jpegdata")}},
        ],
    },
}


def test_parse_message_reads_text_html_and_images():
    email = parse_message(SAMPLE)
    assert email.message_id == "18f00aa"
    assert email.subject == "Acme SWE intern"
    assert "https://jobs.lever.co/acme/123" in email.body
    assert len(email.images) == 2
    assert email.images[0].attachment_id == "ATT1"
    assert email.images[1].data == b"\xff\xd8jpegdata"
    assert email.images[1].filename.startswith("image")


def test_search_query_uses_label_and_capture_address():
    client = GmailClient(None, None, "Jobs", "Jobs/Processed", "me+jobs@gmail.com")
    assert client.query() == "(label:jobs OR to:me+jobs@gmail.com) -label:jobs-processed"
    assert search_label("My Jobs/Processed") == "my-jobs-processed"


def test_safe_filename():
    assert safe_filename("../../etc/pass wd.png") == "pass_wd.png"


def test_same_named_screenshots_get_unique_names():
    msg = {
        "id": "x",
        "internalDate": "0",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [],
            "parts": [
                {"mimeType": "image/png", "filename": "image.png", "body": {"attachmentId": "A"}},
                {"mimeType": "image/png", "filename": "image.png", "body": {"attachmentId": "B"}},
            ],
        },
    }
    names = [a.filename for a in parse_message(msg).images]
    assert names == ["image.png", "image-2.png"]
