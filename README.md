# TRV-C2-036 — Destination Market-Comparison Evidence Brief Generator

> **Category**: Cat 2 (orchestrates multiple steps to accomplish a specific use case)
> **Industry**: TRV

## Overview

This agent produces a side-by-side comparison brief for two travel markets from aggregate
statistics you supply. Before comparing anything it checks the two tables against an approved
definition sheet to confirm the periods, populations and units actually line up, then writes the
brief with each figure linked to the table it came from. When none of the requested metrics turn out
to be comparable it says so in a validation report instead of producing a misleading comparison. It
is aimed at prefectural and destination-marketing analysts who need a draft another person can audit.

It works only from the tables you give it and is read-only. It does not retrieve or refresh data,
forecast demand, rank markets, recommend where to spend, or touch traveller-level data — a human
analyst reviews every output.

## What it returns

Every run completes and says what happened.

| Result | When |
|---|---|
| The comparison brief | at least one requested metric turns out comparable. Metrics that are not comparable are listed in the brief with the reason — that is the point of the brief, not a failure |
| A validation report instead of a brief | none of the requested metrics are comparable. This is a real finding: the tables were checked against the definition sheet and the periods, populations or units do not line up |
| A notice instead of either, with a code | the request is one you can correct — not the documented envelope, or a table, source or definition sheet this agent cannot read. The notice names what to send instead, and never repeats what you sent |
| An error, with no output | the payload is not really a document (oversized, over-nested, control characters), the composed brief is blocked at the output gate, or the run hits a defect in this agent. These are never reported as a completed request |

A notice and a validation report are deliberately different outcomes: the report means the metrics
were checked and none were comparable, which would be the wrong thing to say about a request that
was never examined. This agent works only from the tables you give it — it does not retrieve or
refresh data, forecast, rank markets, or recommend where to spend. The input contract, the codes
and the full classification are in [`docs/02_design.md`](docs/02_design.md).

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent raises
`PlatformRequired` during graph compile / start-up preflight rather than starting in a partially
working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/` for the design specification and the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

