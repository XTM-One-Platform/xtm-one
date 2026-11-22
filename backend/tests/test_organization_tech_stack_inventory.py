"""The tech stack built from the assets' software (#1178, S4): pure rule of the inventory items."""

from __future__ import annotations

from app.services.organization_asset_store import inventory_items


def _v(vendor: str, product: str, version: str, count: int, cpe: str = "") -> dict[str, object]:
    return {
        "vendor": vendor,
        "product": product,
        "vendor_key": vendor.casefold(),
        "product_key": product.casefold(),
        "version": version,
        "asset_count": count,
        "cpe": cpe,
    }


def test_one_product_from_two_sources_is_one_item_with_each_version_and_its_asset_count() -> None:
    items = inventory_items(
        [_v("F5", "nginx", "1.25.3", 1), _v("F5", "nginx", "1.27.1", 2)],
        {("f5", "nginx"): 3},
        {("f5", "nginx"): ["intune", "openaev"]},
        [],
    )
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "installed"
    assert item["asset_count"] == 3
    assert item["versions"] == [{"version": "1.25.3", "asset_count": 1}, {"version": "1.27.1", "asset_count": 2}]
    assert item["sources"] == ["intune", "openaev"]
    assert item["confidence"] == "unresolved" and item["cpe"] == ""


def test_a_typed_cpe_is_the_override_and_survives_a_reported_one() -> None:
    items = inventory_items(
        [_v("F5", "nginx", "1.27.1", 1, cpe="cpe:2.3:a:nginx:nginx:1.27.1:*:*:*:*:*:*:*")],
        {("f5", "nginx"): 1},
        {("f5", "nginx"): ["intune"]},
        [{"product": "nginx", "vendor": "F5", "cpe": "cpe:2.3:a:f5:nginx:*:*:*:*:*:*:*:*", "source": "manual"}],
    )
    assert items[0]["cpe"] == "cpe:2.3:a:f5:nginx:*:*:*:*:*:*:*:*"
    assert items[0]["confidence"] == "override"
    assert items[0]["manual"] is True


def test_a_cpe_from_a_platform_row_is_reported_not_an_override() -> None:
    items = inventory_items(
        [],
        {},
        {},
        [
            {
                "product": "Splunk",
                "vendor": "Splunk",
                "cpe": "cpe:2.3:a:splunk:splunk:*:*:*:*:*:*:*:*",
                "source": "openaev",
            }
        ],
    )
    assert items[0]["confidence"] == "reported"


def test_saas_rows_no_source_sees_stay_without_versions_and_a_vendorless_row_joins_its_product() -> None:
    items = inventory_items(
        [_v("F5", "nginx", "1.27.1", 4)],
        {("f5", "nginx"): 4},
        {("f5", "nginx"): ["intune"]},
        [{"product": "HubSpot", "kind": "saas"}, {"product": "NGINX"}],
    )
    by_product = {i["product"].casefold(): i for i in items}
    assert set(by_product) == {"hubspot", "nginx"}
    assert by_product["hubspot"]["kind"] == "saas"
    assert by_product["hubspot"]["versions"] == [] and by_product["hubspot"]["asset_count"] == 0
    assert by_product["nginx"]["manual"] is True and by_product["nginx"]["asset_count"] == 4
    assert by_product["nginx"]["sources"] == ["intune", "manual"]


def test_a_single_version_resolves_the_cpe_with_that_version() -> None:
    def resolver(vendor: str, product: str, version: str) -> tuple[str, str]:
        return ("cpe:2.3:a:f5:nginx:*:*:*:*:*:*:*:*", "high")

    items = inventory_items([_v("F5", "nginx", "1.27.1", 1)], {("f5", "nginx"): 1}, {}, [], resolver=resolver)
    assert items[0]["cpe"] == "cpe:2.3:a:f5:nginx:1.27.1:*:*:*:*:*:*:*"
    assert items[0]["confidence"] == "high"


def test_an_unknown_kind_reads_as_installed() -> None:
    items = inventory_items([], {}, {}, [{"product": "Thing", "kind": "weird"}])
    assert items[0]["kind"] == "installed"
