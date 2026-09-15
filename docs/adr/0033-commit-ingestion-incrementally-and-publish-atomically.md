# Commit Market Daily ingestion incrementally and publish it atomically

A Market Daily Ingestion Run commits validated rows independently for each security and persists enough progress to resume only missing securities or dates after interruption. The resulting full-market baseline remains `partial` and unavailable to scheduled research until every expected security has either succeeded or supplied evidence of a legitimate empty history; only then is the run sealed as `complete`, avoiding both a multi-million-row transaction and accidental publication of a partial universe.
