"""
The 8 Task 1.4 downstream analyses as pure functions.

Each function takes an optional db_path, executes read-only SQL + pandas transforms,
and returns a pandas DataFrame. No LLM calls. Independently testable.

Usage:
    from analyses import analysis_1, analysis_2, ...
    df = analysis_1()
"""

import os
import sqlite3

import pandas as pd

DB_PATH = os.environ.get("DB_PATH", "data/contracts.db")


def _read_sql(sql: str, db_path: str = DB_PATH, params=None) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(sql, con, params=params)
    except Exception:
        return pd.DataFrame()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# TIER 1 — ACT NOW
# ---------------------------------------------------------------------------

def analysis_1(db_path: str = DB_PATH) -> pd.DataFrame:
    """Renewal cliff — contracts expiring within next 365 days, sorted by days_to_expiry."""
    sql = """
        SELECT
            contract_number,
            vendor_name,
            contract_end_date,
            total_contract_value,
            service_category,
            renewal_options,
            CAST(julianday(contract_end_date) - julianday('now') AS INTEGER) AS days_to_expiry
        FROM contracts
        WHERE contract_end_date IS NOT NULL
          AND julianday(contract_end_date) >= julianday('now')
          AND julianday(contract_end_date) <= julianday('now', '+365 days')
        ORDER BY days_to_expiry ASC
    """
    df = _read_sql(sql, db_path)
    if not df.empty:
        df["total_contract_value"] = pd.to_numeric(df["total_contract_value"], errors="coerce")
    return df


def analysis_2(db_path: str = DB_PATH) -> pd.DataFrame:
    """Auto-renewal liability — contracts with auto_renewal_flag=1; compute 'act by' date."""
    sql = """
        SELECT
            contract_number,
            vendor_name,
            contract_end_date,
            termination_notice_days,
            total_contract_value,
            renewal_options
        FROM contracts
        WHERE auto_renewal_flag = 1
          AND contract_end_date IS NOT NULL
        ORDER BY contract_end_date ASC
    """
    df = _read_sql(sql, db_path)
    if df.empty:
        return df
    df["contract_end_date"] = pd.to_datetime(df["contract_end_date"], errors="coerce")
    df["termination_notice_days"] = pd.to_numeric(df["termination_notice_days"], errors="coerce").fillna(30)
    df["act_by_date"] = df["contract_end_date"] - pd.to_timedelta(df["termination_notice_days"], unit="d")
    today = pd.Timestamp.now().normalize()
    df["urgent"] = df["act_by_date"] <= today + pd.Timedelta(days=30)
    df["contract_end_date"] = df["contract_end_date"].dt.strftime("%Y-%m-%d")
    df["act_by_date"] = df["act_by_date"].dt.strftime("%Y-%m-%d")
    return df


# ---------------------------------------------------------------------------
# TIER 2 — UNDERSTAND EXPOSURE
# ---------------------------------------------------------------------------

def analysis_3(db_path: str = DB_PATH) -> pd.DataFrame:
    """Spend concentration — top 20 vendors by total contract value (FEA rows only)."""
    sql = """
        SELECT
            vendor_name,
            SUM(total_contract_value) AS total_contract_value,
            COUNT(*) AS contract_count
        FROM contracts
        WHERE doc_type = 'fully_executed_agreement'
          AND total_contract_value IS NOT NULL
        GROUP BY vendor_name
        ORDER BY total_contract_value DESC
        LIMIT 20
    """
    df = _read_sql(sql, db_path)
    if not df.empty:
        total = df["total_contract_value"].sum()
        df["pct_of_total"] = (df["total_contract_value"] / total * 100).round(1) if total else 0.0
    return df


def analysis_4(db_path: str = DB_PATH) -> pd.DataFrame:
    """True total commitment by family — original award + amendment deltas; flag >25% creep."""
    sql = """
        WITH originals AS (
            SELECT contract_number, vendor_name, total_contract_value AS original_value
            FROM contracts
            WHERE doc_type = 'fully_executed_agreement'
              AND total_contract_value IS NOT NULL
        ),
        amendments AS (
            SELECT contract_number, SUM(modification_financial_delta) AS amendment_total
            FROM contracts
            WHERE doc_type = 'modification_amendment'
              AND modification_financial_delta IS NOT NULL
            GROUP BY contract_number
        )
        SELECT
            o.contract_number,
            o.vendor_name,
            o.original_value,
            COALESCE(a.amendment_total, 0) AS amendment_total,
            o.original_value + COALESCE(a.amendment_total, 0) AS true_total,
            CASE
                WHEN o.original_value > 0
                     AND COALESCE(a.amendment_total, 0) > 0.25 * o.original_value
                THEN 1 ELSE 0
            END AS creep_flag
        FROM originals o
        LEFT JOIN amendments a ON o.contract_number = a.contract_number
        ORDER BY true_total DESC
    """
    return _read_sql(sql, db_path)


