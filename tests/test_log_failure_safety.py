"""Synthetic public-log failures; no actual state, credentials or network calls."""
import copy
import io
import logging
import sys
import threading
from types import SimpleNamespace

import pytest

import log_masking as lm


ENV = {"GITHUB_ACTIONS": "true", "LOG_MASK_SALT": "synthetic-private-salt"}


@pytest.fixture
def captured(monkeypatch):
    root = logging.getLogger()
    original_handlers, original_level = root.handlers[:], root.level
    original_sys_hook, original_thread_hook = sys.excepthook, threading.excepthook
    original_factory = logging.getLogRecordFactory()
    stream, commands = io.StringIO(), io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    monkeypatch.setattr(lm, "_installed_filter", None)
    monkeypatch.setattr(lm, "_record_factory_enabled", False)
    try:
        yield SimpleNamespace(stream=stream, commands=commands, handler=handler)
    finally:
        root.handlers, root.level = original_handlers, original_level
        sys.excepthook, threading.excepthook = original_sys_hook, original_thread_hook
        logging.setLogRecordFactory(original_factory)


def install(captured, symbols=("ZZ", "SYM-CURRENT")):
    return lm.install_for_github_actions(symbols, env=ENV, stream=captured.commands)


def test_state_positions_and_alert_only_symbols_extend_existing_masks(captured):
    install(captured)
    state = {"positions": {"OLD-QC": {"symbol": "OLD-QC"}},
             "alert_log": {"QQ": {"date": "2026-01-01"}},
             "other": {"SYNTHETIC-NON-SYMBOL": "SYNTHETIC-PRIVATE-BODY"}}
    original = copy.deepcopy(state)
    mapping = lm.register_state_symbols_for_github_actions(
        state, {"OLD-QC": "옛가상종목"}, env=ENV, stream=captured.commands,
    )
    logging.getLogger("stop_manager").warning("%s %s %s %s", "OLD-QC", "QQ", "SYM-CURRENT", "옛가상종목의")
    output = captured.stream.getvalue()
    assert all(secret not in output for secret in ("OLD-QC", "QQ", "SYM-CURRENT", "옛가상종목"))
    assert lm.pseudonym_for("OLD-QC", ENV["LOG_MASK_SALT"]) in output
    assert set(mapping) == {"OLD-QC", "QQ", "옛가상종목"}
    assert "SYNTHETIC-NON-SYMBOL" not in captured.commands.getvalue()
    assert "SYNTHETIC-PRIVATE-BODY" not in captured.commands.getvalue()
    assert "::add-mask::QQ" not in captured.commands.getvalue()
    assert state == original


def test_state_registration_without_initial_install_is_still_protected(captured):
    mapping = lm.register_state_symbols_for_github_actions(
        {"positions": {}, "alert_log": {"ZZ": {}}}, env=ENV, stream=captured.commands,
    )
    logging.getLogger("review").warning("old alert: ZZ")
    assert mapping["ZZ"] in captured.stream.getvalue()
    assert "old alert: ZZ" not in captured.stream.getvalue()


def test_repeated_state_registration_does_not_duplicate_filters_or_remask_pseudonyms(captured):
    # A real symbol may coincide with the "SYM" pseudonym prefix. Replacement
    # must happen once, otherwise the generated label is rewritten recursively.
    install(captured, ["SYM", "OLD-QC"])
    for _ in range(3):
        lm.register_state_symbols_for_github_actions(
            {"positions": {"OLD-QC": {}}}, env=ENV, stream=captured.commands,
        )
    logging.getLogger("review").warning("old: OLD-QC")
    assert captured.stream.getvalue().strip() == "WARNING: old: " + lm.pseudonym_for("OLD-QC", ENV["LOG_MASK_SALT"])
    assert len([item for item in captured.handler.filters if isinstance(item, lm.RedactingFilter)]) == 1


@pytest.mark.parametrize("method", ["exception", "exc_info", "preformatted"])
def test_exception_values_and_traceback_never_escape_the_logging_filter(captured, method):
    install(captured)
    private = "ZZ SYNTHETIC-PRIVATE-BODY https://api.telegram.org/botSYNTHETIC-TOKEN/sendPhoto"
    try:
        raise KeyError(private)
    except KeyError:
        if method == "exception":
            logging.getLogger("review").exception("synthetic failure")
        elif method == "exc_info":
            logging.getLogger("review").error("synthetic failure", exc_info=sys.exc_info())
        else:
            record = logging.LogRecord("review", logging.ERROR, __file__, 1, "synthetic failure", (), None)
            record.exc_text = private
            captured.handler.handle(record)
    output = captured.stream.getvalue()
    assert "exception details withheld" in output
    assert all(value not in output for value in ("ZZ", "SYNTHETIC-PRIVATE-BODY", "SYNTHETIC-TOKEN", "https://"))


