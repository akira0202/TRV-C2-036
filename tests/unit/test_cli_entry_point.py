# TRV-C2-036 — Unit Tests: Marketplace entry point (cli.py)
#
# cli.py is not imported by any other module, so nothing else in the suite would notice if its
# import path, its graph class, or its runtime-config assembly broke — the image would build and
# then fail when the Pod starts. These exercise it directly.
#
# Skipped where the platform events package is absent: shared.bootstrap.marketplace_app imports it
# at module level, so cli.py cannot be imported at all in that environment.

import importlib.util
import pathlib

import pytest

pytest.importorskip("agenticstar_platform")

_CLI = pathlib.Path(__file__).resolve().parents[2] / "cli.py"


def _load():
    spec = importlib.util.spec_from_file_location("cli_under_test", _CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # __name__ != "__main__" — the runner is not started
    return module


def test_entry_point_imports():
    module = _load()
    assert callable(module.run_agent_marketplace)
    assert isinstance(module.MarketplaceGraph, type)


@pytest.mark.parametrize("supplied", [None, {}], ids=["absent", "empty"])
def test_construction_without_a_supplied_config_carries_the_runtime_config(supplied):
    # run_agent_marketplace constructs the graph itself and hands it an empty mapping when no
    # config is passed. Absent and empty both mean nothing was supplied, so the graph must be built
    # from the same runtime config either way; otherwise every parameter falls back to a framework
    # default.
    module = _load()
    reference = module.MarketplaceGraph()
    agent = module.MarketplaceGraph(config=supplied)
    assert isinstance(agent.config, dict)
    assert agent.config, "config/config.yaml did not reach the graph"
    assert set(agent.config) == set(reference.config)


def test_a_supplied_config_is_used_as_given():
    module = _load()
    agent = module.MarketplaceGraph(config={"max_retry": 2, "entry_point_probe": True})
    assert agent.config.get("entry_point_probe") is True


def test_graph_compiles_from_the_entry_point():
    module = _load()
    agent = module.MarketplaceGraph()
    agent.compile()
    assert agent._nodes
