"""
Field coverage matrix — Task 1.2.

Codifies which fields are Expected (E), Partial (P), or Null-by-design (N)
for each document type, based on extraction_schema.md §6.

Consumers:
  - extractor.py: calls fields_to_extract() to build the per-doc-type field
    list included in the extraction prompt (E and P only; N fields are omitted)
  - eval harness: calls is_expected_null() to distinguish design-nulls from
    pipeline failures when computing field-level accuracy
"""

from enum import Enum

from src.pipeline.classifier import DocType


class Coverage(Enum):
    EXPECTED       = "E"  # reliable; unexpected null is a pipeline failure
    PARTIAL        = "P"  # present in some instances; null is acceptable
    NULL_BY_DESIGN = "N"  # doc type does not contain this field; null is correct


# Coverage matrix — values from extraction_schema.md §6
# Keys are field names as they appear in ContractRecord / the contracts table.
FIELD_COVERAGE: dict[DocType, dict[str, Coverage]] = {
    DocType.FULLY_EXECUTED_AGREEMENT: {
        "contract_number":             Coverage.EXPECTED,
        "doc_type":                    Coverage.EXPECTED,
        "vendor_name":                 Coverage.EXPECTED,
        "doc_date":                    Coverage.EXPECTED,
        "county_department":           Coverage.PARTIAL,
        "total_contract_value":        Coverage.PARTIAL,
        "price_escalator_terms":       Coverage.EXPECTED,
        "modification_financial_delta": Coverage.NULL_BY_DESIGN,
        "contract_start_date":         Coverage.EXPECTED,
        "contract_end_date":           Coverage.EXPECTED,
        "renewal_options":             Coverage.EXPECTED,
        "auto_renewal_flag":           Coverage.PARTIAL,
        "termination_notice_days":     Coverage.EXPECTED,
        "service_category":            Coverage.EXPECTED,
        "procurement_vehicle":         Coverage.EXPECTED,
        "insurance_requirements_flag": Coverage.EXPECTED,
        "parent_contract_number":      Coverage.NULL_BY_DESIGN,
    },
    DocType.RENEWAL_LETTER: {
        "contract_number":             Coverage.EXPECTED,
        "doc_type":                    Coverage.EXPECTED,
        "vendor_name":                 Coverage.EXPECTED,
        "doc_date":                    Coverage.EXPECTED,
        "county_department":           Coverage.PARTIAL,
        "total_contract_value":        Coverage.NULL_BY_DESIGN,
        "price_escalator_terms":       Coverage.NULL_BY_DESIGN,
        "modification_financial_delta": Coverage.NULL_BY_DESIGN,
        "contract_start_date":         Coverage.EXPECTED,
        "contract_end_date":           Coverage.EXPECTED,
        "renewal_options":             Coverage.NULL_BY_DESIGN,
        "auto_renewal_flag":           Coverage.NULL_BY_DESIGN,
        "termination_notice_days":     Coverage.NULL_BY_DESIGN,
        "service_category":            Coverage.EXPECTED,
        "procurement_vehicle":         Coverage.NULL_BY_DESIGN,
        "insurance_requirements_flag": Coverage.NULL_BY_DESIGN,
        "parent_contract_number":      Coverage.EXPECTED,
    },
    DocType.MODIFICATION_AMENDMENT: {
        "contract_number":             Coverage.EXPECTED,
        "doc_type":                    Coverage.EXPECTED,
        "vendor_name":                 Coverage.EXPECTED,
        "doc_date":                    Coverage.EXPECTED,
        "county_department":           Coverage.PARTIAL,
        "total_contract_value":        Coverage.NULL_BY_DESIGN,
        "price_escalator_terms":       Coverage.PARTIAL,
        "modification_financial_delta": Coverage.PARTIAL,
        "contract_start_date":         Coverage.PARTIAL,
        "contract_end_date":           Coverage.PARTIAL,
        "renewal_options":             Coverage.NULL_BY_DESIGN,
        "auto_renewal_flag":           Coverage.NULL_BY_DESIGN,
        "termination_notice_days":     Coverage.NULL_BY_DESIGN,
        "service_category":            Coverage.EXPECTED,
        "procurement_vehicle":         Coverage.NULL_BY_DESIGN,
        "insurance_requirements_flag": Coverage.NULL_BY_DESIGN,
        "parent_contract_number":      Coverage.EXPECTED,
    },
    DocType.AWARD_LETTER: {
        "contract_number":             Coverage.EXPECTED,
        "doc_type":                    Coverage.EXPECTED,
        "vendor_name":                 Coverage.EXPECTED,
        "doc_date":                    Coverage.EXPECTED,
        "county_department":           Coverage.PARTIAL,
        "total_contract_value":        Coverage.PARTIAL,
        "price_escalator_terms":       Coverage.NULL_BY_DESIGN,
        "modification_financial_delta": Coverage.NULL_BY_DESIGN,
        "contract_start_date":         Coverage.EXPECTED,
        "contract_end_date":           Coverage.EXPECTED,
        "renewal_options":             Coverage.EXPECTED,
        "auto_renewal_flag":           Coverage.NULL_BY_DESIGN,
        "termination_notice_days":     Coverage.NULL_BY_DESIGN,
        "service_category":            Coverage.EXPECTED,
        "procurement_vehicle":         Coverage.EXPECTED,
        "insurance_requirements_flag": Coverage.PARTIAL,
        "parent_contract_number":      Coverage.NULL_BY_DESIGN,
    },
    DocType.VENDOR_DISCLOSURE_STATEMENT: {
        "contract_number":             Coverage.EXPECTED,
        "doc_type":                    Coverage.EXPECTED,
        "vendor_name":                 Coverage.EXPECTED,
        "doc_date":                    Coverage.EXPECTED,
        "county_department":           Coverage.NULL_BY_DESIGN,
        "total_contract_value":        Coverage.NULL_BY_DESIGN,
        "price_escalator_terms":       Coverage.NULL_BY_DESIGN,
        "modification_financial_delta": Coverage.NULL_BY_DESIGN,
        "contract_start_date":         Coverage.NULL_BY_DESIGN,
        "contract_end_date":           Coverage.NULL_BY_DESIGN,
        "renewal_options":             Coverage.NULL_BY_DESIGN,
        "auto_renewal_flag":           Coverage.NULL_BY_DESIGN,
        "termination_notice_days":     Coverage.NULL_BY_DESIGN,
        "service_category":            Coverage.NULL_BY_DESIGN,
        "procurement_vehicle":         Coverage.NULL_BY_DESIGN,
        "insurance_requirements_flag": Coverage.NULL_BY_DESIGN,
        "parent_contract_number":      Coverage.PARTIAL,
    },
    DocType.OTHER: {
        "contract_number":             Coverage.EXPECTED,
        "doc_type":                    Coverage.EXPECTED,
        "vendor_name":                 Coverage.EXPECTED,
        "doc_date":                    Coverage.EXPECTED,
        "county_department":           Coverage.PARTIAL,
        "total_contract_value":        Coverage.PARTIAL,
        "price_escalator_terms":       Coverage.NULL_BY_DESIGN,
        "modification_financial_delta": Coverage.NULL_BY_DESIGN,
        "contract_start_date":         Coverage.PARTIAL,
        "contract_end_date":           Coverage.PARTIAL,
        "renewal_options":             Coverage.NULL_BY_DESIGN,
        "auto_renewal_flag":           Coverage.NULL_BY_DESIGN,
        "termination_notice_days":     Coverage.NULL_BY_DESIGN,
        "service_category":            Coverage.PARTIAL,
        "procurement_vehicle":         Coverage.NULL_BY_DESIGN,
        "insurance_requirements_flag": Coverage.NULL_BY_DESIGN,
        "parent_contract_number":      Coverage.PARTIAL,
    },
}


def fields_to_extract(doc_type: DocType) -> list[str]:
    """Return field names the extractor should attempt to populate for this doc type.

    Excludes NULL_BY_DESIGN fields — there is no point asking the LLM to
    extract fields that are structurally absent from this document type.
    Also excludes doc_type (set by classifier) and pipeline metadata fields.
    """
    skip = {"doc_type"}
    return [
        field
        for field, coverage in FIELD_COVERAGE[doc_type].items()
        if coverage != Coverage.NULL_BY_DESIGN and field not in skip
    ]


def is_expected_null(field: str, doc_type: DocType) -> bool:
    """Return True when a null value for this field/doc_type is by design.

    Used by the eval harness to distinguish acceptable nulls (N) from
    pipeline failures (unexpected null on E or P fields).
    """
    return FIELD_COVERAGE[doc_type].get(field) == Coverage.NULL_BY_DESIGN
