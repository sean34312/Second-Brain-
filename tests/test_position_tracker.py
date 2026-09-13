from swing_bot.position_tracker import Position, PositionTracker


def test_open_and_close_position_realizes_pnl():
    tracker = PositionTracker()
    pos = Position(symbol="AAPL", quantity=10, entry_price=100.0, stop_loss_price=98.0, take_profit_price=106.0)
    tracker.open_position(pos)
    assert tracker.has_position("AAPL")
    assert tracker.count() == 1

    pnl = tracker.close_position("AAPL", exit_price=105.0)
    assert pnl == 50.0
    assert tracker.realized_pnl_total == 50.0
    assert not tracker.has_position("AAPL")
    assert tracker.count() == 0


def test_stop_out_and_take_profit_flags():
    pos = Position(symbol="MSFT", quantity=5, entry_price=200.0, stop_loss_price=196.0, take_profit_price=212.0)
    assert pos.should_stop_out(195.0) is True
    assert pos.should_stop_out(200.0) is False
    assert pos.should_take_profit(213.0) is True
    assert pos.should_take_profit(200.0) is False


def test_unrealized_pnl():
    pos = Position(symbol="NVDA", quantity=4, entry_price=50.0, stop_loss_price=49.0, take_profit_price=53.0)
    assert pos.unrealized_pnl(52.0) == 8.0
    assert round(pos.unrealized_pnl_pct(52.0), 4) == 0.04


def test_total_unrealized_pnl_multiple_positions():
    tracker = PositionTracker()
    tracker.open_position(Position("AAPL", 10, 100.0, 98.0, 106.0))
    tracker.open_position(Position("MSFT", 5, 200.0, 196.0, 212.0))
    total = tracker.total_unrealized_pnl({"AAPL": 102.0, "MSFT": 198.0})
    assert total == (2.0 * 10) + (-2.0 * 5)
