"""Seal owner-supplied condition evidence without granting access by hash alone."""
from .evaluate import TaskComparisonConditions
from .uploads import seal_original_upload


def seal_condition_upload(supplied, originals, *, artifacts):
    if not isinstance(originals, dict):
        raise ValueError("runtime_artifacts_base64 must be a map")
    if supplied is None:
        if originals:
            raise ValueError("unbound runtime artifacts are not accepted")
        return None
    if not isinstance(supplied, dict):
        raise ValueError("runtime_conditions must be a task map or null")
    conditions = {task: TaskComparisonConditions.model_validate(value).model_dump(mode="json")
                  for task, value in supplied.items()}
    references = {value for item in conditions.values() for name, value in item.items() if name.endswith("_hash")}
    seal_original_upload(references, originals, artifacts=artifacts)
    return conditions
