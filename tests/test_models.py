import pytest
from pydantic import ValidationError
from spi.models import Evidence, Metric, ResultRecord, Value


def test_evidence_requires_location():
    with pytest.raises(ValidationError):
        Evidence(page=3)


def test_page_must_be_positive():
    with pytest.raises(ValidationError):
        Evidence(page=0, table="Table 2")


def test_unknown_value_cannot_be_result():
    with pytest.raises(ValidationError):
        ResultRecord(result_id="r1", document_id="solar:D001", domain="solar",
                     metric=Metric(name="PCE"), raw=Value(), normalized=Value(),
                     source=Evidence(page=5, table="Table 2"))


def test_domain_is_part_of_document_id():
    with pytest.raises(ValidationError):
        ResultRecord(result_id="r1", document_id="rag:D001", domain="solar",
                     metric=Metric(name="PCE"), raw=Value(value="16.07", unit="%"),
                     normalized=Value(value=16.07, unit="%"),
                     source=Evidence(page=5, table="Table 2"))
