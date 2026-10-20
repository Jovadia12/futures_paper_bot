"""Static checks on pine/V26_webhook.pine (TradingView is the only Pine compiler).

They pin the parts of the script the engine depends on: the JSON it emits, the
engine limits it sizes with, the 8 timeframes, the non-repainting HTF reads and
that the stop and target are set once at entry. V25 is pinned byte-for-byte.
"""

import hashlib
import os
import re
from pathlib import Path

import pytest

from v25bot.config import Settings
from v25bot.schemas import (
    DIAG_KINDS, V26_HTF_MINUTES, V26_SIZING_MODES, BarEvent, Candidate, Candle, Caps, Diag, DiagItem,
    DiagSetup, EntryEvent, ExitEvent, FlattenEvent, FvgInfo, Setup,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "pine" / "V26_webhook.pine").read_text()
LINES = SRC.splitlines()
CODE = "\n".join(l for l in LINES if not l.strip().startswith("//"))


def test_v25_scripts_unchanged():
    digests = {p: hashlib.sha256((ROOT / "pine" / p).read_bytes()).hexdigest()
               for p in ("V25_original.pine", "V25_webhook.pine")}
    assert digests == {
        "V25_original.pine": "b35ef8454d2c1490d1c408b068357f5892d7094ea686c5ad9ae2bd9cf2576f35",
        "V25_webhook.pine": "c0dc554e21ffd5dce5b7c822a377338a23cb153de3d3e07d6fc24761554160b1",
    }


@pytest.mark.skipif(not os.environ.get("PINE_PARSE"), reason="slow (~90 s); run with PINE_PARSE=1")
def test_parses_as_pine():
    pynescript = pytest.importorskip("pynescript.ast")
    pynescript.parse(SRC)


def test_indentation_follows_pine_rules():
    """Blocks use multiples of 4 spaces; wrapped lines must not (pynescript does not check this)."""
    cont = re.compile(r"(\+|,|\?|:|\(|\band|\bor|=)\s*$")
    for i in range(1, len(LINES)):
        line, prev = LINES[i], LINES[i - 1].rstrip()
        if not line.strip() or line.strip().startswith("//"):
            continue
        if prev.lstrip().startswith("//"):
            prev = ""
        indent = len(line) - len(line.lstrip(" "))
        wrapped = bool(cont.search(prev)) and not prev.endswith("=>") and not re.match(r"\s*(if|else|for|type)\b", prev)
        assert (indent % 4 != 0) == wrapped, f"line {i + 1}: {line.strip()[:60]}"


def test_version_constants_and_production_gate():
    assert 'STRATEGY_VERSION = "V26-webhook.1"' in SRC and 'SCHEMA = "v26.webhook.v1"' in SRC
    assert "V26-webhook.1" not in Settings().allowed_strategy_versions  # production stays V25-only
    assert re.search(r'botId = input\.string\("V26-MNQ-1"', SRC)


def test_strategy_settings():
    assert "calc_on_every_tick=false" in SRC and "pyramiding=0" in SRC and "process_orders_on_close=false" in SRC


# ----------------------------------------------------------------------------- HTF FVGs
def test_eight_timeframes_requested_without_repainting():
    calls = re.findall(r'request\.security\(syminfo\.tickerid, "(\d+)", f_htf\(\), lookahead=barmerge\.lookahead_on\)', SRC)
    assert tuple(int(c) for c in calls) == V26_HTF_MINUTES
    for tf in V26_HTF_MINUTES:
        assert re.search(rf"^use{tf} = input\.bool\(true", SRC, re.M), tf
        assert re.search(rf"htfDetected \+= f_onHtf\(b{tf}, s{tf}, b{tf}lo, b{tf}hi, s{tf}lo, s{tf}hi, {tf}, t{tf}", SRC)


