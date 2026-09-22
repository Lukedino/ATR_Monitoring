"""원천 표 전체 검증과 기존 메타데이터 보존을 합성 입력으로 검사한다."""
import copy
import io

import pandas as pd
import pytest

from test_portfolio_configuration import load_config


HEADER = ["Ticker", "구분", "계좌", "종목", "진입가격"]
PRIVATE = "SYNTHETIC-PRIVATE-ROW"


@pytest.fixture
def configured(load_config):
    module, _ = load_config()
    old = pd.DataFrame([["SYM-OLD", "미국", "synthetic-old", "synthetic-name", "125"]], columns=HEADER)
    module._parse_portfolio_df(old)
    return module


def metadata(module):
    return copy.deepcopy((module._kr_names_from_drive, module._symbol_accounts, module._symbol_entry_prices))


def parse(module, text):
    return module._parse_portfolio_df(module._portfolio_frame_from_bytes(text.encode("utf-8")))


@pytest.mark.parametrize("bad", ["", " ", "SYM BAD", "SYM\x00BAD", 'SYM"BAD', ".KS", ".KQ", "-USD", "-USDT"])
def test_explicit_bad_ticker_never_uses_name_or_commits_partial_rows(configured, bad):
    before = metadata(configured)
    frame = pd.DataFrame([
        ["SYM-FIRST", "미국", PRIVATE, PRIVATE, "110"],
        [bad, "미국", PRIVATE, "SYM-FALLBACK", "120"],
        ["SYM-LAST", "미국", PRIVATE, PRIVATE, "130"],
    ], columns=HEADER)
    original = frame.copy(deep=True)
    with pytest.raises(ValueError, match="^portfolio_") as failure:
        configured._parse_portfolio_df(frame)
    assert PRIVATE not in str(failure.value)
    assert metadata(configured) == before
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("value", ["NaN", "Inf", "-Inf", "1e999", "0", "-1", "true", "one", "1,23"])
def test_any_invalid_provided_entry_price_preserves_all_metadata(configured, value):
    before = metadata(configured)
    frame = pd.DataFrame([
        ["SYM-FIRST", "미국", PRIVATE, PRIVATE, "110"],
        ["SYM-BAD", "미국", PRIVATE, PRIVATE, value],
    ], columns=HEADER)
    with pytest.raises(ValueError, match="^portfolio_entry_price_invalid$"):
        configured._parse_portfolio_df(frame)
    assert metadata(configured) == before


@pytest.mark.parametrize("headers", [
    ["Ticker", "Ticker", "구분"], ["Ticker", " Ticker ", "구분"],
    ["Ticker", "ticker", "구분"], ["Ticker", "구분", ""], ["Ticker"], ["구분", "계좌"],
])
@pytest.mark.parametrize("excel", [False, True])
def test_raw_duplicate_or_incomplete_headers_cannot_be_mangled(configured, headers, excel):
    before = metadata(configured)
    if excel:
        output = io.BytesIO()
        pd.DataFrame([headers, ["value"] * len(headers)]).to_excel(output, index=False, header=False)
        raw = output.getvalue()
    else:
        raw = (",".join(headers) + "\n" + ",".join(["value"] * len(headers))).encode()
    with pytest.raises(ValueError, match="^portfolio_"):
        configured._parse_portfolio_df(configured._portfolio_frame_from_bytes(raw, excel=excel))
    assert metadata(configured) == before


@pytest.mark.parametrize("raw", [
    b"Ticker,\xff\nSYM-A,US\n",
    "Ticker,구분,진입가격\nSYM-A,미국,100\nSYM-B,미국\n".encode(),
    "Ticker,구분,진입가격\nSYM-A,미국,100\n,\n".encode(),
    "Ticker,구분\nSYM-A,미국,extra\n".encode(),
    'Ticker,구분\n"SYM-A,미국\n'.encode(),
    'Ticker,구분\n"SYM-A"broken,미국\n'.encode(),
    'Ticker,구분,종목\nSYM-A,미국,synthetic"name\n'.encode(),
])
def test_malformed_csv_and_encoding_do_not_commit_valid_prefix(configured, raw):
    before = metadata(configured)
    with pytest.raises(ValueError, match="^portfolio_"):
        configured._parse_portfolio_df(configured._portfolio_frame_from_bytes(raw))
    assert metadata(configured) == before


