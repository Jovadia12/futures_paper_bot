"""Paper execution simulator: fills, slippage, partial fills, OCO, rejects, disconnects."""

from datetime import datetime

import pytest

from conftest import CONTRACT, T0, make_settings
from v25bot.brokers.base import BracketRequest, BrokerDisconnected, OrderRejected, OrderRole, OrderStatus, Side
from v25bot.brokers.paper import PaperBroker
from v25bot.clock import FakeClock
from v25bot.market_data import Quote, QuoteBook
from v25bot.store import Store


class PB:
    def __init__(self, tmp_path, **overrides):
        import asyncio

        self.loop = asyncio.new_event_loop()
        self.settings = make_settings(tmp_path, **overrides)
        self.clock = FakeClock(T0)
        self.quotes = QuoteBook()
        self.store = Store(self.settings.db_path)
        self.b = PaperBroker(self.settings, self.clock, self.quotes, self.store)
        self.run(self.b.connect())

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def quote(self, last, bid=None, ask=None):
        self.run(self.b.on_quote(Quote(CONTRACT, last=last, bid=bid, ask=ask, ts=self.clock.now())))

    def bracket(self, side=Side.BUY, qty=1, stop=19980.0, target=20060.0):
        return self.run(self.b.place_bracket(BracketRequest("cid", CONTRACT, side, qty, stop, target)))

    def pos(self):
        return {p.symbol: (p.qty, p.avg_price) for p in self.run(self.b.get_positions())}

    def orders(self, role):
        return [o for o in self.b.orders.values() if o.role is role]


@pytest.fixture
def pb(tmp_path):
    p = PB(tmp_path)
    yield p
    p.loop.close()


def test_market_entry_slippage_and_commission(pb):
    pb.quote(20000.0)
    res = pb.bracket()
    assert res.entry.status is OrderStatus.FILLED
    fill = pb.b.fills[0]
    assert fill.price == 20000.5  # 2 ticks adverse slippage (default PAPER_SLIPPAGE_TICKS=2)
    assert fill.commission == 1.5
    assert pb.pos() == {CONTRACT: (1, 20000.5)}
    assert pb.b.balance == pytest.approx(50000 - 1.5)


def test_bid_ask_quotes_buy_at_ask_sell_at_bid(pb):
    pb.quote(20000.0, bid=20000.0, ask=20000.25)
    pb.bracket()
    assert pb.b.fills[0].price == 20000.75  # ask + 2 ticks
    pb.run(pb.b.flatten(CONTRACT))
    assert pb.b.fills[-1].price == 19999.5  # bid - 2 ticks


def test_partial_fills_walk_the_book_and_bracket_tracks_filled_qty(tmp_path):
    p = PB(tmp_path, paper_book_depth=2, paper_slippage_ticks=0)
    p.quote(20000.0)
    res = p.bracket(qty=5)
    entry_fills = [f for f in p.b.fills if f.order_id == res.entry.id]
    assert [(f.qty, f.price) for f in entry_fills] == [(2, 20000.0), (2, 20000.25), (1, 20000.5)]
    stop = p.orders(OrderRole.STOP_LOSS)[0]
    assert stop.qty == 5 and stop.status is OrderStatus.WORKING
    assert p.pos()[CONTRACT][0] == 5
    p.loop.close()


def test_take_profit_fill_cancels_stop_oco(pb):
    pb.quote(20000.0)
    pb.bracket()
    pb.quote(20060.0)  # touch only: no fill with trade-through
    assert pb.pos()[CONTRACT][0] == 1
    pb.quote(20060.25)
    assert pb.pos() == {}
    tp = pb.orders(OrderRole.TAKE_PROFIT)[0]
    sl = pb.orders(OrderRole.STOP_LOSS)[0]
    assert tp.status is OrderStatus.FILLED and tp.avg_fill_price == 20060.0
    assert sl.status is OrderStatus.CANCELLED


def test_limit_touch_fills_when_trade_through_disabled(tmp_path):
    p = PB(tmp_path, paper_limit_trade_through=False)
    p.quote(20000.0)
    p.bracket()
    p.quote(20060.0)
    assert p.pos() == {}
    p.loop.close()


def test_stop_fill_cancels_target_and_applies_slippage(pb):
    pb.quote(20000.0)
    pb.bracket()
    pb.quote(19980.0)
    sl = pb.orders(OrderRole.STOP_LOSS)[0]
    assert sl.status is OrderStatus.FILLED
    assert sl.avg_fill_price == 19979.5  # stop - 2 ticks slippage
    assert pb.orders(OrderRole.TAKE_PROFIT)[0].status is OrderStatus.CANCELLED
    # realised: (19979.5 - 20000.5) * 2 = -42, commissions 3.0
    assert pb.b.balance == pytest.approx(50000 - 42 - 3.0)


def test_gap_through_stop_fills_at_worse_market_price(pb):
    pb.quote(20000.0)
    pb.bracket()
    pb.quote(19950.0)  # gapped 30 points through the 19980 stop
    sl = pb.orders(OrderRole.STOP_LOSS)[0]
    assert sl.avg_fill_price == 19949.5


