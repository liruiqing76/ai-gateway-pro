import pytest
from unittest.mock import AsyncMock, MagicMock
import httpx
from backend.api.provider_client import BaseProviderClient, NVIDIAProvider, AMDProvider, CustomProvider, ProviderError

@pytest.fixture
def mock_http_client():
    return AsyncMock()

def test_base_provider_cannot_be_instantiated():
    with pytest.raises(TypeError):
        BaseProviderClient()

def test_nvidia_provider_init():
    prov = NVIDIAProvider(api_key="nv-key", base_url="https://api.nvidia.com/v1")
    assert prov.api_key == "nv-key"
    assert prov.base_url == "https://api.nvidia.com/v1"

def test_amd_provider_init():
    prov = AMDProvider(api_key="amd-key", base_url="https://developer.amd.com.cn/radeon/api/v1")
    assert prov.api_key == "amd-key"
    assert prov.base_url == "https://developer.amd.com.cn/radeon/api/v1"

def test_custom_provider_init():
    prov = CustomProvider(api_key="cust-key", base_url="https://custom.example.com/v1")
    assert prov.api_key == "cust-key"
    assert prov.base_url == "https://custom.example.com/v1"

def test_provider_repr():
    prov = CustomProvider(api_key="key", base_url="http://test")
    # Ensure __repr__ or __str__ doesn't crash
    rep = repr(prov)
    assert "CustomProvider" in rep

@pytest.mark.asyncio
async def test_provider_post_success(mock_http_client):
    prov = CustomProvider(api_key="key", base_url="http://test")
    prov._client = mock_http_client
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"result": "ok"}
    mock_http_client.post.return_value = mock_resp

    result = await prov.post("/endpoint", json={"data": 1})
    assert result == {"result": "ok"}
    mock_http_client.post.assert_awaited_once_with(
        "http://test/endpoint",
        json={"data": 1},
        headers={"Authorization": "Bearer key", "Content-Type": "application/json"}
    )

@pytest.mark.asyncio
async def test_provider_post_http_error(mock_http_client):
    prov = CustomProvider(api_key="key", base_url="http://test")
    prov._client = mock_http_client
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request"
    mock_http_client.post.return_value = mock_resp

    with pytest.raises(ProviderError) as excinfo:
        await prov.post("/endpoint", json={})
    assert "400" in str(excinfo.value)
    mock_http_client.post.assert_awaited_once()

@pytest.mark.asyncio
async def test_provider_post_network_error(mock_http_client):
    prov = CustomProvider(api_key="key", base_url="http://test")
    prov._client = mock_http_client
    mock_http_client.post.side_effect = httpx.RequestError("Network failure")

    with pytest.raises(httpx.RequestError):
        await prov.post("/endpoint", json={})
    mock_http_client.post.assert_awaited_once()

@pytest.mark.asyncio
async def test_provider_close_calls_client_close(mock_http_client):
    prov = CustomProvider(api_key="key", base_url="http://test")
    prov._client = mock_http_client
    await prov.close()
    mock_http_client.aclose.assert_awaited_once()

def test_provider_headers_include_auth():
    prov = CustomProvider(api_key="mykey", base_url="http://test")
    headers = prov._get_headers()
    assert headers["Authorization"] == "Bearer mykey"
    assert headers["Content-Type"] == "application/json"