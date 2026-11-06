"""Search field, debounce, Enter, stale results, suggestion overlay, IME (design B.3, E.2)."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from fake_engine import *  # noqa: F401,F403
from fake_engine import FakeEngineClient, search, wait_result


def _type(win, text):
    f = win.bar.field
    f.setFocus()
    QTest.keyClicks(f, text)


def test_one_request_per_debounce_window_and_fast_keystrokes(window, client, qtbot):
    f = window.bar.field
    f.setFocus()
    # Warm-up: the first key events pay one-time costs (font metrics, popup, layout).
    # Type two keys and let that search go out so the measured run starts settled.
    before_warm = len(client.searches)
    QTest.keyClicks(f, "gi")
    qtbot.waitUntil(lambda: len(client.searches) > before_warm and not window.bar.timer.isActive(),
                    timeout=1000)
    qtbot.wait(300)
    before = len(client.searches)
    # Measure the per-keystroke handler (last_handler_ms is set synchronously by each key).
    typed = "fttop icon png docs"
    handler_ms = []
    for ch in typed:
        QTest.keyClicks(f, ch)
        handler_ms.append(window.bar.last_handler_ms)
        qtbot.wait(30)            # faster than the 120 ms debounce
    assert len(client.searches) == before        # nothing sent while typing
    qtbot.waitUntil(lambda: len(client.searches) == before + 1, timeout=1000)
    qtbot.wait(300)
    assert len(client.searches) == before + 1    # exactly one request
    assert client.last.text == "gifttop icon png docs" and client.last.commit_history is False
    handler_ms.sort()
    median = handler_ms[len(handler_ms) // 2]
    # Median is the regression gate; the generous max only catches a stall, not load jitter.
    assert median <= 2.0, f"median keystroke handler {median:.2f} ms over {len(handler_ms)} keys"
    assert handler_ms[-1] <= 15.0, f"slowest keystroke handler {handler_ms[-1]:.2f} ms"


def test_enter_flushes_immediately_with_history_commit(window, client, qtbot):
    before = len(client.searches)
    _type(window, "gift")
    QTest.keyClick(window.bar.field, Qt.Key.Key_Return)
    assert len(client.searches) == before + 1
    assert client.last.text == "gift" and client.last.commit_history is True
    assert not window.bar.timer.isActive()
    qtbot.wait(250)
    assert len(client.searches) == before + 1    # the debounce did not fire again


def test_search_button_flushes(window, client):
    _type(window, "gift")
    window.bar.search_btn.click()
    assert client.last.text == "gift" and client.last.commit_history


def test_enter_without_pending_search_opens_current_result(window, client, qtbot, fake_os):
    search(qtbot, window, "gifttopicon.png")
    QTest.keyClick(window.bar.field, Qt.Key.Key_Return)    # nothing pending: open the first row
    assert fake_os and fake_os[-1][0] == "open"
    assert ("record_open" in [op for op, *_ in client.user_ops]) or client.searches[-1].commit_history


def test_stale_generation_is_never_shown(make_window, dataset, qtbot):
    client = FakeEngineClient(dataset, auto=False)
    win = make_window(client)
    f = win.bar.field
    f.setFocus()
    QTest.keyClicks(f, "invoice")
    QTest.keyClick(f, Qt.Key.Key_Return)
    f.selectAll()
    QTest.keyClicks(f, "gifttopicon")
    QTest.keyClick(f, Qt.Key.Key_Return)
    assert len(client.held) == 2
    client.deliver(1)            # the newest first
    client.deliver(0)            # then the stale one
    QApplication.processEvents()
    names = [r.name.lower() for r in win.results.model.rows]
    assert names and all("gifttopicon" in n for n in names)
    assert win._last_result.gen == client.current_gen


def test_suggestions_overlay_keeps_focus_and_keys(window, client, dataset, qtbot):
    dataset.history = ["gift card receipts"]
    _type(window, "gift")
    popup = window.bar.popup
    qtbot.waitUntil(popup.isVisible, timeout=1000)
    assert QApplication.focusWidget() is window.bar.field           # never steals focus
    assert popup.highlighted() == -1                                 # nothing highlighted at first
    assert popup.items[0].kind == "history" and len(popup.items) <= 6
    QTest.keyClick(window.bar.field, Qt.Key.Key_Down)
    assert popup.highlighted() == 0
    QTest.keyClick(window.bar.field, Qt.Key.Key_Down)
    QTest.keyClick(window.bar.field, Qt.Key.Key_Up)
    assert popup.highlighted() == 0
    QTest.keyClick(window.bar.field, Qt.Key.Key_Return)               # accepts the highlighted row
    assert window.bar.text() == "gift card receipts"
    assert client.last.text == "gift card receipts" and client.last.commit_history
    assert not popup.isVisible()
    assert QApplication.focusWidget() is window.bar.field


def test_tab_accepts_name_suggestion(window, client, qtbot):
    _type(window, "gifttop")
    popup = window.bar.popup
    qtbot.waitUntil(popup.isVisible, timeout=1000)
    QTest.keyClick(window.bar.field, Qt.Key.Key_Down)
    chosen = popup.items[popup.highlighted()].text
    QTest.keyClick(window.bar.field, Qt.Key.Key_Tab)
    assert window.bar.text() == chosen and client.last.text == chosen
    assert QApplication.focusWidget() is window.bar.field


def test_popup_hides_on_esc_enter_and_focus_out(window, client, qtbot):
    popup = window.bar.popup
    _type(window, "gifttop")
    qtbot.waitUntil(popup.isVisible, timeout=1000)
    QTest.keyClick(window.bar.field, Qt.Key.Key_Escape)
    assert not popup.isVisible() and window.bar.text() == "gifttop"   # Esc closes, keeps text
    # Not shown again for the same text.
    window._send_search()
    qtbot.wait(50)
    assert not popup.isVisible()

    QTest.keyClicks(window.bar.field, "i")
    qtbot.waitUntil(popup.isVisible, timeout=1000)
    QTest.keyClicks(window.bar.field, "c")                             # pending: popup still up
    assert popup.isVisible() and window.bar.timer.isActive()
    QTest.keyClick(window.bar.field, Qt.Key.Key_Return)                # nothing highlighted
    assert not popup.isVisible()
    assert client.last.text == "gifttopic" and client.last.commit_history
    window.bar.field.backspace()

    QTest.keyClicks(window.bar.field, "c")
    qtbot.waitUntil(popup.isVisible, timeout=1000)
    window.results.view.setFocus()
    assert not popup.isVisible()


def test_navigating_results_hides_popup_and_keeps_focus(window, qtbot):
    search(qtbot, window, "gifttopicon")
    popup = window.bar.popup
    QTest.keyClicks(window.bar.field, "x")
    window.bar.field.backspace()
    window.bar.flush()
    wait_result(qtbot, window)
    window.bar.hide_popup()
    QTest.keyClick(window.bar.field, Qt.Key.Key_Down)
    assert window.results.view.currentIndex().row() == 0
    QTest.keyClick(window.bar.field, Qt.Key.Key_Down)
    assert window.results.view.currentIndex().row() == 1
    QTest.keyClick(window.bar.field, Qt.Key.Key_Up)
    assert window.results.view.currentIndex().row() == 0
    QTest.keyClick(window.bar.field, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
    assert window.results.view.currentIndex().row() == window.results.model.rowCount() - 1
    QTest.keyClick(window.bar.field, Qt.Key.Key_Home, Qt.KeyboardModifier.ControlModifier)
    assert window.results.view.currentIndex().row() == 0
    QTest.keyClick(window.bar.field, Qt.Key.Key_PageDown)
    assert window.results.view.currentIndex().row() > 1
    assert QApplication.focusWidget() is window.bar.field
    assert not popup.isVisible()


def test_no_suggestions_for_regex_queries(window, client, qtbot):
    _type(window, "re:^gift")
    qtbot.waitUntil(lambda: bool(client.searches) and client.last.text == "re:^gift", timeout=1000)
    assert client.last.suggestions is False
    qtbot.wait(50)
    assert not window.bar.popup.isVisible()


def test_ime_preedit_sends_nothing(window, client, qtbot):
    f = window.bar.field
    f.setFocus()
    before = len(client.searches)
    QApplication.sendEvent(f, QInputMethodEvent("にほ", []))
    qtbot.wait(250)
    assert len(client.searches) == before
    commit = QInputMethodEvent("", [])
    commit.setCommitString("日本")
    QApplication.sendEvent(f, commit)
    qtbot.waitUntil(lambda: len(client.searches) == before + 1, timeout=1000)
    assert client.last.text == "日本"


def test_empty_field_focus_requests_history_only(window, client, dataset, qtbot):
    dataset.history = ["budget 2025"]
    before = len(client.searches)
    window.act_find.trigger()
    assert len(client.searches) == before + 1
    req = client.last
    assert req.text == "" and req.limit == 0 and req.suggestions and not req.commit_history
    qtbot.waitUntil(window.bar.popup.isVisible, timeout=1000)
    assert window.bar.popup.items[0].text == "budget 2025"
    assert not window.results.is_showing_results()


def test_shortcut_hint_and_accessibility():
    from fast_file_search.ui.search_bar import shortcut_text

    assert shortcut_text("Ctrl+K") == QKeySequence("Ctrl+K").toString(QKeySequence.SequenceFormat.NativeText)


def test_hint_sits_inside_the_field_and_left_of_clear_button(window, qtbot):
    f = window.bar.field
    hint = f.hint
    assert hint.isVisible() and hint.text()
    assert f.rect().contains(hint.geometry())
    right_empty = hint.geometry().right()
    QTest.keyClicks(f, "abc")
    f._place_hint()
    assert hint.geometry().right() < right_empty          # moved left for the clear button
    assert f.textMargins().right() >= hint.width()


def test_escape_chain_popup_then_text_then_minimize(window, qtbot, monkeypatch):
    f = window.bar.field
    _type(window, "gifttop")
    qtbot.waitUntil(window.bar.popup.isVisible, timeout=1000)
    QTest.keyClick(f, Qt.Key.Key_Escape)
    assert window.bar.text() == "gifttop" and not window.bar.popup.isVisible()
    QTest.keyClick(f, Qt.Key.Key_Escape)
    assert window.bar.text() == ""
    assert window.results.empty_state.kind == "ready"
    hidden = []
    monkeypatch.setattr(window, "showMinimized", lambda: hidden.append("min"))
    monkeypatch.setattr(window, "hide", lambda: hidden.append("hide"))
    QTest.keyClick(f, Qt.Key.Key_Escape)
    assert hidden in (["min"], ["hide"])
