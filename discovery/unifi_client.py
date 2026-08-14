"""Client for the local UniFi Network Integration API (v1, X-API-KEY auth).

Confirmed against the auto-generated OpenAPI client in
https://github.com/wittypluck/ha-unifi-network (2026), since Ubiquiti's own
docs page requires an authenticated session to view. Endpoints used:

  GET /proxy/network/integration/v1/sites
  GET /proxy/network/integration/v1/sites/{site_id}/clients
  GET /proxy/network/integration/v1/sites/{site_id}/devices
  GET /proxy/network/integration/v1/sites/{site_id}/networks
  GET /proxy/network/integration/v1/sites/{site_id}/networks/{network_id}

All are paginated with {offset, limit, count, totalCount, data: [...]} except
the single-network detail fetch. Note: this API does not expose per-client
VLAN/uplink topology directly (that only exists on the older
username/password Controller API) -- macAddress on clients is also not
guaranteed to be populated. IP address is used as the join key back to
nmap's ARP-derived MAC for reconciliation, and also (via subnet matching
against /networks) to derive each device's VLAN -- confirmed live: the
networks overview gives {name, vlanId}, and the per-network detail adds
ipv4Configuration.{hostIpAddress, prefixLength} for the subnet.

A second, older endpoint is also used, solely because the v1 API above has
no serial number field anywhere:

  GET /proxy/network/api/s/{site_name}/stat/device

This is the legacy username/password-era Controller API, confirmed live to
also accept the same X-API-KEY. Two things differ from the v1 endpoints
above: it's keyed by site *name* (e.g. "default"), not the v1 site UUID, and
its response is a flat {"data": [...]} with no pagination envelope.
"""

import os
from typing import Any

import httpx

API_PREFIX = "/proxy/network/integration/v1"
LEGACY_API_PREFIX = "/proxy/network/api"


class UnifiClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        verify_tls: bool | None = None,
    ):
        self.base_url = (base_url or os.environ.get("UNIFI_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("UNIFI_API_KEY", "")
        if verify_tls is None:
            # Default to verifying: the API key is a bearer credential and this
            # controller shares the LAN with the untrusted devices this app
            # inventories, so an unverified connection is MITM-able. A
            # self-signed UDM needs an explicit UNIFI_VERIFY_TLS=false opt-out
            # (see .env.example / README).
            verify_tls = os.environ.get("UNIFI_VERIFY_TLS", "true").strip().lower() != "false"
        self._client = httpx.Client(
            base_url=self.base_url,
            verify=verify_tls,
            headers={"X-API-KEY": self.api_key, "Accept": "application/json"},
            timeout=30.0,
        )

    def close(self):
        self._client.close()

    def _paginate(self, path: str, params: dict | None = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("limit", 200)
        offset = 0
        results: list[dict[str, Any]] = []
        while True:
            params["offset"] = offset
            resp = self._client.get(f"{API_PREFIX}{path}", params=params)
            resp.raise_for_status()
            page = resp.json()
            data = page.get("data", [])
            results.extend(data)
            total = page.get("totalCount", len(results))
            offset += page.get("limit", len(data) or 1)
            if not data or offset >= total:
                break
        return results

    def list_sites(self) -> list[dict[str, Any]]:
        return self._paginate("/sites")

    def resolve_site_id(self, site_ref: str) -> str:
        """Accepts either a site UUID or the site's internalReference/name."""
        sites = self.list_sites()
        for site in sites:
            if site.get("id") == site_ref:
                return site["id"]
        for site in sites:
            if site.get("internalReference") == site_ref or site.get("name") == site_ref:
                return site["id"]
        if sites:
            return sites[0]["id"]
        raise RuntimeError(f"No UniFi sites found (looked for '{site_ref}')")

    def list_clients(self, site_id: str) -> list[dict[str, Any]]:
        return self._paginate(f"/sites/{site_id}/clients")

    def list_devices(self, site_id: str) -> list[dict[str, Any]]:
        return self._paginate(f"/sites/{site_id}/devices")

    def list_devices_legacy(self, site_name: str) -> list[dict[str, Any]]:
        """Infrastructure devices via the legacy Controller API, keyed by
        site *name* (not the v1 site UUID -- pass e.g. "default", not
        resolve_site_id()'s result). Each entry includes 'serial' and a
        SKU-style 'model', which the v1 /devices endpoint omits."""
        resp = self._client.get(f"{LEGACY_API_PREFIX}/s/{site_name}/stat/device")
        resp.raise_for_status()
        return resp.json().get("data", [])

    def list_networks(self, site_id: str) -> list[dict[str, Any]]:
        return self._paginate(f"/sites/{site_id}/networks")

    def get_network(self, site_id: str, network_id: str) -> dict[str, Any]:
        resp = self._client.get(f"{API_PREFIX}/sites/{site_id}/networks/{network_id}")
        resp.raise_for_status()
        return resp.json()

    def list_networks_with_subnets(self, site_id: str) -> list[dict[str, Any]]:
        """Networks overview enriched with each network's IPv4 subnet, since
        the overview alone doesn't include it (confirmed live)."""
        networks = self.list_networks(site_id)
        enriched = []
        for net in networks:
            detail = self.get_network(site_id, net["id"])
            ipv4 = detail.get("ipv4Configuration") or {}
            enriched.append(
                {
                    "id": net["id"],
                    "name": net.get("name"),
                    "vlan_id": net.get("vlanId"),
                    "host_ip": ipv4.get("hostIpAddress"),
                    "prefix_length": ipv4.get("prefixLength"),
                }
            )
        return enriched
