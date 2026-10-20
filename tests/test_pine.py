"""Static checks on the Pine Script (TradingView is the only Pine compiler)."""

import re
from pathlib import Path

from v25bot.schemas import BarEvent, EntryEvent, ExitEvent, FlattenEvent

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = (ROOT / "pine" / "V25_original.pine").read_text().splitlines()
WEBHOOK = (ROOT / "pine" / "V25_webhook.pine").read_text()
WEBHOOK_LINES = WEBHOOK.splitlines()


def test_webhook_script_only_adds_lines():
    """Every original line appears, in order, in the webhook version (pure insertion)."""
    it = iter(WEBHOOK_LINES)
    for line in ORIGINAL:
        for candidate in it:
            if candidate == line:
                break
        else:
            raise AssertionError(f"original line missing or reordered: {line!r}")


def _code(lines):
    return [l for l in lines if not l.strip().startswith("//")]


def test_trading_calls_unchanged():
    def calls(lines):
        return [l.strip() for l in _code(lines) if re.search(r"strategy\.(entry|exit|close_all|cancel_all)\(", l)]

    assert calls(ORIGINAL) == calls(WEBHOOK_LINES)


def test_added_lines_contain_no_order_logic():
    original = set(ORIGINAL)
    added = [l for l in _code(WEBHOOK_LINES) if l not in original]
    joined = "\n".join(added)
    assert not re.search(r"strategy\.(entry|exit|order|close|close_all|cancel|cancel_all)\(", joined)
    assert not re.search(r"\b(state|qty|tradeStop|tradeTarget|tradesToday)\s*:=", joined)


def _fields_after(marker: str) -> set[str]:
    """Collect JSON keys emitted by the alert block that starts at ``marker``."""
    start = WEBHOOK.index(marker)
    block = WEBHOOK[start:start + 2500].split("alert.freq_once_per_bar_close")[0]
    return set(re.findall(r'\\"([a-z_]+)\\":', block))


_helper_start = WEBHOOK.index("f_base(string evt")
_helper = WEBHOOK[_helper_start:WEBHOOK.index("// ====", _helper_start)]
BASE = set(re.findall(r'\\"([a-z_]+)\\":', _helper))


V26_ONLY = {"caps", "setup", "diag"}  # optional schema fields the V25 script never sends


def _schema_fields(model) -> set[str]:
    return {f.alias or name for name, f in model.model_fields.items()} - V26_ONLY


def test_base_fields_match_schema():
    assert BASE == {"schema", "strategy", "strategy_version", "bot_id", "event", "event_id", "symbol",
                    "contract", "timeframe", "bar_time", "sent_at", "passphrase"}


def test_entry_alert_fields_match_schema():
    assert BASE | _fields_after('f_base("entry"') == _schema_fields(EntryEvent)


def test_exit_alert_fields_match_schema():
    assert BASE | _fields_after('f_base("exit"') == _schema_fields(ExitEvent)


def test_bar_alert_fields_match_schema():
    assert BASE | _fields_after('f_base("bar"') == _schema_fields(BarEvent)


def test_flatten_alert_fields_match_schema():
    for marker in ('f_base("flatten", f_eid("flatten-prop"))', 'f_base("flatten", f_eid("flatten-session"))'):
        assert BASE | _fields_after(marker) == _schema_fields(FlattenEvent)


def test_strategy_version_matches_engine_default():
    from v25bot.config import Settings

    version = re.search(r'STRATEGY_VERSION = "([^"]+)"', WEBHOOK).group(1)
    assert version in Settings().allowed_strategy_versions
