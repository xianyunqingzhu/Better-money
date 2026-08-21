"""Upload validation: size limits, image signatures, statement allowlists."""
from io import BytesIO
import re

import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
EXE = b"MZ\x90\x00" + b"\x00" * 64

NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|jpeg|webp)$")


@pytest.fixture
def no_ai(monkeypatch):
    monkeypatch.setattr(
        "app.ai.parse_image",
        lambda path, note, date: {"items": [], "questions": []},
    )


def _upload(client, name, content, content_type="application/octet-stream", endpoint="/api/upload_images", data=None):
    return client.post(
        endpoint,
        files=[("files", (name, BytesIO(content), content_type))],
        data=data or {},
    )


@pytest.mark.parametrize("name,content,expected_ext", [
    ("xiaopiao.png", PNG, "png"),
    ("ticket.jpg", JPEG, "jpg"),
    ("pic.webp", WEBP, "webp"),
])
def test_valid_images_are_saved_with_generated_names(
        client, no_ai, name, content, expected_ext):
    response = _upload(client, name, content)
    assert response.status_code == 200, response.text
    images = response.json().get("images") or []
    assert images, "accepted image should be stored"
    for path in images:
        basename = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        assert NAME_RE.match(basename), basename
        assert "evil-name" not in basename
        assert expected_ext in basename


@pytest.mark.parametrize("name,content", [
    ("anim.gif", GIF),
    ("virus.exe", EXE),
    ("photo.png", b"plain text, not an image"),
])
def test_invalid_image_types_are_rejected(client, no_ai, name, content):
    response = _upload(client, name, content)
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_file_type"


def test_empty_image_returns_stable_error(client, no_ai):
    response = _upload(client, "empty.png", b"")
    assert response.status_code == 400
    assert response.json()["error"] == "empty_file"


def test_oversized_image_returns_413(client, no_ai):
    response = _upload(client, "big.png", PNG + b"\x00" * (10 * 1024 * 1024))
    assert response.status_code == 413
    assert response.json()["error"] == "file_too_large"


def test_more_than_ten_images_returns_too_many(client, no_ai):
    files = [("files", (f"img{i}.png", BytesIO(PNG), "image/png"))
             for i in range(11)]
    response = client.post("/api/upload_images", files=files, data={})
    assert response.status_code == 400
    assert response.json()["error"] == "too_many_files"


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["交易时间", "金额", "类型", "分类", "商家"])
    sheet.append(["2026-08-18 12:00:00", "15.5", "支出", "餐饮", "食堂"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_statement_csv_xlsx_and_xlsm_are_accepted(client, tmp_path):
    csv_path = tmp_path / "bill.csv"
    csv_path.write_text("交易时间,金额,类型,分类,商家\n2026-08-18 12:00,15.5,支出,餐饮,食堂\n",
                        encoding="utf-8")
    csv_response = client.post(
        "/api/import_csv",
        files=[("file", ("bill.csv", csv_path.open("rb"), "text/csv"))],
    )
    assert csv_response.status_code == 200, csv_response.text

    xlsx_response = client.post(
        "/api/import_csv",
        files=[("file", ("bill.xlsx", BytesIO(_xlsx_bytes()),
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
    )
    assert xlsx_response.status_code == 200, xlsx_response.text
    assert "unsupported_file_type" not in xlsx_response.text

    xlsm_response = client.post(
        "/api/import_csv",
        files=[("file", ("bill.xlsm", BytesIO(b"PK" + _xlsx_bytes()),
                         "application/vnd.ms-excel.sheet.macroEnabled.12"))],
    )
    # signature passes; openpyxl may fail on the fake xlsm → format error is fine
    assert xlsm_response.status_code in (200, 400)
    assert "unsupported_file_type" not in xlsm_response.text


def test_statement_invalid_signatures_are_rejected(client):
    xlsx_bad = client.post(
        "/api/import_csv",
        files=[("file", ("bill.xlsx", BytesIO(b"not-a-zip"),
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
    )
    assert xlsx_bad.status_code == 400
    assert xlsx_bad.json()["error"] == "unsupported_file_type"

    xls_legacy = client.post(
        "/api/import_csv",
        files=[("file", ("bill.xls", BytesIO(b"\xd0\xcf\x11\xe0"),
                         "application/vnd.ms-excel"))],
    )
    assert xls_legacy.status_code == 400
    assert xls_legacy.json()["error"] == "unsupported_file_type"

    csv_binary = client.post(
        "/api/import_csv",
        files=[("file", ("bill.csv", BytesIO(b"\xff\xfe\x00\x01\x02"),
                         "text/csv"))],
    )
    assert csv_binary.status_code == 400
    assert csv_binary.json()["error"] == "unsupported_file_type"


def test_oversized_statement_returns_413(client):
    content = b"a,b,c\n" + b"x" * (20 * 1024 * 1024)
    response = client.post(
        "/api/import_csv",
        files=[("file", ("huge.csv", BytesIO(content), "text/csv"))],
    )
    assert response.status_code == 413
    assert response.json()["error"] == "file_too_large"