def analysis_5(db_path: str = DB_PATH) -> pd.DataFrame:
    """Price escalation exposure — contracts with risky escalator terms expiring within 365 days."""
    sql = """
        SELECT
            contract_number,
            vendor_name,
            price_escalator_terms,
            contract_end_date,
            total_contract_value,
            service_category,
            CAST(julianday(contract_end_date) - julianday('now') AS INTEGER) AS days_to_renewal
        FROM contracts
        WHERE price_escalator_terms IN ('cpi_capped', 'fixed_percentage', 'negotiated_at_renewal')
          AND contract_end_date IS NOT NULL
          AND julianday(contract_end_date) >= julianday('now')
          AND julianday(contract_end_date) <= julianday('now', '+365 days')
        ORDER BY total_contract_value DESC NULLS LAST
    """
    df = _read_sql(sql, db_path)
    if not df.empty:
        df["total_contract_value"] = pd.to_numeric(df["total_contract_value"], errors="coerce")
        df["days_to_renewal"] = pd.to_numeric(df["days_to_renewal"], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# TIER 3 — IMPROVE POSITION
# ---------------------------------------------------------------------------

def analysis_6(db_path: str = DB_PATH) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Procurement channel mix.

    Returns (channel_summary_df, sole_source_coop_df).
    channel_summary_df: spend by procurement_vehicle + pct_of_total.
    sole_source_coop_df: ranked sole-source/coop contracts by value.
    """
    channel_sql = """
        SELECT
            COALESCE(procurement_vehicle, 'unknown') AS procurement_vehicle,
            SUM(total_contract_value) AS total_contract_value,
            COUNT(*) AS contract_count
        FROM contracts
        WHERE doc_type IN ('fully_executed_agreement', 'award_letter')
          AND total_contract_value IS NOT NULL
        GROUP BY procurement_vehicle
        ORDER BY total_contract_value DESC
    """
    detail_sql = """
        SELECT
            contract_number,
            vendor_name,
            procurement_vehicle,
            total_contract_value,
            service_category
        FROM contracts
        WHERE procurement_vehicle IN ('sole_source', 'cooperative_piggyback')
          AND doc_type IN ('fully_executed_agreement', 'award_letter')
          AND total_contract_value IS NOT NULL
        ORDER BY total_contract_value DESC
    """
    channel_df = _read_sql(channel_sql, db_path)
    detail_df = _read_sql(detail_sql, db_path)
    if not channel_df.empty:
        total = channel_df["total_contract_value"].sum()
        channel_df["pct_of_total"] = (channel_df["total_contract_value"] / total * 100).round(1) if total else 0.0
    return channel_df, detail_df


def analysis_7(db_path: str = DB_PATH) -> pd.DataFrame:
    """Vendor consolidation map — fragmentation by service category."""
    sql = """
        SELECT
            COALESCE(service_category, 'unknown') AS service_category,
            COUNT(DISTINCT vendor_name) AS vendor_count,
            SUM(total_contract_value) AS total_spend,
            AVG(total_contract_value) AS avg_contract_value,
            COUNT(*) AS contract_count
        FROM contracts
        WHERE doc_type = 'fully_executed_agreement'
          AND total_contract_value IS NOT NULL
        GROUP BY service_category
        ORDER BY vendor_count DESC, total_spend DESC
    """
    df = _read_sql(sql, db_path)
    if not df.empty:
        df["total_spend"] = pd.to_numeric(df["total_spend"], errors="coerce")
        df["avg_contract_value"] = pd.to_numeric(df["avg_contract_value"], errors="coerce")
        # Fragmentation signal: many vendors + low avg value = high fragmentation
        df["fragmentation_signal"] = df.apply(
            lambda r: "High" if r["vendor_count"] >= 3 and (r["avg_contract_value"] or 0) < 100_000
            else ("Medium" if r["vendor_count"] >= 2 else "Low"),
            axis=1,
        )
    return df


def analysis_8(db_path: str = DB_PATH) -> pd.DataFrame:
    """Incumbent dependency — vendors with 3+ renewals; rank by renewal count and current value.

    The "current value" source document falls back to award_letter when no
    fully_executed_agreement row exists for a contract_number — some contract families'
    originating document is classified as award_letter rather than FEA, and excluding
    those contracts entirely (rather than deprioritizing them behind a real FEA row when
    one exists) would silently drop real incumbents with active renewal histories.
    """
    sql = """
        WITH renewal_counts AS (
            SELECT contract_number, COUNT(*) AS renewal_count
            FROM contracts
            WHERE doc_type = 'renewal_letter'
            GROUP BY contract_number
        ),
        candidate_originals AS (
            SELECT contract_number, vendor_name, total_contract_value,
                   contract_start_date, contract_end_date, doc_date,
                   CASE doc_type WHEN 'fully_executed_agreement' THEN 0 ELSE 1 END AS doc_type_priority
            FROM contracts
            WHERE doc_type IN ('fully_executed_agreement', 'award_letter')
        ),
        latest_values AS (
            SELECT contract_number, vendor_name, total_contract_value,
                   contract_start_date, contract_end_date,
                   ROW_NUMBER() OVER (
                       PARTITION BY contract_number
                       ORDER BY doc_type_priority ASC, doc_date DESC NULLS LAST
                   ) AS rn
            FROM candidate_originals
        )
        SELECT
            lv.contract_number,
            lv.vendor_name,
            rc.renewal_count,
            lv.total_contract_value AS current_value,
            lv.contract_start_date,
            lv.contract_end_date,
            CASE WHEN rc.renewal_count >= 3 THEN 1 ELSE 0 END AS stale_incumbent
        FROM renewal_counts rc
        JOIN latest_values lv ON rc.contract_number = lv.contract_number AND lv.rn = 1
        ORDER BY rc.renewal_count DESC, lv.total_contract_value DESC NULLS LAST
    """
    df = _read_sql(sql, db_path)
    if not df.empty:
        df["current_value"] = pd.to_numeric(df["current_value"], errors="coerce")
    return df
