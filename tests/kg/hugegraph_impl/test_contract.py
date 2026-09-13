"""Run the existing cross-backend contracts in UUID-owned HugeGraph scopes."""

import pytest

from tests.kg import test_graph_storage as contracts
from tests.kg.hugegraph_impl.test_integration import (
    hugegraph_factory as hugegraph_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


@pytest.mark.parametrize(
    "contract",
    [
        contracts.test_graph_basic,
        contracts.test_graph_advanced,
        contracts.test_graph_batch_operations,
        contracts.test_graph_batch_upsert,
        contracts.test_graph_query_helpers,
        contracts.test_graph_special_characters,
        contracts.test_graph_string_escaping_regressions,
        contracts.test_graph_undirected_property,
    ],
    ids=lambda f: f.__name__,
)
async def test_existing_graph_contract(hugegraph_factory, contract):
    await contract(await hugegraph_factory())


async def test_self_relation_is_one_edge_with_two_incident_ends(hugegraph_factory):
    storage = await hugegraph_factory()
    await storage.upsert_node("Self", {"entity_id": "Self"})
    await storage.upsert_edge("Self", "Self", {"weight": 0.5})
    await storage.upsert_edge("Self", "Self", {"description": "loop"})
    assert len(await storage.get_all_edges()) == 1
    assert await storage.node_degree("Self") == 2
    assert await storage.get_node_edges("Self") == [("Self", "Self")]
    await storage.remove_edges([("Self", "Self")])
    assert await storage.node_degree("Self") == 0
