"""Validate calibration declarations and seal all referenced original evidence."""
from .budget import PriceTable, ResourceEnvelope
from .uploads import seal_original_upload


def seal_calibration_upload(table, envelopes, originals, *, artifacts):
    table = PriceTable.model_validate(table)
    if not isinstance(envelopes, list):
        raise ValueError("envelopes must be a complete task list")
    envelopes = tuple(ResourceEnvelope.model_validate(item) for item in envelopes)
    references = {table.source_hash, *(tariff.usage_semantics_hash for tariff in table.tariffs),
                  *(item.replay_evidence_hash for item in envelopes)}
    seal_original_upload(references, originals, artifacts=artifacts)
    return table, envelopes
