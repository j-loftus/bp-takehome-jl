"""Data visualization components for Streamlit."""

import streamlit as st


def render_table(df) -> None:
    """Render a pandas DataFrame as an interactive Streamlit table."""
    # TODO: st.dataframe with column config
    raise NotImplementedError


def render_bar_chart(df, x: str, y: str, title: str = "") -> None:
    """Render a bar chart from a DataFrame."""
    # TODO: st.bar_chart or matplotlib figure via st.pyplot
    raise NotImplementedError


def render_timeline(df, date_col: str, label_col: str, title: str = "") -> None:
    """Render a timeline/scatter of contract dates."""
    # TODO: matplotlib scatter via st.pyplot
    raise NotImplementedError