def test_every_lookahead_read_uses_closed_bars_only():
    for line in CODE.splitlines():
        if "lookahead_on" in line:
            expr = line.split(",", 2)[2]
            assert "f_htf()" in expr or re.search(r"\[1\]", expr), line
    body = SRC[SRC.index("f_htf() =>"):SRC.index("[b5, s5")]
    offsets = set(re.findall(r"\b(?:high|low|time|close|open)\[(\d+)\]", body))
    assert offsets == {"1", "3"}  # candle 1 = [3], candle 3 = [1]; never the forming bar [0]
    assert re.search(r"bull = low\[1\] > high\[3\]", body) and re.search(r"bear = high\[1\] < low\[3\]", body)


def test_all_zones_tracked_with_invalidation_and_expiry():
    assert "var array<Zone> zones" in SRC
    for k in ("expired", "failed", "touched"):
        assert re.search(rf"\b{k} = ", SRC), k


# ----------------------------------------------------------------------------- setup logic
def test_most_recent_opposing_gap_selected():
    assert "g = array.last(bearGaps)" in SRC and "g = array.last(bullGaps)" in SRC
    assert "longInv := barstate.isconfirmed and close > longSel.top" in SRC
    assert "shortInv := barstate.isconfirmed and close < shortSel.bottom" in SRC


def test_gap_candles_indexed_explicitly():
    """Candle 1 = [2], candle 2 = [1], candle 3 = the confirmed bar; times and high/low of each."""
    bear = "Gap.new(-1, low[2], high, time[2], time[1], time, high[2], low[2], high[1], low[1], high, low, bar_index)"
    bull = "Gap.new(1, low, high[2], time[2], time[1], time, high[2], low[2], high[1], low[1], high, low, bar_index)"
    assert bear in SRC and bull in SRC
    # Gap bookkeeping runs after the entry decision, so a gap is never inverted on its own bar.
    assert SRC.index("if goShort") < SRC.index(bear)


def test_filters_only_in_filtered_variant():
    body = SRC[SRC.index("f_filterReason(int dir) =>"):SRC.index("// Stop from")]
    assert "if isFiltered" in body
    for flag in ("useTrend", "useSweep", "useDisplacement", "useStructure", "useCandleDir"):
        assert flag in body
    assert 'isFiltered = variant == "Filtered"' in SRC
    assert re.search(r'variant = input\.string\("Core"', SRC)


def test_stop_from_inverted_gap_candles():
    body = SRC[SRC.index("f_stop(Gap g, int dir) =>"):SRC.index("// Nearest untapped")]
    assert "math.min(g.c1l, g.c2l, g.c3l)" in body and "math.max(g.c1h, g.c2h, g.c3h)" in body
    assert "g.bottom" in body and "g.top" in body and "math.round_to_mintick" in body


def test_stop_and_target_set_once_and_never_moved():
    assigns = [l.strip() for l in LINES if re.search(r"\btrade(Stop|Target) :=", l)]
    assert assigns == ["tradeStop := lStop", "tradeTarget := lTgt", "tradeStop := sStop", "tradeTarget := sTgt",
                       "tradeStop := na", "tradeTarget := na"]


def test_no_fixed_r_target():
    assert re.search(r'minRR = input\.float\(2\.0, "Minimum reward:risk", minval=2\.0', SRC)
    assert not re.search(r"(entry|close|estFill)\s*[+-]\s*\w*[rR]isk\s*\*\s*(2|rr|RR|minRR)\b(?!.*lowB|.*highB)",
                         CODE.replace("lowB = ", "").replace("highB = ", ""))
    assert 'reason := "target: no untapped swing "' in SRC


def test_engine_limit_defaults_equal_engine_settings():
    s = Settings()
    want = {"engMaxContracts": s.max_contracts, "engMaxRisk": s.max_trade_risk_usd,
            "engCostReserve": s.cost_reserve_per_contract_usd, "engSlipTicks": s.risk_entry_slippage_ticks}
    for name, value in want.items():
        m = re.search(rf"^{name} = input\.(?:int|float)\(([\d.]+),", SRC, re.M)
        assert m and float(m.group(1)) == value, name
    assert re.search(r"^maxTrades = input\.int\((\d+),", SRC, re.M).group(1) == str(s.max_trades_per_day)
    opts = re.search(r'sizingMode = input\.string\("Engine limits", "Sizing", options=\[(.*?)\]', SRC).group(1)
    assert tuple(re.findall(r'"([^"]+)"', opts)) == V26_SIZING_MODES


