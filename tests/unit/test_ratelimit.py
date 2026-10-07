from nirantar.api.ratelimit import RateLimiter


def test_public_pay_confirm_is_limited_per_client_and_window_slides() -> None:
    rl = RateLimiter()
    path = "/v1/public/pay/ten_x.prq_y.z/confirm"
    assert all(rl.allow(path, "POST", "1.1.1.1", now=100.0)[0] for _ in range(10))
    ok, retry = rl.allow(path, "POST", "1.1.1.1", now=100.0)
    assert not ok and 1 <= retry <= 61
    assert rl.allow(path, "POST", "2.2.2.2", now=100.0)[0]          # another customer is unaffected
    assert rl.allow(path, "POST", "1.1.1.1", now=161.0)[0]          # the window slid


def test_viewing_and_webhooks_have_their_own_budgets_and_signed_in_paths_are_untouched() -> None:
    rl = RateLimiter()
    assert all(rl.allow("/v1/public/pay/t", "GET", "3.3.3.3", now=0)[0] for _ in range(60))
    assert not rl.allow("/v1/public/pay/t", "GET", "3.3.3.3", now=0)[0]
    assert all(rl.allow("/webhooks/razorpay/ten_1", "POST", "3.3.3.3", now=0)[0] for _ in range(500))
    assert all(rl.allow("/v1/overview", "GET", "3.3.3.3", now=0)[0] for _ in range(5000))
