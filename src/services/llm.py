"""AgentCore Platform v1.0 — runtime LLM adapter for TRV-C2-036 (the non-authoritative brief narrative only).

BriefGenerateNode is injected with this adapter at graph construction (tests inject a fake instead). The
adapter holds no credential: it resolves the Azure connection values at CALL time from the
invocation-bound SecretProvider (``framework.secrets.context.current_secrets`` — the same provider
``InvocationContext.from_state(state).secrets`` exposes), never from ``os.environ``, State, or a
build-time / import-time read. It exposes the shared ``complete(messages) -> dict`` surface the node
already calls, so an injected fake and this adapter are interchangeable at the call site.
"""

from typing import Any

from framework.secrets.context import current_secrets


# Connection values the deployment binds for the Azure OpenAI endpoint. Declared in
# config/agent.yaml requires.secrets and resolved from the invocation-bound provider at complete()
# time. AZURE_OPENAI_ENDPOINT must be the bare resource endpoint: the client rejects a value that
# already carries an API path. AZURE_OPENAI_DEPLOYMENT is the deployment name, which the endpoint
# uses as its model routing key.
_API_KEY = "AZURE_OPENAI_API_KEY"
_ENDPOINT = "AZURE_OPENAI_ENDPOINT"
_DEPLOYMENT = "AZURE_OPENAI_DEPLOYMENT"


def _build_azure_client(config: dict[str, Any]) -> Any:
    """Construct the shared AzureOpenAIClient. Isolated (and lazily imported) so the provider extra
    is only required on the LLM path, and so tests can substitute it."""
    from shared.services.llm.azure_openai_client import AzureOpenAIClient

    return AzureOpenAIClient(config)


class AzureOpenAINarrativeLLM:
    """Keyless runtime adapter exposing ``complete(messages) -> dict``.

    A missing connection value or a provider failure raises here; the calling node catches it and
    applies its documented behaviour for an unavailable LLM. The values are never placed in State,
    trace, or error text."""

    def __init__(self, temperature: float = 0.0) -> None:
        self._temperature = temperature

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        secrets = current_secrets()
        client = _build_azure_client(
            {
                "api_key": secrets.require(_API_KEY),
                "azure_endpoint": secrets.require(_ENDPOINT),
                "azure_deployment": secrets.require(_DEPLOYMENT),
                "temperature": self._temperature,
            }
        )
        response = client.complete(messages)
        return response if isinstance(response, dict) else {"content": str(response)}