@pytest.mark.parametrize("kind", ["positional", "mapping", "direct"])
def test_exception_objects_are_not_stringified_in_plain_log_messages(captured, kind):
    install(captured)
    class PrivateError(RuntimeError):
        def __str__(self):
            pytest.fail("Exception values must not be formatted into public logs")
    error = PrivateError("SYNTHETIC-PRIVATE-BODY")
    logger = logging.getLogger("review")
    if kind == "positional":
        logger.error("failure: %s", error)
    elif kind == "mapping":
        logger.error("failure: %(error)s", {"error": error})
    else:
        logger.error(error)
    assert "PrivateError" in captured.stream.getvalue()
    assert "SYNTHETIC-PRIVATE" not in captured.stream.getvalue()


def test_stack_text_and_full_urls_are_sanitized_even_with_no_symbols(captured):
    install(captured, [])
    record = logging.LogRecord("review", logging.ERROR, __file__, 1,
                               "request failed https://user:SYNTHETIC-PASSWORD@example.invalid/private?token=SYNTHETIC-TOKEN",
                               (), None)
    record.stack_info = "synthetic stack https://example.invalid/SYNTHETIC-FILE-ID"
    captured.handler.handle(record)
    output = captured.stream.getvalue()
    assert output.count("[REDACTED_URL]") == 2
    assert "SYNTHETIC-" not in output


def test_stack_text_masks_short_symbols_without_changing_other_words(captured):
    install(captured, ["Q"])
    record = logging.LogRecord("review", logging.WARNING, __file__, 1, "Stop check", (), None)
    record.stack_info = "synthetic stack: ['Q'] Quiet quality"
    captured.handler.handle(record)
    output = captured.stream.getvalue()
    assert "['Q']" not in output
    assert "Quiet quality" in output
    assert "Stop check" in output


@pytest.mark.parametrize("message", [
    "chat_id=SYNTHETIC-CHAT token=SYNTHETIC-TOKEN",
    "Authorization: Bearer SYNTHETIC-TOKEN",
    "{'access_token': 'SYNTHETIC-TOKEN'}",
])
def test_credential_fields_outside_urls_are_removed(captured, message):
    install(captured)
    logging.getLogger("review").error(message)
    assert "SYNTHETIC-" not in captured.stream.getvalue()


@pytest.mark.parametrize("threaded", [False, True])
def test_unhandled_hooks_do_not_print_short_symbols_or_exception_bodies(captured, threaded):
    install(captured)
    error = KeyError("ZZ SYNTHETIC-PRIVATE https://example.invalid/SYNTHETIC-TOKEN")
    if threaded:
        threading.excepthook(SimpleNamespace(exc_type=KeyError, exc_value=error, exc_traceback=None))
    else:
        sys.excepthook(KeyError, error, None)
    output = captured.stream.getvalue()
    assert "Unhandled KeyError" in output
    assert "exception details withheld" in output
    assert "ZZ" not in output and "SYNTHETIC-" not in output and "https://" not in output


def test_workflow_mask_commands_escape_multiline_values(captured):
    lm.emit_gha_masks(["SYNTHETIC\n::error::injected%\r"], stream=captured.commands)
    lines = captured.commands.getvalue().splitlines()
    assert all(line.startswith("::add-mask::") for line in lines)
    assert all("%0A" in line and "%25" in line and "%0D" in line for line in lines)


def test_local_registration_and_hooks_remain_unchanged(captured):
    old_sys, old_thread = sys.excepthook, threading.excepthook
    assert lm.register_state_symbols_for_github_actions(
        {"positions": {"OLD-QC": {}}}, env={}, stream=captured.commands,
    ) == {}
    assert sys.excepthook is old_sys and threading.excepthook is old_thread
    assert captured.commands.getvalue() == ""
    logging.getLogger("review").warning("local OLD-QC")
    assert "local OLD-QC" in captured.stream.getvalue()