def test_valid_csv_excel_identity_duplicate_accounts_and_average_match(configured):
    rows = [
        ["NA", "미국", "account-one", "NA", "100"],
        ["NA", "미국", "account-two", "NA", "200"],
        ["NA", "미국", "account-two", "NA", ""],
        ["NULL", "미국", "NULL", "NULL", ""],
        ["991.0", "한국", "account-one", "synthetic-kr", "1,200.50"],
        ["999992.kq", "ETF", "account-one", "synthetic-etf", "150"],
        ["SYNTH32196-USD", "크립토", "account-one", "synthetic-coin", ".5"],
        ["", "", "", "", ""],
    ]
    frame = pd.DataFrame(rows, columns=HEADER)
    csv = frame.to_csv(index=False).encode("utf-8-sig")
    excel = io.BytesIO()
    frame.to_excel(excel, index=False)
    results = []
    for raw, is_excel in [(csv, False), (excel.getvalue(), True)]:
        result = configured._parse_portfolio_df(configured._portfolio_frame_from_bytes(raw, excel=is_excel))
        results.append((result, metadata(configured)))
    assert results[0] == results[1]
    assert results[0][0] == {"포트폴리오": ["NA", "NULL", "000991.KS", "999992.kq", "SYNTH32196-USD"]}
    assert configured._symbol_accounts["NA"] == ["account-one", "account-two"]
    assert configured._symbol_entry_prices["NA"] == 150
    assert "NULL" not in configured._symbol_entry_prices
    assert configured._symbol_entry_prices["000991.KS"] == 1200.5


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_name_column_fallback_only_when_ticker_header_absent(configured, delimiter):
    result = parse(configured, delimiter.join(["종목", "구분", "진입가격"]) + "\n" +
                   delimiter.join(["NA", "미국", ""]) + "\n")
    assert result == {"포트폴리오": ["NA"]}


def test_valid_quoted_csv_keeps_delimiters_and_escaped_quotes(configured):
    result = parse(configured, 'Ticker,구분,종목,진입가격\r\nSYM-A,미국,"synthetic, ""name""","1,200"\r\n')
    assert result == {"포트폴리오": ["SYM-A"]}
    assert configured._kr_names_from_drive["SYM-A"] == 'synthetic, "name"'
    assert configured._symbol_entry_prices["SYM-A"] == 1200.


@pytest.mark.parametrize("category", ["", "synthetic-unknown"])
def test_invalid_meaningful_category_rejects_whole_table(configured, category):
    before = metadata(configured)
    with pytest.raises(ValueError, match="^portfolio_category_invalid$"):
        parse(configured, f"Ticker,구분\nSYM-A,미국\nSYM-B,{category}\n")
    assert metadata(configured) == before


def test_blank_rows_only_do_not_replace_previous_metadata(configured):
    before = metadata(configured)
    with pytest.raises(ValueError, match="^portfolio_empty$"):
        parse(configured, "Ticker,구분\n,\n\n")
    assert metadata(configured) == before


def test_drive_parse_failure_cannot_leave_partial_imported_metadata(load_config, caplog):
    module, calls = load_config({"GOOGLE_SERVICE_ACCOUNT_JSON": '{"synthetic": true}',
                                 "GDRIVE_PORTFOLIO_FILE_ID": "synthetic-file"}, csv=(
        f"Ticker,구분,계좌,종목,진입가격\nSYM-A,미국,{PRIVATE},{PRIVATE},100\n"
        f"SYM-B,미국,{PRIVATE},{PRIVATE},Inf\n").encode())
    assert module.PORTFOLIO_SOURCE == "invalid" and module.ALL_SYMBOLS == []
    assert module.SYMBOL_ACCOUNTS == module.SYMBOL_ENTRY_PRICES == module._kr_names_from_drive == {}
    assert calls == ["credentials", "build", "metadata", "csv"]
    assert PRIVATE not in caplog.text and "SYM-A" not in caplog.text
