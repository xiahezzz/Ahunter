# Package MX events as RID-scoped feeds

`mx_events@2` remains one versioned Data Product type, but materializes each included RID as a separately identified, hashed, and provenance-bearing MX RID Feed within the sealed Research Data Snapshot. The system does not create a Product Manifest or Provider Adapter per RID: RID changes remain authorization and selection data, while Agents receive explicit non-mixed feeds and every research result can identify the exact RID packages it used.
