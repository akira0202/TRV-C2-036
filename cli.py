"""AGENTIC STAR Marketplace entry point — one-shot Pod process.

Executed by the Marketplace image's ``CMD ["python", "cli.py"]``.

``run_agent_marketplace`` owns the whole lifecycle: it constructs the graph,
compiles it, builds the SecretProvider, provisions secrets, then runs the
Marketplace loop (input, progress events, terminal delivery, exit). The graph
class is passed uninstantiated.

``agent_name`` / ``namespace`` mirror ``config/agent.yaml`` so the Pod is
identifiable by the same values the manifest and the image tag carry.

``run_agent_marketplace`` constructs the graph itself and hands it an empty mapping when no
``config`` is passed, so ``config/config.yaml`` would never reach it and every runtime parameter
would silently fall back to its framework default. The thin subclass below treats an absent or
empty config as "not supplied" and loads the file, using the same loader and the same path
convention as ``src/api/server.py``, so the runtime configuration is identical on both entry paths.
"""

import hashlib
from pathlib import Path
from typing import Any

from framework.utils.config_loader import load_config
from shared.bootstrap.marketplace_app import run_agent_marketplace
from shared.utils.log_helper import get_logger
from src.graph.graph import DestinationMarketComparisonAgent


# cli.py sits next to config/ both in the repository and in the image
# (/app/cli.py + /app/config/config.yaml), so one expression covers both.
_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "config.yaml"


class MarketplaceGraph(DestinationMarketComparisonAgent):
    """Loads config/config.yaml whenever no configuration is supplied (absent or empty)."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        if not config:
            config = load_config(str(_CONFIG_PATH)) if _CONFIG_PATH.exists() else {}
        super().__init__(config=config)


if __name__ == "__main__":
    # Surface the S-4 audit trail on the Pod's stdout, and name the running build on its first
    # line. The framework already records each domain event — including the exception type when a
    # node fails — but it emits at INFO on the "agentcore.audit" logger, and with no handler
    # attached Python discards everything below WARNING; without this, a failed run leaves nothing
    # in the container log to diagnose from. The build identifier matters because a node only
    # pulls an image tag it does not already hold, so re-publishing under the same tag leaves two
    # builds in service at once — and a log that cannot be attributed to the code that produced it
    # makes a working fix look broken when the run simply used the cached earlier build.
    get_logger("agentcore.audit").info(
        "TRV-C2-036 starting (entry point %s)",
        hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()[:12],
    )

    run_agent_marketplace(
        MarketplaceGraph,
        agent_name="TRV-C2-036",
        namespace="trv-c2-036",
    )