def test_missing_salt_keeps_state_symbols_opaque(captured):
    env = {"GITHUB_ACTIONS": "true"}
    lm.install_for_github_actions([], env=env, stream=captured.commands)
    mapping = lm.register_state_symbols_for_github_actions(
        {"positions": {"OLD-QC": {}}, "alert_log": {"ZZ": {}}}, env=env, stream=captured.commands,
    )
    assert set(mapping.values()) == {"SYM-****"}
    logging.getLogger("review").warning("OLD-QC ZZ")
    assert "OLD-QC" not in captured.stream.getvalue()
    assert "ZZ" not in captured.stream.getvalue()


def test_gha_protects_later_nonpropagating_handlers_and_preserves_existing_factory(captured):
    previous = logging.getLogRecordFactory()
    calls = []

    def custom_factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.synthetic_factory_marker = "preserved"
        calls.append(record)
        return record

    logging.setLogRecordFactory(custom_factory)
    install(captured, ["SYM", "OLD-QC"])
    first_installed = logging.getLogRecordFactory()
    install(captured, ["SYM", "OLD-QC"])
    assert logging.getLogRecordFactory() is first_installed
    late_output = io.StringIO()
    late_handler = logging.StreamHandler(late_output)
    late_handler.setFormatter(logging.Formatter("%(synthetic_factory_marker)s %(message)s"))
    logger = logging.getLogger("synthetic.private.handler")
    original_handlers, original_propagate = logger.handlers[:], logger.propagate
    logger.handlers, logger.propagate = [late_handler], False
    try:
        logger.error("OLD-QC https://example.invalid/SYNTHETIC-TOKEN")
    finally:
        logger.handlers, logger.propagate = original_handlers, original_propagate
    assert len(calls) == 1
    assert late_output.getvalue().strip() == "preserved " + lm.pseudonym_for("OLD-QC", ENV["LOG_MASK_SALT"]) + " [REDACTED_URL]"


def test_test_constructor_does_not_install_process_factories_or_hooks(captured):
    factory, hook, thread_hook = logging.getLogRecordFactory(), sys.excepthook, threading.excepthook
    lm.install(["ZZ"], salt="synthetic-salt", stream=captured.commands)
    assert logging.getLogRecordFactory() is factory
    assert sys.excepthook is hook and threading.excepthook is thread_hook


@pytest.mark.parametrize("in_ci", [False, True])
def test_bootstrap_hook_is_standalone_and_does_not_require_config_or_masks(captured, in_ci):
    hook, thread_hook = sys.excepthook, threading.excepthook
    factory = logging.getLogRecordFactory()
    lm.install_exception_hooks_for_github_actions(env={"GITHUB_ACTIONS": str(in_ci)})
    assert logging.getLogRecordFactory() is factory
    assert lm._installed_filter is None
    assert captured.commands.getvalue() == ""
    if in_ci:
        sys.excepthook(ImportError, ImportError("ZZ https://example.invalid/SYNTHETIC-TOKEN"), None)
        assert "Unhandled ImportError" in captured.stream.getvalue()
        assert "SYNTHETIC" not in captured.stream.getvalue()
        assert "ZZ" not in captured.stream.getvalue()
    else:
        assert sys.excepthook is hook and threading.excepthook is thread_hook


def test_malformed_log_format_does_not_fall_back_to_raw_args_on_stderr(captured):
    install(captured)
    logging.getLogger("review").warning("bad numeric field: %d", "SYNTHETIC-PRIVATE-BODY")
    assert "[log message formatting failed]" in captured.stream.getvalue()
    assert "SYNTHETIC-PRIVATE" not in captured.stream.getvalue()


def test_exc_info_without_an_active_exception_does_not_break_logging(captured):
    install(captured)
    logging.getLogger("review").warning("synthetic diagnostic", exc_info=True)
    assert "synthetic diagnostic" in captured.stream.getvalue()


def test_exception_hook_cannot_reexpose_original_failure_when_logging_is_broken(captured, monkeypatch, capsys):
    lm.install_exception_hooks_for_github_actions(env=ENV)

    def broken_logger(*args, **kwargs):
        raise RuntimeError("SYNTHETIC-PRIVATE-BACKEND")

    monkeypatch.setattr(lm.logger, "critical", broken_logger)
    sys.excepthook(KeyError, KeyError("ZZ SYNTHETIC-PRIVATE-ORIGINAL"), None)
    output = capsys.readouterr().err
    assert "Unhandled exception" in output
    assert "SYNTHETIC-PRIVATE" not in output and "ZZ" not in output
