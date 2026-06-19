"""
Visualization module — fixed chart menu + deterministic guard.

The guard reconciles a requested chart_spec against the realized DataFrame
before any rendering call is made. `table` is always the valid terminal fallback.

Usage:
    from viz import render, CHART_TYPES
    render(df, {"chart_type": "bar", "x": "vendor_name", "y": "total_contract_value",
                "series": None, "title": "Spend by Vendor"})
"""

import pandas as pd
import plotly.express as px
import streamlit as st

CHART_TYPES = ["bar", "grouped_bar", "line", "scatter", "metric", "table"]

_MAX_BAR_CATEGORIES = 25

# ---------------------------------------------------------------------------
# Column display names — used for axis titles, legends, and table headers
# so a PE audience sees "Total Contract Value ($)", not "total_contract_value".
# ---------------------------------------------------------------------------

COLUMN_LABELS = {
    "vendor_name": "Vendor",
    "contract_number": "Contract #",
    "parent_contract_number": "Parent Contract #",
    "doc_type": "Document Type",
    "doc_date": "Document Date",
    "county_department": "Department",
    "total_contract_value": "Total Contract Value ($)",
    "total_value": "Total Value ($)",
    "modification_financial_delta": "Amendment Value ($)",
    "contract_start_date": "Start Date",
    "contract_end_date": "End Date",
    "renewal_options": "Renewal Options",
    "auto_renewal_flag": "Auto-Renews",
    "termination_notice_days": "Termination Notice (Days)",
    "service_category": "Service Category",
    "procurement_vehicle": "Procurement Vehicle",
    "insurance_requirements_flag": "Insurance Required",
    "price_escalator_terms": "Escalator Type",
    "days_to_expiry": "Days Until Expiration",
    "days_to_renewal": "Days Until Renewal",
    "days_until_expiration": "Days Until Expiration",
    "act_by_date": "Act-By Date",
    "urgent": "Urgent (≤30 Days)",
    "pct_of_total": "% of Total Spend",
    "contract_count": "# of Contracts",
    "original_value": "Original Award ($)",
    "amendment_total": "Amendment Total ($)",
    "true_total": "True Total Commitment ($)",
    "creep_flag": "Amendment >25% of Award",
    "vendor_count": "# of Vendors",
    "total_spend": "Total Spend ($)",
    "avg_contract_value": "Avg. Contract Value ($)",
    "fragmentation_signal": "Fragmentation",
    "renewal_count": "# of Renewals",
    "current_value": "Current Value ($)",
    "stale_incumbent": "Stale Incumbent (3+ Renewals)",
    "extraction_confidence": "Extraction Confidence",
}

# Columns that hold dollar amounts — get $ prefix + thousands separators on axes.
_DOLLAR_COLUMNS = {
    "total_contract_value", "total_value", "modification_financial_delta",
    "original_value", "amendment_total", "true_total", "total_spend",
    "avg_contract_value", "current_value",
}

# Columns that hold percentages — get a % suffix.
_PERCENT_COLUMNS = {"pct_of_total"}


def _humanize(col: str | None) -> str:
    """Return a friendly display label for a column name."""
    if not col:
        return ""
    return COLUMN_LABELS.get(col, col.replace("_", " ").title())


