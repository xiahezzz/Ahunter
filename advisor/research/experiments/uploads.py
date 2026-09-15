"""Exact owner-supplied evidence bytes; a known hash alone grants no new link."""
import hashlib

from .candidates import decode_candidate_bytes


def seal_original_upload(references, originals, *, artifacts):
    if not isinstance(originals, dict) or set(originals) != set(references):
        raise ValueError("every referenced evidence hash requires its exact original bytes")
    captured = {content_hash: decode_candidate_bytes(value) for content_hash, value in originals.items()}
    if any(hashlib.sha256(data).hexdigest() != content_hash for content_hash, data in captured.items()):
        raise ValueError("evidence bytes differ from their declared hash")
    # Validate the entire upload before writing any bytes, including when its
    # references already exist in another record or retention scope.
    for data in captured.values():
        artifacts.put_bytes(data)
