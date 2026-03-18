"""Tests for the scenario loading system."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from agentbreak.config.loader import load_config, load_scenario, _parse_config
from agentbreak.config.models import OpenAIServiceConfig, MCPServiceConfig
from agentbreak.config.scenarios import SCENARIOS, apply_scenario


class TestScenariosDict:
    def test_all_scenarios_have_fault_or_latency(self):
        for name, settings in SCENARIOS.items():
            assert "fault" in settings or "latency" in settings, (
                f"Scenario {name!r} has neither fault nor latency"
            )

    def test_known_scenarios_present(self):
        expected = {
            "mixed-transient",
            "rate-limited",
            "brownout",
            "mcp-tool-failures",
            "mcp-resource-unavailable",
            "mcp-slow-tools",
            "mcp-initialization-failure",
            "mcp-mixed-transient",
        }
        assert expected.issubset(set(SCENARIOS.keys()))


class TestApplyScenario:
    def test_applies_fault_to_empty_config(self):
        config = {"name": "svc", "type": "openai", "port": 5000}
        result = apply_scenario(config, "rate-limited")
        assert "fault" in result
        assert result["fault"]["available_codes"] == [429]

    def test_applies_latency_to_empty_config(self):
        config = {"name": "svc", "type": "openai", "port": 5000}
        result = apply_scenario(config, "brownout")
        assert result["latency"]["probability"] == 0.2

    def test_scenario_merges_with_existing_fault(self):
        config = {
            "name": "svc",
            "type": "openai",
            "port": 5000,
            "fault": {"enabled": False},
        }
        result = apply_scenario(config, "rate-limited")
        # Scenario values win in merge
        assert result["fault"]["available_codes"] == [429]

    def test_unknown_scenario_leaves_config_unchanged(self):
        config = {"name": "svc", "type": "openai", "port": 5000}
        result = apply_scenario(config, "nonexistent-scenario")
        assert result == config

    def test_does_not_mutate_original(self):
        config = {"name": "svc", "type": "openai", "port": 5000}
        original = dict(config)
        apply_scenario(config, "brownout")
        assert config == original

    def test_preserves_existing_fields(self):
        config = {
            "name": "my-svc",
            "type": "openai",
            "mode": "proxy",
            "port": 5000,
            "upstream_url": "https://api.openai.com",
        }
        result = apply_scenario(config, "rate-limited")
        assert result["name"] == "my-svc"
        assert result["upstream_url"] == "https://api.openai.com"


class TestLoadScenario:
    def test_returns_copy_not_original(self):
        s1 = load_scenario("brownout")
        s2 = load_scenario("brownout")
        s1["__modified__"] = True
        assert "__modified__" not in s2

    def test_raises_for_unknown_scenario(self):
        with pytest.raises(ValueError, match="Unknown scenario"):
            load_scenario("does-not-exist")

    def test_all_scenarios_loadable(self):
        for name in SCENARIOS:
            result = load_scenario(name)
            assert isinstance(result, dict)


class TestScenarioInConfig:
    def _make_yaml(self, content: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        )
        tmp.write(content)
        tmp.flush()
        tmp.close()
        return Path(tmp.name)

    def test_scenario_applied_to_openai_service(self):
        path = self._make_yaml(
            "version: '1.0'\n"
            "services:\n"
            "  - name: openai\n"
            "    type: openai\n"
            "    mode: mock\n"
            "    port: 5000\n"
            "    scenario: rate-limited\n"
        )
        cfg = load_config(path)
        svc = cfg.get_service("openai")
        assert isinstance(svc, OpenAIServiceConfig)
        # rate-limited scenario sets available_codes to [429]
        assert svc.fault.available_codes == (429,)

    def test_scenario_applied_to_mcp_service(self):
        path = self._make_yaml(
            "version: '1.0'\n"
            "services:\n"
            "  - name: mcp\n"
            "    type: mcp\n"
            "    mode: mock\n"
            "    port: 5001\n"
            "    scenario: mcp-slow-tools\n"
        )
        cfg = load_config(path)
        svc = cfg.get_service("mcp")
        assert isinstance(svc, MCPServiceConfig)
        # mcp-slow-tools scenario sets latency probability to 0.9
        assert svc.latency.probability == 0.9

    def test_no_scenario_uses_defaults(self):
        path = self._make_yaml(
            "version: '1.0'\n"
            "services:\n"
            "  - name: openai\n"
            "    type: openai\n"
            "    mode: mock\n"
            "    port: 5000\n"
        )
        cfg = load_config(path)
        svc = cfg.get_service("openai")
        # Default FaultConfig overall_rate is 0.1
        assert svc.fault.overall_rate == 0.1

    def test_brownout_scenario_sets_latency(self):
        path = self._make_yaml(
            "version: '1.0'\n"
            "services:\n"
            "  - name: openai\n"
            "    type: openai\n"
            "    mode: mock\n"
            "    port: 5000\n"
            "    scenario: brownout\n"
        )
        cfg = load_config(path)
        svc = cfg.get_service("openai")
        assert svc.latency.probability == 0.2
        assert svc.latency.min_seconds == 5
        assert svc.latency.max_seconds == 15

    def test_multi_service_with_different_scenarios(self):
        path = self._make_yaml(
            "version: '1.0'\n"
            "services:\n"
            "  - name: llm\n"
            "    type: openai\n"
            "    mode: mock\n"
            "    port: 5000\n"
            "    scenario: rate-limited\n"
            "  - name: mcp\n"
            "    type: mcp\n"
            "    mode: mock\n"
            "    port: 5001\n"
            "    scenario: mcp-tool-failures\n"
        )
        cfg = load_config(path)
        assert len(cfg.services) == 2
        llm = cfg.get_service("llm")
        mcp = cfg.get_service("mcp")
        assert llm.fault.available_codes == (429,)
        assert mcp.fault.overall_rate == 0.3


class TestServiceConfigScenarioField:
    def test_scenario_field_stored_on_config(self):
        """The scenario name is preserved as a field on the service config."""
        data = {
            "version": "1.0",
            "services": [
                {
                    "name": "svc",
                    "type": "openai",
                    "mode": "mock",
                    "port": 5000,
                    "scenario": "brownout",
                }
            ],
        }
        cfg = _parse_config(data)
        svc = cfg.get_service("svc")
        assert svc.scenario == "brownout"
