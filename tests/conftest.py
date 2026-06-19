"""
Pytest configuration.

Sets ANTHROPIC_API_KEY before any test module imports so that src/llm_client.py
can initialize without raising EnvironmentError. Tests that call the LLM mock
call_llm directly, so this dummy key is never sent to the API.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-tests")
