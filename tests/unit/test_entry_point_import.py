"""Deployment entry-point smoke test.

`src/api/server.py` is what `uvicorn src.api.server:app` loads. Importing it runs every
module-level statement: the graph is built and compiled and the secret provider is provisioned.
A module-level failure there — an import that does not resolve against the installed framework,
a renamed symbol — means the deployment does not start, while every unit test that exercises
nodes and services in isolation still passes.

No other test in this suite imports that module, so this file is the only thing standing between
such a failure and a deployment. The import is the assertion.

This template ships no `config/config.yaml`; the graph is built with the empty config, which is
what AgentRegistry also passes when the file is absent. The assertion below pins that the entry
point still hands the graph a config mapping rather than leaving it unset.
"""

import importlib


def _entry_point():
    return importlib.import_module("src.api.server")


def test_entry_point_module_imports():
    module = _entry_point()

    assert module.app is not None
    assert module.agent is not None


def test_entry_point_hands_the_graph_a_config_mapping():
    module = _entry_point()

    assert isinstance(module.agent.config, dict)
