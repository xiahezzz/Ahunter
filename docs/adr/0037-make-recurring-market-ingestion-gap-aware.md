# Make recurring Market Daily ingestion gap-aware

The 21:00 recurring job first refreshes the Shanghai-Shenzhen security list and then requests each security's missing interval from its last evidenced coverage through the latest completed session, rather than assuming only the current day can be absent. Normal runs still fetch one day, while outages and source failures heal automatically on the next run without replaying the five-year cold start; delisted securities stop and new listings begin at their listing dates.