def humanize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with display-friendly column headers (for direct st.dataframe calls)."""
    return df.rename(columns={c: _humanize(c) for c in df.columns})


def _axis_kwargs(col: str | None) -> dict:
    """Plotly axis tickformat kwargs for a given column, based on its semantic type."""
    if not col:
        return {}
    if col in _DOLLAR_COLUMNS:
        return {"tickprefix": "$", "tickformat": ",.0f"}
    if col in _PERCENT_COLUMNS:
        return {"ticksuffix": "%", "tickformat": ",.1f"}
    return {"tickformat": ","}


def render(df: pd.DataFrame, chart_spec: dict) -> None:
    """
    Render df according to chart_spec = {chart_type, x, y, series, title}.

    Applies the deterministic guard before calling the matching renderer.
    Falls back to table on any mismatch or degenerate case.
    """
    chart_type = (chart_spec.get("chart_type") or "table").lower()
    x = chart_spec.get("x")
    y = chart_spec.get("y")
    series = chart_spec.get("series")
    title = chart_spec.get("title") or ""

    if chart_type not in CHART_TYPES:
        chart_type = "table"

    # Guard 1: empty DataFrame
    if df is None or df.empty:
        st.info("No results found.")
        return

    # Guard 2: single row → metric or table
    if len(df) == 1 and chart_type not in ("metric", "table"):
        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        if numeric_cols and y and y in df.columns and pd.api.types.is_numeric_dtype(df[y]):
            _render_metric(df, x, y, title)
        elif numeric_cols:
            _render_metric(df, None, numeric_cols[0], title)
        else:
            _render_table(df, title)
        return

    # Guard 3: referenced columns missing
    for col in [x, y, series]:
        if col and col not in df.columns:
            _render_table(df, title)
            return

    # Guard 4: bar/grouped_bar with too many categories
    if chart_type in ("bar", "grouped_bar") and x and x in df.columns:
        if df[x].nunique() > _MAX_BAR_CATEGORIES:
            _render_table(df, title)
            return

    # Guard 5: scatter with non-numeric axes
    if chart_type == "scatter":
        x_bad = x and x in df.columns and not pd.api.types.is_numeric_dtype(df[x])
        y_bad = y and y in df.columns and not pd.api.types.is_numeric_dtype(df[y])
        if x_bad or y_bad:
            # Try to salvage as bar if there's one categorical + one numeric
            cat_cols = df.select_dtypes(exclude="number").columns.tolist()
            num_cols = df.select_dtypes(include="number").columns.tolist()
            if cat_cols and num_cols:
                chart_type = "bar"
                x = cat_cols[0]
                y = num_cols[0]
            else:
                _render_table(df, title)
                return

    # Dispatch to renderer
    if chart_type == "bar":
        _render_bar(df, x, y, series, title)
    elif chart_type == "grouped_bar":
        _render_grouped_bar(df, x, y, series, title)
    elif chart_type == "line":
        _render_line(df, x, y, series, title)
    elif chart_type == "scatter":
        _render_scatter(df, x, y, series, title)
    elif chart_type == "metric":
        _render_metric(df, x, y, title)
    else:
        _render_table(df, title)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _labels_for(*cols: str | None) -> dict:
    return {c: _humanize(c) for c in cols if c}


def _render_bar(df: pd.DataFrame, x: str, y: str, series: str | None, title: str) -> None:
    color = series if series and series in df.columns else None
    fig = px.bar(df, x=x, y=y, color=color, title=title, labels=_labels_for(x, y, color))
    fig.update_layout(xaxis_tickangle=-45)
    fig.update_yaxes(**_axis_kwargs(y))
    fig.update_xaxes(**_axis_kwargs(x))
    st.plotly_chart(fig, use_container_width=True)


def _render_grouped_bar(df: pd.DataFrame, x: str, y: str, series: str | None, title: str) -> None:
    color = series if series and series in df.columns else None
    fig = px.bar(df, x=x, y=y, color=color, barmode="group", title=title, labels=_labels_for(x, y, color))
    fig.update_layout(xaxis_tickangle=-45)
    fig.update_yaxes(**_axis_kwargs(y))
    fig.update_xaxes(**_axis_kwargs(x))
    st.plotly_chart(fig, use_container_width=True)


def _render_line(df: pd.DataFrame, x: str, y: str, series: str | None, title: str) -> None:
    color = series if series and series in df.columns else None
    fig = px.line(df, x=x, y=y, color=color, title=title, markers=True, labels=_labels_for(x, y, color))
    fig.update_yaxes(**_axis_kwargs(y))
    fig.update_xaxes(**_axis_kwargs(x))
    st.plotly_chart(fig, use_container_width=True)


def _render_scatter(df: pd.DataFrame, x: str, y: str, series: str | None, title: str) -> None:
    color = series if series and series in df.columns else None
    fig = px.scatter(
        df, x=x, y=y, color=color, title=title,
        labels=_labels_for(x, y, color),
        hover_data=df.columns.tolist(),
    )
    fig.update_yaxes(**_axis_kwargs(y))
    fig.update_xaxes(**_axis_kwargs(x))
    st.plotly_chart(fig, use_container_width=True)


def _render_metric(df: pd.DataFrame, label_col: str | None, value_col: str, title: str) -> None:
    if title:
        st.caption(title)
    val = df[value_col].iloc[0]
    label = str(df[label_col].iloc[0]) if label_col and label_col in df.columns else _humanize(value_col)
    if isinstance(val, (int, float)) and not pd.isna(val):
        if value_col in _DOLLAR_COLUMNS:
            display = f"${val:,.0f}"
        elif value_col in _PERCENT_COLUMNS:
            display = f"{val:,.1f}%"
        else:
            display = f"{val:,.0f}" if float(val).is_integer() else f"{val:,.2f}"
    else:
        display = str(val)
    st.metric(label=label, value=display)


def _render_table(df: pd.DataFrame, title: str) -> None:
    if title:
        st.caption(title)
    st.dataframe(humanize_columns(df), use_container_width=True)
