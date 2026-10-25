"""0.79: 4개 방송사가 거의 같은 때 정답·사연을 내도 먼저 온 것부터 하나씩 — 한 방송사 입력이 끝나야 다음 방송사."""

import threading
import time

import pytest

from radio_helper.autofill import AutofillError
from radio_helper.quizbot import runner


def test_sends_go_one_at_a_time_in_arrival_order(data_dir, monkeypatch):
    monkeypatch.setattr(runner, "TURN_GAP_SECONDS", 0.05)
    log, inside = [], []

    def station(name, arrive):
        time.sleep(arrive)
        with runner.shared_input_lock(10):
            inside.append(name)
            assert len(inside) == 1                       # 동시에 두 방송사가 입력하지 않음
            log.append(name)
            time.sleep(0.2)                               # 입력하는 시간
            inside.remove(name)

    threads = [threading.Thread(target=station, args=(n, i * 0.03)) for i, n in enumerate(["SBS", "MBC", "KBS", "CBS"])]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert log == ["SBS", "MBC", "KBS", "CBS"]
    assert not list((data_dir / runner.QUEUE_DIR).iterdir())                 # 줄 표는 다 치움


def test_zero_wait_does_not_jump_the_line(data_dir, monkeypatch):
    monkeypatch.setattr(runner, "TURN_GAP_SECONDS", 0.0)
    holding = threading.Event()
    release = threading.Event()

    def sender():
        with runner.shared_input_lock(5):
            holding.set()
            release.wait(5)
    t = threading.Thread(target=sender)
    t.start()
    holding.wait(5)
    with pytest.raises(AutofillError):                                       # 채팅창 찍기의 '확인 창 닫기'는 비었을 때만
        with runner.shared_input_lock(0):
            pass
    release.set()
    t.join()
    with runner.shared_input_lock(0):                                        # 비면 바로
        pass


def test_dead_listener_ticket_is_cleared(data_dir, monkeypatch):
    monkeypatch.setattr(runner, "TURN_GAP_SECONDS", 0.0)
    q = data_dir / runner.QUEUE_DIR
    q.mkdir(exist_ok=True)
    (q / f"{time.time() - 5:.6f}_999999_abcdef").write_text("")                # 꺼진 청취기의 표
    with runner.shared_input_lock(2):
        pass
