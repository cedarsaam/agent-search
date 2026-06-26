# Contributing to Agent Search

Thanks for your interest! This is a small, best-effort project — issues and PRs are welcome.

## Dev setup

```bash
git clone https://github.com/cedarsaam/agent-search.git
cd agent-search
cp .env.example .env            # fill SEARXNG_SECRET_KEY (+ optional LLM key)
docker compose up -d searxng    # local SearXNG backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-optional.txt
```

## Before you open a PR

```bash
python -m py_compile search.py server.py mcp_server.py
python -m unittest -v test_search.py          # unit tests must pass
python bench/run_eval.py --offline            # regression check (uses cached fixtures)
```

- `bench/` is a self-contained eval harness (50 cases). Ranking/extraction/RAG changes are scored against cached network fixtures, so you can measure improvements without spending API calls. See `EVAL_REPORT.md`.
- Keep the offline eval green (no score regression) and add a unit test for new behavior.

## Guidelines

- **Keep core deps light.** New heavy/optional capabilities (JS rendering, rerankers, etc.) go in `requirements-optional.txt` and must degrade gracefully when absent — never a hard dependency.
- **Don't break the public surface:** CLI, HTTP (`server.py`), and MCP (`mcp_server.py`) APIs.
- **Never commit secrets.** `.env` is gitignored; use `.env.example` for new config keys.
- Match the surrounding code style; comments can be English or 中文.

## License

By contributing, you agree your contributions are licensed under the [MIT License](LICENSE).
