from arb_memory.mcp.health import liveness, readiness


class FailingConn:
    def execute(self, _query):
        raise RuntimeError("pg blip")


class PassingConn:
    def __init__(self):
        self.queries = []

    def execute(self, query):
        self.queries.append(query)
        return self

    def fetchone(self):
        return (1,)


def test_readiness_degrades_instead_of_raising_on_pg_blip():
    result = readiness(conn_factory=lambda: FailingConn())

    assert result["ready"] is False
    assert result["degraded"] is True
    assert "pg blip" in result["error"]


def test_readiness_checks_memory_and_auth_reads():
    conn = PassingConn()

    result = readiness(conn_factory=lambda: conn)

    assert result == {"ready": True, "degraded": False}
    assert any("FROM hints" in query for query in conn.queries)
    assert any("FROM mcp_auth.oauth_clients" in query for query in conn.queries)


def test_liveness_stays_true_during_dependency_blip():
    assert liveness() == {"alive": True}
