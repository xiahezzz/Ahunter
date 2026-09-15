from advisor.research.catalog import load_catalog


def test_a_share_core_contains_exactly_the_seven_initial_agents():
    catalog = load_catalog()
    assert {str(ref) for ref in catalog.team("a_share_core@1").agents} == {
        "market@1",
        "social@1",
        "news@1",
        "fundamentals@1",
        "policy@1",
        "hot_money@1",
        "lockup@1",
    }


def test_latest_security_teams_use_the_versioned_scoped_mx_agent():
    catalog = load_catalog()
    social = catalog.latest_agent("social")

    assert str(social.agent) == "social@2"
    assert [str(access.product) for access in social.product_accesses] == [
        "company_identity@1",
        "company_news@1",
        "mx_events@2",
    ]
    assert social.product_accesses[-1].feed_scope is not None
    assert social.product_accesses[-1].feed_scope.rids == (111,)
    assert "social@2" in {str(ref) for ref in catalog.latest_team("a_share_core").agents}
    assert "social@2" in {str(ref) for ref in catalog.latest_team("normal").agents}
