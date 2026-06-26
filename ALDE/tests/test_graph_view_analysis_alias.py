from alde.agents_tools import _TOOL_IMPLEMENTATIONS
from alde.agents_runtime import TOOL_CONFIGS


def test_graph_view_analysis_alias_is_registered():
    tool_names = {str(config.get("name") or "") for config in TOOL_CONFIGS}
    assert "graph_view_analysis" in tool_names
    assert "graph_view_analysis" in _TOOL_IMPLEMENTATIONS
