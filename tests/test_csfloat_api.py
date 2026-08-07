import pytest

from braindamage import csfloat_api


def _raw_listing(
    id_, price_cents, float_value, market_hash_name="AK-47 | Redline (Field-Tested)", listing_type="buy_now"
):
    return {
        "id": id_,
        "price": price_cents,
        "type": listing_type,
        "item": {
            "market_hash_name": market_hash_name,
            "wear_name": "Field-Tested",
            "float_value": float_value,
        },
    }


class TestParseListing:
    def test_maps_cents_to_usd_and_pulls_item_fields(self):
        raw = _raw_listing("123", 26000, 0.2222)
        listing = csfloat_api._parse_listing(raw)
        assert listing == csfloat_api.FloatListing(
            listing_id="123",
            market_hash_name="AK-47 | Redline (Field-Tested)",
            wear_name="Field-Tested",
            float_value=0.2222,
            price=260.0,
            listing_type="buy_now",
            raw=raw,
        )


class TestCheapestListingsInFloatRange:
    def test_returns_parsed_listings_and_sends_correct_params(self, monkeypatch):
        captured = {}

        def fake_fetch(params):
            captured.update(params)
            return [_raw_listing("1", 1000, 0.16), _raw_listing("2", 2000, 0.20)]

        monkeypatch.setattr(csfloat_api, "_fetch_listings_with_retry", fake_fetch)

        listings = csfloat_api.cheapest_listings_in_float_range(
            "AK-47 | Redline (Field-Tested)", 0.15, 0.22, limit=10
        )

        assert [listing.price for listing in listings] == [10.0, 20.0]
        assert captured == {
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
            "min_float": "0.150000",
            "max_float": "0.220000",
            "type": "buy_now",
            "sort_by": "lowest_price",
            "limit": 10,
        }


class TestLowestAsk:
    def test_returns_cheapest_price(self, monkeypatch):
        monkeypatch.setattr(
            csfloat_api, "_fetch_listings_with_retry", lambda params: [_raw_listing("1", 500, 0.05)]
        )
        assert csfloat_api.lowest_ask("AK-47 | Redline (Factory New)") == pytest.approx(5.0)

    def test_returns_none_when_nothing_listed(self, monkeypatch):
        monkeypatch.setattr(csfloat_api, "_fetch_listings_with_retry", lambda params: [])
        assert csfloat_api.lowest_ask("AK-47 | Redline (Factory New)") is None


@pytest.fixture
def isolated_limiter(monkeypatch):
    """A throwaway _RateLimiter swapped in for the module-level singleton --
    without this, a test that triggers slow_down() would permanently mutate
    the shared _limiter's pace for every later test in the process, since
    monkeypatch only undoes attributes it set directly, not mutations a real
    (unpatched) method makes to an object's own state."""
    fresh = csfloat_api._RateLimiter(0.01)  # nonzero so slow_down()'s doubling is observable
    monkeypatch.setattr(fresh, "wait", lambda: None)
    monkeypatch.setattr(csfloat_api, "_limiter", fresh)
    return fresh


class TestRetryBehavior:
    def test_retries_on_429_then_succeeds(self, monkeypatch, isolated_limiter):
        monkeypatch.setattr(csfloat_api.time, "sleep", lambda _seconds: None)
        calls = {"count": 0}

        def flaky_fetch(params):
            calls["count"] += 1
            if calls["count"] < 3:
                raise csfloat_api.CsfloatRateLimitError(retry_after=0.0)
            return [_raw_listing("1", 100, 0.1)]

        monkeypatch.setattr(csfloat_api, "_fetch_listings", flaky_fetch)

        listings = csfloat_api.cheapest_listings_in_float_range("X (Factory New)", 0.0, 0.07)

        assert calls["count"] == 3
        assert len(listings) == 1

    def test_gives_up_after_exhausting_retries(self, monkeypatch, isolated_limiter):
        monkeypatch.setattr(csfloat_api.time, "sleep", lambda _seconds: None)

        def always_rate_limited(params):
            raise csfloat_api.CsfloatRateLimitError(retry_after=0.0)

        monkeypatch.setattr(csfloat_api, "_fetch_listings", always_rate_limited)

        with pytest.raises(csfloat_api.CsfloatRateLimitError):
            csfloat_api.cheapest_listings_in_float_range("X (Factory New)", 0.0, 0.07)

    def test_a_429_permanently_slows_the_steady_pace_down(self, monkeypatch, isolated_limiter):
        """Not just a retry-then-give-up on one call -- every later call in the
        same run should inherit the slower pace too."""
        monkeypatch.setattr(csfloat_api.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(
            csfloat_api, "_fetch_listings", lambda params: (_ for _ in ()).throw(csfloat_api.CsfloatRateLimitError(0.0))
        )
        starting_interval = isolated_limiter._min_interval

        with pytest.raises(csfloat_api.CsfloatRateLimitError):
            csfloat_api.cheapest_listings_in_float_range("X (Factory New)", 0.0, 0.07)

        assert isolated_limiter._min_interval > starting_interval

    def test_backoff_saturating_at_the_ceiling_raises_immediately_instead_of_retrying(
        self, monkeypatch, isolated_limiter
    ):
        """A server-supplied Retry-After at or past _MAX_BACKOFF_SECONDS means
        CSFloat is sustained-blocked, not flaky -- this should stop retrying
        (and sleeping out that wait) right away rather than exhausting the
        rest of max_retries."""
        monkeypatch.setattr(csfloat_api.time, "sleep", lambda _seconds: None)
        calls = {"count": 0}

        def always_hard_limited(params):
            calls["count"] += 1
            raise csfloat_api.CsfloatRateLimitError(retry_after=csfloat_api._MAX_BACKOFF_SECONDS)

        monkeypatch.setattr(csfloat_api, "_fetch_listings", always_hard_limited)

        with pytest.raises(csfloat_api.CsfloatMaxBackoffExceeded):
            csfloat_api.cheapest_listings_in_float_range("X (Factory New)", 0.0, 0.07)

        assert calls["count"] == 1  # gave up on the very first 429, no retries

    def test_backoff_table_reaching_the_ceiling_also_raises_immediately(self, monkeypatch, isolated_limiter):
        """Same ceiling check, but hit via the fixed backoff table (no
        server-supplied Retry-After) rather than a large Retry-After value."""
        monkeypatch.setattr(csfloat_api.time, "sleep", lambda _seconds: None)
        calls = {"count": 0}

        def always_rate_limited(params):
            calls["count"] += 1
            raise csfloat_api.CsfloatRateLimitError(retry_after=0.0)

        monkeypatch.setattr(csfloat_api, "_fetch_listings", always_rate_limited)

        with pytest.raises(csfloat_api.CsfloatMaxBackoffExceeded):
            csfloat_api.cheapest_listings_in_float_range("X (Factory New)", 0.0, 0.07)

        # _RETRY_BACKOFF_SECONDS reaches its cap (300s) on the 6th attempt --
        # stops there instead of continuing through the remaining retries.
        assert calls["count"] == len(csfloat_api._RETRY_BACKOFF_SECONDS)


class TestRateLimiterSlowDown:
    def test_doubles_the_interval(self):
        limiter = csfloat_api._RateLimiter(1.0)
        limiter.slow_down()
        assert limiter._min_interval == pytest.approx(2.0)

    def test_caps_at_the_maximum(self):
        limiter = csfloat_api._RateLimiter(csfloat_api._MAX_STEADY_INTERVAL_SECONDS)
        limiter.slow_down()
        assert limiter._min_interval == csfloat_api._MAX_STEADY_INTERVAL_SECONDS