def test_sizing_formula_matches_engine():
    assert "estFill = dir == 1 ? close + engSlipTicks * tick : close - engSlipTicks * tick" in SRC
    assert "rpc := risk * pointValue + engCostReserve" in SRC
    assert "qty := math.min(engMaxContracts, int(math.floor(engMaxRisk / rpc)))" in SRC
    assert "if qty < 1" in SRC


# ----------------------------------------------------------------------------- JSON vs schema
def _keys(text: str) -> set[str]:
    return set(re.findall(r'\\"([a-z_0-9]+)\\":', text))


def _fields(model) -> set[str]:
    return {f.alias or name for name, f in model.model_fields.items()}


def _between(start: str, end: str) -> str:
    i = SRC.index(start)
    return SRC[i:SRC.index(end, i)]


BASE = _keys(_between("f_base(string evt", "f_item("))


def test_base_keys():
    assert BASE == {"schema", "strategy", "strategy_version", "bot_id", "event", "event_id", "symbol",
                    "contract", "timeframe", "bar_time", "sent_at", "passphrase"}
    assert '"\\"strategy\\":\\"V26\\","' in SRC


def test_entry_keys_match_schema():
    entry = _keys(_between("f_entryJson(string side", "anchorCode ="))
    caps_keys = {"max_contracts", "max_trade_risk", "cost_reserve", "slippage_ticks"}
    assert BASE | entry - caps_keys == _fields(EntryEvent)
    assert caps_keys == _fields(Caps)


def test_setup_keys_match_schema():
    setup = _keys(_between("f_setup(int tf", "f_entryJson("))
    fvg = {"c1", "c2", "c3", "top", "bottom"}
    assert setup - fvg | {"fvg"} == _fields(Setup) | {"fvg"}
    assert fvg == _fields(FvgInfo)
    assert _keys(_between("f_candle(int t", "// Diagnostic items")) == _fields(Candle)
    cand = re.search(r'cand := cand \+ .*', SRC).group(0)
    assert _keys(cand) == _fields(Candidate)


def test_exit_flatten_keys_match_schema():
    exit_block = SRC[SRC.index('f_base("exit"'):].split("alert.freq_once_per_bar_close")[0]
    assert BASE | _keys(exit_block) == _fields(ExitEvent)
    for marker in ('f_base("flatten", f_eid("flatten-prop"))', 'f_base("flatten", f_eid("flatten-session"))'):
        block = SRC[SRC.index(marker):].split("alert.freq_once_per_bar_close")[0]
        assert BASE | _keys(block) == _fields(FlattenEvent)


def test_bar_and_diag_keys_match_schema():
    bar = SRC[SRC.index('f_base("bar"'):].split("alert.freq_once_per_bar_close")[0]
    diag = _keys(_between('diagJson := ",\\"diag\\"', "alert("))
    assert BASE | _keys(bar) | {"diag"} == _fields(BarEvent)
    assert diag - {"diag"} == _fields(Diag)
    assert _keys(_between("f_setupState(bool armed", "if enableWebhook and emitBars")) == _fields(DiagSetup)
    assert _keys(_between("f_item(string k", "f_push(")) == _fields(DiagItem)


def test_diag_kinds_and_item_cap_match_schema():
    kinds = set(re.findall(r'f_item\("([a-z_]+)"', SRC))
    assert kinds == set(DIAG_KINDS)
    cap = int(re.search(r"if array\.size\(items\) < (\d+)", SRC).group(1))
    assert cap == Diag.model_fields["items"].metadata[0].max_length


def test_stop_anchor_codes_match_schema():
    assert 'anchorCode = stopAnchor == "Three-candle extreme" ? "structure" : "boundary"' in SRC
    assert set(Setup.model_fields["stop_anchor"].annotation.__args__) == {"structure", "boundary"}


def test_entry_alerts_once_per_bar_close():
    for call in re.findall(r"alert\((?:.|\n)*?alert\.freq_\w+", SRC):
        assert call.endswith("alert.freq_once_per_bar_close")
