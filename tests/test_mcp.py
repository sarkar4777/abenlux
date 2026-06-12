"""The MCP read plane: thin tool functions over existing reads. They resolve the caller's own token,
return only the caller's own data, and add no new permission."""
import pytest

from abenlux.mcp_server import TOOLS, tool_attribute, tool_cost_estimate, tool_my_spend


def test_cost_estimate_prices_a_model_call():
    out = tool_cost_estimate("claude-opus-4-8", input_tokens=1_000_000, output_tokens=0)
    assert out["priced"] is True and out["cost_usd"] > 0


def test_attribute_maps_a_ticket_branch_to_its_objective(tmp_path, monkeypatch):
    kg = tmp_path / "kg.yaml"
    kg.write_text("objectives:\n  - {id: obj-acme, label: Acme Checkout}\n"
                  "ticket_prefix_to_objective:\n  ACME: obj-acme\n", encoding="utf-8")
    monkeypatch.setenv("ABEN_KG", str(kg))
    out = tool_attribute(branch="feature/ACME-12-checkout")
    assert out["objective_id"] == "obj-acme" and out["objective_label"] == "Acme Checkout"


def test_my_spend_requires_a_valid_token(monkeypatch):
    monkeypatch.delenv("ABEN_MCP_TOKEN", raising=False)
    monkeypatch.delenv("ABEN_TOKEN", raising=False)
    with pytest.raises(PermissionError):
        tool_my_spend(token="not-a-real-token")


def test_tool_registry_exposes_the_four_tools():
    assert set(TOOLS) == {"my_spend", "check_reuse", "attribute", "cost_estimate"}
