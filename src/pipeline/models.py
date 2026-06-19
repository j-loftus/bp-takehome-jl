"""
Pydantic models for the contract extraction schema.

ContractRecord is the single output type of the extraction pipeline —
one instance per document. It maps 1:1 to a row in the contracts table.
"""

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from src.pipeline.classifier import DocType


class PriceEscalatorTerms(str, Enum):
    FIXED                 = "fixed"
    CPI_CAPPED            = "cpi_capped"
    FIXED_PERCENTAGE      = "fixed_percentage"
    NEGOTIATED_AT_RENEWAL = "negotiated_at_renewal"
    NOT_SPECIFIED         = "not_specified"


class ServiceCategory(str, Enum):
    PROFESSIONAL_SERVICES  = "professional_services"
    TECHNOLOGY_SOFTWARE    = "technology_software"
    FACILITIES_MAINTENANCE = "facilities_maintenance"
    PUBLIC_SAFETY          = "public_safety"
    INFRASTRUCTURE         = "infrastructure"
    STAFFING               = "staffing"
    SUPPLIES_GOODS         = "supplies_goods"
    BEHAVIORAL_HEALTH      = "behavioral_health"
    OTHER                  = "other"


class ProcurementVehicle(str, Enum):
    DIRECT_RFP            = "direct_rfp"
    COOPERATIVE_PIGGYBACK = "cooperative_piggyback"
    SOLE_SOURCE           = "sole_source"
    OTHER                 = "other"


class ExtractionConfidence(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"


class ContractRecord(BaseModel):
    # --- Group A: Universal Spine (contract_number and vendor_name are non-nullable) ---
    contract_number:  str
    doc_type:         DocType
    vendor_name:      str
    doc_date:         Optional[date] = None
    county_department: Optional[str] = None

    # --- Group B: Financial Exposure ---
    total_contract_value:        Optional[float]                = None
    price_escalator_terms:       Optional[PriceEscalatorTerms] = None
    modification_financial_delta: Optional[float]              = None

    # --- Group C: Term and Renewal Exposure ---
    contract_start_date:      Optional[date] = None
    contract_end_date:        Optional[date] = None
    renewal_options:          Optional[str]  = None
    auto_renewal_flag:        Optional[bool] = None
    termination_notice_days:  Optional[int]  = None

    # --- Group D: Vendor and Compliance Risk ---
    service_category:          Optional[ServiceCategory]   = None
    procurement_vehicle:       Optional[ProcurementVehicle] = None
    insurance_requirements_flag: Optional[bool]            = None

    # --- Group E: Document Linkage ---
    parent_contract_number: Optional[str] = None

    # --- Pipeline metadata ---
    source_filename:        str
    pipeline_run_timestamp: str                          # ISO 8601 datetime string
    extraction_confidence:  Optional[ExtractionConfidence] = None
    extraction_notes:       Optional[str]                = None
    extraction_method:      Optional[str]                = None  # "text" or "vision"

    def to_db_row(self) -> dict:
        """
        Serialize to a flat dict for SQL INSERT.

        - date fields → "YYYY-MM-DD" strings (or None)
        - bool fields → 1 / 0 / None (SQLite boolean)
        - enum fields → .value strings (or None)
        """
        def _date(v: Optional[date]) -> Optional[str]:
            return v.isoformat() if v is not None else None

        def _bool(v: Optional[bool]) -> Optional[int]:
            return int(v) if v is not None else None

        def _enum(v: Optional[Enum]) -> Optional[str]:
            return v.value if v is not None else None

        return {
            "contract_number":             self.contract_number,
            "doc_type":                    _enum(self.doc_type),
            "vendor_name":                 self.vendor_name,
            "doc_date":                    _date(self.doc_date),
            "county_department":           self.county_department,
            "total_contract_value":        self.total_contract_value,
            "price_escalator_terms":       _enum(self.price_escalator_terms),
            "modification_financial_delta": self.modification_financial_delta,
            "contract_start_date":         _date(self.contract_start_date),
            "contract_end_date":           _date(self.contract_end_date),
            "renewal_options":             self.renewal_options,
            "auto_renewal_flag":           _bool(self.auto_renewal_flag),
            "termination_notice_days":     self.termination_notice_days,
            "service_category":            _enum(self.service_category),
            "procurement_vehicle":         _enum(self.procurement_vehicle),
            "insurance_requirements_flag": _bool(self.insurance_requirements_flag),
            "parent_contract_number":      self.parent_contract_number,
            "source_filename":             self.source_filename,
            "pipeline_run_timestamp":      self.pipeline_run_timestamp,
            "extraction_confidence":       _enum(self.extraction_confidence),
            "extraction_notes":            self.extraction_notes,
            "extraction_method":           self.extraction_method,
        }
