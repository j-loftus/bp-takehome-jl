"""Chat message rendering components."""

import streamlit as st


def render_user_message(text: str) -> None:
    """Render a user message bubble."""
    # TODO: st.chat_message("user") block
    raise NotImplementedError


def render_assistant_message(text: str, data=None) -> None:
    """
    Render an assistant response.

    If data is a DataFrame, also calls charts.render_table or charts.render_chart.
    """
    # TODO: st.chat_message("assistant") block; hand off to charts if data present
    raise NotImplementedError