def test_partial_target_fill_reduces_stop_quantity(tmp_path):
    p = PB(tmp_path, paper_book_depth=2, paper_slippage_ticks=0)
    p.quote(20000.0)
    p.bracket(qty=3)
    p.quote(20060.25)  # limit fills only 2 (book depth) on this print
    tp, sl = p.orders(OrderRole.TAKE_PROFIT)[0], p.orders(OrderRole.STOP_LOSS)[0]
    assert tp.status is OrderStatus.PARTIALLY_FILLED and tp.filled_qty == 2
    assert p.pos()[CONTRACT][0] == 1
    assert sl.remaining == 1  # OCO reduced the stop to the remaining position
    p.quote(19979.0)
    assert p.pos() == {}
    assert tp.status is OrderStatus.CANCELLED
    p.loop.close()


def test_short_bracket(pb):
    pb.quote(20000.0)
    pb.bracket(side=Side.SELL, stop=20020.0, target=19940.0)
    assert pb.pos()[CONTRACT] == (-1, 19999.5)
    pb.quote(19939.75)
    assert pb.pos() == {}


def test_bar_with_stop_and_target_inside_range_assumes_stop_first(pb):
    pb.quote(20000.0)
    pb.bracket()
    pb.run(pb.b.on_bar(CONTRACT, 20000.0, 20070.0, 19970.0, 20065.0, pb.clock.now()))
    assert pb.orders(OrderRole.STOP_LOSS)[0].status is OrderStatus.FILLED
    assert pb.orders(OrderRole.TAKE_PROFIT)[0].status is OrderStatus.CANCELLED


@pytest.mark.parametrize("stop,target,match", [
    (20010.0, 20060.0, "InvalidPrice"),      # stop above market for a long
    (19980.0, 19990.0, "InvalidPrice"),      # target below market
    (19980.1, 20060.0, "tick grid"),
])
def test_invalid_bracket_prices_rejected(pb, stop, target, match):
    pb.quote(20000.0)
    with pytest.raises(OrderRejected, match=match):
        pb.bracket(stop=stop, target=target)
    assert pb.pos() == {}
    assert any(o.status is OrderStatus.REJECTED for o in pb.b.orders.values())


def test_entry_rejected_without_fresh_quote(pb):
    with pytest.raises(OrderRejected, match="no market data"):
        pb.bracket()
    pb.quote(20000.0)
    pb.clock.advance(pb.settings.max_quote_age_seconds + 1)
    with pytest.raises(OrderRejected, match="stale"):
        pb.bracket()


def test_injected_entry_rejection(pb):
    pb.quote(20000.0)
    pb.b.inject_reject(OrderRole.ENTRY, "MarginExceeded")
    with pytest.raises(OrderRejected, match="MarginExceeded"):
        pb.bracket()
    pb.bracket()  # next one goes through
    assert pb.pos()[CONTRACT][0] == 1


def test_stop_child_rejected_leaves_position_unprotected(pb):
    pb.quote(20000.0)
    pb.b.inject_reject(OrderRole.STOP_LOSS, "InvalidPrice")
    pb.bracket()
    sl = pb.orders(OrderRole.STOP_LOSS)[0]
    assert sl.status is OrderStatus.REJECTED
    assert pb.pos()[CONTRACT][0] == 1


def test_disconnect_blocks_client_calls_but_server_side_stop_still_works(pb):
    pb.quote(20000.0)
    pb.bracket()
    pb.b.simulate_disconnect()
    with pytest.raises(BrokerDisconnected):
        pb.run(pb.b.get_positions())
    with pytest.raises(BrokerDisconnected):
        pb.bracket()
    with pytest.raises(BrokerDisconnected):
        pb.run(pb.b.connect())  # outage persists
    pb.quote(19979.0)  # market moves while we are disconnected
    pb.b.simulate_reconnect()
    pb.run(pb.b.connect())
    assert pb.pos() == {}
    assert pb.orders(OrderRole.STOP_LOSS)[0].status is OrderStatus.FILLED


def test_flatten_cancels_orders_and_closes_position(pb):
    pb.quote(20000.0)
    pb.bracket(qty=2)
    pb.run(pb.b.flatten(CONTRACT))
    assert pb.pos() == {}
    assert pb.run(pb.b.get_orders(open_only=True)) == []


def test_exit_orders_are_reduce_only(pb):
    pb.quote(20000.0)
    pb.bracket()
    pb.run(pb.b.flatten(CONTRACT))
    pb.quote(19900.0)
    pb.quote(20100.0)
    assert pb.pos() == {}


def test_state_persists_across_restart(tmp_path):
    p = PB(tmp_path)
    p.quote(20000.0)
    p.bracket()
    restored = PaperBroker(p.settings, p.clock, p.quotes, Store(p.settings.db_path))
    assert restored.positions[CONTRACT].qty == 1
    assert restored.balance == p.b.balance
    assert any(o.role is OrderRole.STOP_LOSS and o.status is OrderStatus.WORKING for o in restored.orders.values())
    assert restored._seq == p.b._seq
    p.loop.close()


def test_account_realized_pnl_is_per_trading_day(pb):
    pb.quote(20000.0)
    pb.bracket()
    pb.quote(19979.0)  # gaps 1pt through the stop: fill 19979 - 2 ticks = 19978.5
    acct = pb.run(pb.b.get_account())
    assert acct.realized_pnl_today == pytest.approx((19978.5 - 20000.5) * 2 - 3.0)
    pb.clock.set(datetime(2026, 10, 21, 10, 0, tzinfo=T0.tzinfo))
    pb.quote(20000.0)
    assert pb.run(pb.b.get_account()).realized_pnl_today == 0.0
