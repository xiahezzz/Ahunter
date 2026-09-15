# Preserve the historical Shanghai-Shenzhen A-share universe

Cold-start and recurring market-daily ingestion cover every RMB-denominated common stock listed on the Shanghai or Shenzhen exchanges at any point in the five-year window, including ST, suspended, and later-delisted stocks while excluding Beijing-listed stocks, B shares, funds, bonds, and indexes. Daily universe snapshots add new listings and stop future ingestion after delisting while retaining all history; this window-history membership is preferred over a current-listing snapshot to prevent survivorship bias.
