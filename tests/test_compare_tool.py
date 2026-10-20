"""tools/compare_tv_trades.py on SYNTHETIC exports (format check only, not strategy results)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import compare_tv_trades as cmp  # noqa: E402

NEW = ("Trade #,Type,Date/Time,Signal,Price USD,Position size (qty),Net P&L USD,Net P&L %,Cumulative P&L USD\n"
       "1,Exit long,2026-10-01 10:05,LONG EXIT,20040,2,120.50,0.2,120.50\n"
       "1,Entry long,2026-10-01 10:01,V26 Bull IFVG,20006,2,120.50,0.2,120.50\n"
       "2,Exit short,2026-10-02 11:00,SHORT EXIT,20006,1,\"-33.00\",-0.1,87.50\n"
       "2,Entry short,2026-10-02 10:40,V26 Bear IFVG,19994,1,\"-33.00\",-0.1,87.50\n")
OLD = ("Trade #,Type,Signal,Date/Time,Price USD,Contracts,Profit USD,Profit %,Cumulative profit USD\n"
       "1,Entry Long,Bull,2026-10-01 10:01,20000,1,-40,-0.1,-40\n"
       "1,Exit Long,Stop,2026-10-01 10:09,19980,1,-40,-0.1,-40\n")


def test_reads_current_and_older_export_formats(tmp_path):
    a, b = tmp_path / "v26.csv", tmp_path / "v25.csv"
    a.write_text(NEW)
    b.write_text(OLD)
    assert cmp.load(a) == [120.5, -33.0] and cmp.load(b) == [-40.0]
    s = cmp.summarize(cmp.load(a))
    assert s["trades"] == 2 and s["net_pnl"] == 87.5 and s["profit_factor"] == 3.65 and s["max_drawdown"] == 33.0


def test_output_is_labelled_historical(tmp_path, capsys):
    a, b = tmp_path / "v26.csv", tmp_path / "v25.csv"
    a.write_text(NEW)
    b.write_text(OLD)
    cmp.main([str(b), str(a)])
    assert capsys.readouterr().out.startswith("HISTORICAL SIMULATION")
