import io
import urllib.error

import pytest

from braindamage import config, steamapis_api


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setattr(config, "STEAMAPIS_KEY", "test-key")


class TestFetchCsfloatPrice:
    def test_parses_a_priced_item(self, monkeypatch):
        body = {"name": "AK-47 | Redline (Field-Tested)", "priceUSD": 45.99, "offerCount": 12, "updatedAt": 1700000000}
        monkeypatch.setattr(steamapis_api, "_fetch_item", lambda name: body)

        quote = steamapis_api.fetch_csfloat_price("AK-47 | Redline (Field-Tested)")

        assert quote == steamapis_api.CsfloatQuote(
            market_hash_name="AK-47 | Redline (Field-Tested)",
            price=45.99,
            offer_count=12,
            updated_at=1700000000,
            raw=body,
        )

    def test_returns_none_when_item_not_found(self, monkeypatch):
        monkeypatch.setattr(steamapis_api, "_fetch_item", lambda name: None)
        assert steamapis_api.fetch_csfloat_price("Doesn't Exist (Field-Tested)") is None

    def test_returns_none_when_price_is_null(self, monkeypatch):
        monkeypatch.setattr(steamapis_api, "_fetch_item", lambda name: {"name": "X", "priceUSD": None})
        assert steamapis_api.fetch_csfloat_price("X") is None

    def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.setattr(config, "STEAMAPIS_KEY", None)
        with pytest.raises(RuntimeError, match="STEAMAPIS_KEY"):
            steamapis_api.fetch_csfloat_price("X")

    def test_propagates_api_errors(self, monkeypatch):
        def boom(name):
            raise steamapis_api.SteamApisAPIError(500, "kaboom")

        monkeypatch.setattr(steamapis_api, "_fetch_item", boom)
        with pytest.raises(steamapis_api.SteamApisAPIError):
            steamapis_api.fetch_csfloat_price("X")


class TestRaiseForHttpError:
    def _http_error(self, code, headers=None, body=b"bad request"):
        return urllib.error.HTTPError(
            url="https://marketplaceapi.steamapis.com/v2/items",
            code=code,
            msg="error",
            hdrs=headers or {},
            fp=io.BytesIO(body),
        )

    def test_429_raises_rate_limit_error_with_retry_after(self):
        exc = self._http_error(429, headers={"Retry-After": "12"})
        with pytest.raises(steamapis_api.SteamApisRateLimitError) as excinfo:
            steamapis_api._raise_for_http_error(exc)
        assert excinfo.value.retry_after == 12.0
        assert excinfo.value.status_code == 429

    def test_429_without_retry_after_header(self):
        exc = self._http_error(429)
        with pytest.raises(steamapis_api.SteamApisRateLimitError) as excinfo:
            steamapis_api._raise_for_http_error(exc)
        assert excinfo.value.retry_after is None

    def test_other_status_raises_generic_api_error(self):
        exc = self._http_error(403, body=b"MISSING_API_KEY")
        with pytest.raises(steamapis_api.SteamApisAPIError) as excinfo:
            steamapis_api._raise_for_http_error(exc)
        assert excinfo.value.status_code == 403
        assert "MISSING_API_KEY" in str(excinfo.value)


class TestFetchItemConnectionFailure:
    def test_url_error_raises_api_error_with_no_status_code(self, monkeypatch):
        def fake_urlopen(request, timeout):
            raise urllib.error.URLError("Name or service not known")

        monkeypatch.setattr(steamapis_api.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(steamapis_api.SteamApisAPIError) as excinfo:
            steamapis_api._fetch_item("X (Field-Tested)")
        assert excinfo.value.status_code is None

    def test_404_returns_none(self, monkeypatch):
        import io

        def fake_urlopen(request, timeout):
            raise urllib.error.HTTPError(
                url="https://marketplaceapi.steamapis.com/v2/items",
                code=404,
                msg="not found",
                hdrs={},
                fp=io.BytesIO(b"not found"),
            )

        monkeypatch.setattr(steamapis_api.urllib.request, "urlopen", fake_urlopen)

        assert steamapis_api._fetch_item("X (Field-Tested)") is None
