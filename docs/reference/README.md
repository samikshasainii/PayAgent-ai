# PayPal AI Reference Docs (third-party copies)

These files are **unmodified copies of PayPal's developer documentation**, kept here for offline reference while building PayAgent AI. They are **not** original work and are **not** covered by this repository's MIT license; all rights belong to PayPal. Always prefer the live docs, which may have changed since these copies were made.

| File | PayPal docs path |
| :--- | :--- |
| [PayPal_AI_Integration.md](./PayPal_AI_Integration.md) | `/ai-tools/get-started` |
| [agent_toolkit_quickstart_guide.md](./agent_toolkit_quickstart_guide.md) | `/ai-tools/toolkit` |
| [agent_tools_reference.md](./agent_tools_reference.md) | `/ai-tools/agent-tools` |
| [Mcp_server_quickstart_guide.md](./Mcp_server_quickstart_guide.md) | `/ai-tools/mcp-server` |
| [LLM_integration_quickstart_guide.md](./LLM_integration_quickstart_guide.md) | `/ai-tools/build-with-llm` |
| [best_practices_for_prompts.md](./best_practices_for_prompts.md) | `/ai-tools/prompts` |
| [Agentic_Commerce_Services.md](./Agentic_Commerce_Services.md) | `/agentic-commerce-services/about` |

Toolkit source: <https://github.com/paypal/agent-toolkit>

## What matters for PayPal AI (as of these copies)

- **No Payouts tool** in the Agent Toolkit or PayPal MCP server. PayAgent calls Payouts v1 directly and exposes its own MCP server to agents (ARCHITECTURE.md §6.5).
- **Python package:** `pip install paypal-agent-toolkit`. The LangChain integration is `paypal_agent_toolkit.langchain.toolkit.PayPalToolkit`, and tools can be scoped with `Configuration(actions={"orders": {"create": True, ...}})`. This is the basis for the Orders v2 task-funding stretch item (ARCHITECTURE.md §9).
- **Sandbox MCP endpoint:** `https://mcp.sandbox.paypal.com` (remote server; requires PayPal login consent).
- **Not usable here:** Agentic Commerce Services (merchant-only, access by application form) and the remote-MCP commerce tools (gift cards only, production endpoint, feature-flag header).
- **Tool names differ between these docs and the GitHub README** (`list_transaction` vs `list_transactions`, `list_product` vs `list_products`). Trust the installed package.
- **Version pins:** the toolkit's LangChain example pins `langchain==0.3.23` and `langchain-openai==0.2.2` and uses the deprecated `initialize_agent`. Check for conflicts before pinning `requirements.txt`.
