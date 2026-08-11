"""Ticket #13 / #9/S3-S4: query final inventory from either instance."""


def test_s3_query_exhausted_sku_from_first_instance(base_url, http):
    """#9/S3, TC1/TC3/TC5: GET returns the SKU with final stock zero."""
    _, _, book = http(
        "POST",
        f"{base_url}/books",
        {"title": "三体", "initial_stock": 2},
    )
    for client in ("client-0001", "client-0002"):
        status, _, _ = http(
            "POST",
            f"{base_url}/loans",
            {"book_id": book["id"], "borrower": client},
        )
        assert status == 201

    status, headers, body = http("GET", f"{base_url}/books/{book['id']}")

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body == {
        "id": book["id"],
        "title": "三体",
        "available_stock": 0,
    }


def test_s4_second_process_reads_the_same_final_inventory(two_process_urls, http):
    """#9/S4, TC3/TC5/TC6: instance two directly shows shared stock zero."""
    (first_url, second_url), _ = two_process_urls
    _, _, book = http(
        "POST",
        f"{first_url}/books",
        {"title": "三体", "initial_stock": 1},
    )
    loan_status, _, _ = http(
        "POST",
        f"{first_url}/loans",
        {"book_id": book["id"], "borrower": "client-0001"},
    )
    assert loan_status == 201

    status, headers, body = http("GET", f"{second_url}/books/{book['id']}")

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body == {
        "id": book["id"],
        "title": "三体",
        "available_stock": 0,
    }
