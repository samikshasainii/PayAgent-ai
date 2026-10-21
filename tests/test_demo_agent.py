import pytest
import httpx
from scripts.demo_agent import run_rest_demo, wait_for_gate2


@pytest.mark.asyncio
async def test_counter6_demo_submitted_timeout(capsys):
    """
    Counter-Test 6.1: Payout remains SUBMITTED past polling cap -> explicit timeout message & sys.exit(1).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and "/api/v1/payout-requests" in url:
            return httpx.Response(202, json={"payout_request_id": "req-timeout-1", "status": "RECEIVED"})
        if request.method == "GET" and "/api/v1/payout-requests/req-timeout-1" in url:
            # First Gate 2 decides APPROVED -> SUBMITTED, then stays SUBMITTED
            return httpx.Response(200, json={
                "payout_request_id": "req-timeout-1",
                "status": "SUBMITTED",
                "decision_reasons": ["AUTO_APPROVED"],
                "sender_batch_id": "batch-1",
                "payout_batch_id": "pp-1"
            })
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock-payagent")
    with pytest.raises(SystemExit) as excinfo:
        await run_rest_demo(
            agent_id="test-agent",
            api_key="test-key",
            scenario="happy",
            run_salt="salt1",
            client=client,
            poll_interval=0.01,
            poll_cap_s=0.05
        )
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "TIMEOUT: Payout remained SUBMITTED after 0s polling cap" in captured.out


@pytest.mark.asyncio
async def test_counter6_demo_terminal_failed(capsys):
    """
    Counter-Test 6.2: Payout enters terminal state FAILED -> explicit failure message & sys.exit(1).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and "/api/v1/payout-requests" in url:
            return httpx.Response(202, json={"payout_request_id": "req-failed-1", "status": "RECEIVED"})
        if request.method == "GET" and "/api/v1/payout-requests/req-failed-1" in url:
            return httpx.Response(200, json={
                "payout_request_id": "req-failed-1",
                "status": "FAILED",
                "decision_reasons": ["AUTO_APPROVED"],
                "sender_batch_id": "batch-1",
                "payout_batch_id": "pp-1"
            })
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock-payagent")
    with pytest.raises(SystemExit) as excinfo:
        await run_rest_demo(
            agent_id="test-agent",
            api_key="test-key",
            scenario="happy",
            run_salt="salt2",
            client=client,
            poll_interval=0.01,
            poll_cap_s=1.0
        )
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "FAILURE: Payout entered terminal state FAILED" in captured.out


@pytest.mark.asyncio
async def test_counter6_demo_http_500_polling(capsys):
    """
    Counter-Test 6.3: HTTP 500 error during status polling -> explicit report & sys.exit(1).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and "/api/v1/payout-requests" in url:
            return httpx.Response(202, json={"payout_request_id": "req-500-1", "status": "RECEIVED"})
        if request.method == "GET" and "/api/v1/payout-requests/req-500-1" in url:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock-payagent")
    with pytest.raises(SystemExit) as excinfo:
        await run_rest_demo(
            agent_id="test-agent",
            api_key="test-key",
            scenario="happy",
            run_salt="salt3",
            client=client,
            poll_interval=0.01,
            poll_cap_s=1.0
        )
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Polling failed with HTTP 500" in captured.out


@pytest.mark.asyncio
async def test_counter6_demo_gate2_timeout(capsys):
    """
    Counter-Test 6.4: Gate 2 never decides (exceeds timeout) -> explicit timeout message & sys.exit(1).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and "/api/v1/payout-requests" in url:
            return httpx.Response(202, json={"payout_request_id": "req-gate2-to", "status": "RECEIVED"})
        if request.method == "GET" and "/api/v1/payout-requests/req-gate2-to" in url:
            return httpx.Response(200, json={"payout_request_id": "req-gate2-to", "status": "RECEIVED"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock-payagent")
    with pytest.raises(SystemExit) as excinfo:
        await run_rest_demo(
            agent_id="test-agent",
            api_key="test-key",
            scenario="happy",
            run_salt="salt4",
            client=client,
            poll_interval=0.01,
            poll_cap_s=1.0,
            gate2_timeout_s=0.05
        )
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Gate 2 did not decide within 0.1s (current status: RECEIVED)" in captured.out
