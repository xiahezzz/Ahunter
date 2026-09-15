# Use a five-year cold-start floor and retain accumulated history

The Market Daily Cold Start spans the inclusive interval from the latest completed trading session back five calendar years, advancing a non-session lower bound to its next trading session. Recurring ingestion only appends and never deletes rows that age beyond five years, accepting gradual storage growth in exchange for stable historical evidence and avoiding a rolling retention boundary that would erase previously researched inputs.
